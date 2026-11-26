"""TraceVista analyses of single campaign sessions.

A session's state analysis is written either by a coding agent on this machine that the reader picks
(Claude Code, Codex, or any command the server was given), which follows the TraceVista skill in a working
directory of its own, or by a model the page asks directly with the reader's key. This module does the rest
with the skill's scripts: it extracts the session's agent log into a timeline, compiles an analysis into a
trajectory graph (or says every problem it has, so it can be fixed), runs the agents, and keeps the result.

    <campaign>/vista/<task>/<session>/extracted/   the timeline (redacted, images omitted)
    <campaign>/vista/<task>/<session>/analysis.json
    <campaign>/vista/<task>/<session>/graph.json   the trajectory graph TraceVista opens
    <campaign>/vista/<task>/<session>/quality.json reading-layer coverage and gaps
    <campaign>/vista/<task>/<session>/agent/       an agent's working directory: the skill, the timeline,
                                                   check.py, its prompt, transcript and job.json

Standard library only; Python 3.9 or newer.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,200}$")
# where a session keeps its agent's log: agent.jsonl, or agent.log in runs made by InfiniEvolve's local
# adapter before it adopted that name
LOGS = ("raw/agent.jsonl", "raw/agent.log")
HERE = Path(__file__).resolve().parent


def skill_root() -> Path:
    """The TraceVista skill: in the TraceVista submodule of a checkout of this repository, so that an update of
    TraceVista applies at once; else the copy an installed package carries (scripts/build.mjs makes it)."""
    checkout = HERE.parent / "third_party" / "TraceVista" / "skills" / "tracevista-skill"
    return checkout if (checkout / "SKILL.md").is_file() else HERE / "tracevista_skill"


def skill_scripts() -> Path:
    return skill_root() / "scripts"


TIMEOUT = 600


class VistaError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def session_dir(root: Path, task: str, session: str) -> Path:
    if not (NAME.match(task or "") and NAME.match(session or "")):
        raise VistaError(400, "Unknown task or session")
    path = root / "tasks" / task / "sessions" / session
    if not path.is_dir():
        raise VistaError(404, "Unknown task or session")
    return path


def agent_log(session: Path) -> Path | None:
    return next((session / name for name in LOGS if (session / name).is_file()), None)


def store(root: Path, task: str, session: str) -> Path:
    return root / "vista" / task / session


def private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def run(script: str, *args, scripts: Path | None = None) -> subprocess.CompletedProcess:
    scripts = scripts or skill_scripts()
    if not (scripts / script).is_file():
        raise VistaError(501, "The TraceVista skill scripts are not available to this server")
    try:
        return subprocess.run([sys.executable, str(scripts / script), *map(str, args)], capture_output=True,
                              text=True, timeout=TIMEOUT, cwd=str(scripts))
    except subprocess.TimeoutExpired:
        raise VistaError(504, f"{script} took longer than {TIMEOUT // 60} minutes") from None


def viewer_refusal(graph: Path, strict: bool = True, scripts: Path | None = None) -> str:
    """Why TraceVista's viewer would refuse a compiled graph, by the skill's own import check; '' if it opens."""
    result = run("validate_trace.py", graph, *(["--require-reading-layer"] if strict else []), scripts=scripts)
    if result.returncode == 0:
        return ""
    try:
        said = json.loads(result.stdout.strip().splitlines()[-1])
        return f"validate_trace.py: {said.get('error')} at {said.get('stage')}"
    except (ValueError, IndexError, AttributeError):
        return "validate_trace.py: " + complaint(result, graph.parent)


def complaint(result: subprocess.CompletedProcess, *private: Path) -> str:
    """The script's own message, without the local paths it may mention."""
    lines = [line for line in (result.stderr or result.stdout or "").strip().splitlines() if line.strip()]
    text = lines[-1] if lines else f"exit status {result.returncode}"
    for path in private:
        text = text.replace(str(path), "…")
    return text[:2000]


def status(root: Path, task: str, session: str) -> dict:
    path = session_dir(root, task, session)
    here = store(root, task, session)
    graph = here / "graph.json"
    quality = None
    if (here / "quality.json").is_file():
        try:
            quality = json.loads((here / "quality.json").read_text())
        except ValueError:
            quality = None
    return {"task": task, "session": session, "log": agent_log(path) is not None,
            "ready": graph.is_file(), "builtAt": graph.stat().st_mtime if graph.is_file() else None,
            "quality": quality, "job": None}


def timeline(root: Path, task: str, session: str, scripts: Path | None = None) -> dict:
    """The session's timeline as the skill's Markdown parts, extracted once per version of the log."""
    log = agent_log(session_dir(root, task, session))
    if log is None:
        raise VistaError(404, "This session has no agent log")
    here = private_dir(store(root, task, session))
    out, mark = here / "extracted", here / "extracted.from"
    version = f"{log.stat().st_size}:{log.stat().st_mtime_ns}"
    if not (out / "timeline.json").is_file() or not mark.is_file() or mark.read_text() != version:
        tmp = here / "extracted.tmp"
        shutil.rmtree(tmp, ignore_errors=True)
        result = run("extract_timeline.py", log, "--images", "omit", "--out", tmp, scripts=scripts)
        if result.returncode != 0:
            shutil.rmtree(tmp, ignore_errors=True)
            raise VistaError(422, "The agent log could not be read: " + complaint(result, log.parent, here))
        shutil.rmtree(out, ignore_errors=True)
        os.replace(tmp, out)
        mark.write_text(version)
    parts = sorted(out.glob("timeline*.md"), key=lambda p: int(re.sub(r"\D", "", p.stem) or 1))
    events = json.loads((out / "timeline.json").read_text()).get("timeline", [])
    texts = [p.read_text(errors="replace") for p in parts]
    return {"task": task, "session": session, "events": len(events),
            "first": events[0]["id"] if events else None, "last": events[-1]["id"] if events else None,
            "parts": texts, "characters": sum(map(len, texts))}


