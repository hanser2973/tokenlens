# -*- coding: utf-8 -*-
"""模型价格表与费用计算。用户可在 data/prices.json 中覆盖/补充价格。"""
import json
import os

# 默认价格（美元 / 百万 tokens）
MODEL_PRICES = {
    "Kimi K2":           {"input": 0.60, "output": 2.50},
    "Claude Sonnet 4.5": {"input": 3.00, "output": 15.00},
    "GPT-5.2":           {"input": 1.75, "output": 14.00},
    "DeepSeek V3.2":     {"input": 0.28, "output": 0.42},
    "Gemini 3 Pro":      {"input": 1.25, "output": 10.00},
    "Qwen3 Coder":       {"input": 0.30, "output": 1.20},
    "Doubao Seed 1.6":   {"input": 0.25, "output": 2.00},
}
CACHED_RATIO = 0.1  # 缓存命中 tokens 按输入价 1 折计


def load_prices(data_dir):
    prices = dict(MODEL_PRICES)
    user_file = os.path.join(data_dir, "prices.json")
    if os.path.exists(user_file):
        try:
            with open(user_file, "r", encoding="utf-8") as f:
                for k, v in json.load(f).items():
                    if isinstance(v, dict) and "input" in v and "output" in v:
                        prices[k] = {"input": float(v["input"]), "output": float(v["output"])}
        except (OSError, ValueError):
            pass
    return prices


def calc_cost(prices, model, input_tokens, output_tokens, cached_tokens):
    p = prices.get(model)
    if not p:
        return 0.0
    cost = (input_tokens * p["input"]
            + output_tokens * p["output"]
            + cached_tokens * p["input"] * CACHED_RATIO) / 1_000_000
    return round(cost, 4)
