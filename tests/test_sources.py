# -*- coding: utf-8 -*-
"""snapshot 类采集源（Cline 系 / opencode）与「重建采集数据」。

这两个工具没装在开发机上，解析器是按各自公开的存储格式写的，用固定夹具钉住格式；
真实安装上如果字段有出入，改动会局限在对应的 parse_*_snapshot 一个函数里。
"""
import json
import os
import tempfile
import time
import unittest
from unittest import mock

import collector
import server


def _cline_msg(ts_ms, tokens_in=100, tokens_out=50, cache_r=0, cache_w=0):
    return {"ts": ts_ms, "type": "say", "say": "api_req_started",
            "text": json.dumps({"request": "...", "tokensIn": tokens_in, "tokensOut": tokens_out,
                                "cacheReads": cache_r, "cacheWrites": cache_w, "cost": 0.01})}


class ClineParserTest(unittest.TestCase):
    def test_parses_api_req_started(self):
        data = [{"ts": 1, "type": "say", "say": "text", "text": "任务描述"},
                _cline_msg(1785000000000, 100, 50, cache_r=20, cache_w=10)]
        evs = collector.parse_cline_snapshot(data, "/x/tasks/1/ui_messages.json")
        self.assertEqual(len(evs), 1)
        self.assertEqual(evs[0]["input"], 110)   # tokensIn + cacheWrites，与其他源口径一致
        self.assertEqual(evs[0]["output"], 50)
        self.assertEqual(evs[0]["cached"], 20)
        self.assertEqual(evs[0]["project"], collector.UNKNOWN_PROJECT)

    def test_skips_pending_request(self):
        """请求还没回来时 token 字段是空的，不该记成一条 0 用量事件。"""
        pending = {"ts": 1785000000000, "type": "say", "say": "api_req_started",
                   "text": json.dumps({"request": "..."})}
        self.assertEqual(collector.parse_cline_snapshot([pending], "/x/ui_messages.json"), [])

    def test_model_from_task_metadata(self):
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "ui_messages.json")
        with open(os.path.join(tmp, "task_metadata.json"), "w", encoding="utf-8") as f:
            json.dump({"model_usage": [{"ts": 1784999000000, "model_id": "claude-sonnet-4-5"}]}, f)
        evs = collector.parse_cline_snapshot([_cline_msg(1785000000000)], path)
        self.assertEqual(evs[0]["model"], "claude-sonnet-4-5")

    def test_unknown_model_without_metadata(self):
        evs = collector.parse_cline_snapshot([_cline_msg(1785000000000)], "/x/ui_messages.json")
        self.assertEqual(evs[0]["model"], "unknown")

    def test_garbage_does_not_raise(self):
        self.assertEqual(collector.parse_cline_snapshot({"not": "a list"}, "/x"), [])


class OpencodeParserTest(unittest.TestCase):
    def _msg(self, **over):
        d = {"id": "msg_1", "role": "assistant", "modelID": "claude-sonnet-4-5",
             "path": {"cwd": "/home/u/work/api-svc", "root": "/home/u/work/api-svc"},
             "tokens": {"input": 100, "output": 50, "reasoning": 5, "cache": {"read": 20, "write": 10}},
             "time": {"created": 1785000000000, "completed": 1785000005000}}
        d.update(over)
        return d

    def test_parses_assistant_message(self):
        evs = collector.parse_opencode_snapshot(self._msg(), "/x/msg_1.json")
        self.assertEqual(len(evs), 1)
        self.assertEqual(evs[0]["input"], 110)      # input + cache.write
        self.assertEqual(evs[0]["output"], 55)      # output + reasoning
        self.assertEqual(evs[0]["cached"], 20)
        self.assertEqual(evs[0]["project"], "api-svc")
        self.assertEqual(evs[0]["model"], "claude-sonnet-4-5")

    def test_skips_user_message(self):
        self.assertEqual(collector.parse_opencode_snapshot(self._msg(role="user"), "/x"), [])

    def test_skips_zero_usage(self):
        zero = self._msg(tokens={"input": 0, "output": 0, "cache": {"read": 0, "write": 0}})
        self.assertEqual(collector.parse_opencode_snapshot(zero, "/x"), [])


