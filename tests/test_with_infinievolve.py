"""The page on a real InfiniEvolve campaign (the golf example, run once), read with InfiniEvolve's own snapshot.

Runs where InfiniEvolve is installed beside this package, from a checkout that has its examples."""
from __future__ import annotations

import http.client
import json
import os
from pathlib import Path
import re
import shutil
import sys
import time
import urllib.error
import urllib.request

import pytest

infinievolve = pytest.importorskip("infinievolve")
from infinievolve.cli import main  # noqa: E402

from evolve_vista import Analysts, start  # noqa: E402
from evolve_vista.server import STATIC, VistaServer  # noqa: E402

EXAMPLE = Path(infinievolve.__file__).resolve().parents[1] / "examples" / "golf"


@pytest.fixture(scope="module")
def golf(tmp_path_factory):
    """Nine sessions, the best at s005, three duplicates."""
    if shutil.which("git") is None or not EXAMPLE.is_dir():
        pytest.skip("needs git and InfiniEvolve's examples")
    task = tmp_path_factory.mktemp("watch") / "golf"
    shutil.copytree(EXAMPLE, task, ignore=shutil.ignore_patterns("__pycache__", "runs"))
    assert main(["run", str(task)]) == 0
    return task / "runs" / "default"


def test_infinievolve_campaigns_are_read_with_its_own_snapshot(golf):
    from infinievolve.snapshot import snapshot
    server = start(golf, port=0)
    try:
        assert server.reader.snapshot is snapshot
        assert server.reader.snapshot(golf)["tasks"][0]["metric"]["marks"] == []
    finally:
        server.shutdown(); server.server_close()


def test_the_server_streams_a_snapshot_now_and_on_change_only(golf, tmp_path):
    copy = tmp_path / "run"
    shutil.copytree(golf, copy)
    server = start(copy, port=0)
    try:
        base = f"127.0.0.1:{server.server_port}"
        assert server.url.startswith(f"http://{base}/#live=")
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=10)
        connection.request("GET", f"/api/campaign/events?token={server.token}")
        response = connection.getresponse()
        assert response.status == 200 and response.getheader("Content-Type") == "text/event-stream"

        def snapshot_from_stream():
            lines = []
            while True:
                line = response.fp.readline().decode().rstrip("\n")
                if not line and any(l.startswith("data: ") for l in lines):
                    return json.loads(next(l for l in lines if l.startswith("data: "))[6:])
                if line:
                    lines.append(line)

        first = snapshot_from_stream()
        assert len(first["tasks"][0]["nodes"]) == 9
        session = copy / "tasks" / "golf" / "sessions" / "s009"
        (session / "input").mkdir(parents=True)
        (session / "input" / "spec.json").write_text(json.dumps({"session_id": "s009", "index": 9, "parent_id": "s005"}))
        second = snapshot_from_stream()
        assert second["tasks"][0]["nodes"][-1]["id"] == "s009" and second["stamp"] != first["stamp"]
        connection.close()
    finally:
        server.shutdown(); server.server_close()


def test_what_the_server_serves_and_refuses(golf):
    server = start(golf, port=0)
    try:
        def get(path, **headers):
            request = urllib.request.Request(f"http://127.0.0.1:{server.server_port}{path}", headers=headers)
            try:
                with urllib.request.urlopen(request) as response:
                    return response.status, response.read()
            except urllib.error.HTTPError as error:
                return error.code, error.read()
        status, page = get("/")
        assert status == 200 and b"<div id=\"root\">" in page
        for asset in re.findall(rb'(?:src|href)="\./([^"]+)"', page):
            assert get("/" + asset.decode())[0] == 200
        assert get("/api/campaign/events")[0] == 403, "no token"
        assert get(f"/api/campaign/events?token={server.token}", Origin="http://evil.example")[0] == 403
        assert get("/SOURCE.json")[0] == 404 and get("/../pyproject.toml")[0] == 404
    finally:
        server.shutdown(); server.server_close()


def test_the_port_falls_back_when_taken(golf):
    first = start(golf, port=0)
    try:
        second = start(golf, port=first.server_port)
        assert second.server_port != first.server_port
        second.shutdown(); second.server_close()
    finally:
        first.shutdown(); first.server_close()


def test_the_built_page_is_complete():
    """npm run build: the campaign page, TraceVista's trajectory viewer beside it, and the skill."""
    for page in ("campaign.html", "trajectory.html"):
        for name in re.findall(r'(?:src|href)="\./([^"]+)"', (STATIC / page).read_text()):
            assert (STATIC / name).is_file(), name
    source = json.loads((STATIC / "SOURCE.json").read_text())
    assert source["tracevista"]["license"] == "MIT" and "trajectory.html" in source["trajectoryFiles"]
    from evolve_vista.vista import skill_root
    assert (skill_root() / "SKILL.md").is_file() and (skill_root() / "scripts" / "build_state_graph.py").is_file()


def test_a_directory_that_is_not_a_campaign_is_refused(tmp_path):
    with pytest.raises(ValueError):
        VistaServer(tmp_path, 0)


LOG = "\n".join(json.dumps(r) for r in [
    {"type": "system", "subtype": "init", "session_id": "x", "model": "m", "cwd": "/r"},
    {"type": "assistant", "message": {"role": "assistant", "model": "m", "content": [{"type": "text", "text": "I shortened the loop."}]}},
    {"type": "result", "subtype": "success", "result": "I shortened the loop.", "session_id": "x"}]) + "\n"
