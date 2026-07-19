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


if __name__ == "__main__":
    unittest.main()
