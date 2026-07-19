# 新模型自动匹配价格 · 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新模型首次出现在记录里时，后台自动从 models.dev 匹配价格（只补缺失，不覆盖已有价格），匹配不上的模型在前端以"未定价"徽标可见。

**Architecture:** models.dev 目录构建为索引并缓存到 `data/models_dev_cache.json`（TTL 24h，含匹配失败名单）；`merged_records()` 返回前检测无价格模型，有则起 daemon 线程静默匹配；`/api/records` 新增 `unpriced` 字段，前端模型卡片显示"未定价"徽标与提示条计数。

**Tech Stack:** Python 3 stdlib（http.server / urllib / unittest），前端原生 JS + ECharts。无 pytest，测试用 stdlib `unittest`。

**Spec:** `docs/superpowers/specs/2026-07-19-auto-price-match-design.md`

**运行测试的统一命令**（项目根目录）：

```bash
cd c:/code/pythoncode/tokenlens && .venv/Scripts/python.exe -m unittest discover -s tests -v
```

---

### Task 1: 目录索引构建 + 磁盘缓存

**Files:**
- Create: `tests/__init__.py`（空文件）
- Create: `tests/test_auto_prices.py`
- Modify: `server.py`（顶部 imports；`PREFERRED_PROVIDERS` 之后新增函数）

- [ ] **Step 1: 创建测试包与失败的测试**

创建空文件 `tests/__init__.py`，然后创建 `tests/test_auto_prices.py`：

```python
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
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd c:/code/pythoncode/tokenlens && .venv/Scripts/python.exe -m unittest discover -s tests -v`
Expected: FAIL — `AttributeError: module 'server' has no attribute 'build_catalog_index'`

- [ ] **Step 3: 实现索引构建与缓存**

在 `server.py` 顶部 imports 区（第 17 行 `from urllib.parse import ...` 之后）加：

```python
import urllib.request
```

在 `PREFERRED_PROVIDERS` 定义（server.py:131-132）之后插入：

```python
CATALOG_TTL = 24 * 3600  # models.dev 目录缓存 24 小时


def _fetch_catalog_json():
    req = urllib.request.Request(MODELS_DEV_URL, headers={"User-Agent": "Mozilla/5.0 (TokenLens)"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def build_catalog_index(catalog):
    """把 models.dev 目录构建成 小写模型id → 价格 的索引（provider 按优先级去重）。"""
    index = {}
    providers = sorted(catalog, key=lambda p: (PREFERRED_PROVIDERS.index(p)
                                               if p in PREFERRED_PROVIDERS else 99, p))
    for prov in providers:
        models = (catalog.get(prov) or {}).get("models") or {}
        for mid, m in models.items():
            cost = m.get("cost") or {}
            try:
                entry = {"input": float(cost.get("input") or 0), "output": float(cost.get("output") or 0)}
            except (TypeError, ValueError):
                continue
            if entry["input"] <= 0 and entry["output"] <= 0:
                continue  # 跳过订阅制 0 价条目
            if cost.get("cache_read") is not None:
                try:
                    entry["cache_read"] = float(cost["cache_read"])
                except (TypeError, ValueError):
                    pass
            short = mid.rsplit("/", 1)[-1].lower()
            index.setdefault(short, entry)  # providers 已按优先级排序，先到先得
    return index


def _catalog_cache_file(data_dir):
    return os.path.join(data_dir, "models_dev_cache.json")


def _read_catalog_cache(data_dir):
    try:
        with open(_catalog_cache_file(data_dir), "r", encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict) and isinstance(d.get("index"), dict):
            d.setdefault("unmatched", {})
            return d
    except (OSError, ValueError):
        pass
    return None


def _write_catalog_cache(data_dir, index, unmatched):
    os.makedirs(data_dir, exist_ok=True)
    with open(_catalog_cache_file(data_dir), "w", encoding="utf-8") as f:
        json.dump({"fetched_at": int(time.time()), "index": index,
                   "unmatched": unmatched}, f, ensure_ascii=False)


def get_catalog_index(data_dir, force=False):
    """返回 models.dev 目录索引。缓存 24h 内复用；force 强制刷新。
    网络失败时回退到过期缓存；连缓存都没有则抛异常。成功刷新会清空 unmatched。"""
    cache = _read_catalog_cache(data_dir)
    fresh = cache and (time.time() - float(cache.get("fetched_at") or 0) < CATALOG_TTL)
    if cache and fresh and not force:
        return cache["index"]
    try:
        index = build_catalog_index(_fetch_catalog_json())
    except Exception:
        if cache:
            return cache["index"]  # 离线时用旧缓存
        raise
    _write_catalog_cache(data_dir, index, {})
    return index
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd c:/code/pythoncode/tokenlens && .venv/Scripts/python.exe -m unittest discover -s tests -v`
Expected: 4 个测试 PASS