BEHAVIORS = ("Explore", "Plan", "Edit", "Execute", "Verify", "Diagnose")
LINK_KINDS = ("sequence", "informs", "review_request", "review_return")
STATUSES = ("attempted", "retained", "proposed", "rejected")
REF = re.compile(r"(?:(?P<s>[A-Za-z0-9_-]+):)?E(?P<lo>\d+)(?:\s*[-–]\s*(?:(?P<s2>[A-Za-z0-9_-]+):)?E?(?P<hi>\d+))?")


def _events(refs, known: set, where: str, out: list) -> set:
    """Expand 'E3-E9' and 'E12' as the skill's compiler does, noting bad references instead of stopping."""
    picked = set()
    for ref in refs if isinstance(refs, list) else [refs] if refs else []:
        m = REF.fullmatch(str(ref).strip())
        if not m or (m["s2"] and m["s2"] != m["s"]) or int(m["hi"] or m["lo"]) < int(m["lo"]):
            out.append(f"{where}: invalid event reference {ref!r}")
            continue
        for n in range(int(m["lo"]), int(m["hi"] or m["lo"]) + 1):
            key = (m["s"] + ":" if m["s"] else "") + f"E{n}"
            if key in known:
                picked.add(key)
            else:
                out.append(f"{where}: unknown event {key}")
                break
    return picked


def _ranges(ids: set, order: list) -> str:
    runs, start, prev = [], None, None
    for i, key in enumerate(order):
        if key in ids:
            if start is None:
                start = key
            prev = key
        elif start is not None:
            runs.append(start if start == prev else f"{start}-{prev}")
            start = None
    if start is not None:
        runs.append(start if start == prev else f"{start}-{prev}")
    return ", ".join(runs)


