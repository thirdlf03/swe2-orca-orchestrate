"""Per-agent distribution of hooks/no_blind_sleep.py.

One policy file, several delivery mechanisms (docs/plan-generic-harness.md
§4 row 9): sleep polling burned 2h in a measured run, and the policy only
binds if every agent CLI the harness spawns actually loads it.

  claude    launch_args(): `--settings '<json>'` per launch. Never touches the
            user's ~/.claude/settings.json (Orca already registers its own
            status hooks there).
  devin     install(): merge into ~/.config/devin/config.json `hooks.PreToolUse`
            (verified schema; Orca's devin-hook.sh entries live in the same
            file and must survive the merge).
  opencode  install(): generate ~/.config/opencode/plugins/orch-no-blind-sleep.js
            from hooks/opencode/orch-no-blind-sleep.js (OpenCode only has JS
            plugins, not command hooks).
  codex     install(): merge into $CODEX_HOME/hooks.json — EXPERIMENTAL, the
            shell tool name / tool_input shape are unverified. Orca launches
            codex with its own CODEX_HOME (runtime home), so a user-home
            install may not be seen by Orca-spawned codex.

install() defaults to dry_run=True and is idempotent: an existing entry whose
command references no_blind_sleep is left alone (the user's devin config
already carries a copy at ~/.config/devin/hooks/no_blind_sleep.py), and every
real write is preceded by a timestamped backup.
"""
import json
import os
import shlex
import shutil
import sys
import time

HOOK_REL = os.path.join("hooks", "no_blind_sleep.py")
OPENCODE_TEMPLATE_REL = os.path.join("hooks", "opencode", "orch-no-blind-sleep.js")
OPENCODE_PLUGIN_NAME = "orch-no-blind-sleep.js"
HOOK_MARKER = "no_blind_sleep"

DEVIN_MATCHER = "^(exec|write_to_process)$"
CLAUDE_MATCHER = "Bash"
# Unverified: candidate names for codex's shell tool. A non-shell payload is
# a no-op for the hook (no command field -> allow), so over-matching is cheap.
CODEX_MATCHER = "^(Bash|shell|exec_command|local_shell|container\\.exec)$"
ORCA_CODEX_RUNTIME_HOME = os.path.join(
    "Library", "Application Support", "orca", "codex-runtime-home", "home"
)


def hook_path(harness_dir: str) -> str:
    return os.path.join(os.path.abspath(harness_dir), HOOK_REL)


def hook_command(agent_id: str, harness_dir: str) -> str:
    """Shell command line that runs the shared hook for `agent_id`."""
    return "python3 %s --agent %s" % (
        shlex.quote(hook_path(harness_dir)),
        shlex.quote(agent_id),
    )


def claude_settings(harness_dir: str) -> dict:
    return {
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": CLAUDE_MATCHER,
                    "hooks": [
                        {
                            "type": "command",
                            "command": hook_command("claude", harness_dir),
                            "timeout": 5,
                        }
                    ],
                }
            ]
        }
    }


def launch_args(agent_id: str, harness_dir: str) -> list:
    """Extra launch argv. Only claude injects per launch (`claude --settings
    <file-or-json>`, checked against `claude --help`); the others rely on
    install()."""
    if agent_id == "claude":
        return ["--settings", json.dumps(claude_settings(harness_dir))]
    return []


def _home(home):
    return home if home else os.path.expanduser("~")


def _backup(path: str) -> str:
    dst = "%s.bak-orch-%s" % (path, time.strftime("%Y%m%d-%H%M%S"))
    n = 1
    while os.path.exists(dst):
        dst = "%s.bak-orch-%s-%d" % (path, time.strftime("%Y%m%d-%H%M%S"), n)
        n += 1
    shutil.copy2(path, dst)
    return dst


def _atomic_write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".orch-tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def _find_existing(cfg: dict) -> str:
    """Command of an already-registered no_blind_sleep PreToolUse hook, or ""."""
    for ent in (cfg.get("hooks") or {}).get("PreToolUse") or []:
        if not isinstance(ent, dict):
            continue
        for h in ent.get("hooks") or []:
            if isinstance(h, dict) and HOOK_MARKER in str(h.get("command", "")):
                return str(h.get("command"))
    return ""


