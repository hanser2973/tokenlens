# -*- coding: utf-8 -*-
"""项目维度：各工具怎么解析出项目名，以及日聚合键的新旧格式兼容。"""
import json
import os
import tempfile
import unittest
from unittest import mock

import collector
import server
import sync


class ProjectNameTest(unittest.TestCase):
    def test_project_of_takes_last_segment(self):
        self.assertEqual(collector.project_of(r"C:\code\pythoncode\tokenlens"), "tokenlens")
        self.assertEqual(collector.project_of("/home/u/work/api-server/"), "api-server")

    def test_project_of_empty(self):
        self.assertEqual(collector.project_of(""), collector.UNKNOWN_PROJECT)
        self.assertEqual(collector.project_of(None), collector.UNKNOWN_PROJECT)

    def test_kimi_project_from_session_dir(self):
        """Kimi 的 wire.jsonl 里没有 cwd，只能从 wd_<项目>_<hash> 目录名取。"""
        p = r"C:\Users\z\.kimi-code\sessions\wd_codex-skin_f540b62cf6fb\session_x\agents\main\wire.jsonl"
        self.assertEqual(collector.kimi_project_of(p), "codex-skin")

    def test_kimi_project_missing(self):
        self.assertEqual(collector.kimi_project_of("/tmp/whatever.jsonl"), collector.UNKNOWN_PROJECT)

    def test_claude_dir_fallback(self):
        p = os.path.join("x", "projects", "c--code-pythoncode-tokenlens", "s.jsonl")
        self.assertEqual(collector._claude_dir_project(p), "tokenlens")


class ParserProjectTest(unittest.TestCase):
    def test_claude_uses_cwd(self):
        parse = collector.make_claude_parser({}, "/x/projects/c--foo/s.jsonl")
        ev = parse(json.dumps({
            "timestamp": "2026-07-28T10:00:00.000Z", "cwd": r"D:\work\billing-svc",
            "message": {"id": "m1", "model": "claude-sonnet-4-5",
                        "usage": {"input_tokens": 5, "output_tokens": 3}},
        }))
        self.assertEqual(ev["project"], "billing-svc")

    def test_claude_falls_back_to_dir_when_no_cwd(self):
        parse = collector.make_claude_parser({}, "/x/projects/c--code-tokenlens/s.jsonl")
        ev = parse(json.dumps({
            "timestamp": "2026-07-28T10:00:00.000Z",
            "message": {"id": "m2", "model": "m", "usage": {"input_tokens": 5, "output_tokens": 3}},
        }))
        self.assertEqual(ev["project"], "tokenlens")

    def test_codex_tracks_cwd_from_session_meta(self):
        parse = collector.make_codex_parser()
        self.assertIsNone(parse(json.dumps({
            "type": "session_meta", "payload": {"cwd": r"C:\code\sasasas", "model": "gpt-5.2"}})))
        ev = parse(json.dumps({
            "type": "event_msg", "timestamp": "2026-07-28T10:00:00.000Z",
            "payload": {"type": "token_count",
                        "info": {"last_token_usage": {"input_tokens": 10, "output_tokens": 4,
                                                      "cached_input_tokens": 2}}}}))
        self.assertEqual(ev["project"], "sasasas")
        self.assertEqual(ev["model"], "gpt-5.2")

    def test_kimi_parser_carries_project(self):
        parse = collector.make_kimi_parser(r"~/.kimi-code/sessions/wd_myapp_abc123/s/agents/main/wire.jsonl")
        ev = parse(json.dumps({"type": "usage.record", "model": "k2p7", "time": 1785000000000,
                               "usage": {"inputOther": 10, "output": 5, "inputCacheRead": 2}}))
        self.assertEqual(ev["project"], "myapp")


class AutoKeyCompatTest(unittest.TestCase):
    def test_four_part_key(self):
        self.assertEqual(collector.split_auto_key("2026-07-28|Claude Code|sonnet|tokenlens"),
                         ("2026-07-28", "Claude Code", "sonnet", "tokenlens"))

    def test_legacy_three_part_key_gets_unknown_project(self):
        """加项目维度之前采集的老数据仍要能读，只是项目未知。"""
        self.assertEqual(collector.split_auto_key("2026-07-28|Claude Code|sonnet"),
                         ("2026-07-28", "Claude Code", "sonnet", collector.UNKNOWN_PROJECT))

    def test_garbage_key(self):
        self.assertIsNone(collector.split_auto_key("nonsense"))

    def test_load_auto_records_mixes_old_and_new(self):
        tmp = tempfile.mkdtemp()
        with open(os.path.join(tmp, collector.AUTO_FILE), "w", encoding="utf-8") as f:
            json.dump({"2026-07-28|A|m|proj": {"input": 1, "output": 1, "cached": 0, "requests": 1},
                       "2026-07-27|A|m": {"input": 2, "output": 0, "cached": 0, "requests": 1}}, f)
        rows = collector.load_auto_records(tmp, {}, lambda *a: 0.0)
        got = {r["date"]: r["project"] for r in rows}
        self.assertEqual(got["2026-07-28"], "proj")
        self.assertEqual(got["2026-07-27"], collector.UNKNOWN_PROJECT)

    def test_peer_records_carry_project(self):
        peers = [{"device_id": "d1", "device_name": "台式机",
                  "days": {"2026-07-28|Kimi Code|k2p7|api-svc":
                           {"input": 5, "output": 2, "cached": 1, "requests": 1}}}]
        rows = sync.peer_records(peers, {}, lambda *a: 0.0)
        self.assertEqual(rows[0]["project"], "api-svc")


class ManualRecordProjectTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for p in (mock.patch.object(server, "DATA_DIR", self.tmp.name),
                  mock.patch.object(server, "DATA_FILE",
                                    os.path.join(self.tmp.name, "records.json")),
                  mock.patch.object(server, "maybe_auto_match", lambda *a, **k: None)):
            p.start()
            self.addCleanup(p.stop)

    def test_manual_record_without_project_defaults_to_unknown(self):
        """手动录入不填项目时，与采集不到工作目录的记录归到同一桶，别分裂成两个。"""
        server.save_records([{"id": 1, "date": "2026-07-28", "agent": "Cursor", "model": "m",
                              "requests": 1, "input": 1, "output": 1, "cached": 0, "cost": 0}])
        records, _ = server.merged_records()
        self.assertEqual(records[0]["project"], collector.UNKNOWN_PROJECT)


if __name__ == "__main__":
    unittest.main()
