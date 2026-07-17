# -*- coding: utf-8 -*-
"""模型价格表与费用计算。

价格来源（优先级从高到低）：
1. data/prices.json —— 用户手动填写或从 models.dev 导入（键一律小写）
2. 下面的默认价格（键一律小写；订阅制模型为同系列官方 API 参考价，仅供估算）

价格单位：美元 / 百万 tokens。cache_read 缺省时按输入价 1 折估算。
"""
import json
import os

# 默认价格（美元 / 百万 tokens），键必须小写
MODEL_PRICES = {
    # Kimi 系列（models.dev / moonshotai 官方价；订阅模型为参考价）
    "k3":                 {"input": 3.00, "output": 15.00, "cache_read": 0.30},
    "kimi-k3":            {"input": 3.00, "output": 15.00, "cache_read": 0.30},
    "k2p6":               {"input": 0.95, "output": 4.00, "cache_read": 0.16},
    "kimi-k2.6":          {"input": 0.95, "output": 4.00, "cache_read": 0.16},
    "k2p7":               {"input": 0.95, "output": 4.00, "cache_read": 0.19},
    "kimi-k2.7-code":     {"input": 0.95, "output": 4.00, "cache_read": 0.19},
    "kimi-for-coding":    {"input": 0.95, "output": 4.00, "cache_read": 0.19},
    # 其他常见模型
    "kimi k2":            {"input": 0.60, "output": 2.50},
    "claude sonnet 4.5":  {"input": 3.00, "output": 15.00},
    "gpt-5.2":            {"input": 1.75, "output": 14.00},
    "deepseek v3.2":      {"input": 0.28, "output": 0.42},
    "gemini 3 pro":       {"input": 1.25, "output": 10.00},
    "qwen3 coder":        {"input": 0.30, "output": 1.20},
    "doubao seed 1.6":    {"input": 0.25, "output": 2.00},
}
CACHED_RATIO = 0.1  # 无 cache_read 价格时，缓存命中 tokens 按输入价 1 折计


def _norm_entry(v):
    """校验并规范化一条价格，非法返回 None。"""
    if not isinstance(v, dict) or "input" not in v or "output" not in v:
        return None
    try:
        entry = {"input": float(v["input"]), "output": float(v["output"])}
        if v.get("cache_read") is not None:
            entry["cache_read"] = float(v["cache_read"])
        return entry
    except (TypeError, ValueError):
        return None


def load_prices(data_dir):
    prices = {}
    for k, v in MODEL_PRICES.items():
        e = _norm_entry(v)
        if e:
            prices[k.lower()] = e
    user_file = os.path.join(data_dir, "prices.json")
    if os.path.exists(user_file):
        try:
            with open(user_file, "r", encoding="utf-8") as f:
                for k, v in json.load(f).items():
                    e = _norm_entry(v)
                    if e:
                        prices[str(k).lower()] = e
        except (OSError, ValueError):
            pass
    return prices


def save_user_price(data_dir, model, entry):
    """把单个模型价格写入 data/prices.json（用户层，覆盖默认与同名项）。"""
    user_file = os.path.join(data_dir, "prices.json")
    data = {}
    if os.path.exists(user_file):
        try:
            with open(user_file, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = {}
    data[str(model).lower()] = entry
    os.makedirs(data_dir, exist_ok=True)
    with open(user_file, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)


def calc_cost(prices, model, input_tokens, output_tokens, cached_tokens):
    p = prices.get(str(model or "").lower())
    if not p:
        return 0.0
    cache_price = p.get("cache_read")
    if cache_price is None:
        cache_price = p["input"] * CACHED_RATIO
    cost = (input_tokens * p["input"]
            + output_tokens * p["output"]
            + cached_tokens * cache_price) / 1_000_000
    return round(cost, 4)
