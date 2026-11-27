"""The campaign snapshot and its server, on synthetic campaigns in both layouts."""
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
import unittest.mock
import urllib.error
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from evolve_vista import vista as campaign_vista  # noqa: E402
from evolve_vista.campaign import snapshot, stamp  # noqa: E402
from evolve_vista.server import HEADER, Reader, VistaServer  # noqa: E402


def write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data) if not isinstance(data, str) else data)


def campaign(root: Path) -> Path:
    """An InfiniEvolve campaign: a baseline, two sessions from it, one running, a duplicate, a flagged one."""
    task = root / "tasks" / "golf"
    write(root / "config.yaml", "campaign: {adapter: local, tasks: [golf]}\n")
    write(task / "task.json", {"declaration": {"rank_by": "bytes", "metrics": {
        "bytes": {"direction": "min", "scale": "absolute", "format": "{:.0f} B", "anchor": "baseline",
                  "marks": {"known best": 90}}}}})
    node = lambda sid, index, parent, value, **more: {
        "session_id": sid, "index": index, "parent_id": parent, "start_strategy": "best" if parent else "root",
        "chosen_label": "final", "valid": True, "rank_metric": "bytes", "rank_value": value,
        "scores": [{"label": "final", "checks": {}, "metrics": {"bytes": value, "lines": 7}}],
        "flags": {}, "report": "approach: shorter", "timing": {"agent_seconds": 60}, "usage": {"cost_usd": 0.5}, **more}
    write(task / "sessions" / "s000" / "node.json", node("s000", 0, None, 466))
    write(task / "sessions" / "s001" / "node.json", node("s001", 1, "s000", 286))
    write(task / "sessions" / "s002" / "node.json", node("s002", 2, "s000", 286, flags={"duplicate": {"level": "soft", "note": "same"}}))
    write(task / "sessions" / "s003" / "node.json", node("s003", 3, "s001", 100, flags={"cheated": {"level": "hard", "note": "read answers"}}))
    write(task / "sessions" / "s004" / "input" / "spec.json", {"session_id": "s004", "index": 4, "parent_id": "s001", "start_strategy": "best"})
    write(task / "sessions" / "s004" / "job.json", {"submitted_at": "2026-11-21T10:00:00+00:00"})
    write(root / "events.jsonl", json.dumps({"type": "task_stop", "task": "golf", "data": {"reason": "done"}}) + "\n" +
          json.dumps({"type": "job_submitted", "task": "golf", "data": {}}) + "\n")
    return root


