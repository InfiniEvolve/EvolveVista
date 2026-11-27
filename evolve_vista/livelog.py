"""A session's agent log as it is written, for a reader watching a session that is still running.

The page opens /api/campaign/agent-log?token=&task=&session= as a stream of server-sent events. Each event is a
batch of entries read from the session's agent log (raw/agent.jsonl) since the last one, and its id is the byte
offset reached, so a reconnecting page carries on where it stopped (Last-Event-ID). A new connection starts near
the end of a long log: the last TAIL_ENTRIES entries within the last TAIL_BYTES.

An entry is one thing the agent said or did, shortened and redacted with the TraceVista skill's own patterns,
the ones its timeline extraction uses:

    {"kind": "text" | "thinking" | "tool" | "result" | "error" | "system" | "done", "text": ..., "tool": name,
     "sub": true when a subagent's}

Claude Code's stream-json is read in full; other agents' JSON lines are read the way the analysis page shows
what an analysing agent does (vista._say). Bookkeeping events (thinking-token counts, progress heartbeats) are
(and any system event not named in entries) are left out. Standard library only.
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import sys

from . import vista

TAIL_BYTES = 4 * 1024 * 1024    # how far back from the end a new connection reads
TAIL_ENTRIES = 400              # and how many of the entries found there it sends
CHUNK_BYTES = 1024 * 1024       # read at most this much per batch; the rest comes in the next one
LIMITS = {"text": 4000, "thinking": 1500, "tool": 400, "result": 1500, "error": 1500, "system": 400, "done": 1500}
SECRET_NAMES = ("TOKEN", "KEY", "SECRET", "PASSWORD", "CREDENTIAL")

_redact = None


def redactor():
    """The skill's redact(text, exact) from extract_timeline.py, so the live log hides what the timeline hides.
    Raises VistaError when the skill's scripts are not available: the log is not shown unredacted."""
    global _redact
    if _redact is None:
        scripts = vista.skill_scripts()
        path = scripts / "extract_timeline.py"
        if not path.is_file():
            raise vista.VistaError(501, "The TraceVista skill scripts are not available to this server")
        sys.path.insert(0, str(scripts))   # it imports its sibling modules
        try:
            spec = importlib.util.spec_from_file_location("tracevista_extract_timeline", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        except Exception:
            raise vista.VistaError(501, "The TraceVista skill's redaction could not be loaded") from None
        finally:
            sys.path.remove(str(scripts))
        _redact = module.redact
    return _redact


def environment_secrets() -> tuple:
    """Values of this server's own credentials (CLAUDE_CODE_OAUTH_TOKEN and the like), hidden wherever they appear."""
    return tuple(value for name, value in os.environ.items()
                 if any(word in name.upper() for word in SECRET_NAMES) and len(value) >= 12)


def _short(value, limit: int) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    text = text.strip()
    return text if len(text) <= limit else text[:limit] + f" … [{len(text) - limit:,} more characters]"


def _tool_input(given) -> str:
    """The part of a tool call worth a line: the command, the file, the pattern; else the input as JSON."""
    if not isinstance(given, dict):
        return _short(given, LIMITS["tool"])
    for key in ("command", "file_path", "path", "pattern", "url", "query", "prompt", "description", "skill"):
        if isinstance(given.get(key), str) and given[key].strip():
            more = given.get("description") if key == "command" and isinstance(given.get("description"), str) else ""
            return given[key] + (f"   # {more}" if more else "")
    return json.dumps(given, ensure_ascii=False)


def _result_text(content) -> str:
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                parts.append(str(part.get("text", "")))
            elif isinstance(part, dict) and part.get("type") == "image":
                parts.append("[image omitted]")
        return "\n".join(parts)
    return "" if content is None else str(content)


def entries(event: dict) -> list:
    """What one line of the agent log says, as entries (unshortened, unredacted)."""
    kind, sub = event.get("type"), bool(event.get("parent_tool_use_id"))
    out = []
    if kind == "system":
        subtype = event.get("subtype")
        if subtype == "init":
            out.append({"kind": "system", "text": f"Session started · {event.get('model') or 'model unknown'}"
                                                  + (f" · {event['cwd']}" if event.get("cwd") else "")})
        elif subtype == "task_started":
            out.append({"kind": "system", "text": f"Task started: {event.get('description') or event.get('task_type') or ''}"})
        elif subtype == "task_notification":
            out.append({"kind": "system", "text": f"Task {event.get('status') or 'update'}: {event.get('summary') or ''}"})
        elif subtype == "compact_boundary":
            out.append({"kind": "system", "text": "The conversation was compacted"})
        elif "error" in str(subtype):
            out.append({"kind": "error", "text": str(event.get("error") or event.get("message") or subtype)})
    elif kind in ("assistant", "user") and isinstance(event.get("message"), dict):
        content = event["message"].get("content")
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        for part in content or []:
            if not isinstance(part, dict):
                continue
            ptype = part.get("type")
            if ptype == "text" and str(part.get("text", "")).strip():
                out.append({"kind": "text" if kind == "assistant" else "system", "text": part["text"]})
            elif ptype == "thinking" and str(part.get("thinking", "")).strip():
                out.append({"kind": "thinking", "text": part["thinking"]})
            elif ptype == "tool_use":
                out.append({"kind": "tool", "tool": str(part.get("name") or "tool"),
                            "text": _tool_input(part.get("input"))})
            elif ptype == "tool_result":
                out.append({"kind": "error" if part.get("is_error") else "result", "text": _result_text(part.get("content"))})
        if event.get("error") and not out:
            out.append({"kind": "error", "text": str(event["error"])})
    elif kind == "result":
        cost, turns = event.get("total_cost_usd"), event.get("num_turns")
        head = "Finished" if event.get("subtype") == "success" and not event.get("is_error") else f"Ended: {event.get('subtype') or 'error'}"
        head += (f" · {turns} turns" if isinstance(turns, int) else "") + (f" · ${cost:.2f}" if isinstance(cost, (int, float)) else "")
        out.append({"kind": "done", "text": head + (f"\n{event['result']}" if event.get("result") else "")})
    elif kind not in ("tool_progress", "stream_event", "rate_limit_event"):
        said = vista._say(event)   # Codex, Gemini CLI and others
        if said:
            out.append({"kind": "text", "text": said})
    for entry in out:
        if sub:
            entry["sub"] = True
    return out


def clean(entry: dict, redact, secrets: tuple, private: tuple) -> dict:
    text = redact(_short(entry.get("text", ""), LIMITS.get(entry["kind"], 1500)), secrets)
    for path in private:
        text = text.replace(path, "…")
    return {**entry, "text": text}


def read(log: Path, offset: int | None, private: tuple = ()) -> tuple:
    """(entries, next offset) read from offset, which is None for a new reader: then from near the end.
    Only complete lines are read; a line being written is read next time."""
    redact, secrets = redactor(), environment_secrets()
    size = log.stat().st_size
    fresh = offset is None or offset > size   # a log that shrank was rewritten: start again
    start = max(0, size - TAIL_BYTES) if fresh else offset
    found = []
    with open(log, "rb") as f:
        f.seek(start)
        data = f.read(TAIL_BYTES if fresh else CHUNK_BYTES)
        if fresh and start > 0:
            cut = data.find(b"\n")   # the first line is a part of one
            data, start = (data[cut + 1:], start + cut + 1) if cut >= 0 else (b"", size)
        end = data.rfind(b"\n")
        if end < 0 and len(data) == CHUNK_BYTES:   # one line longer than a chunk: skip it, say so
            skipped = len(data)
            while (block := f.read(CHUNK_BYTES)) and b"\n" not in block:
                skipped += len(block)
            if b"\n" not in (block or b""):
                return [], start   # it is still being written
            skipped += block.index(b"\n") + 1
            note = {"kind": "system", "text": f"[a log line of {skipped:,} bytes is not shown]"}
            return [clean(note, redact, secrets, private)], start + skipped
    if end < 0:
        return [], start
    for line in data[:end].splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            text = line.decode(errors="replace").strip()
            found += [{"kind": "system", "text": text}] if text else []
            continue
        if isinstance(event, dict):
            found += entries(event)
    if fresh:
        found = found[-TAIL_ENTRIES:]
    return [clean(e, redact, secrets, private) for e in found], start + end + 1


def log_for(root: Path, task: str, session: str) -> Path:
    """The session's agent log, which may not exist yet (a session submitted but not started)."""
    path = vista.session_dir(root, task, session)
    return vista.agent_log(path) or path / vista.LOGS[0]
