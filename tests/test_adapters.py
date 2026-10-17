import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
from orchlib import adapters as A  # noqa: E402
from orchlib import events  # noqa: E402


class ResolveTest(unittest.TestCase):
    def test_legacy_devin_model_picks_devin(self):
        r = A.resolve("swe-2-high", agent="claude")
        self.assertEqual((r["agent"], r["tier"], r["model"]),
                         ("devin", "high", "swe-2-high"))

    def test_tier_uses_default_agent(self):
        r = A.resolve("high", agent="claude")
        self.assertEqual((r["agent"], r["model"]), ("claude", "opus"))

    def test_prefixed(self):
        r = A.resolve("codex:max")
        self.assertEqual((r["agent"], r["model"], r["effort"]),
                         ("codex", "gpt-5.6-terra", "xhigh"))

    def test_empty_is_default_tier(self):
        self.assertEqual(A.resolve("", agent="devin")["model"], "swe-2-medium")

    def test_unknown_agent(self):
        with self.assertRaises(A.AdapterError):
            A.resolve("nope:high")

    def test_opencode_tier_has_no_model(self):
        r = A.resolve("opencode:high")
        self.assertIsNone(r["model"])
        self.assertEqual(r["tier"], "high")


class GateTest(unittest.TestCase):
    def test_devin_fuzzy_max(self):
        for m in ("max", "swe-2-max", "swe-2-ma", "swe-2", "swe-1-7",
                  "devin:claude-opus-5-max"):
            self.assertTrue(A.is_gated(A.resolve(m, agent="devin")), m)
        self.assertFalse(A.is_gated(A.resolve("swe-2-high")))
        self.assertFalse(A.is_gated(A.resolve("medium", agent="devin")))

    def test_claude_gate(self):
        self.assertTrue(A.is_gated(A.resolve("claude:fable")))
        self.assertTrue(A.is_gated(A.resolve("claude:max")))
        self.assertFalse(A.is_gated(A.resolve("claude:opus")))

    def test_allow_tokens(self):
        self.assertEqual(A.normalize_allow("swe-2-max"), {"devin:max"})
        self.assertEqual(A.normalize_allow("max"), {"*:max"})
        self.assertEqual(A.normalize_allow(["claude", "codex:max"]),
                         {"claude:max", "codex:max"})

    def test_allowed(self):
        r = A.resolve("claude:max")
        self.assertFalse(A.allowed(r, A.normalize_allow("swe-2-max")))
        self.assertTrue(A.allowed(r, A.normalize_allow("claude")))
        self.assertTrue(A.allowed(r, A.normalize_allow("max")))
        self.assertTrue(A.allowed(A.resolve("claude:high"), set()))


class LaunchTest(unittest.TestCase):
    def test_devin_command_unchanged(self):
        a = A.load("devin")
        self.assertEqual(A.launch_argv(a, A.resolve("swe-2-high")),
                         ["devin", "--model", "swe-2-high",
                          "--permission-mode", "bypass"])

    def test_claude_with_effort_and_hooks(self):
        a = A.load("claude")
        argv = A.launch_argv(a, A.resolve("claude:max"),
                             hook_args=["--settings", "{}"])
        self.assertEqual(argv, ["claude", "--model", "fable", "--effort",
                                "max", "--dangerously-skip-permissions",
                                "--settings", "{}"])

    def test_codex_effort_config(self):
        argv = A.launch_argv(A.load("codex"), A.resolve("codex:high"))
        self.assertIn("model_reasoning_effort=high", argv)
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", argv)

    def test_opencode_drops_model(self):
        argv = A.launch_argv(A.load("opencode"), A.resolve("opencode:high"))
        self.assertEqual(argv, ["opencode", "--auto"])

    def test_binary_env_override(self):
        os.environ["ORCH_DEVIN_CMD"] = "/opt/devin --x"
        try:
            argv = A.launch_argv(A.load("devin"), A.resolve("swe-2-high"))
            self.assertEqual(argv[:2], ["/opt/devin", "--x"])
        finally:
            del os.environ["ORCH_DEVIN_CMD"]

    def test_command_quotes(self):
        cmd = A.launch_command(A.load("claude"), A.resolve("claude:high"),
                               hook_args=["--settings", '{"a": 1}'],
                               env={"ORCH_ALLOW": ""})
        self.assertTrue(cmd.startswith("env ORCH_ALLOW= claude"))
        self.assertIn("'{\"a\": 1}'", cmd)


class ScreenTest(unittest.TestCase):
    def test_devin_ready_and_echo(self):
        a = A.load("devin")
        self.assertTrue(A.screen_match(a, "ready", "foo\n SWE-2 High  ..."))
        self.assertTrue(A.screen_match(a, "input_echo", "❭ あなたはこの",
                                       first="あなたはこの"))
        self.assertFalse(A.screen_match(a, "input_echo", "❭ ", first="abc"))

    def test_devin_menu_key(self):
        a = A.load("devin")
        scr = ("Allow exec?\n 1. (Approve once)\n 2. This session\n"
               " 3. Yes, switch to bypass mode\n")
        self.assertEqual(A.approval_key(a, scr), "3")
        self.assertIsNone(A.approval_key(a, "nothing here"))

    def test_codex_menu_key(self):
        a = A.load("codex")
        scr = ("Would you like to run the following command?\n"
               "› 1. Yes, proceed\n  2. Yes, and don't ask again for this\n"
               "  3. No\n")
        self.assertEqual(A.approval_key(a, scr), "2")

    def test_startup_prompt(self):
        a = A.load("claude")
        self.assertEqual(A.startup_prompt(
            a, "Do you trust the files in this folder?"), ["enter"])
        self.assertIsNone(A.startup_prompt(a, "? for shortcuts"))

    def test_rate_limit(self):
        self.assertTrue(A.screen_match(A.load("devin"), "rate_limit",
                                       "Reached free model rate limit"))
        self.assertTrue(A.screen_match(A.load("claude"), "rate_limit",
                                       "5-hour limit reached ∙ resets 3pm"))

    def test_all_builtins_load(self):
        for aid in ("devin", "claude", "codex", "opencode", "generic"):
            self.assertIn(aid, A.list_ids())
            A.load(aid)


class UserAdapterTest(unittest.TestCase):
    def test_override_dir_wins(self):
        d = tempfile.mkdtemp()
        with open(os.path.join(d, "mine.json"), "w") as f:
            f.write('{"provider":"x","binary":"mine","launch":{"argv":'
                    '["{binary}","{bypass}"],"bypass":["--yolo"]},'
                    '"models":{"tiers":{"medium":{},"high":{},"max":{}}},'
                    '"screen":{}}')
        os.environ["ORCH_ADAPTERS_DIR"] = d
        A._CACHE.clear()
        try:
            argv = A.launch_argv(A.load("mine"), A.resolve("mine:high"))
            self.assertEqual(argv, ["mine", "--yolo"])
        finally:
            del os.environ["ORCH_ADAPTERS_DIR"]
            A._CACHE.clear()


class EventsTest(unittest.TestCase):
    def test_durations(self):
        cwd = os.getcwd()
        os.chdir(tempfile.mkdtemp())
        try:
            events.emit("spawn", dispatch="d1", task="t1", agent="claude",
                        model="opus")
            events.emit("done", dispatch="d1", task="t1")
            d = events.durations()
            self.assertEqual(d["t1"]["agent"], "claude")
            self.assertEqual(d["t1"]["gen_min"], 0)
        finally:
            os.chdir(cwd)


if __name__ == "__main__":
    unittest.main()
