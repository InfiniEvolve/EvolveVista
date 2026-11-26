"""How fast a session's agent got answers from its model: time to first token and tokens per second, per call.

A Claude Code transcript names each model call by its request_id. When the agent talks to its model through
OpenRouter, that id is OpenRouter's generation id ("gen-..."), and OpenRouter's generation endpoint says how the
call went: its latency (time to first token), how long the answer took, how many tokens it had, which provider
served it. This module finds the ids in the agent log as it grows and asks OpenRouter about each, with the key in
OPENROUTER_API_KEY. The key stays in this server: the page gets the numbers only.

    point = {"id", "time", "ttft" (ms), "gen" (ms), "tokens", "tps", "provider", "finish", "sub"}

tps is tokens / generation time: OpenRouter's generation time already leaves the time to first token out.
OpenRouter has a call's numbers a few seconds after it ends, so a lookup that finds none is tried again later,
backing off; a refusal for too many requests pauses every lookup. At most WORKERS lookups run at once over the
whole server. What was found is kept in <campaign>/vista/<task>/<session>/api-stats.json, so a restarted server
does not ask again. Standard library only.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import threading
import time
import urllib.error
import urllib.request

from . import vista

ENDPOINT = "https://openrouter.ai/api/v1/generation?id="
WORKERS = 4                                  # lookups at once, over the whole server
RETRIES = (2, 4, 8, 15, 30, 60, 120, 240)    # seconds before each next try of a call whose numbers are not there yet
TIMEOUT = 15
CACHE = "api-stats.json"


class Refused(Exception):
    """OpenRouter would not answer this key at all (401/403): no point asking about the other calls."""


class Busy(Exception):
    """Too many requests: wait this many seconds before any lookup."""

    def __init__(self, seconds: float):
        super().__init__(seconds)
        self.seconds = seconds


def openrouter(generation: str, key: str) -> dict | None:
    """OpenRouter's numbers for one generation; None while it has none yet (or does not know the id)."""
    request = urllib.request.Request(ENDPOINT + generation, headers={"Authorization": f"Bearer {key}",
                                                                     "User-Agent": "evolve-vista"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            data = json.loads(response.read()).get("data")
    except urllib.error.HTTPError as error:
        if error.code in (401, 403):
            raise Refused() from None
        if error.code == 429:
            given = error.headers.get("Retry-After") or ""
            raise Busy(float(given) if given.replace(".", "", 1).isdigit() else 30.0) from None
        return None   # 404 until the numbers are there, or a passing server error
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def point(generation: str, data: dict, when: str | None, sub: bool) -> dict | None:
    """One call as the page plots it, from OpenRouter's generation record; None if it lacks the timings."""
    def number(key):
        value = data.get(key)
        return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None
    ttft, gen = number("latency"), number("generation_time")
    tokens = number("native_tokens_completion")
    if tokens is None:
        tokens = number("tokens_completion")
    if ttft is None and gen is None:
        return None
    providers = data.get("provider_responses")
    provider = data.get("provider_name")
    if isinstance(providers, list) and providers and isinstance(providers[-1], dict):
        provider = providers[-1].get("provider_name") or provider
    return {"id": generation, "time": when or data.get("created_at"), "ttft": ttft, "gen": gen, "tokens": tokens,
            "tps": round(tokens / (gen / 1000), 1) if tokens and gen else None,
            "provider": provider if isinstance(provider, str) else None,
            "finish": data.get("finish_reason") if isinstance(data.get("finish_reason"), str) else None, "sub": sub}


def calls(data: bytes) -> list:
    """(request_id, timestamp, subagent) of each model call in these complete log lines, in order, once each."""
    found, seen = [], set()
    for line in data.splitlines():
        if b'"request_id"' not in line:   # most lines of a long log are bookkeeping; skip them unparsed
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict) or event.get("type") != "assistant":
            continue
        rid = event.get("request_id")
        if isinstance(rid, str) and rid and rid not in seen:
            seen.add(rid)
            found.append((rid, event.get("timestamp") if isinstance(event.get("timestamp"), str) else None,
                          bool(event.get("parent_tool_use_id"))))
    return found


class Session:
    """One session's calls and what OpenRouter said about them, shared by every page that watches it."""

    def __init__(self, stats: "ApiStats", log: Path, cache: Path):
        self.stats, self.log, self.cache = stats, log, cache
        self.lock = threading.Lock()
        self.offset = 0
        self.order: list = []          # request ids, in the order the agent made the calls
        self.calls: dict = {}          # id -> (timestamp, subagent)
        self.points: dict = {}         # id -> point
        self.tries: dict = {}          # id -> (tries so far, when to try next)
        self.running: set = set()
        self.missing: set = set()      # ids OpenRouter never had numbers for
        try:
            kept = json.loads(cache.read_text())
            self.points = {p["id"]: p for p in kept.get("points", []) if isinstance(p, dict) and isinstance(p.get("id"), str)}
        except (OSError, ValueError, AttributeError):
            pass

    def advance(self) -> None:
        """Read the calls the log gained, and start the lookups that are due."""
        with self.lock:
            if self.log.is_file() and self.log.stat().st_size > self.offset:
                with open(self.log, "rb") as f:
                    f.seek(self.offset)
                    data = f.read()
                end = data.rfind(b"\n")
                if end >= 0:
                    for rid, when, sub in calls(data[:end]):
                        if rid not in self.calls:
                            self.calls[rid] = (when, sub)
                            self.order.append(rid)
                            if rid in self.points:   # from the cache: the line's own time and kind
                                self.points[rid] = {**self.points[rid], "time": when or self.points[rid].get("time"), "sub": sub}
                    self.offset += end + 1
            if self.state() != "ok":
                return
            now = time.monotonic()
            for rid in self.order:
                if rid in self.points or rid in self.missing or rid in self.running:
                    continue
                if self.tries.get(rid, (0, 0))[1] > now or self.stats.paused_until > now:
                    continue
                self.running.add(rid)
                self.stats.pool.submit(self._look_up, rid)

    def _look_up(self, rid: str) -> None:
        found, refused, busy = None, False, False
        try:
            if self.stats.paused_until <= time.monotonic():
                data = self.stats.fetch(rid, self.stats.key)
                found = point(rid, data, *self.calls[rid]) if data else None
        except Refused:
            refused = True
        except Busy as told:
            busy = True
            self.stats.paused_until = max(self.stats.paused_until, time.monotonic() + told.seconds)
        except Exception:   # a lookup never takes the server down
            found = None
        with self.lock:
            self.running.discard(rid)
            if refused:
                self.stats.refused = True
            elif found:
                self.points[rid] = found
                self._save()
            elif not busy:   # a call turned away for too many requests is tried again as it was
                tries = self.tries.get(rid, (0, 0))[0]
                if tries >= len(RETRIES):
                    self.missing.add(rid)
                else:
                    self.tries[rid] = (tries + 1, time.monotonic() + RETRIES[tries])

    def _save(self) -> None:
        try:
            vista.private_dir(self.cache.parent)
            temp = self.cache.with_suffix(".tmp")
            temp.write_text(json.dumps({"points": list(self.points.values())}))
            os.replace(temp, self.cache)
        except OSError:
            pass   # the numbers are still in memory

    def state(self) -> str:
        if not self.stats.key:
            return "no-key"
        if self.stats.refused:
            return "refused"
        if self.order and not any(rid.startswith("gen-") for rid in self.order):
            return "not-openrouter"
        return "ok"

    def view(self) -> dict:
        """The points found, in call order, and how many calls still wait for their numbers."""
        with self.lock:
            shown = [self.points[rid] for rid in self.order if rid in self.points]
            return {"state": self.state(), "calls": len(self.order), "points": shown,
                    "waiting": len(self.order) - len(shown) - len(self.missing), "missing": len(self.missing)}


class ApiStats:
    """The lookups of one server: its key, its workers, and each watched session's calls."""

    def __init__(self, key: str | None = None, fetch=openrouter, workers: int = WORKERS):
        self.key = key if key is not None else os.environ.get("OPENROUTER_API_KEY", "")
        self.fetch = fetch
        self.pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="api-stats")
        self.paused_until = 0.0
        self.refused = False
        self.sessions: dict = {}
        self.lock = threading.Lock()

    def session(self, root: Path, task: str, session: str) -> Session:
        path = vista.session_dir(root, task, session)
        with self.lock:
            key = (str(root), task, session)
            if key not in self.sessions:
                log = vista.agent_log(path) or path / vista.LOGS[0]
                self.sessions[key] = Session(self, log, vista.store(root, task, session) / CACHE)
            return self.sessions[key]
