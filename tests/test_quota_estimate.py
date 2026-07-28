# -*- coding: utf-8 -*-
"""额度估计的手动开关：停止后只留官方百分比，重新开始则从当刻重新锚定。"""
import os
import tempfile
import time
import unittest
from unittest import mock

import collector
import server


class EstimationToggleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for p in (mock.patch.object(server, "DATA_DIR", self.tmp.name),
                  mock.patch.object(server, "CAL_FILE",
                                    os.path.join(self.tmp.name, "quota_cal.json"))):
            p.start()
            self.addCleanup(p.stop)
        self.resets_at = time.time() + 3600

    def _window(self, pct):
        return {"label": "5 小时窗口", "used_percent": pct,
                "resets_at": self.resets_at, "window_minutes": 300}

    def _estimate(self, pct, measured):
        """跑一次窗口估算；measured 直接喂给「本机实测」，不去碰真实日志。"""
        with mock.patch.object(collector, "usage_since",
                               return_value={"input": measured, "output": 0, "cached": 0}):
            return server._estimate_window("kimi", self._window(pct), peers=[])

    def test_enabled_by_default(self):
        out = self._estimate(40.0, 4000)
        self.assertTrue(out["est_enabled"])
        self.assertEqual(out["cal_key"], "kimi|5 小时窗口")
        self.assertEqual(out["est_quota"], 10000)  # 时点法：4000 / 40%

    def test_stopped_hides_absolutes_but_keeps_percent(self):
        server.set_estimation("kimi|5 小时窗口", False)
        out = self._estimate(40.0, 4000)
        self.assertFalse(out["est_enabled"])
        self.assertTrue(out["est_paused"])
        self.assertIsNone(out["est_quota"])
        self.assertIsNone(out["est_remaining"])
        self.assertFalse(out["absolute_hidden"])   # 不是多端隐藏，是用户主动停的
        self.assertEqual(out["used_percent"], 40.0)
        self.assertEqual(out["measured_tokens"], 4000)  # 实测量照常展示

    def test_restart_discards_old_calibration(self):
        self._estimate(40.0, 4000)                       # 先攒一个旧校准值
        self.assertEqual(server._load_cal()["kimi|5 小时窗口"]["quota"], 10000)
        server.set_estimation("kimi|5 小时窗口", True)
        self.assertNotIn("quota", server._load_cal()["kimi|5 小时窗口"])

    def test_restart_waits_for_differential(self):
        """手动重启后只认差分：先给不出数（标记 calibrating），百分比涨了才出。"""
        server.set_estimation("kimi|5 小时窗口", True)
        out = self._estimate(40.0, 4000)
        self.assertIsNone(out["est_quota"])          # 时点法被禁用，不拿脏口径凑数
        self.assertTrue(out["est_calibrating"])
        self.assertTrue(out["est_since"])

        out = self._estimate(50.0, 5000)             # 涨 10% 期间实测 +1000
        self.assertEqual(out["est_quota"], 10000)
        self.assertEqual(out["est_remaining"], 5000)
        self.assertFalse(out["est_calibrating"])

    def test_reused_window_dict_has_no_stale_flags(self):
        """回归：Kimi 卡片有 120s 缓存，window 是被复用的同一个 dict。
        停止→开启后若不重写全部标记，est_paused 会留着 True，UI 仍显示「已停止」。"""
        w = self._window(40.0)
        server.set_estimation("kimi|5 小时窗口", False)
        with mock.patch.object(collector, "usage_since",
                               return_value={"input": 4000, "output": 0, "cached": 0}):
            server._estimate_window("kimi", w, peers=[])
            self.assertTrue(w["est_paused"])
            server.set_estimation("kimi|5 小时窗口", True)
            server._estimate_window("kimi", w, peers=[])   # 同一个 dict 再算一次
        self.assertFalse(w["est_paused"])
        self.assertTrue(w["est_enabled"])

    def test_multi_device_hiding_still_wins(self):
        """多端未同步时绝对值一律隐藏，与这个开关无关。"""
        server.set_estimation("kimi|5 小时窗口", True)
        with mock.patch.object(collector, "usage_since",
                               return_value={"input": 4000, "output": 0, "cached": 0}):
            out = server._estimate_window("kimi", self._window(40.0), peers=[],
                                          hide_absolute=True)
        self.assertTrue(out["absolute_hidden"])
        self.assertFalse(out.get("est_paused"))
        self.assertIsNone(out["est_quota"])


if __name__ == "__main__":
    unittest.main()