class SnapshotTest(unittest.TestCase):
    def test_a_campaign_in_the_infinievolve_layout(self):
        with tempfile.TemporaryDirectory() as temp:
            data = snapshot(campaign(Path(temp) / "run"))
            self.assertEqual(data["format"], "infinievolve-campaign")
            task = data["tasks"][0]
            self.assertEqual(task["metric"]["name"], "bytes")
            self.assertEqual(task["metric"]["direction"], "min")
            self.assertEqual(task["metric"]["marks"], [{"label": "known best", "value": 90}])
            nodes = {n["id"]: n for n in task["nodes"]}
            self.assertEqual(list(nodes), ["s000", "s001", "s002", "s003", "s004"])
            self.assertEqual(nodes["s001"]["display"], "286 B")
            self.assertEqual(nodes["s001"]["metrics"], {"bytes": 286, "lines": 7})
            self.assertEqual(nodes["s001"]["texts"], {"bytes": "286 B"}, "an undeclared metric has no text")
            self.assertTrue(nodes["s002"]["duplicate"])
            self.assertFalse(nodes["s002"]["excluded"])
            self.assertTrue(nodes["s003"]["excluded"])
            self.assertEqual(nodes["s004"]["status"], "running")
            self.assertEqual(nodes["s004"]["parent"], "s001")
            self.assertIsNone(task["stopped"], "a stop followed by a new submission is no longer current")

    def test_a_campaign_in_the_older_layout(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "old"
            write(root / "config.yaml", "campaign: {}\n")
            write(root / "tasks" / "t" / "task.json", {"metric": "paired"})
            write(root / "tasks" / "t" / "sessions" / "s000" / "node.json", {
                "session_id": "s000", "index": 0, "parent_id": None, "correct": True, "primary_speedup": 0.5,
                "chosen_label": "baseline", "scores": [{"label": "baseline", "official_speedup": 1.0, "paired_speedup": 0.5}]})
            write(root / "tasks" / "t" / "sessions" / "s001" / "node.json", {
                "session_id": "s001", "index": 1, "parent_id": "s000", "correct": True, "primary_speedup": 2.0,
                "flags": ["duplicate_code", "unstable_measurement"], "flag_notes": {"unstable_measurement": "noisy"}, "scores": []})
            task = snapshot(root)["tasks"][0]
            self.assertEqual(task["metric"]["name"], "paired")
            s0, s1 = task["nodes"]
            self.assertEqual(s0["metrics"], {"official_speedup": 1.0, "paired_speedup": 0.5})
            self.assertEqual({f["name"]: f["level"] for f in s1["flags"]}, {"duplicate_code": "soft", "unstable_measurement": "hard"})
            self.assertTrue(s1["excluded"])

    def test_the_stamp_follows_changes_and_only_changes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = campaign(Path(temp) / "run")
            before = stamp(root)
            self.assertEqual(stamp(root), before)
            write(root / "tasks" / "golf" / "sessions" / "s004" / "node.json", {"session_id": "s004", "index": 4, "parent_id": "s001"})
            self.assertNotEqual(stamp(root), before)

    def test_a_directory_that_is_not_a_campaign_is_refused(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(ValueError):
                snapshot(Path(temp))


class ServerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        write(root / "viewer" / "campaign.html", "<html>viewer</html>")
        write(root / "secret.txt", "outside the viewer")
        self.campaign = campaign(root / "run")
        self.server = VistaServer(self.campaign, 0, root / "viewer", token="t0ken", reader=Reader(snapshot, stamp))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.temp.cleanup()

    def get(self, path, **headers):
        request = urllib.request.Request(self.base + path, headers=headers)
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, dict(response.headers), response.read()
        except urllib.error.HTTPError as error:
            return error.code, dict(error.headers), error.read()

    def test_the_page_and_the_snapshot(self):
        status, _, body = self.get("/")
        self.assertEqual((status, body), (200, b"<html>viewer</html>"))
        status, headers, body = self.get("/api/campaign", **{HEADER: "t0ken"})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["tasks"][0]["name"], "golf")
        status, _, _ = self.get("/api/campaign", **{HEADER: "t0ken", "If-None-Match": headers["ETag"]})
        self.assertEqual(status, 304, "nothing changed, nothing sent")
        write(self.campaign / "tasks" / "golf" / "sessions" / "s005" / "job.json", {})
        status, _, _ = self.get("/api/campaign", **{HEADER: "t0ken", "If-None-Match": headers["ETag"]})
        self.assertEqual(status, 200, "a new session is a change")

    def test_the_stream_pushes_a_snapshot_now_and_on_change_only(self):
        import http.client
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=10)
        connection.request("GET", "/api/campaign/events?token=t0ken")
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader("Content-Type"), "text/event-stream")

        def next_snapshot():
            lines = []
            while True:
                line = response.fp.readline().decode().rstrip("\n")
                if line == "" and any(l.startswith("data: ") for l in lines):
                    return json.loads(next(l for l in lines if l.startswith("data: "))[6:])
                if line:
                    lines.append(line)

        first = next_snapshot()
        self.assertEqual([n["id"] for n in first["tasks"][0]["nodes"]][-1], "s004")
        write(self.campaign / "tasks" / "golf" / "sessions" / "s005" / "input" / "spec.json",
              {"session_id": "s005", "index": 5, "parent_id": "s001"})
        write(self.campaign / "tasks" / "golf" / "sessions" / "s005" / "job.json", {})
        second = next_snapshot()
        self.assertEqual(second["tasks"][0]["nodes"][-1]["id"], "s005")
        self.assertNotEqual(first["stamp"], second["stamp"])
        connection.close()
        self.assertEqual(self.get("/api/campaign/events?token=wrong")[0], 403)

    def test_each_session_has_its_time_limit_validation_time_and_phase(self):
        sessions = self.campaign / "tasks" / "golf" / "sessions"
        write(sessions / "s001" / "input" / "spec.json", {"session_id": "s001", "time_limit_seconds": 600})
        node = json.loads((sessions / "s001" / "node.json").read_text())
        write(sessions / "s001" / "node.json", {**node, "timing": {"agent_seconds": 300, "evaluate_seconds": 42}})
        write(sessions / "s004" / "input" / "spec.json", {"session_id": "s004", "index": 4, "parent_id": "s001", "time_limit_seconds": 900})
        status, headers, body = self.get("/api/campaign", **{HEADER: "t0ken"})
        nodes = {n["id"]: n for n in json.loads(body)["tasks"][0]["nodes"]}
        self.assertEqual((nodes["s001"]["timeLimitSeconds"], nodes["s001"]["checkSeconds"], nodes["s001"]["phase"]), (600, 42, None))
        self.assertEqual((nodes["s004"]["timeLimitSeconds"], nodes["s004"]["phase"]), (900, "develop"))
        write(sessions / "s004" / "raw" / "final.patch", "diff")   # the agent ended; its work is evaluated
        status, _, body = self.get("/api/campaign", **{HEADER: "t0ken", "If-None-Match": headers["ETag"]})
        self.assertEqual(status, 200, "a session moving to validation is a change")
        s004 = json.loads(body)["tasks"][0]["nodes"][-1]
        self.assertEqual(s004["phase"], "check")
        self.assertTrue(s004["developedAt"])

    def test_what_is_refused(self):
        self.assertEqual(self.get("/api/campaign")[0], 403, "no token")
        self.assertEqual(self.get("/api/campaign", **{HEADER: "wrong"})[0], 403)
        self.assertEqual(self.get("/api/campaign", **{HEADER: "t0ken", "Origin": "http://evil.example"})[0], 403)
        self.assertEqual(self.get("/../secret.txt")[0], 404, "nothing outside the viewer is served")
        self.assertEqual(self.get("/%2e%2e/secret.txt")[0], 404)


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


