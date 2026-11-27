"""What the page shows of a session beyond the snapshot: its time, split into development and validation,
and its agent's log as it is written.

Both readers of a campaign (InfiniEvolve's snapshot and campaign.py) leave out a session's time limit, how long
its evaluation took and, while it runs, which phase it is in; enrich() adds them from the session's own files:

    input/spec.json        time_limit_seconds: the agent's time limit
    node.json              timing.evaluate_seconds: how long scoring took
    raw/final.patch        written when the agent ends and the evaluation starts (the local adapter)
    raw/agent.jsonl        the agent's output, as it writes it (agent.log in older runs)
    confirm/r*/job.json    a re-measurement under way, until sample.json appears beside it

Read only, standard library only.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path

LOGS = ("raw/agent.jsonl", "raw/agent.log")
TAIL_BYTES = 256 * 1024     # how much of a log the first look reads, from its end
CHUNK_BYTES = 512 * 1024    # how much one later look reads at most
TEXT_CHARS = 2000           # an entry's text is cut here


def _json(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


def _iso(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat(timespec="seconds")


def stamp(root: Path) -> str:
    """Changes when a running session moves from development to validation, which the snapshots' own stamps
    do not see."""
    parts = []
    for path in sorted(Path(root).glob("tasks/*/sessions/*/raw/final.patch")):
        try:
            parts.append(f"{path.relative_to(root)}:{path.stat().st_mtime_ns}")
        except OSError:
            continue
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()[:12]


def enrich(root: Path, data: dict) -> dict:
    """Add to each node: timeLimitSeconds, checkSeconds and, for one running or re-measured, phase
    ("develop" or "check") and developedAt (when its agent ended)."""
    for task in data.get("tasks") or []:
        sessions = Path(root) / "tasks" / str(task.get("name")) / "sessions"
        for node in task.get("nodes") or []:
            here = sessions / str(node.get("id"))
            if not here.is_dir():
                continue
            spec = _json(here / "input" / "spec.json")
            node["timeLimitSeconds"] = _number((spec or {}).get("time_limit_seconds")) if isinstance(spec, dict) else None
            archived = _json(here / "node.json") if node.get("status") in ("archived", "remeasuring") else None
            timing = (archived or {}).get("timing") if isinstance(archived, dict) else None
            node["checkSeconds"] = _number((timing or {}).get("evaluate_seconds")) if isinstance(timing, dict) else None
            node["phase"], node["developedAt"] = None, None
            if node.get("status") == "running":
                patch = here / "raw" / "final.patch"
                if patch.is_file():
                    node["phase"], node["developedAt"] = "check", _iso(patch.stat().st_mtime)
                else:
                    node["phase"] = "develop"
            elif node.get("status") == "remeasuring":
                node["phase"] = "check"
    return data


def log_path(session: Path) -> Path | None:
    return next((session / name for name in LOGS if (session / name).is_file()), None)


def _cut(text: str) -> str:
    text = text.strip()
    return text if len(text) <= TEXT_CHARS else text[:TEXT_CHARS] + " …"


def _text(content) -> str:
    """A tool result's content: a string, or a list of parts of which the text ones are kept."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(p.get("text", "")) for p in content if isinstance(p, dict) and p.get("type") == "text")
    return ""


def _tool(name, given) -> str:
    given = given if isinstance(given, dict) else {}
    arg = (given.get("command") or given.get("file_path") or given.get("path") or given.get("pattern")
           or given.get("url") or given.get("query") or given.get("description") or "")
    return f"{name}: {arg}" if arg else str(name)