- [ ] **Step 5: Commit**

```bash
cd c:/code/pythoncode/tokenlens
git add tests/ server.py
git commit -m "新增 models.dev 目录索引构建与 24h 磁盘缓存

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 2: 抽取 match_model / unpriced_models，重构 import_models_dev 走缓存

**Files:**
- Modify: `server.py`（替换 `import_models_dev` 整个函数，server.py:135-180；GET `/api/prices` 分支 server.py:419-423）
- Test: `tests/test_auto_prices.py`（追加）

- [ ] **Step 1: 追加失败的测试**

在 `tests/test_auto_prices.py` 的 `if __name__ == "__main__":` 之前追加：

```python
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
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd c:/code/pythoncode/tokenlens && .venv/Scripts/python.exe -m unittest discover -s tests -v`
Expected: FAIL — `AttributeError: module 'server' has no attribute 'match_model'`（注意 `maybe_auto_match` 也尚不存在，patch.object 用 `create=True` 会报错——直接先实现 Task 3 的占位也可；最简单：mock.patch.object 第四个参数无 create 要求是因为 Task 3 之后才有该函数。**执行顺序要求：先完成本 Task Step 3 的实现再跑全部测试**；单独验证本步失败可预期看到 match_model 相关 AttributeError）

> 说明：`mock.patch.object(server, "maybe_auto_match", ...)` 在函数不存在时会抛 AttributeError，导致测试在 setUp 就失败。这是可接受的"失败信号"；Step 3 会同时实现 `maybe_auto_match` 的占位版本（Task 3 补全逻辑）。

- [ ] **Step 3: 实现**

在 `get_catalog_index` 之后插入：

```python
def match_model(index, model):
    """按 别名 → 精确 → 子串（取最短 id）匹配目录，返回价格 entry 或 None。"""
    target = MODEL_ALIASES.get(model, model)
    entry = index.get(target)
    if entry:
        return entry
    subs = [mid for mid in index if target in mid or mid in target]
    if subs:
        return index[min(subs, key=len)]
    return None


def unpriced_models(records, prices):
    """记录中出现过、但价格表里没有的模型（小写，排序）。"""
    return sorted({str(r.get("model", "")).lower() for r in records if r.get("model")}
                  - set(prices))


def maybe_auto_match(data_dir, records, prices):
    """占位：Task 3 实现自动匹配逻辑。"""
    return None
```

把 `import_models_dev`（server.py:135-180）整个替换为：

```python
def import_models_dev(data_dir=None):
    """强制刷新 models.dev 目录，按当前记录里的模型匹配并写入 prices.json（覆盖同名）。

    手动导入是显式操作：强制刷新目录缓存、覆盖匹配到的已有价格，
    并把未匹配模型写入缓存 unmatched（目录刷新前自动匹配不再重试）。
    """
    data_dir = data_dir or DATA_DIR
    index = get_catalog_index(data_dir, force=True)
    records, _ = merged_records()
    our_models = sorted({str(r.get("model", "")).lower() for r in records if r.get("model")})
    matched, unmatched = {}, []
    for model in our_models:
        entry = match_model(index, model)
        if entry:
            matched[model] = entry
        else:
            unmatched.append(model)
    for model, entry in matched.items():
        with _lock:
            save_user_price(data_dir, model, entry)
    _write_catalog_cache(data_dir, index, {m: int(time.time()) for m in unmatched})
    return {"matched": matched, "unmatched": unmatched}
