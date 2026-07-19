# -*- coding: utf-8 -*-
import json
import os
import tempfile
import time
import unittest
from unittest import mock

import server


CATALOG = {
    "other-provider": {
        "models": {
            # 与 moonshotai 重名（带前缀），优先级低，不应覆盖
            "prefix/Kimi-K3": {"cost": {"input": 9.0, "output": 9.0}},
            # 0 价订阅条目，应跳过
            "Zero Model": {"cost": {"input": 0, "output": 0}},
        }
    },
    "moonshotai": {
        "models": {
            "kimi-k3": {"cost": {"input": 3.0, "output": 15.0, "cache_read": 0.3}},
            "Kimi K2": {"cost": {"input": 0.6, "output": 2.5}},
        }
    },
    "openai": {
        "models": {
            "gpt-5.2": {"cost": {"input": 1.75, "output": 14.0}},
        }
    },
}


class BuildIndexTest(unittest.TestCase):
    def test_build_catalog_index(self):
        index = server.build_catalog_index(CATALOG)
        # 小写短 id；高优先级 provider 优先（moonshotai 的 3.0 覆盖 other-provider 的 9.0）
        self.assertEqual(index["kimi-k3"], {"input": 3.0, "output": 15.0, "cache_read": 0.3})
        # 0 价条目跳过
        self.assertNotIn("zero model", index)
        # 无 cache_read 时不带该键
        self.assertNotIn("cache_read", index["kimi k2"])


class CatalogCacheTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def _fetch_ok(self):
        return mock.patch.object(server, "_fetch_catalog_json", return_value=CATALOG)

    def _cache_path(self):
        return os.path.join(self.tmp, "models_dev_cache.json")

    def test_fetch_and_cache(self):
        with self._fetch_ok() as fetch:
            index = server.get_catalog_index(self.tmp)
            self.assertEqual(index["kimi-k3"]["input"], 3.0)
            # 第二次调用命中缓存，不再请求网络
            server.get_catalog_index(self.tmp)
            self.assertEqual(fetch.call_count, 1)
        self.assertTrue(os.path.exists(self._cache_path()))

    def test_force_refresh(self):
        with self._fetch_ok() as fetch:
            server.get_catalog_index(self.tmp)
            server.get_catalog_index(self.tmp, force=True)
            self.assertEqual(fetch.call_count, 2)

    def test_stale_cache_used_when_offline(self):
        with self._fetch_ok():
            server.get_catalog_index(self.tmp)
        # 把缓存时间改成 48h 前，模拟过期
        with open(self._cache_path(), encoding="utf-8") as f:
            d = json.load(f)
        d["fetched_at"] = int(time.time()) - 48 * 3600
        with open(self._cache_path(), "w", encoding="utf-8") as f:
            json.dump(d, f)
        # 网络失败 → 回退到过期缓存，不抛异常
        with mock.patch.object(server, "_fetch_catalog_json", side_effect=OSError("offline")):
            index = server.get_catalog_index(self.tmp)
        self.assertEqual(index["kimi-k3"]["input"], 3.0)

    def test_no_cache_offline_raises(self):
        with mock.patch.object(server, "_fetch_catalog_json", side_effect=OSError("offline")):
            with self.assertRaises(OSError):
                server.get_catalog_index(self.tmp)

    def test_corrupt_cache_returns_none(self):
        with open(self._cache_path(), "w", encoding="utf-8") as f:
            f.write("{ not valid json")
        self.assertIsNone(server._read_catalog_cache(self.tmp))

    def test_bad_fetched_at_treated_as_stale(self):
        with self._fetch_ok() as fetch:
            server.get_catalog_index(self.tmp)
        with open(self._cache_path(), encoding="utf-8") as f:
            d = json.load(f)
        d["fetched_at"] = "not-a-number"
        with open(self._cache_path(), "w", encoding="utf-8") as f:
            json.dump(d, f)
        # fetched_at 非数字 → 按过期处理，重新拉取且不抛异常
        with self._fetch_ok() as fetch:
            index = server.get_catalog_index(self.tmp)
        self.assertEqual(index["kimi-k3"]["input"], 3.0)
        self.assertEqual(fetch.call_count, 1)


