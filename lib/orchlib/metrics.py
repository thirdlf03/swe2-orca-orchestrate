"""Per-worker transcript metrics across agent CLIs (advisory).

Every Orca-dispatched worker's first prompt carries the worker preamble
("... dispatched worker ... task ID is: <id>"), whatever agent runs it,
so each agent's local transcript store joins to orchestration.db by
task id. This generalizes bin/orch `_session_task_map` /
`_worker_metrics_all` (devin only) to claude / codex / opencode.

    collect(reader, task_ids=None, since=None) -> {task_id: {
        "agent", "session", "model", "in_tok", "out_tok", "gen_ms",
        "started", "ended"}}

Token semantics per agent (each store counts differently — measured):
  devin     in_tok = metrics.input_tokens (cache_read_tokens is reported
            separately and NOT added — kept identical to bin/orch so
            existing weight history stays comparable)
  claude    in_tok = input + cache_read_input + cache_creation_input
            (raw input_tokens is ~2/turn with caching on; without the
            cache fields the number is meaningless)
  codex     in_tok = total_token_usage.input_tokens (already includes
            cached_input_tokens; output already includes reasoning)
  opencode  in_tok = input + cache.read + cache.write; out = output +
            reasoning
gen_ms: devin = sum of assistant metrics.total_time_ms (sessions.db
wall clock is unreliable: 99/138 workers measured dur=0). claude = sum
of system/turn_duration; codex = sum of task_complete.duration_ms;
opencode = sum of assistant (completed - created). Fallback for all
but devin: last_ts - first_ts of the transcript.
started/ended are epoch seconds (float).

Fail-soft: any error -> {} or the session is skipped. Never raises.
Paths are overridable by env (tests point them at synthetic fixtures):
ORCH_DEVIN_DB, ORCH_CLAUDE_PROJECTS, ORCH_CODEX_HOME (colon separated),
ORCH_OPENCODE_DB.
"""
import datetime
import glob
import json
import os
import re
import sqlite3

# Orca ids look like task_ab12...; the strict charset keeps prose that
# merely *quotes* the preamble ("task ID is: <id>") from joining.
_TASK_RE = re.compile(r"task ID is: ([A-Za-z0-9][\w.-]*)")
_PREAMBLE = "dispatched worker"
# The preamble is the first prompt; give up on a transcript after this
# many user turns (claude/codex files reach hundreds of MB).
_USER_SCAN = 20
_CHUNK = 500   # sqlite IN (...) chunk; well under SQLITE_MAX_VARIABLE_NUMBER

_CACHE = {}


def _task_id(text):
    """First preamble task id in text, or None."""
    if not text or _PREAMBLE not in text:
        return None
    m = _TASK_RE.search(text)
    return m.group(1) if m else None