def entries(event) -> list:
    """What one line of an agent's log says, as entries {k, t}: k is say (the agent wrote), think, tool (a call),
    out (its result; err marks a failure), meta (start, usage) or end."""
    if not isinstance(event, dict):
        return []
    kind = event.get("type")
    # Claude Code, --output-format stream-json
    if kind == "system":
        if event.get("subtype") == "init":
            return [{"k": "meta", "t": "Started" + (f" · {event['model']}" if event.get("model") else "")}]
        return []
    if kind in ("assistant", "user"):
        out = []
        for part in (event.get("message") or {}).get("content") or []:
            if not isinstance(part, dict):
                continue
            p = part.get("type")
            if p == "text" and str(part.get("text", "")).strip():
                out.append({"k": "say", "t": _cut(str(part["text"]))})
            elif p == "thinking" and str(part.get("thinking", "")).strip():
                out.append({"k": "think", "t": _cut(str(part["thinking"]))})
            elif p == "tool_use":
                out.append({"k": "tool", "t": _cut(_tool(part.get("name"), part.get("input")))})
            elif p == "tool_result":
                text = _text(part.get("content"))
                if text.strip():
                    out.append({"k": "out", "t": _cut(text), **({"err": True} if part.get("is_error") else {})})
        return out
    if kind == "result":
        cost, turns = _number(event.get("total_cost_usd")), _number(event.get("num_turns"))
        said = ["Finished"] + ([f"${cost:.2f}"] if cost is not None else []) + ([f"{turns} turns"] if turns is not None else [])
        return [{"k": "end", "t": " · ".join(said)}]
    # Codex, exec --json
    if kind == "thread.started":
        return [{"k": "meta", "t": "Started"}]
    if kind == "turn.completed":
        usage = event.get("usage") or {}
        tokens = sum(v for v in (_number(usage.get("input_tokens")), _number(usage.get("output_tokens"))) if v)
        return [{"k": "end", "t": "Turn finished" + (f" · {tokens:,} tokens" if tokens else "")}]
    if kind in ("turn.failed", "error"):
        return [{"k": "out", "t": _cut(str((event.get("error") or {}).get("message") or event.get("message") or kind)), "err": True}]
    if kind == "item.completed" and isinstance(event.get("item"), dict):
        item = event["item"]
        t = item.get("type")
        if t == "agent_message" and item.get("text"):
            return [{"k": "say", "t": _cut(str(item["text"]))}]
        if t == "reasoning" and item.get("text"):
            return [{"k": "think", "t": _cut(str(item["text"]))}]
        if t == "command_execution":
            out = [{"k": "tool", "t": _cut(f"shell: {item.get('command', '')}")}]
            if str(item.get("aggregated_output") or "").strip():
                out.append({"k": "out", "t": _cut(str(item["aggregated_output"])),
                            **({"err": True} if item.get("exit_code") not in (0, None) else {})})
            return out
        if t == "file_change":
            paths = [str(c.get("path")) for c in item.get("changes") or [] if isinstance(c, dict)]
            return [{"k": "tool", "t": _cut("edit: " + ", ".join(paths))}]
        if t == "mcp_tool_call":
            return [{"k": "tool", "t": _cut(f"{item.get('server')}.{item.get('tool')}")}]
        if t == "web_search":
            return [{"k": "tool", "t": _cut(f"search: {item.get('query', '')}")}]
        if t == "error":
            return [{"k": "out", "t": _cut(str(item.get("message", ""))), "err": True}]
        return []
    if kind in ("item.started", "item.updated", "turn.started"):
        return []
    # Gemini CLI and others
    if kind == "tool_use":
        return [{"k": "tool", "t": _cut(_tool(event.get("tool_name") or event.get("name"), event.get("parameters") or event.get("input")))}]
    if kind == "message" and event.get("role") == "assistant" and isinstance(event.get("content"), str):
        return [{"k": "say", "t": _cut(event["content"])}]
    return []


def read_log(session: Path, start: int = -1) -> dict:
    """The log from byte start on, or its last part when start is -1 (or beyond the end, as after a restart).
    Only whole lines are read; offset is where the next look starts, and more says it can start at once."""
    path = log_path(session)
    if path is None:
        return {"exists": False, "entries": [], "offset": 0, "size": 0, "cut": False, "more": False}
    size = path.stat().st_size
    cut = False
    if start < 0 or start > size:
        start, cut = max(0, size - TAIL_BYTES), size > TAIL_BYTES
    with open(path, "rb") as f:
        f.seek(start)
        chunk = f.read(CHUNK_BYTES)
    if cut:   # the first line is a part of one
        newline = chunk.find(b"\n")
        start, chunk = (start + newline + 1, chunk[newline + 1:]) if newline >= 0 else (start, b"")
    end = chunk.rfind(b"\n") + 1
    if end == 0 and len(chunk) < CHUNK_BYTES:
        return {"exists": True, "entries": [], "offset": start, "size": size, "cut": cut, "more": False}
    whole = chunk[:end] if end else chunk   # a single line longer than a chunk is taken as it is
    found, work = [], str(session / "work")
    for line in whole.decode(errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            said = entries(json.loads(line))
        except ValueError:
            said = [{"k": "raw", "t": _cut(line)}]
        for entry in said:
            entry["t"] = entry["t"].replace(work, ".")
        found.extend(said)
    more = len(chunk) == CHUNK_BYTES and start + len(whole) < size
    return {"exists": True, "entries": found, "offset": start + len(whole), "size": size, "cut": cut, "more": more}