class MatchModelTest(unittest.TestCase):
    def test_alias(self):
        index = {"kimi-k2.7-code": {"input": 0.95, "output": 4.0}}
        self.assertEqual(server.match_model(index, "k2p7"), {"input": 0.95, "output": 4.0})

    def test_exact(self):
        index = {"gpt-5.2": {"input": 1.75, "output": 14.0}}
        self.assertEqual(server.match_model(index, "gpt-5.2"), {"input": 1.75, "output": 14.0})

    def test_substring_picks_shortest(self):
        index = {"gpt-5.2-mini": {"input": 0.4, "output": 1.6},
                 "gpt-5.2-mini-2026": {"input": 0.5, "output": 2.0}}
        self.assertEqual(server.match_model(index, "gpt-5.2"), {"input": 0.4, "output": 1.6})

    def test_no_match(self):
        self.assertIsNone(server.match_model({"a": {"input": 1, "output": 1}}, "zzz"))


class UnpricedModelsTest(unittest.TestCase):
    def test_unpriced(self):
        records = [{"model": "Kimi K2"}, {"model": "New Thing"}, {"model": ""}]
        prices = {"kimi k2": {"input": 0.6, "output": 2.5}}
        self.assertEqual(server.unpriced_models(records, prices), ["new thing"])


class ImportModelsDevTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._patchers = [
            mock.patch.object(server, "DATA_DIR", self.tmp),
            mock.patch.object(server, "DATA_FILE", os.path.join(self.tmp, "records.json")),
            # 隔离自动匹配后台线程，保证测试确定性（Task 3 实现，此处防御性 patch）
            mock.patch.object(server, "maybe_auto_match", lambda *a, **k: None),
        ]
        for p in self._patchers:
            p.start()

    def tearDown(self):
        for p in self._patchers:
            p.stop()

    def _write_records(self, models):
        with open(server.DATA_FILE, "w", encoding="utf-8") as f:
            json.dump([{"id": i + 1, "date": "2026-07-19", "agent": "A", "model": m,
                        "requests": 1, "input": 1, "output": 1, "cached": 0, "cost": 0}
                       for i, m in enumerate(models)], f)

    def _read_prices(self):
        with open(os.path.join(self.tmp, "prices.json"), encoding="utf-8") as f:
            return json.load(f)

    def test_import_matches_and_writes(self):
        self._write_records(["Kimi K2", "Unknown XYZ"])
        with mock.patch.object(server, "_fetch_catalog_json", return_value=CATALOG):
            result = server.import_models_dev()
        self.assertIn("kimi k2", result["matched"])
        self.assertEqual(result["unmatched"], ["unknown xyz"])
        self.assertEqual(self._read_prices()["kimi k2"], {"input": 0.6, "output": 2.5})
        # 手动导入应刷新目录缓存，并把未匹配模型写入缓存 unmatched
        cache = server._read_catalog_cache(self.tmp)
        self.assertIsNotNone(cache)
        self.assertIn("unknown xyz", cache["unmatched"])

    def test_import_overwrites_existing(self):
        """手动点导入是显式操作，保持现有覆盖行为（与自动匹配'只补缺失'不同）。"""
        self._write_records(["Kimi K2"])
        with open(os.path.join(self.tmp, "prices.json"), "w", encoding="utf-8") as f:
            json.dump({"kimi k2": {"input": 99.0, "output": 99.0}}, f)
        with mock.patch.object(server, "_fetch_catalog_json", return_value=CATALOG):
            server.import_models_dev()
        self.assertEqual(self._read_prices()["kimi k2"]["input"], 0.6)


if __name__ == "__main__":
    unittest.main()
