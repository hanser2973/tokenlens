# -*- coding: utf-8 -*-
"""多端合并与滚动窗口统计的测试。

重点覆盖三件事：
1) 分钟桶的滚动窗口统计（修掉 5h 窗口按自然日截断的老 bug）
2) 同步目录的导出/读取：每台设备只写自己的文件，读取时跳过自己
3) 多端未同步时不输出会误导人的绝对 token 估算
"""
import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta

import collector
import server
import storage
import sync


def _minute_key(dt, agent="Kimi Code", model="k2p7"):
    return f"{dt.strftime('%Y-%m-%dT%H:%M')}|{agent}|{model}"


def _slot(n):
    return {"input": n, "output": n, "cached": n, "requests": 1}


class MinutesUsageTest(unittest.TestCase):
    def test_only_counts_after_start(self):
        now = datetime.now()
        minutes = {
            _minute_key(now - timedelta(hours=9)): _slot(100),   # 窗口外
            _minute_key(now - timedelta(hours=2)): _slot(10),    # 窗口内
            _minute_key(now - timedelta(minutes=5)): _slot(1),   # 窗口内
        }
        got = collector.minutes_usage_since(minutes, time.time() - 5 * 3600)
        self.assertEqual(got["input"], 11)
        self.assertEqual(got["requests"], 2)

    def test_filters_by_agent(self):
        now = datetime.now()
        minutes = {
            _minute_key(now - timedelta(minutes=10), agent="Kimi Code"): _slot(7),
            _minute_key(now - timedelta(minutes=10), agent="Codex CLI"): _slot(5),
        }
        start = time.time() - 3600
        self.assertEqual(collector.minutes_usage_since(minutes, start, agent="Kimi Code")["input"], 7)
        self.assertEqual(collector.minutes_usage_since(minutes, start, kind="codex")["input"], 5)

    def test_prune_drops_old_days(self):
        today = datetime.now().date()
        old = (today - timedelta(days=collector.MINUTES_RETENTION_DAYS + 2)).isoformat()
        minutes = {f"{old}T10:00|Kimi Code|k2p7": _slot(1),
                   f"{today.isoformat()}T10:00|Kimi Code|k2p7": _slot(2)}
        collector._prune_minutes(minutes, today)
        self.assertEqual(len(minutes), 1)
        self.assertIn(today.isoformat() + "T10:00|Kimi Code|k2p7", minutes)


