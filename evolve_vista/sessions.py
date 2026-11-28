"""What the page shows of a session beyond the snapshot: its time, split into development and validation.

Both readers of a campaign (InfiniEvolve's snapshot and campaign.py) leave out a session's time limit, how long
its evaluation took and, while it runs, which phase it is in; enrich() adds them from the session's own files:

    input/spec.json        time_limit_seconds: the agent's time limit
    node.json              timing.evaluate_seconds: how long scoring took
    raw/final.patch        written when the agent ends and the evaluation starts (the local adapter)
    confirm/r*/job.json    a re-measurement under way, until sample.json appears beside it

Read only, standard library only.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path


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
