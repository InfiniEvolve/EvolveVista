"""Read an InfiniEvolve campaign directory into one JSON snapshot for the campaign viewer.

Everything comes from what the campaign has archived; nothing is inferred and no model is asked.

    tasks/<task>/task.json                 the declaration: which metric ranks, which way is better
    tasks/<task>/sessions/<sid>/node.json  one archived session: its parent, value, validity, flags, report
    tasks/<task>/sessions/<sid>/job.json   a session submitted and not archived yet (shown as running)
    events.jsonl                           when a task stopped or paused

Campaigns written by the older infra-evolve layout (primary_speedup, correct, a list of flags) are read too.
Standard library only; Python 3.9 or newer.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path

REPORT_CHARS = 6000
# older layout: which flags kept a node in the ranking, and which meant that no session happened
OLD_SOFT = {"duplicate_code", "search_without_access"}
OLD_VOID = {"agent_unavailable"}
DEFAULT_METRIC = {"direction": "max", "scale": "ratio", "format": "{:.3f}"}


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def number(value):
    """A finite number, or None."""
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


def formatted(spec: str, value) -> str:
    if number(value) is None:
        return "-"
    try:
        return spec.format(value)
    except (ValueError, IndexError, KeyError):
        return f"{value:.4g}"


def stamp(root: Path) -> str:
    """Changes whenever anything the snapshot reads changes; cheap enough to compute on every request."""
    parts = []
    events = root / "events.jsonl"
    if events.is_file():
        st = events.stat()
        parts.append(f"events:{st.st_size}:{st.st_mtime_ns}")
    for task in sorted((root / "tasks").glob("*")) if (root / "tasks").is_dir() else []:
        for name in ("task.json",):
            if (task / name).is_file():
                parts.append(f"{task.name}/{name}:{(task / name).stat().st_mtime_ns}")
        for session in sorted((task / "sessions").glob("*")) if (task / "sessions").is_dir() else []:
            for name in ("node.json", "job.json"):
                path = session / name
                if path.is_file():
                    parts.append(f"{task.name}/{session.name}/{name}:{path.stat().st_mtime_ns}")
            confirm = session / "confirm"
            if confirm.is_dir():
                parts.extend(f"{task.name}/{session.name}/confirm/{p.parent.name}/{p.name}"
                             for p in sorted(confirm.glob("r*/*.json")))
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()[:20]


def task_events(root: Path) -> dict:
    """Per task: the last stop and pause, unless a session was archived after it (which reopens it)."""
    state: dict = {}
    events = root / "events.jsonl"
    if not events.is_file():
        return state
    for line in events.read_text(errors="replace").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        task = event.get("task")
        if not task:
            continue
        entry = state.setdefault(task, {"stop": None, "paused": None})
        kind = event.get("type")
        if kind == "task_reset":
            state[task] = {"stop": None, "paused": None}
        elif kind == "task_stop":
            entry["stop"] = (event.get("data") or {}).get("reason") or "stopped"
        elif kind == "task_paused":
            entry["paused"] = (event.get("data") or {}).get("message") or "paused"
        elif kind in ("archived", "flagged", "flags_cleared", "rescored", "job_submitted"):
            entry["stop"] = None
            if kind == "job_submitted":
                entry["paused"] = None
    return state


def metric_spec(task_dir: Path) -> dict:
    meta = read_json(task_dir / "task.json", {}) or {}
    declaration = meta.get("declaration")
    if isinstance(declaration, dict):   # InfiniEvolve
        name = declaration.get("rank_by")
        spec = (declaration.get("metrics") or {}).get(name) or {}
        return {"name": name, "direction": spec.get("direction", "max"), "scale": spec.get("scale", "absolute"),
                "format": spec.get("format", "{:.3f}"), "anchor": spec.get("anchor", "baseline"),
                "marks": [{"label": str(label), "value": number(value)} for label, value in (spec.get("marks") or {}).items()
                          if number(value) is not None]}
    return {"name": meta.get("metric") or "speedup", "anchor": "baseline", "marks": [], **DEFAULT_METRIC}   # older layout


def flags_of(node: dict) -> list:
    raw = node.get("flags") or {}
    if isinstance(raw, dict):   # InfiniEvolve: {name: {level, note}}
        return [{"name": name, "level": (flag or {}).get("level", "hard"), "note": (flag or {}).get("note", "")}
                for name, flag in raw.items()]
    notes = node.get("flag_notes") or {}
    return [{"name": name, "level": "void" if name in OLD_VOID else "soft" if name in OLD_SOFT else "hard",
             "note": notes.get(name, "")} for name in raw]


def chosen_metrics(node: dict) -> dict:
    row = next((r for r in node.get("scores") or [] if r.get("label") == node.get("chosen_label")), {}) or {}
    metrics = row.get("metrics")
    if isinstance(metrics, dict):
        return {k: number(v) for k, v in metrics.items()}
    keys = ("official_speedup", "paired_speedup", "reward", "simulation_speedup")   # older layout
    return {k: number(row.get(k)) for k in keys if k in row}


def metric_formats(task_dir: Path) -> dict:
    declaration = (read_json(task_dir / "task.json", {}) or {}).get("declaration")
    metrics = (declaration or {}).get("metrics") if isinstance(declaration, dict) else None
    return {name: (m or {}).get("format", "{:.3f}") for name, m in (metrics or {}).items()}


def node_record(node: dict, spec: dict, formats: dict | None = None) -> dict:
    flags = flags_of(node)
    cleared = bool(node.get("flags_cleared"))
    live = [f for f in flags if not cleared]
    value = number(node.get("rank_value", node.get("primary_speedup")))
    valid = node.get("valid", node.get("correct"))
    timing = node.get("timing") or {}
    report = node.get("report") or ""
    return {
        "id": node.get("session_id"), "index": node.get("index", 0), "parent": node.get("parent_id"),
        "start": node.get("start_strategy"), "status": "archived", "valid": bool(valid),
        "value": value, "display": formatted(spec["format"], value), "metrics": chosen_metrics(node),
        "texts": {k: formatted(formats[k], v) for k, v in chosen_metrics(node).items() if k in (formats or {})},
        "flags": flags, "flagsCleared": cleared,
        "void": any(f["level"] == "void" for f in live), "excluded": any(f["level"] in ("hard", "void") for f in live),
        "duplicate": any(f["name"] in ("duplicate", "duplicate_code") for f in flags),
        "agentSeconds": number(timing.get("agent_seconds")), "wallSeconds": number(timing.get("wall_seconds")),
        "archivedAt": node.get("archived_at"), "error": node.get("error"),
        "report": report if len(report) <= REPORT_CHARS else report[:REPORT_CHARS] + "\n[...]",
        "cost": number((node.get("usage") or node.get("tokens") or {}).get("cost_usd")),
    }


def pending_record(session: Path) -> dict | None:
    spec = read_json(session / "input" / "spec.json", {}) or {}
    if spec.get("session_id") is None and not (session / "job.json").is_file():
        return None
    job = read_json(session / "job.json", {}) or {}
    return {"id": session.name, "index": spec.get("index", 0), "parent": spec.get("parent_id"),
            "start": spec.get("start_strategy"), "status": "running" if (session / "job.json").is_file() else "prepared", "valid": False,
            "value": None, "display": "-", "metrics": {}, "texts": {}, "flags": [], "flagsCleared": False, "void": False,
            "excluded": False, "duplicate": False, "agentSeconds": None, "wallSeconds": None,
            "archivedAt": None, "error": None, "report": "", "cost": None, "submittedAt": job.get("submitted_at")}


def task_snapshot(task_dir: Path, events: dict) -> dict:
    spec, formats = metric_spec(task_dir), metric_formats(task_dir)
    nodes, sessions = [], task_dir / "sessions"
    for session in sorted(sessions.glob("*")) if sessions.is_dir() else []:
        node = read_json(session / "node.json")
        if isinstance(node, dict) and node.get("session_id"):
            record = node_record(node, spec, formats)
            remeasuring = [p for p in (session / "confirm").glob("r*/job.json") if not (p.parent / "sample.json").is_file()]
            if remeasuring:
                record["status"] = "remeasuring"
            nodes.append(record)
        elif (session / "job.json").is_file() or (session / "input" / "spec.json").is_file():
            record = pending_record(session)
            if record:
                nodes.append(record)
    nodes.sort(key=lambda n: (n["index"], n["id"]))
    state = events.get(task_dir.name, {})
    return {"name": task_dir.name, "metric": spec, "nodes": nodes,
            "stopped": state.get("stop"), "paused": state.get("paused")}


def snapshot(root) -> dict:
    root = Path(root)
    if not (root / "config.yaml").is_file() and not (root / "tasks").is_dir():
        raise ValueError(f"{root} is not an InfiniEvolve campaign directory")
    events = task_events(root)
    tasks = [task_snapshot(task, events) for task in sorted((root / "tasks").glob("*"))
             if task.is_dir()] if (root / "tasks").is_dir() else []
    return {"format": "infinievolve-campaign", "version": 1, "campaign": root.resolve().name,
            "stamp": stamp(root), "tasks": tasks}


if __name__ == "__main__":
    import sys
    print(json.dumps(snapshot(sys.argv[1]), indent=1, ensure_ascii=False))