class VistaTest(ServerTest):
    """A session's TraceVista analysis: the timeline for the model, and compiling what it wrote."""

    def setUp(self):
        super().setUp()
        write(self.campaign / "tasks" / "golf" / "sessions" / "s001" / "raw" / "agent.jsonl", LOG)

    def post(self, path, data, **headers):
        body = data if isinstance(data, bytes) else json.dumps(data).encode()
        request = urllib.request.Request(self.base + path, data=body, method="POST",
                                         headers={"Content-Type": "application/json", **headers})
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read() or b"{}")

    def vista(self, route="", session="s001"):
        status, _, body = self.get(f"/api/campaign/vista{route}?task=golf&session={session}", **{HEADER: "t0ken"})
        return status, json.loads(body)

    def test_a_session_is_extracted_compiled_and_kept(self):
        self.assertEqual(self.vista()[1]["log"], True)
        self.assertEqual(self.vista()[1]["ready"], False)
        self.assertEqual(self.vista(session="s000")[1]["log"], False, "the baseline ran no agent")
        status, timeline = self.vista("/timeline")
        self.assertEqual((status, timeline["events"], timeline["first"], timeline["last"]), (200, 3, "E1", "E3"))
        self.assertIn("I shortened the loop.", timeline["parts"][0])
        status, answer = self.post("/api/campaign/vista/build?task=golf&session=s001", {"title": "x"}, **{HEADER: "t0ken"})
        self.assertEqual((status, answer["ok"]), (200, False), "a rejection is an answer the page sends back to the model")
        self.assertIn("states is missing", answer["error"])
        broken = json.loads(json.dumps(ANALYSIS))
        broken["actions"][0]["state"] = "nowhere"
        key = next(iter(broken["states"][0]["snapshot"]))
        for state in broken["states"]:
            state["snapshot"][key] = 1
        status, answer = self.post("/api/campaign/vista/build?task=golf&session=s001", broken, **{HEADER: "t0ken"})
        self.assertIn("branch/state mismatch", answer["error"], "every problem in one answer, not the compiler's first")
        self.assertIn("snapshot values must be text or null", answer["error"], "the viewer's rule, which the compiler does not check")
        self.assertNotIn(str(self.campaign), answer["error"], "no local paths in what the page sees")
        status, answer = self.post("/api/campaign/vista/build?task=golf&session=s001", ANALYSIS, **{HEADER: "t0ken"})
        self.assertEqual((status, answer["ok"], answer["ready"]), (200, True, True))
        status, graph = self.vista("/graph")
        self.assertEqual((status, graph["format"]), (200, "coding-agent-state-trajectory"))
        self.assertTrue((self.campaign / "vista" / "golf" / "s001" / "graph.json").is_file())

    def test_a_session_of_an_older_local_run_keeps_its_log_as_agent_log(self):
        write(self.campaign / "tasks" / "golf" / "sessions" / "s002" / "raw" / "agent.log", LOG)
        self.assertEqual(self.vista(session="s002")[1]["log"], True)
        status, timeline = self.vista("/timeline", session="s002")
        self.assertEqual((status, timeline["events"]), (200, 3))

    def test_what_the_analysis_endpoints_refuse(self):
        self.assertEqual(self.get("/api/campaign/vista?task=golf&session=s001")[0], 403, "no token")
        self.assertEqual(self.get("/api/campaign/vista?task=..&session=s001", **{HEADER: "t0ken"})[0], 400)
        self.assertEqual(self.get("/api/campaign/vista?task=golf&session=s999", **{HEADER: "t0ken"})[0], 404)
        self.assertEqual(self.vista("/graph")[0], 404, "not analysed yet")
        self.assertEqual(self.vista("/timeline", session="s000")[0], 404, "no agent log")
        self.assertEqual(self.post("/api/campaign/vista/build?task=golf&session=s001", ANALYSIS,
                                   **{HEADER: "t0ken", "Origin": "http://evil.example"})[0], 403)
        self.assertEqual(self.post("/api/campaign/vista/build?task=golf&session=s001", b"not json", **{HEADER: "t0ken"})[1]["ok"], False)

    # the page and snapshot tests are inherited unchanged; skip their second run
    test_the_page_and_the_snapshot = test_the_stream_pushes_a_snapshot_now_and_on_change_only = test_what_is_refused = None


# A stand-in agent: reads its prompt on standard input, writes the analysis given in its environment and
# runs check.py as the prompt asks, or (SLEEP) waits to be stopped.
FAKE_AGENT = r'''
import json, os, subprocess, sys, time
prompt = sys.stdin.read()
assert "skill/SKILL.md" in prompt and os.path.isfile("skill/SKILL.md") and os.path.isfile("extracted/timeline.json")
print(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "Reading the timeline"}]}}), flush=True)
if os.environ.get("SLEEP"):
    time.sleep(60)
open("state-analysis.json", "w").write(os.environ["ANALYSIS"])
check = subprocess.run([sys.executable, "check.py"], capture_output=True, text=True)
print(check.stdout, flush=True)
'''


