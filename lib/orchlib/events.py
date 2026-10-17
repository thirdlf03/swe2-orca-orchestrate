"""orch's own event log: `.orch/events.jsonl`.

Every weight estimate and before/after comparison in the month report had
to be reverse-engineered from Devin's private sessions.db (6.5GB, schema
undocumented, wall-clock durations unreliable). An append-only log that
orch writes itself works for every agent: spawn -> done gives a duration
even when no transcript reader exists for that CLI.
"""
import json
import os
import time

EVENTS_FILE = os.path.join(".orch", "events.jsonl")


def emit(kind, **fields):
    """Best-effort append; logging must never break a command."""
    try:
        os.makedirs(os.path.dirname(EVENTS_FILE), exist_ok=True)
        rec = {"ts": int(time.time()), "kind": kind}
        rec.update({k: v for k, v in fields.items() if v is not None})
        with open(EVENTS_FILE, "a") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def read(kinds=None):
    out = []
    try:
        with open(EVENTS_FILE) as f:
            for ln in f:
                try:
                    e = json.loads(ln)
                except ValueError:
                    continue
                if not kinds or e.get("kind") in kinds:
                    out.append(e)
    except OSError:
        pass
    return out


def durations():
    """task -> {"agent","model","spec","gen_min"} from spawn->done pairs
    (first done after the latest spawn of that dispatch)."""
    spawns, out = {}, {}
    for e in read(("spawn", "done")):
        d = e.get("dispatch")
        if not d:
            continue
        if e["kind"] == "spawn":
            spawns[d] = e
        elif d in spawns and e.get("task") not in out:
            s = spawns[d]
            out[s.get("task")] = {
                "agent": s.get("agent"), "model": s.get("model"),
                "spec": s.get("spec_len", 0),
                "gen_min": round((e["ts"] - s["ts"]) / 60)}
    return out