def problems(analysis: dict, order: list) -> list:
    """Every structural rule of build_state_graph.py that a model tends to break, all at once.

    The compiler stops at the first problem, so a model fixing a long analysis one problem per answer
    needs many answers. This lists them together; the compiler still has the last word.
    Snapshot values must also be text or null, which the viewer requires and the compiler does not check.
    """
    out: list = []
    known = set(order)
    if not analysis.get("scope"):
        out.append("scope is required")
    items, seen = {}, set()
    for name in ("states", "branches", "actions"):
        items[name] = {}
        group = analysis.get(name)
        if not isinstance(group, list) or not group:
            out.append(f"{name} is missing or empty")
            continue
        for item in group:
            if not isinstance(item, dict):
                out.append(f"{name}: every entry must be an object")
                continue
            key = item.get("id")
            if not key or key in seen:
                out.append(f"{name}: missing or duplicate id {key!r}")
                continue
            seen.add(key)
            items[name][key] = item
            if not item.get("title") or not item.get("events"):
                out.append(f"{key}: title and events are required")
            item["_events"] = _events(item.get("events"), known, key, out)
    states, branches, actions = items["states"], items["branches"], items["actions"]
    keys = None
    for sid, state in states.items():
        snap = state.get("snapshot")
        if not isinstance(snap, dict) or not snap:
            out.append(f"{sid}: snapshot must be a non-empty object")
            continue
        bad = [k for k, v in snap.items() if v is not None and not isinstance(v, str)]
        if bad:
            out.append(f"{sid}: snapshot values must be text or null, not numbers or objects: {', '.join(bad)}")
        lineage = state.get("lineage")
        if lineage is not None:
            if not isinstance(lineage, dict) or lineage.get("parent") not in states or lineage.get("parent") == sid:
                out.append(f"{sid}: lineage.parent must name another state")
            elif lineage.get("relation") not in ("derived", "restored", "retained"):
                out.append(f"{sid}: lineage.relation must be derived, restored or retained")
            elif not str(lineage.get("reason") or "").strip():
                out.append(f"{sid}: lineage needs a reason")
            elif not _events(lineage.get("events"), known, f"{sid} lineage", out):
                out.append(f"{sid}: lineage needs events")
        if keys is None:
            keys = set(snap)
        elif set(snap) != keys:
            out.append(f"{sid}: snapshot keys differ from the first state's: {', '.join(sorted(set(snap) ^ keys))}")
    for bid, branch in branches.items():
        if branch.get("state") not in states:
            out.append(f"{bid}: unknown state {branch.get('state')!r}")
        if branch.get("status") not in STATUSES:
            out.append(f"{bid}: status must be one of {', '.join(STATUSES)}")
    for aid, action in actions.items():
        branch = branches.get(action.get("branch"))
        if not branch:
            out.append(f"{aid}: unknown branch {action.get('branch')!r}")
        elif action.get("state") != branch.get("state"):
            out.append(f"{aid}: branch/state mismatch: the action says state {action.get('state')!r}, "
                       f"its branch {branch['id']} is in state {branch.get('state')!r}")
        if action.get("behavior") not in BEHAVIORS:
            out.append(f"{aid}: behavior must be one of {', '.join(BEHAVIORS)}")
        if any(b not in BEHAVIORS for b in action.get("secondary", []) or []):
            out.append(f"{aid}: unknown secondary behavior")
        if not isinstance(action.get("actor"), str) or not action["actor"].strip():
            out.append(f"{aid}: actor is required")
        if not action.get("classification_reason"):
            out.append(f"{aid}: classification_reason is required")
        if branch and branch.get("status") in ("proposed", "rejected") and action.get("outcome") != "proposed":
            out.append(f"{aid}: an untried branch ({branch['id']}, {branch.get('status')}) can only hold outcome 'proposed'")
    for i, link in enumerate(analysis.get("links") or []):
        where = f"links[{i}]"
        if link.get("source") not in actions or link.get("target") not in actions:
            out.append(f"{where}: source and target must be action ids")
        if link.get("kind") not in LINK_KINDS or not link.get("evidence"):
            out.append(f"{where}: kind must be one of {', '.join(LINK_KINDS)}, with evidence text")
        if not _events(link.get("events"), known, where, out):
            out.append(f"{where}: events are required")
    for i, t in enumerate(analysis.get("transitions") or []):
        where = f"transitions[{i}] {t.get('source')}->{t.get('target')}"
        start, end = t.get("source"), t.get("target")
        if start not in states or end not in states or start == end:
            out.append(f"{where}: source and target must be two different state ids")
            continue
        action = actions.get(t.get("action"))
        if not action or action.get("state") != start:
            out.append(f"{where}: its action must belong to the source state {start}")
        elif action.get("behavior") not in ("Edit", "Execute") or action.get("outcome") != "success":
            out.append(f"{where}: its action {action['id']} must be an Edit or Execute with outcome 'success'")
        elif branches.get(action.get("branch"), {}).get("status") in ("proposed", "rejected"):
            out.append(f"{where}: an untried branch cannot create a state")
        before, after = states[start].get("snapshot") or {}, states[end].get("snapshot") or {}
        if not isinstance(before, dict) or not isinstance(after, dict):
            continue
        changed = {k for k in before.keys() | after.keys() if before.get(k) != after.get(k)}
        changes = [c for c in t.get("changes") or [] if isinstance(c, dict)]
        listed = {c.get("component") for c in changes}
        if not changes:
            out.append(f"{where}: changes are required")
        elif changed != listed:
            out.append(f"{where}: changes must list exactly the differing snapshot keys {sorted(changed)}, not {sorted(map(str, listed))}")
        for c in changes:
            k = c.get("component")
            if k in changed and (c.get("before") != before.get(k) or c.get("after") != after.get(k)):
                out.append(f"{where}: change {k} must have before/after equal to the two snapshots' values")
            if not c.get("basis") or not c.get("events"):
                out.append(f"{where}: change {k} needs basis and events")
    covered = set().union(*(a["_events"] for a in actions.values())) if actions else set()
    excluded = set()
    for i, item in enumerate(analysis.get("excluded") or []):
        if not item.get("reason"):
            out.append(f"excluded[{i}]: reason is required")
        excluded |= _events(item.get("events"), known, f"excluded[{i}]", out)
    missing = known - covered - excluded
    if missing:
        out.append(f"Events cited by no action and not excluded: {_ranges(missing, order)}")
    both = covered & excluded
    if both:
        out.append(f"Events both cited by an action and excluded: {_ranges(both, order)}")
    for group in items.values():
        for item in group.values():
            item.pop("_events", None)
    return out


def build(root: Path, task: str, session: str, analysis, strict: bool = True, scripts: Path | None = None) -> dict:
    """Compile what the model wrote. {"ok": True} with the reading quality, or {"ok": False, "error": ...}."""
    if not isinstance(analysis, dict):
        return {"ok": False, "error": "The analysis must be one JSON object"}
    session_dir(root, task, session)
    here = store(root, task, session)
    if not (here / "extracted" / "timeline.json").is_file():
        timeline(root, task, session, scripts)
    (here / "analysis.json").write_text(json.dumps(analysis, ensure_ascii=False, indent=1))
    os.chmod(here / "analysis.json", 0o600)
    order = [e["id"] for e in json.loads((here / "extracted" / "timeline.json").read_text()).get("timeline", [])]
    found = problems(analysis, order)
    if found:
        shown = found[:80]
        more = f"\n- … and {len(found) - len(shown)} more" if len(found) > len(shown) else ""
        return {"ok": False, "error": f"{len(found)} problem(s); fix all of them:\n- " + "\n- ".join(shown) + more}
    pending, quality = here / "graph.pending.json", here / "quality.pending.json"
    args = [here / "analysis.json", "--timeline", here / "extracted" / "timeline.json", "--format", "state",
            "--quality-report", quality, "--out", pending]
    result = run("build_state_graph.py", *args, *(["--require-reading-layer"] if strict else []), scripts=scripts)
    if result.returncode != 0 or not pending.is_file():
        for p in (pending, quality):
            p.unlink(missing_ok=True)
        return {"ok": False, "error": complaint(result, here, scripts or skill_scripts())}
    refused = viewer_refusal(pending, strict, scripts)
    if refused:
        for p in (pending, quality):
            p.unlink(missing_ok=True)
        return {"ok": False, "error": "It compiles, but TraceVista's viewer would refuse it (" + refused + "). "
                                      "Check the fields the compiler passes through unchanged, such as states[].lineage."}
    os.replace(pending, here / "graph.json")
    if quality.is_file():
        os.replace(quality, here / "quality.json")
    return {"ok": True, **status(root, task, session)}