class SnapshotDedupTest(unittest.TestCase):
    """整份重写的文件会被反复读到，靠时间戳去重必须保证不重复计数。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.tasks = os.path.join(self.tmp, "tasks", "t1")
        os.makedirs(self.tasks)
        self.path = os.path.join(self.tasks, "ui_messages.json")
        src = collector.Source("Cline", [os.path.join(self.tmp, "tasks", "*", "ui_messages.json")],
                               "cline", "snapshot")
        p = mock.patch.object(collector, "SOURCES", [src])
        p.start()
        self.addCleanup(p.stop)

    def _write(self, msgs):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(msgs, f)
        # collect_once 靠 mtime 判断要不要重读，测试里手工推进避免同秒写入被当成没变
        st = os.stat(self.path)
        stamp = max(st.st_mtime, getattr(self, "last_write_mtime", 0)) + 10
        self.last_write_mtime = stamp
        os.utime(self.path, (st.st_atime, stamp))

    def _total(self):
        auto = collector._load_json(os.path.join(self.tmp, collector.AUTO_FILE), {})
        return sum(v["input"] + v["output"] + v["cached"] for v in auto.values())

    def test_rescan_does_not_double_count(self):
        self._write([_cline_msg(1785000000000, 100, 50)])
        self.assertEqual(collector.collect_once(self.tmp)["Cline"]["events"], 1)
        self.assertEqual(self._total(), 150)
        # 文件没变：不重读
        self.assertEqual(collector.collect_once(self.tmp).get("Cline", {}).get("events"), 0)
        self.assertEqual(self._total(), 150)

    def test_appended_message_counted_once(self):
        self._write([_cline_msg(1785000000000, 100, 50)])
        collector.collect_once(self.tmp)
        # 整份重写，旧消息还在，只应吃进新增的那条
        self._write([_cline_msg(1785000000000, 100, 50), _cline_msg(1785000600000, 7, 3)])
        self.assertEqual(collector.collect_once(self.tmp)["Cline"]["events"], 1)
        self.assertEqual(self._total(), 160)


class RebuildTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for p in (mock.patch.object(server, "DATA_DIR", self.tmp.name),
                  mock.patch.object(server, "SETTINGS_FILE",
                                    os.path.join(self.tmp.name, "settings.json"))):
            p.start()
            self.addCleanup(p.stop)

    def test_backs_up_then_rescans(self):
        stale = {"2020-01-01|Old|m": {"input": 9, "output": 9, "cached": 0, "requests": 1}}
        for name, body in ((collector.AUTO_FILE, stale), (collector.STATE_FILE, {"offsets": {"x": 5}}),
                           (collector.MINUTES_FILE, {})):
            with open(os.path.join(self.tmp.name, name), "w", encoding="utf-8") as f:
                json.dump(body, f)
        with mock.patch.object(collector, "collect_once", return_value={"Kimi Code": {"files": 1, "events": 3}}):
            summary, backups = server.rebuild_collection()
        self.assertEqual(summary["Kimi Code"]["events"], 3)
        self.assertEqual(len(backups), 3)
        # 原文件已改名备份，重扫从零开始；备份仍在，重扫结果不对还能捞回来
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, collector.AUTO_FILE)))
        for b in backups:
            self.assertTrue(os.path.exists(os.path.join(self.tmp.name, b)))

    def test_missing_files_is_not_an_error(self):
        with mock.patch.object(collector, "collect_once", return_value={}):
            summary, backups = server.rebuild_collection()
        self.assertEqual(summary, {})
        self.assertEqual(backups, [])


if __name__ == "__main__":
    unittest.main()