class AgentTest(VistaTest):
    """An agent the reader picks writes the analysis in a directory of its own; the server compiles it."""

    def setUp(self):
        super().setUp()
        self.vista_module = campaign_vista
        agent = Path(self.temp.name) / "fake_agent.py"
        agent.write_text(FAKE_AGENT)
        self.server.analysts = campaign_vista.Analysts([f"fake=ANALYSIS='{json.dumps(ANALYSIS)}' {sys.executable} {agent}",
                                                        f"sleepy=SLEEP=1 {sys.executable} {agent}"], detect=False)

    def wait(self, state="running"):
        for _ in range(200):
            job = self.vista()[1]["job"]
            if job and job["state"] != state:
                return job
            threading.Event().wait(0.05)
        self.fail("the agent did not finish")

    def test_a_picked_agent_writes_the_analysis_and_the_server_compiles_it(self):
        status, listing = self.vista("/analysts")
        self.assertEqual([a["id"] for a in listing["analysts"]], ["fake", "sleepy"])
        status, job = self.post("/api/campaign/vista/agent?task=golf&session=s001&analyst=fake", {"facts": "Scored 286 B."}, **{HEADER: "t0ken"})
        self.assertEqual((status, job["state"], job["label"]), (200, "running", "fake"))
        job = self.wait()
        self.assertEqual(job["state"], "done", job)
        self.assertIn("OK: it compiles", " ".join(job["activity"]), "the agent ran the page's own checks")
        self.assertTrue(self.vista()[1]["ready"])
        work = self.campaign / "vista" / "golf" / "s001" / "agent"
        self.assertIn("Scored 286 B.", (work / "PROMPT.md").read_text())
        self.assertFalse((work / "skill" / "tests").exists())

    def test_an_agent_can_be_stopped_and_only_offered_agents_start(self):
        self.assertEqual(self.post("/api/campaign/vista/agent?task=golf&session=s001&analyst=rm", {}, **{HEADER: "t0ken"})[0], 400)
        self.assertEqual(self.post("/api/campaign/vista/agent?task=golf&session=s001&analyst=sleepy", {}, **{HEADER: "t0ken"})[0], 200)
        self.assertEqual(self.post("/api/campaign/vista/agent?task=golf&session=s001&analyst=fake", {}, **{HEADER: "t0ken"})[0], 409,
                         "one agent per session")
        status, job = self.post("/api/campaign/vista/agent/stop?task=golf&session=s001", {}, **{HEADER: "t0ken"})
        self.assertEqual((status, job["state"]), (200, "stopped"))
        self.assertFalse(self.vista()[1]["ready"])

    def test_agent_settings(self):
        command, shell, label = self.vista_module.agent_command("claude/claude-opus-5-5:high")
        self.assertEqual((command[:6], shell, label), (["claude", "-p", "--model", "claude-opus-5-5", "--effort", "high"], False,
                                                       "Claude Code · claude-opus-5-5 · high"))
        self.assertNotIn("--dangerously-skip-permissions", command)
        command, _, _ = self.vista_module.agent_command("codex")
        self.assertEqual(command[command.index("--sandbox") + 1], "workspace-write")
        self.assertEqual(self.vista_module.agent_command("gemini=gemini --yolo")[1:], (True, "gemini"))
        with self.assertRaises(ValueError):
            self.vista_module.agent_command("claude/x; rm -rf ~")

    def test_installed_agents_are_found_and_started_as_their_documentation_says(self):
        """A `gemini` on the PATH is offered, marked untested, and given the prompt's file instead of stdin."""
        bin_dir = Path(self.temp.name) / "bin"
        bin_dir.mkdir()
        fake = bin_dir / "gemini"
        fake.write_text(f"#!{sys.executable}\n"
                        "import json, os, subprocess, sys\n"
                        "assert sys.argv[-2:] == ['-p', 'Read PROMPT.md in your working directory and follow it exactly.'], sys.argv\n"
                        "assert '--yolo' in sys.argv and sys.stdin.read() == ''\n"
                        "assert 'check.py' in open('PROMPT.md').read()\n"
                        "print(json.dumps({'type': 'tool_use', 'tool_name': 'run_shell_command', 'parameters': {'command': 'python3 check.py'}}))\n"
                        "open('state-analysis.json', 'w').write(os.environ['ANALYSIS'])\n"
                        "print(subprocess.run([sys.executable, 'check.py'], capture_output=True, text=True).stdout)\n")
        fake.chmod(0o755)
        saved = os.environ["PATH"], os.environ.get("ANALYSIS")
        os.environ["PATH"], os.environ["ANALYSIS"] = str(bin_dir), json.dumps(ANALYSIS)
        try:
            self.server.analysts = self.vista_module.Analysts()
            listing = self.vista("/analysts")[1]["analysts"]
            self.assertEqual([(a["id"], a["label"], a["untested"]) for a in listing], [("gemini", "Gemini CLI", True)])
            self.assertEqual(self.post("/api/campaign/vista/agent?task=golf&session=s001&analyst=gemini", {}, **{HEADER: "t0ken"})[0], 200)
            job = self.wait()
        finally:
            os.environ["PATH"] = saved[0]
            if saved[1] is None:
                os.environ.pop("ANALYSIS", None)
        self.assertEqual(job["state"], "done", job)
        self.assertIn("run_shell_command: python3 check.py", job["activity"])

    def test_every_documented_agent_has_a_command(self):
        for program in self.vista_module.PRESETS:
            if program in ("amp", "goose"):   # these take their model from their own settings
                with self.assertRaises(ValueError):
                    self.vista_module.agent_launch(f"{program}/some-model")
                continue
            launch = self.vista_module.agent_launch(f"{program}/some-model")
            self.assertEqual(launch["command"][0], self.vista_module.PRESETS[program][0])
            self.assertIn("some-model", launch["command"])
        with self.assertRaises(ValueError):
            self.vista_module.agent_launch("gemini/pro:high")   # effort is Claude Code's and Codex's
        self.assertEqual(self.vista_module.agent_launch("goose")["env"], {"GOOSE_MODE": "auto"})

    def test_lineage_ranges_open_and_bad_lineage_is_listed(self):
        """An agent's lineage with an event range compiles into a graph the viewer opens (s004's case)."""
        analysis = json.loads(json.dumps(ANALYSIS))
        order = ["E1", "E2", "E3"]
        analysis["states"][0]["lineage"] = {"parent": "nowhere", "relation": "restored", "reason": "r", "events": ["E1-E2"]}
        self.assertIn("s1: lineage.parent must name another state", self.vista_module.problems(analysis, order))
        analysis["states"][0]["lineage"]["parent"] = "s1"
        self.assertIn("s1: lineage.parent must name another state", self.vista_module.problems(analysis, order), "not itself")

    def test_the_reader_sets_model_and_effort_where_the_agent_takes_them(self):
        analysts = self.vista_module.Analysts(["claude/sonnet", "amp", "own=run-it"], detect=False)
        listing = {a["id"]: a for a in analysts.listing()}
        self.assertEqual((listing["claude/sonnet"]["takesModel"], listing["claude/sonnet"]["model"]), (True, "sonnet"))
        self.assertIn("max", listing["claude/sonnet"]["efforts"])
        self.assertFalse(listing["amp"]["takesModel"])
        self.assertNotIn("takesModel", listing["own"])
        self.assertEqual(analysts.spec_for("claude/sonnet", "", ""), "claude/sonnet")
        self.assertEqual(analysts.spec_for("claude/sonnet", "opus", "high"), "claude/opus:high")
        self.assertEqual(analysts.spec_for("claude/sonnet", "", "max"), "claude/sonnet:max")
        for bad in (("claude/sonnet", "", "turbo"), ("claude/sonnet", "x; rm", ""), ("amp", "gpt", ""), ("own", "m", "")):
            with self.assertRaises(self.vista_module.VistaError):
                analysts.spec_for(*bad)

    def test_the_agent_that_started_the_server_is_offered_first(self):
        vista = self.vista_module
        self.assertEqual(vista.launching_agent({}, ["zsh", "codex", "claude", "login"]), "codex", "the nearest")
        self.assertEqual(vista.launching_agent({"GEMINI_CLI": "1"}, ["zsh", "tmux"]), "gemini")
        self.assertEqual(vista.launching_agent({"CLAUDECODE": "1"}, ["cursor-agent"]), "cursor", "processes first")
        self.assertIsNone(vista.launching_agent({"TERM": "x"}, ["zsh", "sshd"]))
        self.assertIsInstance(vista.ancestor_names(), list)
        with tempfile.TemporaryDirectory() as bin_dir:
            for name in ("claude", "codex", "gemini"):
                Path(bin_dir, name).write_text("#!/bin/sh\n")
                Path(bin_dir, name).chmod(0o755)
            saved = os.environ["PATH"]
            os.environ["PATH"] = bin_dir
            try:
                order = lambda *a, **k: [c["id"] for c in vista.Analysts(*a, **k).listing()]
                self.assertEqual(order(launcher="codex"), ["codex", "claude", "gemini"])
                self.assertEqual(order(launcher=""), ["claude", "codex", "gemini"], "none found: as listed")
                self.assertEqual(order(["claude/opus", "codex"], launcher="gemini"), ["claude/opus", "codex", "gemini", "claude"])
            finally:
                os.environ["PATH"] = saved

    test_a_session_is_extracted_compiled_and_kept = test_what_the_analysis_endpoints_refuse = None