def graph(root: Path, task: str, session: str) -> bytes:
    session_dir(root, task, session)
    path = store(root, task, session) / "graph.json"
    if not path.is_file():
        raise VistaError(404, "This session has not been analysed")
    return path.read_bytes()


# -- the routes both campaign servers answer --------------------------------------------------------------
MAX_ANALYSIS_BYTES = 32 * 1024 * 1024
MAX_FACTS_BYTES = 64 * 1024


def handle(server, method: str, route: str, query: dict, read_body) -> tuple:
    """(status, JSON or graph bytes) for /api/campaign/vista<route>. The caller has checked origin and token.

    server has .campaign (the campaign directory), .vista_lock and .analysts (an Analysts, or None).

        GET  /analysts                     the agents the reader may pick
        GET  ?task=&session=               whether the session was analysed, and its agent's job
        GET  /timeline?task=&session=      its extracted timeline, for a model the page asks
        GET  /graph?task=&session=         the compiled trajectory graph
        POST /build?task=&session=         compile the analysis in the body (strict=0: allow reading gaps)
        POST /agent?task=&session=&analyst=   start an agent; the body is {"facts", "model", "effort"}
        POST /agent/stop?task=&session=    stop it
    """
    task, session = (query.get("task") or [""])[0], (query.get("session") or [""])[0]
    root, analysts = server.campaign, getattr(server, "analysts", None)
    try:
        if method == "GET" and route == "/analysts":
            answer = {"analysts": analysts.listing() if analysts else []}
        elif method == "GET" and route == "":
            job = None
            if analysts:
                with server.vista_lock:
                    job = analysts.job(root, task, session)   # first: a job that just ended is compiled here
            answer = {**status(root, task, session), "job": job}
        elif method == "GET" and route == "/timeline":
            with server.vista_lock:
                answer = timeline(root, task, session)
        elif method == "GET" and route == "/graph":
            return 200, graph(root, task, session)
        elif method == "POST" and route == "/build":
            body = read_body(MAX_ANALYSIS_BYTES)
            if body is None:
                return 413, b'{"error":"The analysis is empty or too large"}'
            try:
                analysis = json.loads(body)
            except ValueError:
                answer = {"ok": False, "error": "The analysis is not valid JSON"}
            else:
                with server.vista_lock:
                    answer = build(root, task, session, analysis, strict=(query.get("strict") or ["1"])[0] != "0")
        elif method == "POST" and route in ("/agent", "/agent/stop"):
            if not analysts:
                raise VistaError(404, "This server runs no agents")
            if route == "/agent/stop":
                with server.vista_lock:
                    answer = analysts.stop(root, task, session)
            else:
                body = read_body(MAX_FACTS_BYTES) or b"{}"
                try:
                    given = json.loads(body)
                    facts, model, effort = (str(given.get(k) or "") for k in ("facts", "model", "effort"))
                except (ValueError, AttributeError):
                    facts = model = effort = ""
                with server.vista_lock:
                    answer = analysts.start(root, task, session, (query.get("analyst") or [""])[0], facts, model, effort)
        else:
            return 404, b'{"error":"Not found"}'
    except VistaError as error:
        return error.status, json.dumps({"error": str(error)}).encode()
    except (OSError, ValueError):
        return 500, b'{"error":"The analysis could not be prepared"}'
    return 200, json.dumps(answer, ensure_ascii=False).encode()


# -- agents ---------------------------------------------------------------------------------------------
AGENT_HOURS = 3    # an agent still running after this long is stopped
MAX_AGENTS = 2     # agents running at once, over all sessions
WORD = re.compile(r"^[A-Za-z0-9._@/\[\]-]*$")
CUSTOM = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._ -]{0,60})=(.+)$", re.S)
# Claude Code may edit files in its working directory and run these commands, nothing else, without asking.
CLAUDE_TOOLS = ("Read", "Write", "Edit", "Glob", "Grep", "Bash(python3:*)", "Bash(python:*)", "Bash(ls:*)",
                "Bash(wc:*)", "Bash(head:*)", "Bash(tail:*)", "Bash(grep:*)")
# Agents whose prompt does not come on standard input are given this; the prompt is PROMPT.md beside them.
READ_PROMPT = "Read PROMPT.md in your working directory and follow it exactly."


def _model(flag: str):
    return lambda model: [flag, model] if model else []


