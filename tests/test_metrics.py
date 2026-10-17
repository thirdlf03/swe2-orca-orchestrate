"""metrics readers against small synthetic fixtures (no real data)."""
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
from orchlib import metrics  # noqa: E402

PRE = ("You are a dispatched worker in an Orca run.\n"
       "Your task ID is: {tid}\nDo the thing.")


def _jsonl(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


class Base(unittest.TestCase):
    ENV = ("ORCH_DEVIN_DB", "ORCH_CLAUDE_PROJECTS", "ORCH_CODEX_HOME",
           "ORCH_OPENCODE_DB")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = self.tmp.name
        self._old = {k: os.environ.get(k) for k in self.ENV}
        for k in self.ENV:
            os.environ[k] = os.path.join(self.d, "missing-" + k)
        metrics.clear_cache()

    def tearDown(self):
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        metrics.clear_cache()
        self.tmp.cleanup()


class TestDevin(Base):
    def _db(self):
        p = os.path.join(self.d, "sessions.db")
        db = sqlite3.connect(p)
        db.executescript("""
            CREATE TABLE sessions (id TEXT PRIMARY KEY, model TEXT,
              created_at INTEGER, last_activity_at INTEGER);
            CREATE TABLE message_nodes (row_id INTEGER PRIMARY KEY,
              session_id TEXT, chat_message TEXT, created_at INTEGER);
            CREATE INDEX idx_message_nodes_session ON message_nodes(session_id);
            CREATE TABLE prompt_history (id INTEGER PRIMARY KEY,
              content TEXT, timestamp INTEGER, session_id TEXT);
        """)
        db.executemany("INSERT INTO sessions VALUES (?,?,?,?)", [
            ("s1", "swe-2-high", 1000, 2000),
            ("s2", "swe-2-medium", 1000, 1500),
            ("s3", "swe-2-high", 1000, 1500)])
        db.executemany(
            "INSERT INTO prompt_history (content,timestamp,session_id) "
            "VALUES (?,?,?)", [
                (PRE.format(tid="task_a"), 1000, "s1"),
                (PRE.format(tid="task_other"), 1001, "s1"),  # first wins
                (PRE.format(tid="task_b"), 1000, "s2"),
                ("plain prompt task ID is: task_c", 1000, "s3")])

        def am(i, o, ms):
            return json.dumps({"role": "assistant", "metadata": {
                "metrics": {"input_tokens": i, "output_tokens": o,
                            "total_time_ms": ms}}})
        db.executemany(
            "INSERT INTO message_nodes (session_id,chat_message,created_at) "
            "VALUES (?,?,?)", [
                ("s1", am(100, 10, 1000), 1),
                ("s1", am(200, 20, 2000), 2),
                ("s1", json.dumps({"role": "user", "metadata": {}}), 3),
                ("s1", "not json", 4),
                ("s2", am(5, 1, 50), 1),
                ("s3", am(9, 9, 9), 1)])
        db.commit()
        db.close()
        os.environ["ORCH_DEVIN_DB"] = p

    def test_join_and_sum(self):
        self._db()
        r = metrics.collect("devin_sessions_db")
        self.assertEqual(set(r), {"task_a", "task_b"})   # s3 lacks preamble
        a = r["task_a"]
        self.assertEqual((a["in_tok"], a["out_tok"], a["gen_ms"]),
                         (300, 30, 3000))
        self.assertEqual((a["agent"], a["session"], a["model"]),
                         ("devin", "s1", "swe-2-high"))
        self.assertEqual((a["started"], a["ended"]), (1000.0, 2000.0))

    def test_filters(self):
        self._db()
        self.assertEqual(set(metrics.collect("devin_sessions_db",
                                             task_ids={"task_b"})),
                         {"task_b"})
        self.assertEqual(set(metrics.collect("devin_sessions_db",
                                             since=1800)), {"task_a"})

    def test_missing_db(self):
        self.assertEqual(metrics.collect("devin_sessions_db"), {})


class TestClaude(Base):
    def _write(self, name, tid, extra_users=0, preamble=True):
        msg_id = 0

        def asst(i, o, ts, mid, model="claude-x"):
            return {"type": "assistant", "timestamp": ts, "message": {
                "id": mid, "model": model, "usage": {
                    "input_tokens": i, "output_tokens": o,
                    "cache_read_input_tokens": 1000,
                    "cache_creation_input_tokens": 100}}}
        rows = [{"type": "permission-mode", "permissionMode": "bypass"}]
        for k in range(extra_users):
            rows.append({"type": "user", "timestamp": "2026-01-01T00:00:00Z",
                         "message": {"role": "user", "content": "hi"}})
        if preamble:
            rows.append({"type": "user", "timestamp": "2026-01-01T00:00:00Z",
                         "message": {"role": "user", "content": [
                             {"type": "text", "text": PRE.format(tid=tid)}]}})
        rows += [
            # one API response split into two lines with repeated usage
            asst(2, 50, "2026-01-01T00:00:05Z", "m1"),
            asst(2, 50, "2026-01-01T00:00:06Z", "m1"),
            asst(3, 70, "2026-01-01T00:01:00Z", "m2"),
            {"type": "assistant", "timestamp": "2026-01-01T00:01:01Z",
             "message": {"id": "m3", "model": "<synthetic>", "usage": {
                 "input_tokens": 0, "output_tokens": 0}}},
            {"type": "system", "subtype": "turn_duration",
             "durationMs": 42000, "timestamp": "2026-01-01T00:01:02Z"},
        ]
        _jsonl(os.path.join(self.d, "claude", "-proj", name + ".jsonl"), rows)

    def test_dedupe_cache_tokens_turn_duration(self):
        os.environ["ORCH_CLAUDE_PROJECTS"] = os.path.join(self.d, "claude")
        self._write("uuid1", "task_c1")
        _jsonl(os.path.join(self.d, "claude", "-proj", "uuid1", "subagents",
                            "agent-1.jsonl"),
               [{"type": "assistant", "timestamp": "2026-01-01T00:00:30Z",
                 "message": {"id": "x", "usage": {"input_tokens": 1,
                                                  "output_tokens": 4}}}])
        r = metrics.collect("claude_projects")
        a = r["task_c1"]
        self.assertEqual(a["in_tok"], 1102 + 1103 + 1)   # cache incl, deduped
        self.assertEqual(a["out_tok"], 50 + 70 + 4)
        self.assertEqual(a["gen_ms"], 42000)
        self.assertEqual(a["model"], "claude-x")      # <synthetic> skipped
        self.assertEqual(a["session"], "uuid1")
        self.assertEqual(a["ended"] - a["started"], 62)

    def test_early_stop_and_quoted_preamble(self):
        os.environ["ORCH_CLAUDE_PROJECTS"] = os.path.join(self.d, "claude")
        self._write("late", "task_late", extra_users=metrics._USER_SCAN)
        self._write("none", "x", preamble=False)
        _jsonl(os.path.join(self.d, "claude", "-proj", "quote.jsonl"), [
            {"type": "user", "message": {"content":
             "a dispatched worker prompt has `task ID is: <id>`"}}])
        self.assertEqual(metrics.collect("claude_projects"), {})

    def test_since_mtime(self):
        os.environ["ORCH_CLAUDE_PROJECTS"] = os.path.join(self.d, "claude")
        self._write("old", "task_old")
        p = os.path.join(self.d, "claude", "-proj", "old.jsonl")
        os.utime(p, (1000, 1000))
        self.assertEqual(metrics.collect("claude_projects",
                                         since=time.time() - 60), {})
        self.assertIn("task_old", metrics.collect("claude_projects"))


class TestCodex(Base):
    def _write(self, home, tid, dur=True):
        rows = [
            {"timestamp": "2026-01-01T00:00:00Z", "type": "session_meta",
             "payload": {"id": "cx-1", "cwd": "/w"}},
            {"timestamp": "2026-01-01T00:00:00Z", "type": "response_item",
             "payload": {"type": "message", "role": "user", "content": [
                 {"type": "input_text", "text": "<environment_context/>"},
                 {"type": "input_text", "text": PRE.format(tid=tid)}]}},
            {"timestamp": "2026-01-01T00:00:01Z", "type": "turn_context",
             "payload": {"model": "gpt-x"}},
            {"timestamp": "2026-01-01T00:00:10Z", "type": "event_msg",
             "payload": {"type": "token_count", "info": {"total_token_usage":
                         {"input_tokens": 100, "cached_input_tokens": 80,
                          "output_tokens": 10}}}},
            {"timestamp": "2026-01-01T00:00:20Z", "type": "event_msg",
             "payload": {"type": "token_count", "info": {"total_token_usage":
                         {"input_tokens": 300, "cached_input_tokens": 200,
                          "output_tokens": 40}}}},
            {"timestamp": "2026-01-01T00:00:30Z", "type": "event_msg",
             "payload": {"type": "token_count", "info": None}},
        ]
        if dur:
            rows.append({"timestamp": "2026-01-01T00:00:31Z",
                         "type": "event_msg", "payload": {
                             "type": "task_complete", "duration_ms": 25000}})
        _jsonl(os.path.join(home, "sessions", "2026", "01", "01",
                            f"rollout-2026-01-01-{tid}.jsonl"), rows)

    def test_cumulative_and_multi_home(self):
        h1, h2 = os.path.join(self.d, "c1"), os.path.join(self.d, "c2")
        self._write(h1, "task_x")
        self._write(h2, "task_y", dur=False)
        os.environ["ORCH_CODEX_HOME"] = f"{h1}:{h2}"
        r = metrics.collect("codex_rollout")
        self.assertEqual(set(r), {"task_x", "task_y"})
        x = r["task_x"]
        self.assertEqual((x["in_tok"], x["out_tok"], x["gen_ms"]),
                         (300, 40, 25000))
        self.assertEqual((x["model"], x["session"], x["agent"]),
                         ("gpt-x", "cx-1", "codex"))
        self.assertEqual(r["task_y"]["gen_ms"], 30000)   # last - first


class TestOpencode(Base):
    def test_v2_with_child(self):
        p = os.path.join(self.d, "oc.db")
        db = sqlite3.connect(p)
        db.executescript("""
            CREATE TABLE session_v2 (id TEXT PRIMARY KEY, parent_id TEXT,
              time_created INTEGER, time_updated INTEGER);
            CREATE TABLE session_message (id TEXT PRIMARY KEY,
              session_id TEXT, type TEXT, seq INTEGER, time_created INTEGER,
              data TEXT);
        """)
        db.executemany("INSERT INTO session_v2 VALUES (?,?,?,?)", [
            ("ses_a", None, 1_000_000, 1_060_000),
            ("ses_child", "ses_a", 1_010_000, 1_020_000),
            ("ses_z", None, 1_000_000, 1_001_000)])

        def asst(i, o, c0, c1):
            return json.dumps({"model": {"id": "beam"}, "time": {
                "created": c0, "completed": c1}, "tokens": {
                "input": i, "output": o, "reasoning": 1,
                "cache": {"read": 10, "write": 0}}})
        db.executemany("INSERT INTO session_message VALUES (?,?,?,?,?,?)", [
            ("1", "ses_a", "user", 1, 0,
             json.dumps({"text": PRE.format(tid="task_o")})),
            ("2", "ses_a", "assistant", 2, 0, asst(100, 5, 0, 1500)),
            ("3", "ses_a", "assistant", 3, 0, asst(200, 5, 0, 500)),
            ("4", "ses_child", "user", 1, 0, json.dumps({"text": "sub"})),
            ("5", "ses_child", "assistant", 2, 0, asst(7, 2, 0, 99)),
            ("6", "ses_z", "user", 1, 0, json.dumps({"text": "hello"})),
            ("7", "ses_z", "assistant", 2, 0, asst(1, 1, 0, 1))])
        db.commit()
        db.close()
        os.environ["ORCH_OPENCODE_DB"] = p
        r = metrics.collect("opencode_sqlite")
        self.assertEqual(set(r), {"task_o"})
        o = r["task_o"]
        self.assertEqual(o["in_tok"], 110 + 210 + 17)
        self.assertEqual(o["out_tok"], 6 + 6 + 3)
        self.assertEqual(o["gen_ms"], 2000)
        self.assertEqual((o["model"], o["session"]), ("beam", "ses_a"))
        self.assertEqual(o["started"], 1000.0)

    def test_legacy(self):
        p = os.path.join(self.d, "oc.db")
        db = sqlite3.connect(p)
        db.executescript("""
            CREATE TABLE session (id TEXT PRIMARY KEY, parent_id TEXT,
              time_created INTEGER, time_updated INTEGER);
            CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT,
              time_created INTEGER, data TEXT);
            CREATE TABLE part (id TEXT PRIMARY KEY, message_id TEXT,
              session_id TEXT, data TEXT);
        """)
        db.execute("INSERT INTO session VALUES ('s', NULL, 5000, 9000)")
        db.executemany("INSERT INTO message VALUES (?,?,?,?)", [
            ("m1", "s", 1, json.dumps({"role": "user"})),
            ("m2", "s", 2, json.dumps({"role": "assistant", "modelID": "q",
                                       "tokens": {"input": 3, "output": 4},
                                       "time": {"created": 1}}))])
        db.execute("INSERT INTO part VALUES ('p1','m1','s',?)",
                   (json.dumps({"type": "text",
                                "text": PRE.format(tid="task_l")}),))
        db.commit()
        db.close()
        os.environ["ORCH_OPENCODE_DB"] = p
        o = metrics.collect("opencode_sqlite")["task_l"]
        self.assertEqual((o["in_tok"], o["out_tok"], o["model"]), (3, 4, "q"))
        self.assertEqual(o["gen_ms"], 4000)   # no completed -> span


class TestApi(Base):
    def test_unknown_reader_and_collect_all(self):
        self.assertEqual(metrics.collect("nope"), {})
        self.assertEqual(metrics.collect_all(list(metrics.READERS)), {})

    def test_corrupt_db_failsoft(self):
        p = os.path.join(self.d, "bad.db")
        with open(p, "w") as f:
            f.write("not sqlite")
        os.environ["ORCH_DEVIN_DB"] = p
        os.environ["ORCH_OPENCODE_DB"] = p
        self.assertEqual(metrics.collect("devin_sessions_db"), {})
        self.assertEqual(metrics.collect("opencode_sqlite"), {})


if __name__ == "__main__":
    unittest.main()