# A running session's agent log, Claude Code stream-json, as the live view reads it.
LIVE = [
    {"type": "system", "subtype": "init", "model": "claude-x", "cwd": "/work"},
    {"type": "system", "subtype": "thinking_tokens", "estimated_tokens": 12},
    {"type": "assistant", "message": {"content": [{"type": "thinking", "thinking": "Try a shorter loop."}]}},
    {"type": "assistant", "message": {"content": [{"type": "text", "text": "Measuring first."},
        {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "python3 bench.py", "description": "Time it"}}]}},
    {"type": "tool_progress", "tool_name": "Bash", "elapsed_time_seconds": 3},
    {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "is_error": True,
        "content": "Traceback: ANTHROPIC_API_KEY=sk-ant-abcdefghijklmnopqrstuvwx" + "x" * 5000}]}},
    {"type": "assistant", "parent_tool_use_id": "t2", "message": {"content": [
        {"type": "tool_use", "id": "t3", "name": "Read", "input": {"file_path": "solution.py"}}]}},
]


class LiveLogTest(ServerTest):
    """A session still running: its agent log, redacted and shortened, streamed as it grows."""

    def setUp(self):
        super().setUp()
        self.log = self.campaign / "tasks" / "golf" / "sessions" / "s004" / "raw" / "agent.jsonl"

    def append(self, *events, partial=""):
        self.log.parent.mkdir(parents=True, exist_ok=True)
        with open(self.log, "a") as f:
            f.write("".join(json.dumps(e) + "\n" for e in events) + partial)

    def open(self, session="s004", last=None):
        import http.client
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=10)
        connection.request("GET", f"/api/campaign/agent-log?token=t0ken&task=golf&session={session}",
                           headers={"Last-Event-ID": last} if last else {})
        response = connection.getresponse()
        self.addCleanup(connection.close)
        return response

    @staticmethod
    def batch(response):
        """(id, entries) of the next batch."""
        lines = []
        while True:
            line = response.fp.readline().decode().rstrip("\n")
            if line == "" and any(l.startswith("data: ") for l in lines):
                given = next((l[4:] for l in lines if l.startswith("id: ")), None)
                return given, json.loads(next(l for l in lines if l.startswith("data: "))[6:])["entries"]
            if line and not line.startswith(":"):
                lines.append(line)

    def test_what_the_agent_does_is_read_shortened_and_redacted(self):
        from evolve_vista import livelog
        self.append(*LIVE)
        found, offset = livelog.read(self.log, None, (str(self.campaign),))
        self.assertEqual(offset, self.log.stat().st_size)
        self.assertEqual([e["kind"] for e in found], ["system", "thinking", "text", "tool", "error", "tool"],
                         "token counts and progress heartbeats are left out")
        self.assertEqual(found[0]["text"], "Session started · claude-x · /work")
        self.assertEqual((found[3]["tool"], found[3]["text"]), ("Bash", "python3 bench.py   # Time it"))
        self.assertNotIn("sk-ant-abcdefghijklmnopqrstuvwx", found[4]["text"], "redacted as the timeline is")
        self.assertIn("[REDACTED", found[4]["text"])
        self.assertLess(len(found[4]["text"]), 1600, "a long result is cut")
        self.assertIn("more characters]", found[4]["text"])
        self.assertTrue(found[5].get("sub"), "a subagent's step is marked")
        self.assertNotIn("sub", found[3])
        with unittest.mock.patch.dict("os.environ", {"MY_SERVICE_TOKEN": "s3cr3t-value-of-this-server"}):
            self.append({"type": "assistant", "message": {"content": [{"type": "text", "text": "env: s3cr3t-value-of-this-server"}]}})
            found, _ = livelog.read(self.log, offset, ())
        self.assertEqual(found[0]["text"], "env: [REDACTED CREDENTIAL]", "the server's own credentials never appear")

    def test_a_long_log_opens_near_its_end_and_a_long_line_is_skipped(self):
        from evolve_vista import livelog
        self.append(*[{"type": "assistant", "message": {"content": [{"type": "text", "text": f"step {i}"}]}} for i in range(50)])
        with unittest.mock.patch.object(livelog, "TAIL_ENTRIES", 5):
            found, _ = livelog.read(self.log, None)
        self.assertEqual([e["text"] for e in found], [f"step {i}" for i in range(45, 50)])
        with unittest.mock.patch.object(livelog, "TAIL_BYTES", 200):
            found, _ = livelog.read(self.log, None)
        self.assertTrue(found and found[-1]["text"] == "step 49" and all(e["text"].startswith("step ") for e in found),
                        "a cut first line is dropped, not shown as garbage")
        offset = self.log.stat().st_size
        with unittest.mock.patch.object(livelog, "CHUNK_BYTES", 128):
            self.append({"type": "assistant", "message": {"content": [{"type": "text", "text": "x" * 500}]}},
                        {"type": "assistant", "message": {"content": [{"type": "text", "text": "after"}]}})
            found, offset = livelog.read(self.log, offset)
            self.assertEqual(found[0]["kind"], "system")
            self.assertIn("is not shown", found[0]["text"])
            found, offset = livelog.read(self.log, offset)
        self.assertEqual([e["text"] for e in found], ["after"])

    def test_the_stream_sends_new_entries_only_and_carries_on_after_a_reconnect(self):
        response = self.open()
        self.assertEqual((response.status, response.getheader("Content-Type")), (200, "text/event-stream"))
        self.assertEqual(self.batch(response), (None, []), "connected; the agent has not started yet")
        self.append(*LIVE[:4], partial='{"type": "assistant", "mess')
        first, found = self.batch(response)
        self.assertEqual([e["kind"] for e in found], ["system", "thinking", "text", "tool"], "the line being written waits")
        with open(self.log, "a") as f:
            f.write('age": {"content": [{"type": "text", "text": "Done measuring."}]}}\n')
        second, found = self.batch(response)
        self.assertEqual(found, [{"kind": "text", "text": "Done measuring."}])
        self.assertEqual(int(second), self.log.stat().st_size)
        response.close()
        self.append({"type": "result", "subtype": "success", "num_turns": 3, "total_cost_usd": 0.25, "result": "Shorter."})
        again = self.open(last=second)
        _, found = self.batch(again)
        self.assertEqual(found, [{"kind": "done", "text": "Finished · 3 turns · $0.25\nShorter."}], "nothing sent twice")

    def test_what_the_live_log_refuses(self):
        self.assertEqual(self.get("/api/campaign/agent-log?task=golf&session=s004")[0], 403, "no token")
        self.assertEqual(self.get("/api/campaign/agent-log?token=wrong&task=golf&session=s004")[0], 403)
        self.assertEqual(self.get("/api/campaign/agent-log?task=golf&session=s004", **{HEADER: "t0ken"})[0], 403,
                         "an EventSource carries its token in the address")
        self.assertEqual(self.get("/api/campaign/agent-log?token=t0ken&task=golf&session=s999")[0], 404)
        self.assertEqual(self.get("/api/campaign/agent-log?token=t0ken&task=..&session=s004")[0], 400)
        self.assertEqual(self.get("/api/campaign/agent-log?token=t0ken&task=golf&session=s004",
                                  Origin="http://evil.example")[0], 403)

    test_the_page_and_the_snapshot = test_the_stream_pushes_a_snapshot_now_and_on_change_only = test_what_is_refused = None