# The agents offered when installed: how each runs unattended, as its documentation says. Only Claude Code
# and Codex have been run here; the page marks the others untested. The checks are the page's either way.
#   binary, label, command(model, effort), whether the prompt goes on standard input, extra environment, tested
PRESETS = {
    "claude": ("claude", "Claude Code", lambda m, e: [
        "claude", "-p", *_model("--model")(m), *(["--effort", e] if e else []), "--permission-mode", "acceptEdits",
        "--allowedTools", *CLAUDE_TOOLS, "--output-format", "stream-json", "--verbose", "--no-session-persistence"],
        True, {}, True),
    "codex": ("codex", "Codex", lambda m, e: [
        "codex", "exec", "--json", "--sandbox", "workspace-write", "--skip-git-repo-check", *_model("-m")(m),
        *(["-c", f'model_reasoning_effort="{e}"'] if e else []), "-"], True, {}, True),
    # https://geminicli.com/docs/cli/headless/ : -p runs headless; --yolo approves tool calls
    "gemini": ("gemini", "Gemini CLI", lambda m, e: [
        "gemini", *_model("-m")(m), "--yolo", "--output-format", "stream-json", "-p", READ_PROMPT], False, {}, False),
    # https://qwenlm.github.io/qwen-code-docs/en/users/features/headless : -p headless, --yolo approves
    "qwen": ("qwen", "Qwen Code", lambda m, e: ["qwen", *_model("-m")(m), "--yolo", "-p", READ_PROMPT], False, {}, False),
    # https://opencode.ai/docs/permissions/ : `opencode run --auto` approves what would ask
    "opencode": ("opencode", "OpenCode", lambda m, e: [
        "opencode", "run", "--auto", *_model("-m")(m), "--format", "json", READ_PROMPT], False, {}, False),
    # https://cursor.com/docs/cli/headless : -p --force edits without asking
    "cursor": ("cursor-agent", "Cursor CLI", lambda m, e: [
        "cursor-agent", "-p", "--force", *_model("-m")(m), "--output-format", "stream-json", READ_PROMPT], False, {}, False),
    # https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-programmatic-reference :
    # -p with --allow-all-tools; paths stay limited to the working directory
    "copilot": ("copilot", "GitHub Copilot CLI", lambda m, e: [
        "copilot", *_model("--model")(m), "--allow-all-tools", "-p", READ_PROMPT], False, {}, False),
    # https://ampcode.com/docs/cli/execute-mode : -x executes; --dangerously-allow-all approves commands
    "amp": ("amp", "Amp", lambda m, e: ["amp", "--dangerously-allow-all", "--stream-json", "-x", READ_PROMPT], False, {}, False),
    # https://block.github.io/goose/docs/guides/running-tasks : goose run -t, GOOSE_MODE=auto
    "goose": ("goose", "Goose", lambda m, e: ["goose", "run", "-t", READ_PROMPT], False, {"GOOSE_MODE": "auto"}, False),
    # https://docs.factory.ai/cli/droid-exec/overview : droid exec --auto medium edits and runs commands
    "droid": ("droid", "Factory Droid", lambda m, e: [
        "droid", "exec", "--auto", "medium", *_model("-m")(m), "-o", "stream-json", READ_PROMPT], False, {}, False),
}


# How an agent marks the commands it runs, where its source or documentation says so: Claude Code sets CLAUDECODE,
# Codex CODEX_THREAD_ID (and CODEX_SANDBOX inside its sandbox), Gemini CLI GEMINI_CLI. Any agent in PRESETS is also
# known by its program's name among the processes that started this one.
LAUNCH_MARKS = {"claude": ("CLAUDECODE",), "codex": ("CODEX_THREAD_ID", "CODEX_SANDBOX"), "gemini": ("GEMINI_CLI",)}


def ancestor_names(limit: int = 40) -> list:
    """The program names of the processes that started this one, nearest first; none where `ps` is missing."""
    names, pid = [], os.getppid()
    while pid > 1 and len(names) < limit:
        try:
            row = subprocess.run(["ps", "-o", "ppid=,comm=", "-p", str(pid)], capture_output=True, text=True,
                                 timeout=5, stdin=subprocess.DEVNULL).stdout.strip().split(None, 1)
            pid = int(row[0])
        except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
            break
        names.append(Path(row[1]).name if len(row) > 1 else "")
    return names


def launching_agent(environ=None, ancestors=None):
    """The agent in PRESETS this server was started from: the nearest of its processes named for one, else one
    whose mark is in the environment (it survives tmux and nohup, which hide the processes); None if neither."""
    programs = {binary: program for program, (binary, *_) in PRESETS.items()}
    for name in ancestor_names() if ancestors is None else ancestors:
        if name in programs:
            return programs[name]
    environ = os.environ if environ is None else environ
    return next((program for program, marks in LAUNCH_MARKS.items() if any(environ.get(m) for m in marks)), None)


# What the page offers beside each agent: models to pick from (any other may be typed) and effort levels.
# Claude Code cannot list its models, so these are the current ones; Codex lists its own (codex_models).
CHOICES = {
    "claude": ([{"id": "claude-opus-5-5", "name": "Claude Opus 5.5"}, {"id": "claude-sonnet-5-5", "name": "Claude Sonnet 5.5"},
                {"id": "claude-haiku-5-5", "name": "Claude Haiku 5.5"}, {"id": "claude-fable-5-2", "name": "Claude Fable 5.2"}],
               ["low", "medium", "high", "xhigh", "max"]),
    "codex": ([], ["minimal", "low", "medium", "high", "xhigh"]),
}


