"""Tests for hooks/no_blind_sleep.py (per-agent input/output) and
lib/orchlib/hooks.py (launch_args / install). Never touches the real $HOME."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOOK = os.path.join(ROOT, "hooks", "no_blind_sleep.py")
sys.path.insert(0, os.path.join(ROOT, "lib"))

from orchlib import hooks  # noqa: E402

BLIND = "sleep 60"
COND = "until x; do sleep 30; done"


def shapes(cmd):
    return {
        "devin-exec": ({"tool_name": "exec", "tool_input": {"command": cmd}}, "devin"),
        "devin-text": ({"tool_name": "write_to_process",
                        "tool_input": {"text_input": cmd + "\n"}}, "devin"),
        "devin-bytes": ({"tool_name": "write_to_process",
                         "tool_input": {"bytes_input": cmd}}, "devin"),
        "claude": ({"hook_event_name": "PreToolUse", "tool_name": "Bash",
                    "tool_input": {"command": cmd}}, "claude"),
        "codex-claude-shape": ({"hook_event_name": "PreToolUse", "tool_name": "shell",
                                "tool_input": {"command": ["bash", "-lc", cmd]}}, "codex"),
        "codex-top-str": ({"command": cmd}, "codex"),
        "codex-top-argv": ({"command": ["bash", "-lc", cmd]}, "codex"),
        "opencode": ({"tool": "bash", "args": {"command": cmd}}, "opencode"),
    }


def run_hook(payload, *argv, raw=None):
    p = subprocess.run(
        [sys.executable, HOOK] + list(argv),
        input=raw if raw is not None else json.dumps(payload),
        capture_output=True, text=True, timeout=10,
    )
    return p.returncode, p.stdout, p.stderr


class HookInputShapes(unittest.TestCase):
    def test_blocks_blind_sleep_every_shape(self):
        for name, (payload, _) in shapes(BLIND).items():
            with self.subTest(name):
                rc, out, err = run_hook(payload)
                self.assertEqual(rc, 2, name)
                self.assertIn("sleep 60", err)
                self.assertEqual(json.loads(out)["decision"], "block")

    def test_allows_condition_loop_every_shape(self):
        for name, (payload, _) in shapes(COND).items():
            with self.subTest(name):
                rc, out, err = run_hook(payload)
                self.assertEqual((rc, out, err), (0, "", ""), name)

    def test_with_explicit_agent_flag(self):
        for name, (payload, agent) in shapes(BLIND).items():
            with self.subTest(name):
                self.assertEqual(run_hook(payload, "--agent", agent)[0], 2)
            with self.subTest(name + "-cond"):
                p2 = shapes(COND)[name][0]
                self.assertEqual(run_hook(p2, "--agent", agent)[0], 0)

    def test_garbage_input_allows(self):
        for raw in ("", "not json", "[]", '{"tool_input": {}}', '{"command": 5}'):
            with self.subTest(raw):
                self.assertEqual(run_hook(None, raw=raw)[0], 0)

    def test_no_devin_only_tools_in_reason(self):
        _, _, err = run_hook({"command": BLIND})
        for devin_only in ("exec(", "write_to_process", "text_input"):
            self.assertNotIn(devin_only, err)


class HookOutputFormat(unittest.TestCase):
    def out(self, payload, *argv):
        rc, out, _ = run_hook(payload, *argv)
        self.assertEqual(rc, 2)
        return json.loads(out)

    def test_devin_legacy_shape(self):
        o = self.out(shapes(BLIND)["devin-exec"][0])
        self.assertEqual(set(o), {"decision", "reason"})

    def test_opencode_legacy_shape(self):
        o = self.out(shapes(BLIND)["opencode"][0])
        self.assertEqual(set(o), {"decision", "reason"})

    def test_claude_both_shapes(self):
        for argv in ((), ("--agent", "claude"), ("--agent=claude",)):
            with self.subTest(argv):
                o = self.out(shapes(BLIND)["claude"][0], *argv)
                self.assertEqual(o["decision"], "block")
                hso = o["hookSpecificOutput"]
                self.assertEqual(hso["hookEventName"], "PreToolUse")
                self.assertEqual(hso["permissionDecision"], "deny")
                self.assertEqual(hso["permissionDecisionReason"], o["reason"])

    def test_codex_and_unknown_get_both(self):
        self.assertIn("hookSpecificOutput", self.out({"command": BLIND}))
        self.assertIn("hookSpecificOutput",
                      self.out({"tool_input": {"command": BLIND}}))

    def test_flag_overrides_detection(self):
        o = self.out(shapes(BLIND)["claude"][0], "--agent", "devin")
        self.assertNotIn("hookSpecificOutput", o)


class LaunchArgs(unittest.TestCase):
    def test_claude_settings_json(self):
        args = hooks.launch_args("claude", ROOT)
        self.assertEqual(args[0], "--settings")
        cfg = json.loads(args[1])
        ent = cfg["hooks"]["PreToolUse"][0]
        self.assertEqual(ent["matcher"], "Bash")
        cmd = ent["hooks"][0]["command"]
        self.assertEqual(ent["hooks"][0]["type"], "command")
        self.assertIn(os.path.join(ROOT, "hooks", "no_blind_sleep.py"), cmd)
        self.assertTrue(cmd.endswith("--agent claude"))

    def test_claude_command_runs(self):
        cmd = json.loads(hooks.launch_args("claude", ROOT)[1])[
            "hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        p = subprocess.run(cmd, shell=True, input=json.dumps(shapes(BLIND)["claude"][0]),
                           capture_output=True, text=True, timeout=10)
        self.assertEqual(p.returncode, 2)

    def test_others_empty(self):
        for a in ("devin", "codex", "opencode", "generic", "nope"):
            self.assertEqual(hooks.launch_args(a, ROOT), [])


class Install(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="orch-hooks-home-")
        self._env = {k: os.environ.get(k) for k in ("HOME", "CODEX_HOME")}
        os.environ["HOME"] = self.home
        os.environ.pop("CODEX_HOME", None)

    def tearDown(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.home, ignore_errors=True)

    def _devin_cfg(self):
        path = os.path.join(self.home, ".config", "devin", "config.json")
        os.makedirs(os.path.dirname(path))
        cfg = {
            "version": 1, "permissions": {"allow": ["Exec(orca*)"]},
            "hooks": {"SessionStart": [{"hooks": [{"type": "command",
                                                   "command": "orca-devin-hook.sh"}]}],
                      "PreToolUse": [{"hooks": [{"type": "command",
                                                 "command": "orca-devin-hook.sh"}]}]},
        }
        with open(path, "w") as f:
            json.dump(cfg, f)
        return path, cfg

    def _backups(self, path):
        d = os.path.dirname(path)
        return [n for n in os.listdir(d) if ".bak-orch-" in n]

    def test_devin_dry_run_writes_nothing(self):
        path, cfg = self._devin_cfg()
        r = hooks.install("devin", ROOT, dry_run=True)
        self.assertEqual((r["action"], r["changed"]), ("install", True))
        with open(path) as f:
            self.assertEqual(json.load(f), cfg)
        self.assertEqual(self._backups(path), [])

    def test_devin_install_idempotent_preserves_keys(self):
        path, cfg = self._devin_cfg()
        r = hooks.install("devin", ROOT, dry_run=False)
        self.assertEqual(r["action"], "install")
        self.assertTrue(r["backup"] and os.path.exists(r["backup"]))
        with open(path) as f:
            new = json.load(f)
        self.assertEqual(new["permissions"], cfg["permissions"])
        self.assertEqual(new["hooks"]["SessionStart"], cfg["hooks"]["SessionStart"])
        pre = new["hooks"]["PreToolUse"]
        self.assertEqual(pre[0], cfg["hooks"]["PreToolUse"][0])
        self.assertEqual(pre[1]["matcher"], "^(exec|write_to_process)$")
        r2 = hooks.install("devin", ROOT, dry_run=False)
        self.assertEqual((r2["action"], r2["changed"]), ("unchanged", False))
        with open(path) as f:
            self.assertEqual(json.load(f), new)
        self.assertEqual(len(self._backups(path)), 1)

    def test_devin_existing_copy_counts_as_installed(self):
        path, cfg = self._devin_cfg()
        cfg["hooks"]["PreToolUse"].insert(0, {
            "matcher": "^(exec|write_to_process)$",
            "hooks": [{"type": "command",
                       "command": "/x/.config/devin/hooks/no_blind_sleep.py"}]})
        with open(path, "w") as f:
            json.dump(cfg, f)
        r = hooks.install("devin", ROOT, dry_run=False)
        self.assertEqual(r["action"], "unchanged")
        self.assertIn("no_blind_sleep", r["existing"])

    def test_devin_missing_config_created(self):
        r = hooks.install("devin", ROOT, dry_run=False)
        self.assertEqual(r["action"], "install")
        self.assertIsNone(r["backup"])
        with open(r["target"]) as f:
            self.assertEqual(len(json.load(f)["hooks"]["PreToolUse"]), 1)

    def test_devin_broken_config_untouched(self):
        path = os.path.join(self.home, ".config", "devin", "config.json")
        os.makedirs(os.path.dirname(path))
        with open(path, "w") as f:
            f.write("{broken")
        r = hooks.install("devin", ROOT, dry_run=False)
        self.assertEqual(r["action"], "error")
        with open(path) as f:
            self.assertEqual(f.read(), "{broken")

    def test_opencode_plugin(self):
        r = hooks.install("opencode", ROOT, dry_run=True)
        self.assertFalse(os.path.exists(r["target"]))
        r = hooks.install("opencode", ROOT, dry_run=False)
        self.assertEqual(r["action"], "install")
        with open(r["target"]) as f:
            js = f.read()
        self.assertNotIn("__ORCH_", js)
        self.assertIn(json.dumps(os.path.join(ROOT, "hooks", "no_blind_sleep.py")), js)
        self.assertIn('"tool.execute.before"', js)
        r2 = hooks.install("opencode", ROOT, dry_run=False)
        self.assertEqual(r2["action"], "unchanged")
        with open(r["target"], "w") as f:
            f.write("// stale")
        r3 = hooks.install("opencode", ROOT, dry_run=False)
        self.assertEqual(r3["action"], "update")
        self.assertTrue(os.path.exists(r3["backup"]))

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_opencode_plugin_blocks_via_node(self):
        r = hooks.install("opencode", ROOT, dry_run=False)
        mjs = os.path.join(self.home, "plugin.mjs")
        shutil.copy(r["target"], mjs)
        script = (
            "import { OrchNoBlindSleepPlugin as P } from %s;\n"
            "const h = await P({});\n"
            "const f = h['tool.execute.before'];\n"
            "await f({tool:'bash'}, {args:{command:%s}});\n"
            "try { await f({tool:'bash'}, {args:{command:%s}}); console.log('ALLOWED'); }\n"
            "catch (e) { console.log(e.message.startsWith('Blocked') ? 'BLOCKED' : 'ERR'); }\n"
        ) % (json.dumps("file://" + mjs), json.dumps(COND), json.dumps(BLIND))
        p = subprocess.run(["node", "--input-type=module", "-e", script],
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(p.stdout.strip(), "BLOCKED", p.stderr)

    def test_codex_experimental_idempotent(self):
        r = hooks.install("codex", ROOT, dry_run=True)
        self.assertTrue(r["experimental"])
        self.assertFalse(r["verified"])
        self.assertEqual(r["target"], os.path.join(self.home, ".codex", "hooks.json"))
        self.assertIn("orca", r["orca_runtime_home"])
        cx = os.path.join(self.home, "cx")
        os.makedirs(cx)
        with open(os.path.join(cx, "hooks.json"), "w") as f:
            json.dump({"hooks": {}}, f)
        os.environ["CODEX_HOME"] = cx
        r = hooks.install("codex", ROOT, dry_run=False)
        self.assertEqual(r["action"], "install")
        self.assertEqual(hooks.install("codex", ROOT, dry_run=False)["action"], "unchanged")

    def test_claude_and_unknown(self):
        r = hooks.install("claude", ROOT)
        self.assertEqual((r["action"], r["changed"]), ("launch-args", False))
        self.assertEqual(r["launch_args"], hooks.launch_args("claude", ROOT))
        self.assertFalse(hooks.install("goose", ROOT)["supported"])
        self.assertEqual(os.listdir(self.home), [])


if __name__ == "__main__":
    unittest.main()