class RollingWindowTest(unittest.TestCase):
    """5h 窗口过去按自然日截断，晚上查看会把一整天算成 5 小时的用量。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.saved = server.DATA_DIR
        server.DATA_DIR = self.tmp.name
        self.addCleanup(lambda: setattr(server, "DATA_DIR", self.saved))

    def test_sub_day_window_excludes_earlier_today(self):
        now = datetime.now()
        storage.save_json(os.path.join(self.tmp.name, collector.MINUTES_FILE), {
            _minute_key(now - timedelta(hours=8)): _slot(1000),  # 今天但在 5h 窗口之外
            _minute_key(now - timedelta(hours=1)): _slot(3),
        })
        used = server._period_used([], "Kimi Code", 5 * 3600)
        self.assertEqual(used, 9)  # 只有窗口内那条：3+3+3

    def test_week_window_uses_day_records(self):
        today = datetime.now().date()
        records = [
            {"agent": "Kimi Code", "date": today.isoformat(), "input": 1, "output": 1, "cached": 1},
            {"agent": "Kimi Code", "date": (today - timedelta(days=30)).isoformat(),
             "input": 99, "output": 0, "cached": 0},
        ]
        self.assertEqual(server._period_used(records, "Kimi Code", 7 * 86400), 3)


class SyncFilesTest(unittest.TestCase):
    def setUp(self):
        self.a = tempfile.TemporaryDirectory()
        self.b = tempfile.TemporaryDirectory()
        self.shared = tempfile.TemporaryDirectory()
        for t in (self.a, self.b, self.shared):
            self.addCleanup(t.cleanup)

    def _seed(self, data_dir, day_key, minute_dt, n):
        storage.save_json(os.path.join(data_dir, collector.AUTO_FILE), {day_key: _slot(n)})
        storage.save_json(os.path.join(data_dir, collector.MINUTES_FILE),
                          {_minute_key(minute_dt): _slot(n)})

    def test_devices_get_distinct_ids(self):
        self.assertNotEqual(sync.device_identity(self.a.name)["id"],
                            sync.device_identity(self.b.name)["id"])

    def test_identity_is_stable(self):
        self.assertEqual(sync.device_identity(self.a.name)["id"],
                         sync.device_identity(self.a.name)["id"])

    def test_export_then_peer_reads_it_but_not_own(self):
        now = datetime.now()
        self._seed(self.a.name, "2026-07-27|Kimi Code|k2p7", now - timedelta(minutes=30), 5)
        sync.export(self.a.name, self.shared.name)

        # A 读不到自己导出的那份
        self.assertEqual(sync.read_peers(self.a.name, self.shared.name), [])
        # B 能读到 A
        peers = sync.read_peers(self.b.name, self.shared.name)
        self.assertEqual(len(peers), 1)
        self.assertEqual(peers[0]["days"]["2026-07-27|Kimi Code|k2p7"]["input"], 5)

    def test_peer_usage_since_sums_across_devices(self):
        now = datetime.now()
        self._seed(self.a.name, "2026-07-27|Kimi Code|k2p7", now - timedelta(minutes=10), 4)
        sync.export(self.a.name, self.shared.name)
        peers = sync.read_peers(self.b.name, self.shared.name)
        got = sync.peer_usage_since(peers, time.time() - 3600, kind="kimi")
        self.assertEqual(got["input"], 4)
        # 窗口之外则不计入
        self.assertEqual(sync.peer_usage_since(peers, time.time() + 60, kind="kimi")["input"], 0)

    def test_missing_sync_dir_is_not_fatal(self):
        self.assertEqual(sync.read_peers(self.a.name, os.path.join(self.shared.name, "nope")), [])
        self.assertEqual(sync.read_peers(self.a.name, ""), [])

    def test_garbage_files_are_skipped(self):
        with open(os.path.join(self.shared.name, "tokenlens-bad.json"), "w", encoding="utf-8") as f:
            f.write("{not json")
        with open(os.path.join(self.shared.name, "unrelated.txt"), "w", encoding="utf-8") as f:
            f.write("hello")
        self.assertEqual(sync.read_peers(self.b.name, self.shared.name), [])

    def test_newest_rate_limits_prefers_latest_timestamp(self):
        local = {"rate_limits": {"plan_type": "old"}, "timestamp": "2026-07-01T00:00:00Z"}
        peers = [{"device_name": "笔记本",
                  "rate_limits": {"rate_limits": {"plan_type": "new"}, "timestamp": "2026-07-27T00:00:00Z"}}]
        best, source = sync.newest_rate_limits(local, peers)
        self.assertEqual(best["rate_limits"]["plan_type"], "new")
        self.assertEqual(source, "笔记本")

    def test_newest_rate_limits_keeps_local_when_newer(self):
        local = {"rate_limits": {"plan_type": "local"}, "timestamp": "2026-07-27T00:00:00Z"}
        peers = [{"device_name": "笔记本",
                  "rate_limits": {"rate_limits": {"plan_type": "old"}, "timestamp": "2026-07-01T00:00:00Z"}}]
        best, source = sync.newest_rate_limits(local, peers)
        self.assertEqual(best["rate_limits"]["plan_type"], "local")
        self.assertIsNone(source)


class SettingsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.saved_dir, self.saved_file = server.DATA_DIR, server.SETTINGS_FILE
        server.DATA_DIR = self.tmp.name
        server.SETTINGS_FILE = os.path.join(self.tmp.name, "settings.json")

        def restore():
            server.DATA_DIR, server.SETTINGS_FILE = self.saved_dir, self.saved_file
        self.addCleanup(restore)

    def test_defaults(self):
        self.assertEqual(server.load_settings()["multi_device"], "off")

    def test_sync_without_dir_degrades_to_local_only(self):
        """声明合并却没有目录时不能显示成「已合并」，否则用户以为绝对值可信。"""
        server.save_settings({"multi_device": "sync", "sync_dir": ""})
        self.assertEqual(server.load_settings()["multi_device"], "local_only")

    def test_bad_mode_falls_back(self):
        server.save_settings({"multi_device": "nonsense", "sync_dir": ""})
        self.assertEqual(server.load_settings()["multi_device"], "off")

    def test_active_peers_empty_unless_sync(self):
        server.save_settings({"multi_device": "local_only", "sync_dir": self.tmp.name})
        self.assertEqual(server.active_peers(), [])


class HideAbsoluteTest(unittest.TestCase):
    """多端未同步时，绝对 token 估算必须被隐藏而不是给个缩水的数。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.saved = server.DATA_DIR
        server.DATA_DIR = self.tmp.name
        self.addCleanup(lambda: setattr(server, "DATA_DIR", self.saved))

    def test_hidden_window_reports_percent_but_no_absolutes(self):
        w = {"label": "5 小时窗口", "used_percent": 40.0,
             "resets_at": time.time() + 3600, "window_minutes": 300}
        out = server._estimate_window("kimi", w, peers=[], hide_absolute=True)
        self.assertTrue(out["absolute_hidden"])
        self.assertIsNone(out["est_quota"])
        self.assertIsNone(out["est_remaining"])
        self.assertEqual(out["used_percent"], 40.0)  # 百分比仍然保留
        self.assertFalse(out["account_wide"])

    def test_measured_splits_local_and_remote(self):
        now = datetime.now()
        peers = [{"device_name": "台式机",
                  "minutes": {_minute_key(now - timedelta(minutes=5)): _slot(6)}}]
        w = {"label": "5 小时窗口", "used_percent": 10.0,
             "resets_at": time.time() + 3600, "window_minutes": 300}
        out = server._estimate_window("kimi", w, peers=peers, hide_absolute=True)
        self.assertEqual(out["measured_remote"], 18)  # 6+6+6
        self.assertTrue(out["account_wide"])
        self.assertEqual(out["measured_tokens"], out["measured_local"] + 18)


if __name__ == "__main__":
    unittest.main()