def codex_models() -> list:
    """The models Codex offers this login, with their effort levels: `codex debug models`, or none."""
    try:
        done = subprocess.run(["codex", "debug", "models"], capture_output=True, text=True, timeout=20,
                              stdin=subprocess.DEVNULL)
        listed = json.loads(done.stdout)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return []
    found = []
    for m in (listed.get("models") if isinstance(listed, dict) else listed) or []:
        if not isinstance(m, dict) or m.get("visibility") == "hide" or not WORD.match(str(m.get("slug", ""))) or not m.get("slug"):
            continue
        levels = [str(level.get("effort")) for level in m.get("supported_reasoning_levels") or [] if isinstance(level, dict)]
        found.append({"id": m["slug"], "name": str(m.get("display_name") or m["slug"]), "efforts": [x for x in levels if WORD.match(x)],
                      "effort": str(m.get("default_reasoning_level") or ""), "priority": m.get("priority") or 0})
    return [{k: v for k, v in m.items() if k != "priority"} for m in sorted(found, key=lambda m: m["priority"])]


def agent_launch(spec: str) -> dict:
    """How to start an analyst setting:

        <agent>[/<model>[:<effort>]]   one of PRESETS (effort: claude and codex only)
        <name>=<shell command>         any other agent, with the prompt on its standard input

    Every agent starts in its working directory, beside PROMPT.md; its environment also has
    TRACEVISTA_PROMPT (that file's path), TRACEVISTA_WORK and TRACEVISTA_SKILL.
    """
    custom = CUSTOM.match(spec)
    if custom:
        return {"command": custom.group(2), "shell": True, "label": custom.group(1).strip(), "stdin": True,
                "env": {}, "tested": None, "binary": None}
    program, _, rest = spec.partition("/")
    model, _, effort = rest.partition(":")
    preset = PRESETS.get(program)
    if not preset or not (WORD.match(model) and WORD.match(effort)) or (effort and program not in ("claude", "codex")):
        raise ValueError(f"an analyst is <agent>[/<model>[:<effort>]] with <agent> one of {', '.join(PRESETS)} "
                         f"(effort for claude and codex only), or <name>=<shell command>, not {spec!r}")
    binary, label, command, stdin, env, tested = preset
    if model and model not in command(model, effort):
        raise ValueError(f"{label} is started with its own model setting; give {program!r} without a model")
    return {"command": command(model, effort), "shell": False, "stdin": stdin, "env": env, "tested": tested,
            "binary": binary, "label": label + (f" · {model}" if model else "") + (f" · {effort}" if effort else "")}


def agent_command(spec: str) -> tuple:
    """(command, whether it is a shell command, label): see agent_launch."""
    launch = agent_launch(spec)
    return launch["command"], launch["shell"], launch["label"]


PROMPT = """You are analysing one coding agent's session for TraceVista, with the TraceVista skill.

Your working directory holds:
- skill/                the TraceVista skill. Read skill/SKILL.md, then the references it names for creating a
                        new file: conversion.md, state-graph.md, observations.md, reading-layer.md, clear-writing.md.
- extracted/            the session's timeline, already extracted (redacted, images omitted): timeline.json and
                        the timeline*.md parts. Do not extract again and do not look for the raw log.
- check.py              the checks the campaign page applies to your result.

What the campaign knows about the session, outside its log (use it in notes, not as event citations):
{facts}

Do this:
1. Read every timeline part. Look at complete records with
   python3 skill/scripts/show_events.py extracted/timeline.json E12 E40-E45
2. Write ./state-analysis.json as the skill describes, with the reading layer and a journey.
3. Run python3 check.py. It prints every problem at once; fix them all and run it again, until it prints OK.
4. It then lists reading-quality gaps (missing quotes, decisions without the observations that informed them).
   Fill the ones the evidence supports, never inventing evidence, and run check.py again after each change.

Work only in this directory. Do not open a browser, do not run session_state.py, and do not change files
outside this directory: the campaign page opens the result itself. Finish with two sentences on the session.
"""

CHECK = '''#!/usr/bin/env python3
"""The checks the campaign page applies to ./state-analysis.json: prints every problem, or OK and the gaps."""
import sys
sys.dont_write_bytecode = True
sys.path.insert(0, {module!r})
import vista
sys.exit(vista.check_work({work!r}))
'''


