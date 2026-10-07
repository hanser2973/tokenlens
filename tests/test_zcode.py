# -*- coding: utf-8 -*-
"""ZCode 采集源：SQLite model_usage 表解析、项目名、增量按请求 id 去重。"""
import json
import os
import sqlite3
import tempfile
import unittest
from unittest import mock

import collector


def _make_db(path, rows, sessions=None):
    """在 path 建一个最小结构的 zcode db.sqlite。

    rows: [(logical_request_id, model_id, status, completed_ms, started_ms,
            input, output, cache_creation, cache_read, session_id)]
    sessions: [(session_id, directory)]
    """
    con = sqlite3.connect(path)
    con.execute("""CREATE TABLE model_usage (
        logical_request_id TEXT, model_id TEXT, status TEXT,
        completed_at INTEGER, started_at INTEGER,
        input_tokens INTEGER, output_tokens INTEGER,
        cache_creation_input_tokens INTEGER, cache_read_input_tokens INTEGER,
        session_id TEXT)""")
    con.execute("CREATE TABLE session (id TEXT, directory TEXT)")
    for r in rows:
        con.execute("INSERT INTO model_usage VALUES (?,?,?,?,?,?,?,?,?,?)", r)
    for s in sessions or []:
        con.execute("INSERT INTO session (id, directory) VALUES (?,?)", s)
    con.commit()
    con.close()


def _row(rid, model="GLM-5.3-Flash", status="completed", completed=1787757754551,
         started=1787757747907, inp=100, out=50, cache_w=10, cache_r=200,
         sid="sess_1"):
    return (rid, model, status, completed, started, inp, out, cache_w, cache_r, sid)


class ZcodeParserTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "db.sqlite")

    def test_parses_completed_rows(self):
        _make_db(self.db, [_row("r1")], sessions=[("sess_1", "C:\\code\\my-proj")])
        evs = collector.parse_zcode_db(self.db)
        self.assertEqual(len(evs), 1)
        ev = evs[0]
        self.assertEqual(ev["model"], "glm-5.3-flash")   # 小写、去 provider 前缀
        self.assertEqual(ev["project"], "my-proj")       # 目录最后一段
        self.assertEqual(ev["input"], 110)               # input + cache_creation
        self.assertEqual(ev["output"], 50)
        self.assertEqual(ev["cached"], 200)
        self.assertEqual(ev["ts"], 1787757754.551)
        self.assertEqual(ev["_rid"], "r1")

    def test_skips_failed_and_zero_usage(self):
        _make_db(self.db, [
            _row("ok", inp=10, out=5, cache_w=0, cache_r=0),
            _row("failed", status="error", inp=999, out=999, cache_w=0, cache_r=0),
            _row("zero", inp=0, out=0, cache_w=0, cache_r=0),
        ])
        evs = collector.parse_zcode_db(self.db)
        self.assertEqual([e["_rid"] for e in evs], ["ok"])

    def test_missing_session_dir_is_unknown_project(self):
        _make_db(self.db, [_row("r1", sid="sess_gone")], sessions=[])
        evs = collector.parse_zcode_db(self.db)
        self.assertEqual(evs[0]["project"], collector.UNKNOWN_PROJECT)

    def test_completed_at_falls_back_to_started(self):
        _make_db(self.db, [_row("r1", completed=None, started=1787757747907)])
        evs = collector.parse_zcode_db(self.db)
        self.assertEqual(evs[0]["ts"], 1787757747.907)

    def test_missing_or_corrupt_db_returns_empty(self):
        self.assertEqual(collector.parse_zcode_db(os.path.join(self.tmp, "nope.sqlite")), [])
        with open(self.db, "wb") as f:
            f.write(b"not a database")
        self.assertEqual(collector.parse_zcode_db(self.db), [])


class ZcodeCollectTest(unittest.TestCase):
    """增量采集：同一批请求重复扫不重复计数，新行才被收。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "db.sqlite")
        _make_db(self.db, [_row("r1", inp=100, out=10, cache_w=0, cache_r=0)],
                 sessions=[("sess_1", "C:\\code\\proj-a")])
        src = collector.Source("ZCode", [self.db], "zcode", "sqlite")
        p = mock.patch.object(collector, "SOURCES", [src])
        p.start()
        self.addCleanup(p.stop)
        self.data = tempfile.mkdtemp()

    def _zcode_input_total(self):
        auto = collector._load_json(os.path.join(self.data, collector.AUTO_FILE), {})
        return sum(v["input"] for k, v in auto.items() if "|ZCode|" in k)

    def test_first_scan_collects_second_scan_dedups(self):
        s1 = collector.collect_once(self.data)
        self.assertEqual(s1["ZCode"]["events"], 1)
        self.assertEqual(self._zcode_input_total(), 100)
        # 再扫一次，库没变：不应新增
        s2 = collector.collect_once(self.data)
        self.assertEqual(s2.get("ZCode", {}).get("events"), 0)
        self.assertEqual(self._zcode_input_total(), 100)

    def test_new_rows_collected_old_not_double_counted(self):
        collector.collect_once(self.data)
        # 库里追加一行（模拟又调了一次模型）
        con = sqlite3.connect(self.db)
        con.execute("INSERT INTO model_usage VALUES (?,?,?,?,?,?,?,?,?,?)",
                    _row("r2", inp=200, out=20, cache_w=0, cache_r=0))
        con.commit()
        con.close()
        s = collector.collect_once(self.data)
        self.assertEqual(s["ZCode"]["events"], 1)       # 只收新的那条
        self.assertEqual(self._zcode_input_total(), 300)

    def test_seen_sqlite_pruned_by_age(self):
        seen = {"zcode:old": 1000.0, "zcode:fresh": 9_000_000_000.0}
        collector._prune_seen_sqlite(seen, now=9_000_000_000.0)
        self.assertNotIn("zcode:old", seen)
        self.assertIn("zcode:fresh", seen)


if __name__ == "__main__":
    unittest.main()