```

注意：旧实现里函数内的 `import urllib.request` 行随函数体一起删除（Task 1 已移到顶部）。

把 GET `/api/prices` 分支（server.py:419-423）改为复用 helper：

```python
        elif path == "/api/prices":
            prices = load_prices(DATA_DIR)
            records, _ = merged_records()
            self._send_json({"prices": prices, "unpriced": unpriced_models(records, prices)})
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd c:/code/pythoncode/tokenlens && .venv/Scripts/python.exe -m unittest discover -s tests -v`
Expected: 全部 PASS（Task 1 的 5 个 + 本 Task 的 7 个，累计 12 个）

- [ ] **Step 5: Commit**

```bash
cd c:/code/pythoncode/tokenlens
git add tests/test_auto_prices.py server.py
git commit -m "重构价格导入：抽取 match_model/unpriced_models，import 走目录缓存

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 3: 自动匹配核心（auto_match_prices + maybe_auto_match + 挂钩 + unpriced 字段）

**Files:**
- Modify: `server.py`（替换 `maybe_auto_match` 占位；`merged_records` server.py:110-116；`/api/records` 分支 server.py:395-397；POST `/api/prices` 的保存行 server.py:477）
- Test: `tests/test_auto_prices.py`（追加）

- [ ] **Step 1: 追加失败的测试**

在 `tests/test_auto_prices.py` 的 `if __name__ == "__main__":` 之前追加：

```python
class AutoMatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        # 预置目录缓存（含一个可匹配模型），避免走网络
        server._write_catalog_cache(self.tmp, {"new-model": {"input": 1.0, "output": 2.0}}, {})

    def _prices_path(self):
        return os.path.join(self.tmp, "prices.json")

    def test_fills_missing_only(self):
        """只补缺失：已有价格（手动 99）不被覆盖；new-model 自动补；no-match 记入 unmatched。"""
        with open(self._prices_path(), "w", encoding="utf-8") as f:
            json.dump({"existing": {"input": 9.0, "output": 9.0}}, f)
        server.auto_match_prices(self.tmp, ["existing", "new-model", "no-match"])
        with open(self._prices_path(), encoding="utf-8") as f:
            prices = json.load(f)
        self.assertEqual(prices["existing"], {"input": 9.0, "output": 9.0})
        self.assertEqual(prices["new-model"], {"input": 1.0, "output": 2.0})
        cache = server._read_catalog_cache(self.tmp)
        self.assertIn("no-match", cache["unmatched"])
        # 可匹配项不应进 unmatched
        self.assertNotIn("new-model", cache["unmatched"])

    def test_skips_known_unmatched(self):
        server._write_catalog_cache(self.tmp, {"new-model": {"input": 1.0, "output": 2.0}},
                                    {"no-match": int(time.time())})
        with mock.patch.object(server, "get_catalog_index",
                               side_effect=AssertionError("不应拉目录")):
            server.auto_match_prices(self.tmp, ["no-match"])
        self.assertFalse(os.path.exists(self._prices_path()))

    def test_offline_silent(self):
        os.remove(os.path.join(self.tmp, "models_dev_cache.json"))
        with mock.patch.object(server, "_fetch_catalog_json", side_effect=OSError("offline")):
            server.auto_match_prices(self.tmp, ["new-model"])  # 不抛异常
        self.assertFalse(os.path.exists(self._prices_path()))


class MaybeAutoMatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        server._auto_match_last_try = 0  # 重置全局节流

    def test_spawns_thread(self):
        with mock.patch.object(server, "auto_match_prices") as am:
            t = server.maybe_auto_match(self.tmp, [{"model": "New-Model"}], {})
            self.assertIsNotNone(t)
            t.join(5)
            am.assert_called_once_with(self.tmp, ["new-model"])

    def test_skips_when_all_unmatched(self):
        server._write_catalog_cache(self.tmp, {"x": {"input": 1, "output": 1}},
                                    {"new-model": int(time.time())})
        with mock.patch.object(server, "auto_match_prices") as am:
            t = server.maybe_auto_match(self.tmp, [{"model": "New-Model"}], {})
            self.assertIsNone(t)
            am.assert_not_called()

    def test_throttled(self):
        server._auto_match_last_try = time.time()
        with mock.patch.object(server, "auto_match_prices") as am:
            t = server.maybe_auto_match(self.tmp, [{"model": "New-Model"}], {})
            self.assertIsNone(t)
            am.assert_not_called()


class MergedRecordsHookTest(unittest.TestCase):
    def test_merged_records_triggers_auto_match(self):
        tmp = tempfile.mkdtemp()
        with mock.patch.object(server, "DATA_DIR", tmp), \
             mock.patch.object(server, "DATA_FILE", os.path.join(tmp, "records.json")), \
             mock.patch.object(server, "maybe_auto_match") as mam:
            server.save_records([{"id": 1, "date": "2026-07-19", "agent": "A", "model": "Zzz",
                                  "requests": 1, "input": 1, "output": 1, "cached": 0, "cost": 0}])
            records, _ = server.merged_records()
        self.assertTrue(mam.called)
        self.assertEqual(records[0]["source"], "manual")
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd c:/code/pythoncode/tokenlens && .venv/Scripts/python.exe -m unittest discover -s tests -v`
Expected: FAIL — `AttributeError: module 'server' has no attribute 'auto_match_prices'` / `maybe_auto_match` 返回 None 等