def check_work(work) -> int:
    """check.py: the structural problems, then the skill's compiler with the reading layer required."""
    work = Path(work)
    try:
        analysis = json.loads((work / "state-analysis.json").read_text())
    except FileNotFoundError:
        print("Write ./state-analysis.json first.")
        return 1
    except ValueError as error:
        print(f"state-analysis.json is not valid JSON: {error}")
        return 1
    if not isinstance(analysis, dict):
        print("state-analysis.json must be one JSON object.")
        return 1
    order = [e["id"] for e in json.loads((work / "extracted" / "timeline.json").read_text()).get("timeline", [])]
    found = problems(analysis, order)
    if found:
        print(f"{len(found)} problem(s); fix all of them:")
        print("\n".join("- " + line for line in found))
        return 1
    result = run("build_state_graph.py", work / "state-analysis.json", "--timeline", work / "extracted" / "timeline.json",
                 "--format", "state", "--require-reading-layer", "--quality-report", work / "quality.json",
                 "--out", work / "graph.json", scripts=work / "skill" / "scripts")
    if result.returncode != 0:
        print("The compiler refuses it: " + complaint(result, work))
        return 1
    refused = viewer_refusal(work / "graph.json", scripts=work / "skill" / "scripts")
    if refused:
        print(f"It compiles, but TraceVista's viewer would refuse it ({refused}). Check the fields the compiler "
              "passes through unchanged, such as states[].lineage, against skill/references/state-graph.md.")
        return 1
    print("OK: it compiles, with the reading layer, and TraceVista's viewer opens it.")
    try:
        quality = json.loads((work / "quality.json").read_text())
    except (OSError, ValueError):
        return 0
    gaps = {"quotes are missing for": quality.get("missing_quotes"), "causes are missing for": quality.get("missing_reasons"),
            "decisions have no informing observation": quality.get("decisions_without_informs")}
    gaps = {k: v for k, v in gaps.items() if v}
    if gaps:
        print("Reading-quality gaps; fill those the evidence supports and never invent evidence:")
        for k, v in gaps.items():
            print(f"- {k}: {', '.join(map(str, v))}")
    return 0


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _say(event: dict) -> str:
    """One line of what an agent is doing, from a Claude Code or Codex JSON event; '' for the rest."""
    kind = event.get("type")
    if kind == "assistant":   # Claude Code
        said = []
        for part in (event.get("message") or {}).get("content") or []:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "tool_use":
                given = part.get("input") or {}
                said.append(f"{part.get('name')}: {given.get('command') or given.get('file_path') or given.get('pattern') or ''}")
            elif part.get("type") == "text" and str(part.get("text", "")).strip():
                said.append(str(part["text"]))
        return " · ".join(said)
    if kind == "result":
        cost = event.get("total_cost_usd")
        return "Finished" + (f" (${cost:.2f})" if isinstance(cost, (int, float)) else "") + f": {event.get('result') or ''}"
    if str(kind).endswith("started"):
        return ""
    if kind == "tool_use":   # Gemini CLI and others: {"type": "tool_use", "tool_name": ..., "parameters": {...}}
        given = event.get("parameters") or event.get("input") or {}
        given = given if isinstance(given, dict) else {}
        return f"{event.get('tool_name') or event.get('name')}: {given.get('command') or given.get('file_path') or given.get('path') or ''}"
    if kind == "message" and event.get("role") == "assistant" and isinstance(event.get("content"), str):
        return event["content"]
    item = event.get("item") if isinstance(event.get("item"), dict) else event.get("msg")   # Codex
    if isinstance(item, dict):
        text = item.get("command") or item.get("text") or item.get("message")
        if isinstance(text, list):
            text = " ".join(map(str, text))
        if text:
            return (f"{item.get('type')}: {text}" if "command" in str(item.get("type")) else str(text))
    return ""


def activity(work: Path, *private: Path, lines: int = 80) -> list:
    """The last things the agent said or did, from its transcript."""
    said = []
    for name in ("transcript.jsonl", "stderr.log"):
        path = work / name
        if not path.is_file():
            continue
        size = path.stat().st_size
        with open(path, "rb") as f:
            f.seek(max(0, size - 256 * 1024))
            tail = f.read().decode(errors="replace").splitlines()
        if size > 256 * 1024:
            tail = tail[1:]   # the first line is cut
        for line in tail:
            try:
                event = json.loads(line)
                text = _say(event) if isinstance(event, dict) else ""
            except ValueError:
                text = line
            text = " ".join(text.split())
            for path_ in (work, *private):
                text = text.replace(str(path_), ".")
            if text:
                said.append(text[:600])
    return said[-lines:]


