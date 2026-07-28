# -*- coding: utf-8 -*-
"""采集器：Claude 去重的淘汰顺序、时间线的分钟桶快路径。"""
import json
import os
import tempfile
import unittest
from unittest import mock

import collector
import server


def _claude_line(mid, ts="2026-07-28T10:00:00.000Z", tokens=10):
    return json.dumps({
        "timestamp": ts,
        "message": {"id": mid, "model": "claude-sonnet-4-5",
                    "usage": {"input_tokens": tokens, "output_tokens": tokens,
                              "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}},
    })


class SeenClaudeLruTest(unittest.TestCase):
    """按 message id 去重的集合超过上限时，必须淘汰最久没再出现的，而不是随机丢。"""

    def test_duplicate_within_run_counted_once(self):
        state = {}
        parse = collector.make_claude_parser(state)
        self.assertIsNotNone(parse(_claude_line("msg_a")))
        self.assertIsNone(parse(_claude_line("msg_a")))  # 同一 id 再出现直接跳过

    def test_hit_refreshes_position(self):
        """仍在被回放的老 id 命中后要移到队尾，否则它先被裁掉、下轮就重复计数了。"""
        state = {}
        parse = collector.make_claude_parser(state)
        for mid in ("msg_a", "msg_b", "msg_c"):
            parse(_claude_line(mid))
        parse(_claude_line("msg_a"))  # msg_a 又出现一次
        self.assertEqual(list(state["seen_claude"]), ["msg_b", "msg_c", "msg_a"])

    def test_state_survives_json_roundtrip(self):
        """落盘是保序 list，读回来要还原成 dict 继续当 LRU 用。"""
        state = {"seen_claude": ["msg_a", "msg_b"]}
        state["seen_claude"] = dict.fromkeys(state["seen_claude"])
        parse = collector.make_claude_parser(state)
        self.assertIsNone(parse(_claude_line("msg_a")))       # 上次已记过
        self.assertIsNotNone(parse(_claude_line("msg_new")))
        self.assertEqual(list(state["seen_claude"])[-1], "msg_new")


MINUTES = {
    "2026-07-28T13:42|Claude Code|sonnet": {"input": 10, "output": 5, "cached": 2, "requests": 1},
    "2026-07-28T13:47|Claude Code|sonnet": {"input": 1, "output": 1, "cached": 0, "requests": 1},
    "2026-07-28T09:03|Kimi Code|k2p7": {"input": 7, "output": 3, "cached": 1, "requests": 2},
    "2026-07-26T08:00|Kimi Code|k2p7": {"input": 4, "output": 2, "cached": 0, "requests": 1},
}


class BreakdownFromMinutesTest(unittest.TestCase):
    def test_hourly_buckets_merge_minutes(self):
        rows = collector.breakdown_from_minutes(MINUTES, "2026-07-28")
        self.assertEqual([r["bucket"] for r in rows], ["09", "13"])
        self.assertEqual(rows[1], {"bucket": "13", "agent": "Claude Code", "model": "sonnet",
                                   "input": 11, "output": 6, "cached": 2, "requests": 2})

    def test_minute_buckets_within_hour(self):
        rows = collector.breakdown_from_minutes(MINUTES, "2026-07-28", 13)
        self.assertEqual([r["bucket"] for r in rows], ["13:42", "13:47"])

    def test_hour_filter_excludes_others(self):
        self.assertEqual(collector.breakdown_from_minutes(MINUTES, "2026-07-28", 5), [])

    def test_day_outside_retention_returns_none(self):
        """分钟桶只保留最近若干天，覆盖不到的日期返回 None 让调用方回退重扫日志。"""
        self.assertIsNone(collector.breakdown_from_minutes(MINUTES, "2026-07-01"))

    def test_empty_minutes_returns_none(self):
        self.assertIsNone(collector.breakdown_from_minutes({}, "2026-07-28"))

    def test_covered_day_without_usage_is_empty_not_none(self):
        """在覆盖范围内但当天没用量 → 空列表（确实没消耗），不该白扫一遍日志。"""
        self.assertEqual(collector.breakdown_from_minutes(MINUTES, "2026-07-27"), [])


class TimelineRowsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        for p in (mock.patch.object(server, "DATA_DIR", self.tmp),
                  mock.patch.object(server, "collect_if_stale", lambda: None)):
            p.start()
            self.addCleanup(p.stop)
        with open(os.path.join(self.tmp, collector.MINUTES_FILE), "w", encoding="utf-8") as f:
            json.dump(MINUTES, f)

    def test_uses_minutes_without_rescanning_logs(self):
        with mock.patch.object(collector, "breakdown",
                               side_effect=AssertionError("不该重扫日志")):
            rows = server.timeline_rows("2026-07-28")
        self.assertEqual(len(rows), 2)

    def test_falls_back_to_log_scan_outside_coverage(self):
        with mock.patch.object(collector, "breakdown", return_value=[{"bucket": "01"}]) as bd:
            rows = server.timeline_rows("2026-07-01")
        bd.assert_called_once_with("2026-07-01", None)
        self.assertEqual(rows, [{"bucket": "01"}])


if __name__ == "__main__":
    unittest.main()