- [ ] **Step 3: 实现**

把 Task 2 的 `maybe_auto_match` 占位替换为完整实现（放在 `unpriced_models` 之后）：

```python
AUTO_MATCH_MIN_INTERVAL = 300  # 自动匹配最小间隔秒数（失败时避免频繁重试）
_auto_match_lock = threading.Lock()
_auto_match_last_try = 0


def auto_match_prices(data_dir, models):
    """给无价格模型自动匹配目录价格。只补缺失，不覆盖已有价格；失败静默。"""
    cache = _read_catalog_cache(data_dir)
    unmatched = dict((cache or {}).get("unmatched") or {})
    todo = [m for m in models if m not in unmatched]
    if not todo:
        return
    try:
        index = get_catalog_index(data_dir)
    except Exception:
        return  # 离线等失败：静默跳过，下次检测到再试
    prices = load_prices(data_dir)
    for model in todo:
        if model in prices:
            continue  # 只补缺失（含默认价格表里的模型）
        entry = match_model(index, model)
        if entry:
            with _lock:
                save_user_price(data_dir, model, entry)
        else:
            unmatched[model] = int(time.time())
    # 把新出现的未匹配模型写回缓存（目录未刷新前不再重试）
    cache = _read_catalog_cache(data_dir) or {}
    if cache.get("index"):
        merged = {**(cache.get("unmatched") or {}), **unmatched}
        _write_catalog_cache(data_dir, cache["index"], merged)


def maybe_auto_match(data_dir, records, prices):
    """发现无价格模型时后台线程自动匹配（不阻塞请求）。返回 Thread 或 None（供测试 join）。"""
    global _auto_match_last_try
    todo = unpriced_models(records, prices)
    if not todo:
        return None
    cache = _read_catalog_cache(data_dir)
    unmatched = (cache or {}).get("unmatched") or {}
    todo = [m for m in todo if m not in unmatched]
    now = time.time()
    if not todo or now - _auto_match_last_try < AUTO_MATCH_MIN_INTERVAL:
        return None
    if not _auto_match_lock.acquire(blocking=False):
        return None
    _auto_match_last_try = now

    def run():
        try:
            auto_match_prices(data_dir, todo)
        except Exception:
            pass
        finally:
            _auto_match_lock.release()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t
```

把 `merged_records`（server.py:110-116）改为：

```python
def merged_records():
    prices = load_prices(DATA_DIR)
    manual = load_records()
    for r in manual:
        r.setdefault("source", "manual")
    auto = collector.load_auto_records(DATA_DIR, prices, calc_cost)
    records = manual + auto
    maybe_auto_match(DATA_DIR, records, prices)  # 发现新模型时后台自动补价
    return records, prices
```

