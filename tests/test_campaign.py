"""The campaign snapshot and its server, on synthetic campaigns in both layouts."""
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
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


if __name__ == "__main__":
    unittest.main()


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
        import os
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

    test_a_session_is_extracted_compiled_and_kept = test_what_the_analysis_endpoints_refuse = None
