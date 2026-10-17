#!/usr/bin/env python3
"""Fake `orca` CLI for bin/orch integration tests (stdlib only).

Env:
  FAKE_ORCA_LOG          JSONL file; every call's argv is appended (required)
  FAKE_ORCA_DIR          scratch dir for worktree paths (default: log's dir)
  FAKE_ORCA_SCREEN       file whose lines are returned by `terminal read`
  FAKE_ORCA_FAIL         comma list of subcommands that fail (e.g. worker-start)
  FAKE_ORCA_WORKER_STATE state returned by worker-show (default: running)
  FAKE_ORCA_MESSAGES     JSON list of messages for the first `check`; the file
                         is consumed (deleted) so later checks return nothing
"""
import json
import os
import sys


def out(result, code=0):
    print(json.dumps({"ok": True, "result": result}))
    sys.exit(code)


def main():
    argv = sys.argv[1:]
    log = os.environ["FAKE_ORCA_LOG"]
    prior = []
    if os.path.exists(log):
        with open(log) as f:
            prior = [json.loads(ln) for ln in f if ln.strip()]
    with open(log, "a") as f:
        f.write(json.dumps(argv, ensure_ascii=False) + "\n")

    pos = [a for a in argv if not a.startswith("-")]
    group, sub = (pos + ["", ""])[:2]
    fails = set(filter(None, os.environ.get("FAKE_ORCA_FAIL", "").split(",")))
    if sub in fails or group in fails:
        sys.stderr.write(f"fake orca: forced failure of {sub}\n")
        sys.exit(1)

    def nth(g, s):
        return 1 + sum(1 for p in prior if p[:2] == [g, s])

    def flag(name, default=None):
        return argv[argv.index(name) + 1] if name in argv else default

    if group == "repo" and sub == "list":
        out({"repos": [{"id": "repo_1", "path": os.getcwd(),
                        "displayName": "fake"}]})
    if group == "orchestration":
        if sub == "run-create":
            out({"run": {"id": "run_x"}})
        if sub == "task-create":
            out({"task": {"id": f"task_{nth(group, sub)}"}})
        if sub == "worker-start":
            out({"dispatchId": f"disp_{nth(group, sub)}",
                 "state": "dispatched"})
        if sub == "worker-show":
            out({"dispatch": {"id": flag("--dispatch"),
                              "state": os.environ.get(
                                  "FAKE_ORCA_WORKER_STATE", "running")}})
        if sub == "worker-release":
            out({"released": True})
        if sub == "check":
            mf = os.environ.get("FAKE_ORCA_MESSAGES")
            msgs = []
            if mf and os.path.exists(mf):
                with open(mf) as f:
                    msgs = json.load(f)
                os.remove(mf)
            out({"messages": msgs,
                 **({"deliveryId": "del_1"} if msgs else {})})
        if sub == "task-list":
            out({"tasks": []})
    if group == "worktree":
        if sub == "create":
            n = nth(group, sub)
            base = os.environ.get("FAKE_ORCA_DIR") or os.path.dirname(log)
            path = os.path.join(base, f"wt-{n}")
            os.makedirs(path, exist_ok=True)
            out({"worktree": {"id": f"wt_{n}",
                              "branch": f"refs/heads/{flag('--name')}",
                              "path": path}})
        if sub in ("rm", "current", "list"):
            out({"worktrees": []} if sub == "list" else {})
    if group == "terminal":
        if sub == "create":
            out({"terminal": {"handle": f"term_{nth(group, sub)}"}})
        if sub == "read":
            sf = os.environ.get("FAKE_ORCA_SCREEN")
            lines = []
            if sf and os.path.exists(sf):
                with open(sf) as f:
                    lines = f.read().splitlines()
            out({"tail": lines})
        if sub in ("send", "wait", "close"):
            out({})
        if sub == "list":
            out({"terminals": [{"handle": "term_1"}]})
    sys.stderr.write(f"fake orca: unhandled {argv}\n")
    sys.exit(2)


if __name__ == "__main__":
    main()