def _ts(s):
    """ISO-8601 (with Z) -> epoch seconds, None on failure."""
    try:
        return datetime.datetime.fromisoformat(
            s.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def _rec(agent, session, model, in_tok, out_tok, gen_ms, started, ended):
    return {"agent": agent, "session": session, "model": model,
            "in_tok": int(in_tok or 0), "out_tok": int(out_tok or 0),
            "gen_ms": int(gen_ms or 0), "started": started, "ended": ended}


def _merge_into(out, tid, r):
    """Several sessions may join one task (resume / re-dispatch): sum
    usage like bin/orch did, keep the earliest session as the key."""
    a = out.get(tid)
    if a is None:
        out[tid] = r
        return
    for k in ("in_tok", "out_tok", "gen_ms"):
        a[k] += r[k]
    if r["started"] is not None and (a["started"] is None
                                     or r["started"] < a["started"]):
        a["session"], a["started"] = r["session"], r["started"]
    if r["ended"] is not None and (a["ended"] is None
                                   or r["ended"] > a["ended"]):
        a["ended"] = r["ended"]
        a["model"] = r["model"] or a["model"]


def _ro(path, immutable=True):
    q = "mode=ro&immutable=1" if immutable else "mode=ro"
    return sqlite3.connect(f"file:{path}?{q}", uri=True)


def _chunks(xs):
    xs = list(xs)
    for i in range(0, len(xs), _CHUNK):
        yield xs[i:i + _CHUNK]


# ---------- devin: ~/.local/share/devin/cli/sessions.db ----------

def _devin_db():
    return os.environ.get("ORCH_DEVIN_DB") or os.path.expanduser(
        "~/.local/share/devin/cli/sessions.db")


def _read_devin(task_ids, since):
    """sessions.db is ~6.5GB: join via prompt_history first, then read
    message_nodes only for joined sessions through the session index
    (a full message_nodes scan is 10-20s)."""
    path = _devin_db()
    if not os.path.exists(path):
        return {}
    db = _ro(path)
    tmap = {}
    for sid, content in db.execute(
            "SELECT session_id, content FROM prompt_history "
            "WHERE content LIKE ? ORDER BY id", (f"%{_PREAMBLE}%",)):
        if sid in tmap:
            continue
        tid = _task_id(content)
        if tid and (task_ids is None or tid in task_ids):
            tmap[sid] = tid
    if not tmap:
        return {}
    sess = {}
    for ch in _chunks(tmap):
        q = ",".join("?" * len(ch))
        for sid, mdl, c, la in db.execute(
                "SELECT id, model, created_at, last_activity_at FROM sessions "
                f"WHERE id IN ({q})", ch):
            if since is not None and (la or 0) < since:
                continue
            sess[sid] = (mdl, c, la)
    agg = {}
    for ch in _chunks(sess):
        q = ",".join("?" * len(ch))
        for sid, cm in db.execute(
                "SELECT session_id, chat_message FROM message_nodes "
                f"WHERE session_id IN ({q})", ch):
            try:
                j = json.loads(cm)
            except Exception:
                continue
            md = j.get("metadata") or {}
            if j.get("role") != "assistant" or not md.get("metrics"):
                continue
            m = md["metrics"]
            a = agg.setdefault(sid, [0, 0, 0])
            a[0] += m.get("input_tokens") or 0
            a[1] += m.get("output_tokens") or 0
            a[2] += m.get("total_time_ms") or 0
    out = {}
    for sid, (mdl, c, la) in sess.items():
        a = agg.get(sid)
        if not a:
            continue   # joined but no assistant turn yet (zombie dispatch)
        _merge_into(out, tmap[sid], _rec(
            "devin", sid, mdl, a[0], a[1], a[2],
            float(c) if c else None, float(la) if la else None))
    return out


# ---------- claude: ~/.claude/projects/<cwd-slug>/<uuid>.jsonl ----------

def _claude_root():
    return os.environ.get("ORCH_CLAUDE_PROJECTS") or os.path.expanduser(
        "~/.claude/projects")


def _user_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(x.get("text") or "" for x in content
                         if isinstance(x, dict) and x.get("type") == "text")
    return ""


def _claude_usage(path, need_join):
    """One pass over a transcript. Returns (task_id, usage dict) or
    (None, None) when need_join and the preamble isn't in the first
    _USER_SCAN user lines. An API response is written as one line per
    content block with the same message.id and repeated usage, so usage
    is deduped by message id (measured: 1372/1930 ids duplicated)."""
    tid, users = None, 0
    usage, model = {}, None
    turn_ms, first, last = 0, None, None
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                j = json.loads(line)
            except Exception:
                continue
            t = j.get("type")
            ts = _ts(j["timestamp"]) if j.get("timestamp") else None
            if ts is not None:
                first = ts if first is None else first
                last = ts
            if need_join and tid is None:
                if t != "user":
                    continue
                users += 1
                tid = _task_id(_user_text((j.get("message") or {})
                                          .get("content")))
                if tid is None:
                    if users >= _USER_SCAN:
                        return None, None
                    continue
            if t == "assistant":
                m = j.get("message") or {}
                u = m.get("usage")
                if u:
                    usage[m.get("id") or id(j)] = (
                        (u.get("input_tokens") or 0)
                        + (u.get("cache_read_input_tokens") or 0)
                        + (u.get("cache_creation_input_tokens") or 0),
                        u.get("output_tokens") or 0)
                if m.get("model") and not m["model"].startswith("<"):
                    model = m["model"]   # skip "<synthetic>" filler turns
            elif t == "system" and j.get("subtype") == "turn_duration":
                turn_ms += j.get("durationMs") or 0
    if need_join and tid is None:
        return None, None
    return tid, {"in": sum(x[0] for x in usage.values()),
                 "out": sum(x[1] for x in usage.values()),
                 "model": model, "turn_ms": turn_ms,
                 "first": first, "last": last}


def _read_claude(task_ids, since):
    out = {}
    for path in glob.glob(os.path.join(_claude_root(), "*", "*.jsonl")):
        try:
            if since is not None and os.path.getmtime(path) < since:
                continue
            tid, u = _claude_usage(path, True)
            if tid is None or (task_ids is not None and tid not in task_ids):
                continue
            sid = os.path.splitext(os.path.basename(path))[0]
            # Task-tool subagents bill to the worker but live beside it.
            for sp in glob.glob(os.path.join(os.path.dirname(path), sid,
                                             "subagents", "*.jsonl")):
                try:
                    _, su = _claude_usage(sp, False)
                    u["in"] += su["in"]
                    u["out"] += su["out"]
                except Exception:
                    pass
            gen = u["turn_ms"] or (
                (u["last"] - u["first"]) * 1000
                if u["first"] is not None and u["last"] is not None else 0)
            _merge_into(out, tid, _rec("claude", sid, u["model"], u["in"],
                                       u["out"], gen, u["first"], u["last"]))
        except Exception:
            continue
    return out


# ---------- codex: $CODEX_HOME/sessions/YYYY/MM/DD/rollout-*.jsonl ----------

def _codex_homes():
    v = os.environ.get("ORCH_CODEX_HOME")
    if v:
        homes = v.split(":")
    else:
        homes = [os.environ.get("CODEX_HOME") or "", "~/.codex",
                 "~/Library/Application Support/orca/codex-runtime-home/home"]
    seen, out = set(), []
    for h in homes:
        if not h:
            continue
        r = os.path.realpath(os.path.expanduser(h))
        if r not in seen:
            seen.add(r)
            out.append(r)
    return out


def _codex_usage(path):
    """total_token_usage is cumulative per rollout, so the last
    token_count event is the session total."""
    tid, users, sid, model = None, 0, None, None
    tot, dur, first, last = None, 0, None, None
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                j = json.loads(line)
            except Exception:
                continue
            t, p = j.get("type"), j.get("payload")
            if not isinstance(p, dict):
                p = {}
            ts = _ts(j["timestamp"]) if j.get("timestamp") else None
            if ts is not None:
                first = ts if first is None else first
                last = ts
            if t == "session_meta":
                sid = sid or p.get("id") or p.get("session_id")
                continue
            if t == "turn_context":
                model = p.get("model") or model
                continue
            if tid is None:
                text = None
                if t == "response_item" and p.get("type") == "message" \
                        and p.get("role") == "user":
                    text = "\n".join(c.get("text") or "" for c in
                                     p.get("content") or []
                                     if isinstance(c, dict))
                elif t == "event_msg" and p.get("type") == "user_message":
                    text = p.get("message") or ""
                if text is None:
                    continue
                users += 1
                tid = _task_id(text)
                if tid is None and users >= _USER_SCAN:
                    return None, None
                continue
            if t == "event_msg":
                if p.get("type") == "token_count":
                    u = (p.get("info") or {}).get("total_token_usage")
                    if u:
                        tot = u
                elif p.get("type") == "task_complete":
                    dur += p.get("duration_ms") or 0
    if tid is None or tot is None:
        return None, None   # no billed turn (e.g. auth failure) — like devin
    gen = dur or ((last - first) * 1000
                  if first is not None and last is not None else 0)
    return tid, _rec("codex", sid or os.path.basename(path), model,
                     tot.get("input_tokens"), tot.get("output_tokens"),
                     gen, first, last)


def _read_codex(task_ids, since):
    out = {}
    for home in _codex_homes():
        for path in glob.glob(os.path.join(home, "sessions", "*", "*", "*",
                                           "rollout-*.jsonl")):
            try:
                if since is not None and os.path.getmtime(path) < since:
                    continue
                tid, r = _codex_usage(path)
                if tid and (task_ids is None or tid in task_ids):
                    _merge_into(out, tid, r)
            except Exception:
                continue
    return out


# ---------- opencode: ~/.local/share/opencode/opencode.db ----------

def _opencode_db():
    return os.environ.get("ORCH_OPENCODE_DB") or os.path.expanduser(
        "~/.local/share/opencode/opencode.db")


def _oc_tokens(d):
    tk = d.get("tokens") or {}
    c = tk.get("cache") or {}
    return ((tk.get("input") or 0) + (c.get("read") or 0)
            + (c.get("write") or 0),
            (tk.get("output") or 0) + (tk.get("reasoning") or 0))


def _oc_span(d):
    tm = d.get("time") or {}
    if tm.get("created") is not None and tm.get("completed") is not None:
        return max(0, tm["completed"] - tm["created"])
    return 0


def _read_opencode(task_ids, since):
    """Two schemas seen: v2 (session_v2 + session_message{type,data}) —
    the live one locally — and legacy (session + message{data.role} +
    part{data.text}). WAL is live while opencode runs, so open without
    immutable=1 first (immutable would hide un-checkpointed rows)."""
    path = _opencode_db()
    if not os.path.exists(path):
        return {}
    try:
        db = _ro(path, immutable=False)
        tables = {r[0] for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    except Exception:
        db = _ro(path)
        tables = {r[0] for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    out = {}
    if {"session_v2", "session_message"} <= tables:
        _oc_v2(db, task_ids, since, out)
    if {"session", "message", "part"} <= tables:
        _oc_legacy(db, task_ids, since, out)
    return out


def _oc_sessions(db, table, since):
    q = f"SELECT id, parent_id, time_created, time_updated FROM {table}"
    rows = {}
    for sid, parent, c, u in db.execute(q):
        rows[sid] = (parent, c, u)
    keep = {s for s, (_, _, u) in rows.items()
            if since is None or (u or 0) / 1000 >= since}
    return rows, keep


def _oc_finish(out, task_ids, rows, tmap, agg):
    """Roll child (subagent) sessions into their joined root."""
    def root(s):
        seen = set()
        while s in rows and rows[s][0] and s not in seen:
            seen.add(s)
            s = rows[s][0]
        return s
    per = {}
    for sid, a in agg.items():
        r = root(sid) if sid not in tmap else sid
        if r not in tmap:
            continue
        p = per.setdefault(r, [0, 0, 0, None])
        p[0] += a[0]
        p[1] += a[1]
        if r == sid:
            p[2] += a[2]
            p[3] = a[3] or p[3]
    for sid, (i, o, g, mdl) in per.items():
        tid = tmap[sid]
        if task_ids is not None and tid not in task_ids:
            continue
        _, c, u = rows[sid]
        if not g and c and u:
            g = u - c
        _merge_into(out, tid, _rec("opencode", sid, mdl, i, o, g,
                                   c / 1000 if c else None,
                                   u / 1000 if u else None))


def _oc_v2(db, task_ids, since, out):
    rows, keep = _oc_sessions(db, "session_v2", since)
    tmap, users, agg = {}, {}, {}
    for sid, typ, data in db.execute(
            "SELECT session_id, type, data FROM session_message "
            "ORDER BY session_id, seq"):
        if sid not in keep and rows.get(sid, (None,))[0] is None:
            continue
        try:
            d = json.loads(data)
        except Exception:
            continue
        if typ == "user" and sid not in tmap and users.get(sid, 0) < _USER_SCAN:
            users[sid] = users.get(sid, 0) + 1
            tid = _task_id(d.get("text") or "")
            if tid:
                tmap[sid] = tid
        elif typ == "assistant":
            i, o = _oc_tokens(d)
            a = agg.setdefault(sid, [0, 0, 0, None])
            a[0] += i
            a[1] += o
            a[2] += _oc_span(d)
            a[3] = (d.get("model") or {}).get("id") or a[3]
    _oc_finish(out, task_ids, rows, {s: t for s, t in tmap.items()
                                     if s in keep}, agg)


def _oc_legacy(db, task_ids, since, out):
    rows, keep = _oc_sessions(db, "session", since)
    tmap, users, agg = {}, {}, {}
    for sid, data in db.execute(
            "SELECT session_id, data FROM message ORDER BY session_id, "
            "time_created"):
        try:
            d = json.loads(data)
        except Exception:
            continue
        if d.get("role") == "assistant":
            i, o = _oc_tokens(d)
            a = agg.setdefault(sid, [0, 0, 0, None])
            a[0] += i
            a[1] += o
            a[2] += _oc_span(d)
            a[3] = d.get("modelID") or a[3]
    # user prompt text lives in part rows of type "text"
    for sid, mid, data in db.execute(
            "SELECT p.session_id, p.message_id, p.data FROM part p "
            "JOIN message m ON m.id = p.message_id "
            "WHERE json_extract(m.data, '$.role') = 'user' "
            "ORDER BY p.session_id, m.time_created, p.id"):
        if sid in tmap or sid not in keep or users.get(sid, 0) >= _USER_SCAN:
            continue
        users[sid] = users.get(sid, 0) + 1
        try:
            tid = _task_id(json.loads(data).get("text") or "")
        except Exception:
            continue
        if tid:
            tmap[sid] = tid
    _oc_finish(out, task_ids, rows, tmap, agg)


# ---------- public API ----------

READERS = {
    "devin_sessions_db": _read_devin,
    "claude_projects": _read_claude,
    "codex_rollout": _read_codex,
    "opencode_sqlite": _read_opencode,
}


def collect(reader, task_ids=None, since=None):
    """task_id -> {"agent","session","model","in_tok","out_tok","gen_ms",
    "started","ended"} for one reader. since (epoch s) skips sessions
    not touched since then. Cached per process; the unfiltered result
    is reused for later filtered calls. Never raises."""
    fn = READERS.get(reader)
    if fn is None:
        return {}
    tset = frozenset(task_ids) if task_ids is not None else None
    full = _CACHE.get((reader, None, since))
    if full is not None:
        res = full if tset is None else {
            t: v for t, v in full.items() if t in tset}
    else:
        key = (reader, tset, since)
        if key not in _CACHE:
            try:
                _CACHE[key] = fn(tset, since)
            except Exception:
                _CACHE[key] = {}
        res = _CACHE[key]
    return {t: dict(v) for t, v in res.items()}   # callers may mutate


def collect_all(readers, task_ids=None, since=None):
    """Merge several readers. A task seen by two readers (shouldn't
    happen) keeps the record that ended last."""
    out = {}
    for r in readers:
        for tid, v in collect(r, task_ids, since).items():
            o = out.get(tid)
            if o is None or (v["ended"] or 0) > (o["ended"] or 0):
                out[tid] = v
    return out


def clear_cache():
    _CACHE.clear()