把 `/api/records` 分支（server.py:395-397）改为：

```python
        elif path == "/api/records":
            records, prices = merged_records()
            self._send_json({"records": records, "prices": prices, "agents": KNOWN_AGENTS,
                             "unpriced": unpriced_models(records, prices)})
```

把 POST `/api/prices` 里的保存行（server.py:477 `save_user_price(DATA_DIR, model, entry)`）改为加锁：

```python
            with _lock:
                save_user_price(DATA_DIR, model, entry)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd c:/code/pythoncode/tokenlens && .venv/Scripts/python.exe -m unittest discover -s tests -v`
Expected: 全部 PASS（累计 19 个）

- [ ] **Step 5: Commit**

```bash
cd c:/code/pythoncode/tokenlens
git add tests/test_auto_prices.py server.py
git commit -m "新模型自动匹配价格：后台线程只补缺失，unmatched 防重复，/api/records 返回 unpriced

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 4: 前端"未定价"徽标 + 提示条计数

前端无测试框架，本任务为代码修改 + 人工验证。

**Files:**
- Modify: `static/index.html:116`（提示条 span 加 id）
- Modify: `static/app.js`（state、loadData、卡片模板、render）
- Modify: `static/style.css`（`.price-edit` 规则 server 212 行之后）

- [ ] **Step 1: index.html 提示条加 id**

把 `static/index.html:116`：

```html
<span class="card-hint">订阅制模型按官方 API 参考价估算，不代表真实扣费；点卡片上的 ✎ 可改价</span>
```

改为：

```html
<span class="card-hint" id="models-hint">订阅制模型按官方 API 参考价估算，不代表真实扣费；点卡片上的 ✎ 可改价</span>
```

- [ ] **Step 2: app.js 四处修改**

① state（app.js:8 `prices: {},` 之后）加一行：

```js
    unpriced: [],
```

② loadData（app.js:71 `state.prices = data.prices;` 之后）加一行：

```js
    state.unpriced = data.unpriced || [];
```

③ renderEntityCards 卡片头部（app.js:307），把原来一行：

```js
          ${type === "model" ? `<button class="icon-btn price-edit" data-name="${esc(m.name)}" title="编辑价格">✎</button>` : ""}
```

改为两行（徽标在 ✎ 左侧）：

```js
          ${type === "model" && state.unpriced.includes(String(m.name).toLowerCase()) ? `<span class="price-badge">未定价</span>` : ""}
          ${type === "model" ? `<button class="icon-btn price-edit" data-name="${esc(m.name)}" title="编辑价格">✎</button>` : ""}
```

④ render() 的 models 分支（app.js:610-611），把：

```js
    } else if (state.view === "models") {
      renderEntityCards(list, "model", "#model-cards", "model");
    }
```

改为：

```js
    } else if (state.view === "models") {
      renderModelsHint();
      renderEntityCards(list, "model", "#model-cards", "model");
    }