def gen(n: int, **more) -> dict:
    """An assistant line of a Claude Code log whose model call went through OpenRouter."""
    return {"type": "assistant", "request_id": f"gen-{n}", "timestamp": f"2026-11-26T22:00:{n:02d}.000Z",
            "message": {"content": [{"type": "text", "text": f"step {n}"}], "usage": {"output_tokens": 0}}, **more}


class FakeOpenRouter:
    """OpenRouter's generation endpoint: numbers for a call after `ready` lookups of it; or a refusal."""

    def __init__(self, ready=1, refuse=None, busy_once=False):
        self.ready, self.refuse, self.busy_once = ready, refuse, busy_once
        self.asked, self.keys = [], set()
        self.lock = threading.Lock()

    def __call__(self, generation, key):
        from evolve_vista import apistats
        with self.lock:
            self.asked.append(generation)
            self.keys.add(key)
            if self.refuse:
                raise apistats.Refused()
            if self.busy_once:
                self.busy_once = False
                raise apistats.Busy(0.2)
            if self.asked.count(generation) < self.ready:
                return None   # OpenRouter has no numbers for a call this fresh
        n = int(generation.split("-")[1])
        return {"id": generation, "latency": 500 + n, "generation_time": 2000, "native_tokens_completion": 100 * n,
                "created_at": "2026-11-26T22:00:00Z", "finish_reason": "tool_calls", "usage": n / 1000,
                "provider_responses": [{"provider_name": "Old"}, {"provider_name": "Inkling"}]}