def _merge_json_hooks(res: dict, path: str, entry: dict, dry_run: bool) -> dict:
    """Append `entry` to `hooks.PreToolUse` of the JSON file at `path`,
    preserving every other key/entry. Shared by devin and codex (both use
    the Claude-compatible `hooks.<Event>[{matcher, hooks:[...]}]` shape)."""
    res.update(target=path, entry=entry, dry_run=dry_run, backup=None)
    cfg = {}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                cfg = json.load(f)
        except (OSError, ValueError) as e:
            res.update(action="error", changed=False,
                       error="cannot parse %s: %s (not touched)" % (path, e))
            return res
        if not isinstance(cfg, dict):
            res.update(action="error", changed=False,
                       error="%s is not a JSON object (not touched)" % path)
            return res
    existing = _find_existing(cfg)
    if existing:
        res.update(action="unchanged", changed=False, existing=existing)
        return res
    hooks = cfg.get("hooks")
    if not isinstance(hooks, dict):
        hooks = cfg["hooks"] = {}
    pre = hooks.get("PreToolUse")
    if not isinstance(pre, list):
        pre = hooks["PreToolUse"] = []
    pre.append(entry)
    res.update(action="install", changed=True)
    if dry_run:
        return res
    if os.path.exists(path):
        res["backup"] = _backup(path)
    _atomic_write(path, json.dumps(cfg, indent=2, ensure_ascii=False) + "\n")
    return res


def render_opencode_plugin(harness_dir: str, python: str = "python3") -> str:
    tpl_path = os.path.join(os.path.abspath(harness_dir), OPENCODE_TEMPLATE_REL)
    with open(tpl_path, encoding="utf-8") as f:
        tpl = f.read()
    return tpl.replace("__ORCH_PYTHON__", json.dumps(python)).replace(
        "__ORCH_HOOK_SCRIPT__", json.dumps(hook_path(harness_dir))
    )


def _install_opencode(res: dict, harness_dir: str, home: str, dry_run: bool) -> dict:
    path = os.path.join(home, ".config", "opencode", "plugins", OPENCODE_PLUGIN_NAME)
    text = render_opencode_plugin(harness_dir)
    res.update(target=path, dry_run=dry_run, backup=None)
    old = None
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                old = f.read()
        except OSError:
            old = None
    if old == text:
        res.update(action="unchanged", changed=False)
        return res
    res.update(action="install" if old is None else "update", changed=True)
    if dry_run:
        return res
    if old is not None:
        res["backup"] = _backup(path)
    _atomic_write(path, text)
    return res


def install(agent_id: str, harness_dir: str, dry_run: bool = True,
            home: str = None) -> dict:
    """Describe (dry_run) or perform the persistent hook install for an agent.

    `home` overrides the home directory (tests); default is $HOME. Returns
    {"agent", "supported", "method", "verified", "action": install|update|
    unchanged|launch-args|error, "changed", "target", "backup", "notes", ...}.
    """
    home = _home(home)
    res = {"agent": agent_id, "supported": True, "dry_run": dry_run, "notes": []}
    if not os.path.exists(hook_path(harness_dir)):
        res.update(supported=False, action="error", changed=False,
                   error="hook not found: %s" % hook_path(harness_dir))
        return res
    if agent_id == "claude":
        res.update(method="settings-flag", verified=True, action="launch-args",
                   changed=False, target=None, launch_args=launch_args("claude", harness_dir))
        res["notes"].append("injected per launch via --settings; no persistent install")
        return res
    if agent_id == "devin":
        res.update(method="global-config-merge", verified=True)
        entry = {"matcher": DEVIN_MATCHER,
                 "hooks": [{"type": "command",
                            "command": hook_command("devin", harness_dir),
                            "timeout": 5}]}
        path = os.path.join(home, ".config", "devin", "config.json")
        return _merge_json_hooks(res, path, entry, dry_run)
    if agent_id == "opencode":
        res.update(method="opencode-plugin", verified=False)
        res["notes"].append("plugin API shape taken from Orca's orca-opencode-status.js; "
                            "tool.execute.before blocking via throw is unverified live")
        return _install_opencode(res, harness_dir, home, dry_run)
    if agent_id == "codex":
        res.update(method="codex-home-hooks-json", verified=False, experimental=True)
        codex_home = os.environ.get("CODEX_HOME") or os.path.join(home, ".codex")
        orca_home = os.path.join(home, ORCA_CODEX_RUNTIME_HOME)
        res["orca_runtime_home"] = orca_home
        res["notes"] += [
            "schema unverified: shell tool name / tool_input shape / block semantics",
            "Orca launches codex with CODEX_HOME=%s and may rewrite its hooks.json; "
            "re-run with CODEX_HOME set to it to target Orca-spawned codex" % orca_home,
        ]
        entry = {"matcher": CODEX_MATCHER,
                 "hooks": [{"type": "command",
                            "command": hook_command("codex", harness_dir),
                            "timeout": 5}]}
        return _merge_json_hooks(res, os.path.join(codex_home, "hooks.json"), entry, dry_run)
    res.update(supported=False, action="unsupported", changed=False,
               notes=["no hook installer for agent %r" % agent_id])
    return res


if __name__ == "__main__":
    # ad-hoc: python3 -m orchlib.hooks <agent> [--apply]
    here = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    a = sys.argv[1] if len(sys.argv) > 1 else "devin"
    print(json.dumps(install(a, here, dry_run="--apply" not in sys.argv), indent=2))