class Analysts:
    """The agents this server may start for an analysis, and the jobs they run."""

    def __init__(self, specs=(), detect: bool = True, launcher=None):
        """Offered in this order, the page picking the first unless the reader picked before: each --analyst
        setting, then the installed agent that started this server (`launcher`, found when not given), then
        the other installed agents."""
        self.choices = {}
        for spec in specs:
            launch = agent_launch(spec)   # a bad setting fails at start, not on the page
            key = launch["label"] if launch["shell"] else spec
            self.choices[key] = {"id": key, "label": launch["label"], "untested": launch["tested"] is False,
                                 "detail": launch["command"] if launch["shell"] else " ".join(launch["command"][:4]), "spec": spec,
                                 **({} if launch["shell"] else self.options(spec))}
        if detect:
            first = launcher if launcher is not None else launching_agent()
            for program in sorted(PRESETS, key=lambda p: p != first):
                binary, label, _, _, _, tested = PRESETS[program]
                if program not in self.choices and shutil.which(binary):
                    self.choices[program] = {"id": program, "label": label, "detail": binary,
                                             "untested": not tested, "spec": program, **self.options(program)}
        self.running = {}

    @staticmethod
    def options(spec: str) -> dict:
        """Whether the page may set a model and an effort for this agent, which to offer, and its own setting."""
        program, _, rest = spec.partition("/")
        model, _, effort = rest.partition(":")
        models, efforts = CHOICES.get(program, ([], []))
        if program == "codex":
            models = codex_models()
            known = CHOICES["codex"][1]
            efforts = sorted({e for m in models for e in m["efforts"]} | set(efforts),
                             key=lambda e: (known.index(e) if e in known else len(known), e))
        takes_model = "TRACEVISTA-MODEL" in PRESETS[program][2]("TRACEVISTA-MODEL", "")
        return {"takesModel": takes_model, "models": list(models), "efforts": list(efforts), "model": model, "effort": effort}

    def spec_for(self, analyst: str, model: str, effort: str) -> str:
        """The agent's setting with the reader's model and effort, where it takes them."""
        choice = self.choices.get(analyst)
        if not choice:
            raise VistaError(400, "Pick one of the agents this server offers")
        if CUSTOM.match(choice["spec"]):
            if model or effort:
                raise VistaError(400, f"{choice['label']} is a command of its own; it takes no model or effort here")
            return choice["spec"]
        program = choice["spec"].partition("/")[0]
        model, effort = model or choice.get("model", ""), effort or choice.get("effort", "")
        if effort and effort not in choice.get("efforts", ()):
            raise VistaError(400, f"{choice['label']} takes no effort {effort!r}")
        spec = program + (f"/{model}" if model or effort else "") + (f":{effort}" if effort else "")
        try:
            agent_launch(spec)
        except ValueError as error:
            raise VistaError(400, str(error)) from None
        return spec

    def listing(self) -> list:
        return [{k: v for k, v in c.items() if k != "spec"} for c in self.choices.values()]

    def start(self, root: Path, task: str, session: str, analyst: str, facts: str, model: str = "", effort: str = "") -> dict:
        spec = self.spec_for(analyst, model.strip(), effort.strip())
        choice = self.choices[analyst]
        timeline(root, task, session)   # extracted once per version of the log
        here = store(root, task, session)
        work = here / "agent"
        current = self.job(root, task, session)
        if current and current["state"] == "running":
            raise VistaError(409, "An agent is already analysing this session")
        if sum(p.poll() is None for p in self.running.values()) >= MAX_AGENTS:
            raise VistaError(429, f"{MAX_AGENTS} agents are already running; wait for one to finish")
        shutil.rmtree(work, ignore_errors=True)
        private_dir(work)
        shutil.copytree(skill_root(), work / "skill", ignore=shutil.ignore_patterns("tests", "__pycache__", ".gitignore"))
        shutil.copytree(here / "extracted", work / "extracted")
        (work / "check.py").write_text(CHECK.format(module=str(HERE), work=str(work)))
        prompt = work / "PROMPT.md"
        prompt.write_text(PROMPT.replace("{facts}", facts.strip()[:MAX_FACTS_BYTES] or "(nothing)"))
        launch = agent_launch(spec)
        label = launch["label"]
        environment = {**os.environ, **launch["env"], "TRACEVISTA_PROMPT": str(prompt), "TRACEVISTA_WORK": str(work),
                       "TRACEVISTA_SKILL": str(work / "skill")}
        with open(prompt if launch["stdin"] else os.devnull, "rb") as given, \
                open(work / "transcript.jsonl", "wb") as out, open(work / "stderr.log", "wb") as err:
            try:
                process = subprocess.Popen(launch["command"], shell=launch["shell"], cwd=str(work), stdin=given, stdout=out,
                                           stderr=err, env=environment, start_new_session=True)
            except OSError:
                raise VistaError(500, f"{label} could not be started on this machine") from None
        self.running[str(work)] = process
        (work / "job.json").write_text(json.dumps({"analyst": choice["id"], "label": label, "pid": process.pid,
                                                   "started": time.time(), "state": "running"}))
        return self.job(root, task, session)

    def job(self, root: Path, task: str, session: str) -> dict | None:
        """The session's agent job, finished here when its process has ended: compiled, or why not."""
        session_dir(root, task, session)
        work = store(root, task, session) / "agent"
        try:
            job = json.loads((work / "job.json").read_text())
        except (OSError, ValueError):
            return None
        if job.get("state") == "running":
            process = self.running.get(str(work))
            ended = process.poll() is not None if process else not _alive(job["pid"])
            if not ended and time.time() - job["started"] > AGENT_HOURS * 3600:
                self._kill(job["pid"])
                ended, job["note"] = True, f"Stopped after {AGENT_HOURS} hours"
            if ended:
                self.running.pop(str(work), None)
                job.update(ended=time.time(), **self._finish(root, task, session, work))
                (work / "job.json").write_text(json.dumps(job))
        return {"analyst": job.get("analyst"), "label": job.get("label"), "state": job.get("state"),
                "loose": job.get("loose", False), "error": job.get("error"), "note": job.get("note"),
                "elapsed": round((job.get("ended") or time.time()) - job["started"]),
                "activity": activity(work, root)}

    def _finish(self, root: Path, task: str, session: str, work: Path) -> dict:
        try:
            analysis = json.loads((work / "state-analysis.json").read_text())
        except FileNotFoundError:
            return {"state": "failed", "error": "The agent ended without writing state-analysis.json"}
        except ValueError:
            return {"state": "failed", "error": "The agent's state-analysis.json is not valid JSON"}
        result = build(root, task, session, analysis)
        if result.get("ok"):
            return {"state": "done"}
        # structure that compiles with reading gaps is still worth opening; the page says so
        if build(root, task, session, analysis, strict=False).get("ok"):
            return {"state": "done", "loose": True}
        return {"state": "failed", "error": result["error"]}

    @staticmethod
    def _kill(pid: int) -> None:
        try:
            os.killpg(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass

    def stop(self, root: Path, task: str, session: str) -> dict:
        work = store(root, task, session) / "agent"
        try:
            job = json.loads((work / "job.json").read_text())
        except (OSError, ValueError):
            raise VistaError(404, "No agent is analysing this session") from None
        if job.get("state") == "running":
            self._kill(job["pid"])
            process = self.running.pop(str(work), None)
            if process:
                try:
                    process.wait(10)
                except subprocess.TimeoutExpired:
                    os.killpg(job["pid"], signal.SIGKILL)
            job.update(state="stopped", ended=time.time())
            (work / "job.json").write_text(json.dumps(job))
        return self.job(root, task, session)
