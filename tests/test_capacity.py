# -*- coding: utf-8 -*-
"""真实容量：只认「从满额一路观测到用完」的完整一轮，其余情况一律不出数。"""
import os
import tempfile
import time
import unittest
from unittest import mock

import capacity
import collector
import server


def _win(pct, tokens, resets_at, minutes=300):
    return {"label": "5 小时窗口", "used_percent": pct, "measured_tokens": tokens,
            "resets_at": resets_at, "window_minutes": minutes}


class CapacityCycleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.resets = time.time() + 3600
        self.key = "kimi|5 小时窗口"

    def _observe(self, pct, tokens, resets_at=None, **kw):
        return capacity.observe(self.tmp.name, self.key,
                                _win(pct, tokens, resets_at or self.resets), **kw)

    def test_full_cycle_from_zero_to_hundred(self):
        """0% 起观测、跑到 100%：这一轮实测到的 token 总量就是真实容量。"""
        self._observe(0.0, 0)
        self._observe(45.0, 900_000)
        out = self._observe(100.0, 2_050_000)
        self.assertEqual(out["tokens"], 2_050_000)
        self.assertEqual(out["samples"], 1)
        self.assertEqual(out["avg"], 2_050_000)
        self.assertTrue(out["tracking"]["sampled"])

    def test_only_samples_once_per_cycle(self):
        """跑满之后还有零星用量（不计费的模型等），不能把容量越记越大。"""
        self._observe(0.0, 0)
        self._observe(100.0, 1_000_000)
        out = self._observe(100.0, 1_200_000)
        self.assertEqual(out["samples"], 1)
        self.assertEqual(out["tokens"], 1_000_000)

    def test_mid_cycle_start_is_not_counted(self):
        """半路才开始看这个窗口 —— 前面那段没数，不能拿来当真实容量。"""
        self._observe(40.0, 800_000)
        out = self._observe(100.0, 2_000_000)
        self.assertEqual(out["samples"], 0)
        self.assertIsNone(out["tokens"])
        self.assertFalse(out["tracking"]["eligible"])
        self.assertIn("40.0%", out["tracking"]["blocked"])

    def test_next_cycle_starts_clean(self):
        """窗口重置（resets_at 变了）→ 重新起一轮，上一轮的脏状态不带过来。"""
        self._observe(40.0, 800_000)
        later = self.resets + 5 * 3600
        self._observe(0.0, 0, resets_at=later)
        out = self._observe(99.5, 1_500_000, resets_at=later)
        self.assertEqual(out["tokens"], 1_500_000)

    def test_uncovered_cycle_is_discarded(self):
        """观测中途口径变脏（比如对端日志缺一截）：这一轮作废，且洗不白。"""
        self._observe(0.0, 0)
        self._observe(50.0, 900_000, covered=False, blocked="其他设备的采集数据没覆盖到窗口起点")
        out = self._observe(100.0, 2_000_000)   # 后面恢复了也不算
        self.assertEqual(out["samples"], 0)
        self.assertIn("没覆盖到窗口起点", out["tracking"]["blocked"])

    def test_account_switch_discards_cycle(self):
        """本轮中途换了账号：前半段是另一个账号的量，总数没有意义。"""
        self._observe(0.0, 0, account="aaa")
        out = self._observe(100.0, 2_000_000, account="bbb")
        self.assertEqual(out["samples"], 0)
        self.assertIn("切换了账号", out["tracking"]["blocked"])

    def test_samples_are_filtered_by_account(self):
        """换账号之后额度本来就不同，旧账号的样本不能显示成当前容量。"""
        self._observe(0.0, 0, account="aaa")
        self._observe(100.0, 2_000_000, account="aaa")
        later = self.resets + 5 * 3600
        self._observe(0.0, 0, resets_at=later, account="bbb")
        out = self._observe(100.0, 900_000, resets_at=later, account="bbb")
        self.assertEqual(out["samples"], 1)
        self.assertEqual(out["tokens"], 900_000)
        self.assertEqual(capacity.get(self.tmp.name, self.key, "aaa")["tokens"], 2_000_000)

    def test_tracking_shows_progress_before_full(self):
        self._observe(0.0, 0)
        out = self._observe(30.0, 600_000)
        self.assertTrue(out["tracking"]["eligible"])
        self.assertFalse(out["tracking"]["sampled"])
        self.assertEqual(out["tracking"]["tokens"], 600_000)
        self.assertIsNone(out["tokens"])

    def test_repeated_identical_observation_does_not_rewrite(self):
        """配额接口有缓存，界面刷新常拿到一模一样的数 —— 不该跟着写盘。"""
        self._observe(10.0, 100)
        path = os.path.join(self.tmp.name, capacity.FILE)
        before = os.stat(path).st_mtime_ns
        self._observe(10.0, 100)
        self.assertEqual(os.stat(path).st_mtime_ns, before)


class CapacityInWindowTest(unittest.TestCase):
    """真实容量在 _estimate_window 里的接线：跟着每次窗口估算一起记。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for p in (mock.patch.object(server, "DATA_DIR", self.tmp.name),
                  mock.patch.object(server, "CAL_FILE",
                                    os.path.join(self.tmp.name, "quota_cal.json"))):
            p.start()
            self.addCleanup(p.stop)
        self.resets = time.time() + 3600

    def _estimate(self, pct, measured, **kw):
        w = {"label": "5 小时窗口", "used_percent": pct,
             "resets_at": self.resets, "window_minutes": 300}
        with mock.patch.object(collector, "usage_since",
                               return_value={"input": measured, "output": 0, "cached": 0}):
            return server._estimate_window("kimi", w, peers=[], **kw)

    def test_window_carries_capacity_summary(self):
        out = self._estimate(0.0, 0)
        self.assertIn("real_capacity", out)
        self.assertTrue(out["real_capacity"]["tracking"]["eligible"])
        out = self._estimate(100.0, 1_800_000)
        self.assertEqual(out["real_capacity"]["tokens"], 1_800_000)

    def test_local_only_mode_blocks_measurement(self):
        """多端未同步时实测只有本机，数出来的容量必然偏小 —— 不能记。"""
        out = self._estimate(0.0, 0, hide_absolute=True)
        self.assertFalse(out["real_capacity"]["tracking"]["eligible"])
        self.assertIn("多端未同步", out["real_capacity"]["tracking"]["blocked"])

    def test_stopped_estimation_does_not_stop_measurement(self):
        """「停止估计」关的是外推。用户往往正因为外推不准才关它，而真实容量是外推的
        替代品 —— 跟着一起停就等于这个功能永远出不来数。"""
        server.set_estimation("kimi|5 小时窗口", False)
        out = self._estimate(0.0, 0)
        self.assertIsNone(out["est_quota"])                    # 外推确实停了
        self.assertTrue(out["real_capacity"]["tracking"]["eligible"])
        out = self._estimate(100.0, 1_400_000)
        self.assertEqual(out["real_capacity"]["tokens"], 1_400_000)


if __name__ == "__main__":
    unittest.main()