ANALYSIS = {
    "title": "Golf s001", "scope": "the solution file", "language": "en", "notes": [],
    "states": [{"id": "s1", "title": "Start", "summary": "The starting solution.", "events": ["E2"], "snapshot": {"solution": None},
                "ladder": {"topic": "Start", "takeaway": "Start from the given solution"}}],
    "branches": [{"id": "b1", "state": "s1", "title": "Shorten", "summary": "The agent reports a shorter loop.", "events": ["E2"],
                  "status": "attempted", "ladder": {"topic": "shorter loop", "takeaway": "Reports a shorter loop"}}],
    "actions": [{"id": "a1", "state": "s1", "branch": "b1", "title": "Report", "summary": "The agent says it shortened the loop.",
                 "events": ["E2"], "actor": "main", "behavior": "Plan", "outcome": "observed", "classification_reason": "A report."}],
    "transitions": [], "links": [], "excluded": [{"events": ["E1", "E3"], "reason": "Runner status."}],
    "journey": {"text": "An agent reported a shorter loop.", "cites": ["s1", "b1"]}}


def test_a_session_analysis_is_extracted_compiled_and_kept(golf, tmp_path):
    """The page writes the analysis with a model; the server extracts the log and compiles the answer."""
    run = tmp_path / "run"
    shutil.copytree(golf, run)
    task = next((run / "tasks").iterdir()).name
    (run / "tasks" / task / "sessions" / "s001" / "raw").mkdir(parents=True, exist_ok=True)
    (run / "tasks" / task / "sessions" / "s001" / "raw" / "agent.jsonl").write_text(LOG)
    server = start(run, port=0)
    base, header = f"http://127.0.0.1:{server.server_port}/api/campaign/vista", {"X-TraceVista-Campaign": server.token}

    def call(route, session="s001", body=None, **headers):
        request = urllib.request.Request(f"{base}{route}?task={task}&session={session}", method="POST" if body is not None else "GET",
                                         data=None if body is None else json.dumps(body).encode(), headers={**header, **headers})
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read() or b"{}")
    try:
        assert call("")[1]["log"] is True and call("")[1]["ready"] is False
        status, timeline = call("/timeline")
        assert (status, timeline["events"]) == (200, 3) and "I shortened the loop." in timeline["parts"][0]
        status, answer = call("/build", body={"title": "x"})
        assert status == 200 and answer["ok"] is False and "states is missing" in answer["error"]
        status, answer = call("/build", body=ANALYSIS)
        assert status == 200 and answer["ok"] is True and answer["ready"] is True
        status, graph = call("/graph")
        assert status == 200 and graph["format"] == "coding-agent-state-trajectory"
        assert (run / "vista" / task / "s001" / "graph.json").is_file()
        assert call("", **{"X-TraceVista-Campaign": "wrong"})[0] == 403
        assert call("/build", body=ANALYSIS, Origin="http://evil.example")[0] == 403
        assert call("", session="..")[0] == 400 and call("/graph", session="s002")[0] == 404
    finally:
        server.shutdown(); server.server_close()


def test_an_agent_the_reader_picks_writes_the_analysis(golf, tmp_path):
    """--analyst <name>=<command>: any agent, started in its own directory with the skill and check.py."""
    run = tmp_path / "run"
    shutil.copytree(golf, run)
    task = next((run / "tasks").iterdir()).name
    (run / "tasks" / task / "sessions" / "s001" / "raw").mkdir(parents=True, exist_ok=True)
    (run / "tasks" / task / "sessions" / "s001" / "raw" / "agent.jsonl").write_text(LOG)
    agent = tmp_path / "agent.py"   # follows the prompt: writes the analysis, then runs the page's checks
    agent.write_text("import os, subprocess, sys\nassert 'skill/SKILL.md' in sys.stdin.read()\n"
                     "open('state-analysis.json', 'w').write(os.environ['ANALYSIS'])\n"
                     "print(subprocess.run([sys.executable, 'check.py'], capture_output=True, text=True).stdout)\n")
    server = start(run, port=0, analysts=Analysts([f"stand-in={sys.executable} {agent}"], detect=False))
    base, header = f"http://127.0.0.1:{server.server_port}/api/campaign/vista", {"X-TraceVista-Campaign": server.token}

    def call(route, body=None):
        request = urllib.request.Request(f"{base}{route}{'&' if '?' in route else '?'}task={task}&session=s001",
                                         method="POST" if body is not None else "GET",
                                         data=None if body is None else json.dumps(body).encode(), headers=header)
        with urllib.request.urlopen(request) as response:
            return json.loads(response.read())
    os.environ["ANALYSIS"] = json.dumps(ANALYSIS)
    try:
        assert [a["id"] for a in call("/analysts")["analysts"]] == ["stand-in"]
        assert call("/agent?analyst=stand-in", body={"facts": "s001 scored 2."})["state"] == "running"
        for _ in range(200):
            job = call("")["job"]
            if job["state"] != "running":
                break
            time.sleep(0.05)
        assert job["state"] == "done", job
        assert any("OK: it compiles" in line for line in job["activity"])
        assert call("")["ready"] is True
    finally:
        os.environ.pop("ANALYSIS", None)
        server.shutdown(); server.server_close()