class ApiStatsTest(ServerTest):
    """How fast the agent's model answered each call, looked up at OpenRouter by the server, never by the page."""

    def setUp(self):
        super().setUp()
        from evolve_vista import apistats
        self.apistats = apistats
        self.log = self.campaign / "tasks" / "golf" / "sessions" / "s004" / "raw" / "agent.jsonl"
        self.log.parent.mkdir(parents=True)
        patch = unittest.mock.patch.object(apistats, "RETRIES", (0.05, 0.05, 0.05))
        patch.start(); self.addCleanup(patch.stop)

    def use(self, fake, key="or-key-123"):
        self.server.api_stats = self.apistats.ApiStats(key=key, fetch=fake)

    def append(self, *events, partial=""):
        with open(self.log, "a") as f:
            f.write("".join(json.dumps(e) + "\n" for e in events) + partial)

    def settle(self, calls, until):
        for _ in range(200):
            calls.advance()
            view = calls.view()
            if until(view):
                return view
            threading.Event().wait(0.02)
        self.fail(f"never settled: {calls.view()}")

    def test_calls_are_found_once_each_with_their_time(self):
        lines = [gen(1), gen(1), gen(2, parent_tool_use_id="t9"), {"type": "user", "request_id": "gen-9"},
                 {"type": "system", "subtype": "thinking_tokens"}]
        found = self.apistats.calls(("\n".join(json.dumps(l) for l in lines) + "\nnot json").encode())
        self.assertEqual(found, [("gen-1", "2026-11-26T22:00:01.000Z", False), ("gen-2", "2026-11-26T22:00:02.000Z", True)],
                         "one call per request id, assistant lines only, a subagent's marked")

    def test_a_point_is_ttft_and_tokens_over_generation_time(self):
        p = self.apistats.point("gen-3", FakeOpenRouter()("gen-3", "k"), "2026-11-26T22:00:03.000Z", False)
        self.assertEqual(p, {"id": "gen-3", "time": "2026-11-26T22:00:03.000Z", "ttft": 503, "gen": 2000, "tokens": 300,
                             "tps": 150.0, "provider": "Inkling", "finish": "tool_calls", "cost": 0.003, "sub": False})
        self.assertIsNone(self.apistats.point("gen-3", {"id": "gen-3"}, None, False), "no timings, no point")
        self.assertEqual(self.apistats.point("gen-3", {"latency": 1, "total_cost": 0.5}, None, False)["cost"], 0.5)

    def test_lookups_wait_for_the_numbers_back_off_and_are_kept(self):
        fake = FakeOpenRouter(ready=3, busy_once=True)
        self.use(fake)
        self.append(gen(1), gen(2), partial='{"type": "assistant", "request_id": "gen-3"')
        calls = self.server.api_stats.session(self.campaign, "golf", "s004")
        view = self.settle(calls, lambda v: len(v["points"]) == 2)
        self.assertEqual([p["id"] for p in view["points"]], ["gen-1", "gen-2"], "the line being written waits")
        self.assertEqual((view["state"], view["calls"], view["waiting"]), ("ok", 2, 0))
        self.assertEqual(fake.asked.count("gen-1"), 3, "asked again until OpenRouter had the numbers, once more after a 429")
        self.assertEqual(fake.keys, {"or-key-123"})
        kept = self.campaign / "vista" / "golf" / "s004" / "api-stats.json"
        self.assertEqual(len(json.loads(kept.read_text())["points"]), 2)
        self.assertNotIn("or-key-123", kept.read_text())
        # a restarted server reads what was found and asks only about calls it does not know
        again = FakeOpenRouter()
        self.use(again)
        with open(self.log, "a") as f:
            f.write(',"timestamp": "2026-11-26T22:00:03.000Z"}\n')
        calls = self.server.api_stats.session(self.campaign, "golf", "s004")
        view = self.settle(calls, lambda v: len(v["points"]) == 3)
        self.assertEqual(again.asked, ["gen-3"])

    def test_a_call_openrouter_never_has_is_given_up(self):
        self.use(FakeOpenRouter(ready=99))
        self.append(gen(1))
        calls = self.server.api_stats.session(self.campaign, "golf", "s004")
        view = self.settle(calls, lambda v: v["missing"] == 1)
        self.assertEqual((view["points"], view["waiting"]), ([], 0))

    def test_without_openrouter_there_are_no_lookups(self):
        fake = FakeOpenRouter()
        self.use(fake, key="")
        self.append(gen(1))
        self.assertEqual(self.settle(self.server.api_stats.session(self.campaign, "golf", "s004"), lambda v: True)["state"], "no-key")
        self.use(fake)
        other = self.campaign / "tasks" / "golf" / "sessions" / "s003" / "raw" / "agent.jsonl"
        other.parent.mkdir(parents=True)
        other.write_text(json.dumps({**gen(1), "request_id": "msg_011CXyz"}) + "\n")
        calls = self.server.api_stats.session(self.campaign, "golf", "s003")
        self.assertEqual(self.settle(calls, lambda v: v["calls"] == 1)["state"], "not-openrouter")
        self.use(FakeOpenRouter(refuse=True))
        calls = self.server.api_stats.session(self.campaign, "golf", "s004")
        self.assertEqual(self.settle(calls, lambda v: v["state"] == "refused")["points"], [])
        self.assertEqual(fake.asked, [], "no lookup without a key or for another provider's ids")

    def test_the_stream_replays_the_points_then_sends_each_new_one_once(self):
        import http.client
        self.use(FakeOpenRouter())
        self.append(gen(1), gen(2))
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=10)
        connection.request("GET", "/api/campaign/agent-api-stats?token=t0ken&task=golf&session=s004")
        response = connection.getresponse()
        self.addCleanup(connection.close)
        self.assertEqual((response.status, response.getheader("Content-Type")), (200, "text/event-stream"))

        def stats():
            lines = []
            while True:
                line = response.fp.readline().decode().rstrip("\n")
                if line == "" and any(l.startswith("data: ") for l in lines):
                    return json.loads(next(l for l in lines if l.startswith("data: "))[6:])
                if line and not line.startswith(":"):
                    lines.append(line)
        seen = []
        while len(seen) < 2:
            seen += [p["id"] for p in stats()["points"]]
        self.assertEqual(seen, ["gen-1", "gen-2"])
        self.append(gen(3))
        while len(seen) < 3:
            batch = stats()
            seen += [p["id"] for p in batch["points"]]
        self.assertEqual(seen, ["gen-1", "gen-2", "gen-3"], "nothing sent twice")
        self.assertEqual((batch["state"], batch["calls"]), ("ok", 3))
        self.assertNotIn("or-key", json.dumps(batch))

    def test_a_session_costs_what_openrouter_charged_for_its_calls(self):
        self.use(FakeOpenRouter(ready=2))
        self.append(gen(1), gen(2), gen(3))
        calls = self.server.api_stats.session(self.campaign, "golf", "s004")
        calls.advance()
        early = calls.summary()
        self.assertEqual((early["calls"], early["priced"], early["cost"]), (3, 0, 0), "nothing priced before OpenRouter answers")
        self.settle(calls, lambda v: v["priced"] == 3)
        self.assertEqual(calls.summary(), {"state": "ok", "calls": 3, "waiting": 0, "cost": 0.006, "priced": 3})

    def test_numbers_kept_before_costs_were_recorded_are_asked_about_once_more(self):
        kept = self.campaign / "vista" / "golf" / "s004" / "api-stats.json"
        write(kept, {"points": [{"id": f"gen-{n}", "time": None, "ttft": 1, "gen": 2, "tokens": 3, "tps": 1500.0,
                                 "provider": "Old", "finish": "stop", "sub": False} for n in (1, 2)]})
        fake = FakeOpenRouter()
        self.use(fake)
        self.append(gen(1), gen(2))
        calls = self.server.api_stats.session(self.campaign, "golf", "s004")
        calls.advance()
        self.assertEqual(len(calls.view()["points"]), 2, "the kept timings show at once")
        view = self.settle(calls, lambda v: v["priced"] == 2)
        self.assertEqual((view["cost"], sorted(fake.asked)), (0.003, ["gen-1", "gen-2"]))
        self.assertTrue(all("cost" in p for p in json.loads(kept.read_text())["points"]))
        calls.advance()
        self.assertEqual(len(fake.asked), 2, "asked once, not again")

    def test_the_campaign_total_counts_every_session_that_ran_an_agent(self):
        import http.client
        self.use(FakeOpenRouter())
        self.append(gen(1), gen(2))
        other = self.campaign / "tasks" / "golf" / "sessions" / "s003" / "raw" / "agent.jsonl"
        other.parent.mkdir(parents=True)
        other.write_text(json.dumps(gen(4)) + "\n")
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=10)
        connection.request("GET", "/api/campaign/api-costs?token=t0ken")
        response = connection.getresponse()
        self.addCleanup(connection.close)
        self.assertEqual(response.status, 200)
        while True:
            line = response.fp.readline().decode()
            if line.startswith("data: "):
                costs = json.loads(line[6:])
                if costs["complete"] and len(costs["sessions"]) == 2 and costs["cost"]:
                    break
        self.assertEqual(sorted(costs["sessions"]), ["golf/s003", "golf/s004"], "the baseline ran no agent")
        self.assertEqual((costs["sessions"]["golf/s004"]["cost"], costs["sessions"]["golf/s003"]["cost"], costs["cost"]),
                         (0.003, 0.004, 0.007))
        self.assertEqual(self.get("/api/campaign/api-costs?token=wrong")[0], 403)

    def test_without_a_key_the_cost_stays_claude_codes(self):
        self.use(FakeOpenRouter(), key="")
        self.append(gen(1))
        costs = self.server.api_stats.costs(self.campaign)
        self.assertEqual((costs["state"], costs["cost"], costs["sessions"]["golf/s004"]["priced"]), ("no-key", 0, 0),
                         "nothing priced: the page keeps the cost the snapshot reports")
        self.assertEqual(snapshot(self.campaign)["tasks"][0]["nodes"][1]["cost"], 0.5)

    def test_what_the_api_stats_stream_refuses(self):
        self.assertEqual(self.get("/api/campaign/agent-api-stats?task=golf&session=s004")[0], 403, "no token")
        self.assertEqual(self.get("/api/campaign/agent-api-stats?token=wrong&task=golf&session=s004")[0], 403)
        self.assertEqual(self.get("/api/campaign/agent-api-stats?token=t0ken&task=golf&session=s999")[0], 404)
        self.assertEqual(self.get("/api/campaign/agent-api-stats?token=t0ken&task=..&session=s004")[0], 400)

    test_the_page_and_the_snapshot = test_the_stream_pushes_a_snapshot_now_and_on_change_only = test_what_is_refused = None


if __name__ == "__main__":
    unittest.main()