```

并在 render() 函数之前（app.js:600 行 `function render() {` 上方）新增：

```js
  function renderModelsHint() {
    const n = state.unpriced.length;
    $("#models-hint").textContent =
      "订阅制模型按官方 API 参考价估算，不代表真实扣费；点卡片上的 ✎ 可改价"
      + (n ? ` · ${n} 个模型未定价，点 ✎ 设置` : "");
  }
```

- [ ] **Step 3: style.css 加徽标样式**

在 `.price-edit` 规则（style.css:212）之后追加：

```css
.price-badge {
  font-size: 11px; color: #fbbf24; background: rgba(251, 191, 36, 0.12);
  border: 1px solid rgba(251, 191, 36, 0.35); border-radius: 6px;
  padding: 2px 6px; margin-left: auto; flex-shrink: 0;
}
.price-badge + .price-edit { margin-left: 0; }
```

（`.price-edit` 原本靠 `margin-left: auto` 靠右；徽标插入后由徽标占位靠右，✎ 紧随其后。）

- [ ] **Step 4: 人工验证**

```bash
cd c:/code/pythoncode/tokenlens && .venv/Scripts/python.exe server.py
```

窗口打开后：
1. 点「＋ 录入数据」，模型填 `DeepSeek V3.2`（默认价格表里有）保存 → 「模型」页卡片**无**"未定价"徽标
2. 再录入一条，模型填 `TestNoPrice123` 保存 → 切到「模型」页，该卡片出现橙色"未定价"徽标；提示条末尾显示"· 1 个模型未定价，点 ✎ 设置"
3. 点该卡片 ✎ 随便填价保存 → 徽标消失（下次 loadData 后 unpriced 为空）

关闭窗口（或 Ctrl+C）停止服务。

- [ ] **Step 5: Commit**

```bash
cd c:/code/pythoncode/tokenlens
git add static/index.html static/app.js static/style.css
git commit -m "模型卡片显示未定价徽标与提示条计数

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 5: 端到端验证 + 重新打包 exe

**Files:** 无新增代码。

- [ ] **Step 1: 全量测试**

Run: `cd c:/code/pythoncode/tokenlens && .venv/Scripts/python.exe -m unittest discover -s tests -v`
Expected: 19 个测试全部 PASS

- [ ] **Step 2: 自动匹配端到端验证（联网）**

```bash
cd c:/code/pythoncode/tokenlens && .venv/Scripts/python.exe server.py
```

1. 录入一条记录，模型填 `GLM-4.6`（不在默认价格表、models.dev 有收录）
2. 点「⟳ 同步本地数据」（触发 /api/records → 后台自动匹配），等 3-5 秒再点一次同步刷新数据
3. 预期：`data/prices.json` 出现 `glm-4.6` 条目；「模型」页该卡片**无**"未定价"徽标，费用不再为 $0；`data/models_dev_cache.json` 已生成
4. 再录入 `TestNoPrice456` → 同步两次 → 该模型进缓存 `unmatched`，卡片显示"未定价"徽标；费用保持 $0 符合预期
5. 关闭服务，再次启动并同步 → 不再重复请求匹配（缓存 24h 内有效， unmatched 防重试；可从网络无卡顿、徽标依旧来确认）

验证完成后清理测试数据：删除 `data/records.json` 里手动加的测试记录（或在 UI 明细记录页删除），`prices.json` 里不需要的测试条目可保留可删。

- [ ] **Step 3: 重新打包 exe**

项目无 spec 文件，按 `server.py` 的 `sys._MEIPASS/static` 布局，参考命令（如你已有自己的打包方式，用你自己的）：

```bash
cd c:/code/pythoncode/tokenlens && .venv/Scripts/pyinstaller.exe --onefile --windowed --name TokenLens --icon icon.ico --add-data "static;static" server.py
```

生成的 `dist/TokenLens.exe` 替换项目根的 `TokenLens.exe`，双击启动确认正常。

- [ ] **Step 4: 最终 Commit**

```bash
cd c:/code/pythoncode/tokenlens
git add TokenLens.exe
git commit -m "重新打包 exe：新模型自动匹配价格 + 未定价徽标

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Self-Review 记录

- **Spec 覆盖**：目录缓存（Task 1）✓；手动导入强制刷新+覆盖（Task 2）✓；自动触发/只补缺失/unmatched 防重试/静默失败/写文件加锁（Task 3）✓；`/api/records` unpriced 字段（Task 3）✓；前端徽标+提示条（Task 4）✓；exe 打包（Task 5）✓。非目标均未引入。
- **类型一致性**：`build_catalog_index` 返回 `dict[str, entry]`；`get_catalog_index(data_dir, force=False)` 在 Task 1 定义、Task 2/3 使用签名一致；`match_model(index, model)`、`unpriced_models(records, prices)`、`auto_match_prices(data_dir, models)`、`maybe_auto_match(data_dir, records, prices) -> Thread|None` 全文一致；缓存结构 `{fetched_at, index, unmatched}` 各处一致。
- **Placeholder 扫描**：无 TBD/TODO；每个代码步骤均含完整代码。
