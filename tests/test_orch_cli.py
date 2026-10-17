"""Integration tests: bin/orch as a subprocess against tests/fake_orca.py.

No real orca, agent CLI or network. State lives in a temp cwd (.orch/).
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ORCH = os.path.join(os.path.dirname(HERE), "bin", "orch")
FAKE = os.path.join(HERE, "fake_orca.py")

# every builtin adapter's ready signature, and nothing that looks like a
# startup gate / approval menu / rate limit
READY_SCREEN = "SWE-2 High\n? for shortcuts\nask codex to do anything\n"

APPROVAL_SCREEN = """Run command: ls -la ?
 1. Yes (Approve once)
 2. Yes, always for this command
 3. Yes, switch to bypass mode
 4. No
"""


def _own_comm():
    out = subprocess.run(["ps", "-p", str(os.getpid()), "-o", "comm="],
                         capture_output=True, text=True).stdout.strip()
    return os.path.basename(out)


class OrchCLITest(unittest.TestCase):
    def setUp(self):
        os.chmod(FAKE, 0o755)
        self.tmp = tempfile.mkdtemp(prefix="orch-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.log = os.path.join(self.tmp, "orca.jsonl")
        self.screen = os.path.join(self.tmp, "screen.txt")
        self.set_screen(READY_SCREEN)
        nowhere = os.path.join(self.tmp, "nowhere")
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("ORCH_", "FAKE_ORCA_"))
               and k not in ("TYPESAFE_API_KEY",)}
        env.update({
            "ORCA_CLI_COMMAND": FAKE,
            "FAKE_ORCA_LOG": self.log,
            "FAKE_ORCA_DIR": self.tmp,
            "FAKE_ORCA_SCREEN": self.screen,
            "ORCH_PARALLEL_CAP": "0",
            # keep transcript scans off the real machine's logs (slow + private)
            "ORCH_DEVIN_DB": os.path.join(nowhere, "sessions.db"),
            "ORCH_CLAUDE_PROJECTS": nowhere,
            "ORCH_CODEX_HOME": nowhere,
            "ORCH_OPENCODE_DB": os.path.join(nowhere, "oc.db"),
        })
        self.env = env

    # ---------- helpers ----------

    def set_screen(self, text):
        with open(self.screen, "w") as f:
            f.write(text)

    def orch(self, *args, env=None, ok=True):
        e = dict(self.env)
        e.update(env or {})
        p = subprocess.run([sys.executable, ORCH, *args], cwd=self.tmp,
                           env=e, capture_output=True, text=True, timeout=120)
        if ok:
            self.assertEqual(p.returncode, 0,
                             f"orch {args} failed:\n{p.stdout}\n{p.stderr}")
        return p

    def write(self, name, text):
        p = os.path.join(self.tmp, name)
        with open(p, "w") as f:
            f.write(text)
        return p

    def calls(self, group=None, sub=None):
        if not os.path.exists(self.log):
            return []
        with open(self.log) as f:
            rows = [json.loads(ln) for ln in f if ln.strip()]
        return [r for r in rows
                if (group is None or r[:1] == [group])
                and (sub is None or r[1:2] == [sub])]

    @staticmethod
    def flag(argv, name):
        return argv[argv.index(name) + 1]

    def state(self):
        with open(os.path.join(self.tmp, ".orch", "state.json")) as f:
            return json.load(f)

    def run_state(self):
        st = self.state()
        return st["runs"][st["current_run"]]

    def events(self, kind=None):
        p = os.path.join(self.tmp, ".orch", "events.jsonl")
        if not os.path.exists(p):
            return []
        with open(p) as f:
            ev = [json.loads(ln) for ln in f if ln.strip()]
        return [e for e in ev if kind is None or e["kind"] == kind]

    def new_task(self, spec="# task\nRename foo to bar in src/x.py.\n"):
        self.orch("run", "create", "test", "objective")
        self.write("spec.md", spec)
        out = json.loads(self.orch("task", "create", "spec.md").stdout)
        return out[0]["task"]

    def spawn(self, model, env=None, ok=True, name="w1"):
        tid = self.new_task()
        return self.orch("spawn", "--task", tid, "--name", name,
                         "--model", model, env=env, ok=ok)

    def launch_cmds(self):
        return [self.flag(c, "--command")
                for c in self.calls("terminal", "create")]

    # ---------- tests ----------

    def test_spawn_claude(self):
        self.spawn("claude:high")
        cmd = self.launch_cmds()[0]
        self.assertIn("claude --model opus", cmd)
        self.assertIn("--dangerously-skip-permissions", cmd)
        self.assertIn("--settings", cmd)
        disp = self.run_state()["dispatches"]
        self.assertEqual(len(disp), 1)
        d = next(iter(disp.values()))
        self.assertEqual((d["agent"], d["provider"], d["model"]),
                         ("claude", "anthropic", "opus"))
        self.assertEqual(d["branch"], "w1")   # refs/heads/ stripped
        sp = self.events("spawn")
        self.assertEqual(len(sp), 1)
        self.assertEqual(sp[0]["agent"], "claude")
        self.assertEqual(sp[0]["dispatch"], d["dispatch"])

    def test_spawn_legacy_devin_model(self):
        self.spawn("swe-2-high")
        cmd = self.launch_cmds()[0]
        self.assertIn("devin --model swe-2-high --permission-mode bypass", cmd)
        d = next(iter(self.run_state()["dispatches"].values()))
        self.assertEqual((d["agent"], d["provider"]), ("devin", "cognition"))

    def test_max_gate_claude_denied(self):
        p = self.spawn("claude:max", ok=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("max-tier", p.stderr)
        self.assertEqual(self.calls("worktree", "create"), [])
        self.assertEqual(self.run_state()["dispatches"], {})

    def test_max_gate_claude_allowed(self):
        self.spawn("claude:max", env={"ORCH_ALLOW": "claude:max"})
        cmd = self.launch_cmds()[0]
        self.assertIn("--model fable --effort max", cmd)

    def test_max_gate_other_agent_allow_does_not_cover(self):
        p = self.spawn("claude:max", env={"ORCH_ALLOW": "codex:max"}, ok=False)
        self.assertNotEqual(p.returncode, 0)

    def test_max_gate_legacy_swe2max(self):
        p = self.spawn("swe-2-max", ok=False)
        self.assertNotEqual(p.returncode, 0)
        self.spawn("swe-2-max", env={"ORCH_ALLOW": "swe-2-max"}, name="w2")
        self.assertIn("devin --model swe-2-max", self.launch_cmds()[-1])

    def test_worker_start_failure_rolls_back(self):
        p = self.spawn("claude:high", env={"FAKE_ORCA_FAIL": "worker-start"},
                       ok=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("worker-start failed", p.stderr)
        self.assertEqual(len(self.calls("orchestration", "worker-start")), 2)
        closes = self.calls("terminal", "close")
        self.assertEqual([self.flag(c, "--terminal") for c in closes],
                         ["term_1"])
        rms = self.calls("worktree", "rm")
        self.assertEqual([self.flag(c, "--worktree") for c in rms],
                         ["id:wt_1"])
        self.assertIn("--force", rms[0])
        self.assertEqual(self.run_state()["dispatches"], {})
        self.assertEqual(self.events("spawn"), [])

    def test_compete_mixed_agents(self):
        self.orch("run", "create", "compete")
        self.write("feat.md", "# feature\nImplement the widget.\n")
        out = json.loads(self.orch(
            "compete", "feat.md", "-n", "3",
            "--models", "claude:high,codex:medium,devin:medium").stdout)
        self.assertEqual([r["agent"] for r in out],
                         ["claude", "codex", "devin"])
        specs = [self.flag(c, "--spec")
                 for c in self.calls("orchestration", "task-create")]
        self.assertEqual(len(specs), 3)
        for agent, spec in zip(("claude", "codex", "devin"), specs):
            self.assertIn("Implement the widget.", spec)
            self.assertIn(f"## エージェント固有の注意 ({agent})", spec)
        self.assertIn("Bash ツール", specs[0])
        self.assertIn("exec ツール", specs[2])
        cmds = self.launch_cmds()
        self.assertTrue(cmds[0].startswith("claude "))
        self.assertTrue(cmds[1].startswith("codex "))
        self.assertTrue(cmds[2].startswith("devin "))
        rs = self.run_state()
        self.assertEqual(len(rs["tasks"]), 3)
        self.assertEqual({t["cluster"] for t in rs["tasks"].values()},
                         {"feat"})
        self.assertEqual({d["role"] for d in rs["dispatches"].values()},
                         {"candidate"})

    def test_patrol_rate_limit_lowers_cap(self):
        self.spawn("claude:high")
        self.set_screen("working...\n5-hour limit reached ∙ resets 3pm\n")
        p = self.orch("patrol", env={"ORCH_PARALLEL_CAP": "10"})
        out = json.loads(p.stdout)
        self.assertIn("anthropic", out["caps_lowered"])
        entry = out["dispatches"][0]
        self.assertIn("rate-limited", entry["issues"])
        caps = self.run_state()["caps_auto"]
        self.assertGreaterEqual(caps["anthropic"], 1)
        self.assertLess(caps["anthropic"], 10)
        rl = self.events("rate_limit")
        self.assertEqual(len(rl), 1)
        self.assertEqual(rl[0]["provider"], "anthropic")
        d = next(iter(self.run_state()["dispatches"].values()))
        self.assertTrue(d.get("rate_limited_at"))

    def test_patrol_rate_limit_rescue_after_cooldown(self):
        self.spawn("claude:high")
        self.set_screen("5-hour limit reached ∙ resets 3pm\n")
        self.orch("patrol")
        n_send = len(self.calls("terminal", "send"))
        self.orch("patrol", "--rescue", "--cooldown-min", "0")
        sends = self.calls("terminal", "send")[n_send:]
        self.assertEqual(len(sends), 1)
        self.assertIn("continue", self.flag(sends[0], "--text"))
        d = next(iter(self.run_state()["dispatches"].values()))
        self.assertEqual(d.get("resumed"), 1)

    def test_patrol_devin_approval_rescue(self):
        self.spawn("devin:high")
        self.set_screen(APPROVAL_SCREEN)
        n_send = len(self.calls("terminal", "send"))
        p = self.orch("patrol", "--rescue")
        entry = json.loads(p.stdout)[0]
        self.assertIn("permission-menu", entry["issues"])
        self.assertIn("rescued:key=3", entry["issues"])
        sends = self.calls("terminal", "send")[n_send:]
        texts = [self.flag(s, "--text") for s in sends if "--text" in s]
        self.assertEqual(texts, ["3"])
        self.assertTrue(any("--enter" in s for s in sends))

    def test_patrol_approval_without_rescue_sends_nothing(self):
        self.spawn("devin:high")
        self.set_screen(APPROVAL_SCREEN)
        n_send = len(self.calls("terminal", "send"))
        entry = json.loads(self.orch("patrol").stdout)[0]
        self.assertIn("permission-menu", entry["issues"])
        self.assertEqual(len(self.calls("terminal", "send")), n_send)

    def test_wait_records_done(self):
        self.spawn("claude:high")
        did = next(iter(self.run_state()["dispatches"]))
        msgs = self.write("msgs.json", json.dumps([{
            "id": "msg_1", "type": "worker_done", "from_handle": "term_1",
            "subject": "done",
            "payload": json.dumps({"taskId": "task_1", "dispatchId": did,
                                   "outcome": "completed"})}]))
        p = self.orch("wait", "--timeout-min", "0.05",
                      env={"FAKE_ORCA_MESSAGES": msgs})
        lines = [json.loads(ln) for ln in p.stdout.splitlines() if ln.strip()]
        self.assertEqual(lines[0]["type"], "worker_done")
        self.assertEqual(lines[-1]["settled"], [did])
        done = self.events("done")
        self.assertEqual(len(done), 1)
        self.assertEqual((done[0]["dispatch"], done[0]["outcome"]),
                         (did, "completed"))
        checks = self.calls("orchestration", "check")
        self.assertTrue(any("--ack" in c and "del_1" in c for c in checks[1:]))

    def test_wait_settle_all_exits_early(self):
        self.spawn("claude:high")
        did = next(iter(self.run_state()["dispatches"]))
        msgs = self.write("msgs.json", json.dumps([{
            "id": "m", "type": "worker_done",
            "payload": {"dispatchId": did, "outcome": "completed"}}]))
        p = self.orch("wait", "--timeout-min", "5", "--settle-all",
                      env={"FAKE_ORCA_MESSAGES": msgs})
        self.assertEqual(json.loads(p.stdout.splitlines()[-1])["pending"], [])

    def test_batch_manifest_default_agent(self):
        self.orch("run", "create", "batch")
        self.write("a.md", "# a\nDo A.\n")
        self.write("b.md", "# b\nDo B.\n")
        self.write("m.json", json.dumps({
            "defaults": {"agent": "claude", "model": "high"},
            "items": [{"spec_file": "a.md", "name": "ba"},
                      {"spec_file": "b.md", "name": "bb",
                       "model": "codex:medium"}]}))
        out = json.loads(self.orch("batch", "m.json", "--gap-sec", "0").stdout)
        self.assertEqual([s["name"] for s in out["spawned"]], ["ba", "bb"])
        self.assertEqual(out["failed"], [])
        cmds = self.launch_cmds()
        self.assertIn("claude --model opus", cmds[0])
        self.assertTrue(cmds[1].startswith("codex "))
        specs = [self.flag(c, "--spec")
                 for c in self.calls("orchestration", "task-create")]
        self.assertIn("(claude)", specs[0])
        self.assertIn("(codex)", specs[1])

    def test_batch_parallel_cap_defers(self):
        comm = _own_comm()
        self.assertTrue(comm)
        adir = os.path.join(self.tmp, "user-adapters")
        os.makedirs(adir)
        with open(os.path.join(adir, "zz.json"), "w") as f:
            json.dump({
                "provider": "zzprov", "binary": "zz-agent",
                "process_names": [comm], "liveness": "ps",
                "launch": {"argv": ["{binary}", "{model_args}"],
                           "model_args": ["--model", "{model}"]},
                "models": {"tiers": {"medium": {"model": "zm"},
                                     "high": {"model": "zh"},
                                     "max": {"model": "zx"}}},
                "screen": {"ready": "SWE-2 High"},
                "readiness": {"strategy": "screen-regex", "timeout_s": 5},
            }, f)
        self.orch("run", "create", "cap")
        self.write("a.md", "# a\n")
        self.write("b.md", "# b\n")
        self.write("m.json", json.dumps({
            "defaults": {"model": "zz:high"},
            "items": [{"spec_file": "a.md", "name": "ca"},
                      {"spec_file": "b.md", "name": "cb"}]}))
        env = {"ORCH_ADAPTERS_DIR": adir, "ORCH_PARALLEL_CAP_ZZPROV": "1"}
        p = self.orch("batch", "m.json", "--gap-sec", "0", env=env, ok=False)
        self.assertEqual(p.returncode, 3, p.stderr)
        out = json.loads(p.stdout)
        self.assertEqual(out["spawned"], [])
        self.assertEqual([d["name"] for d in out["deferred"]], ["ca", "cb"])
        self.assertIn("parallel cap (zzprov)", p.stderr)
        # deferred items are re-run later: no orphan tasks/worktrees now
        self.assertEqual(self.calls("orchestration", "task-create"), [])
        self.assertEqual(self.calls("worktree", "create"), [])
        # cap disabled -> the same adapter spawns
        env["ORCH_PARALLEL_CAP_ZZPROV"] = "0"
        out = json.loads(self.orch("batch", "m.json", "--gap-sec", "0",
                                   env=env).stdout)
        self.assertEqual(len(out["spawned"]), 2)
        self.assertTrue(self.launch_cmds()[0].startswith("zz-agent --model zh"))

    def test_agents_list_and_check(self):
        rows = json.loads(self.orch("agents", "--no-models").stdout)
        self.assertEqual(sorted(r["agent"] for r in rows),
                         ["claude", "codex", "devin", "generic", "opencode"])
        self.assertFalse([r for r in rows if "error" in r])
        chk = json.loads(self.orch("agents", "--check", "codex:max").stdout)
        self.assertTrue(chk["gated"])
        self.assertEqual(chk["resolved"]["agent"], "codex")
        self.assertTrue(chk["command"].startswith("codex "))
        chk = json.loads(self.orch("agents", "--check", "claude:high").stdout)
        self.assertFalse(chk["gated"])
        self.assertEqual(self.calls(), [])   # --check never touches orca

    def test_unknown_command_suggests(self):
        p = self.orch("spwan", ok=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("did you mean: spawn", p.stderr)

    def test_unknown_flag_rejected(self):
        p = self.orch("agents", "--chek", "claude:high", ok=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("did you mean: --check", p.stderr)

    def test_clean_releases(self):
        self.spawn("claude:high")
        did = next(iter(self.run_state()["dispatches"]))
        out = json.loads(self.orch("clean", did).stdout)
        self.assertEqual(out["released"], [did])
        self.assertEqual(len(self.calls("orchestration", "worker-release")), 1)
        self.assertEqual(len(self.calls("worktree", "rm")), 1)
        self.assertEqual(self.run_state()["dispatches"][did]["status"],
                         "released")
        self.assertEqual(len(self.events("clean")), 1)


if __name__ == "__main__":
    unittest.main()
