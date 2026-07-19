# -*- coding: utf-8 -*-
"""
TokenLens · AI / Agent Token 消耗统计看板
- 自动扫描本机 AI 工具日志采集真实 token 用量（见 collector.py）
- 也可手动录入数据
- 优先以原生窗口运行（pywebview），不可用时回退为浏览器打开
"""
import json
import os
import random
import socket
import sys
import threading
import time
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
import urllib.request

import collector
import quota
from pricing import load_prices, calc_cost, save_user_price

if getattr(sys, "frozen", False):
    # 打包成 exe 后：静态资源在 exe 内（sys._MEIPASS），数据文件放在 exe 同目录
    BASE_DIR = os.path.dirname(sys.executable)
    STATIC_DIR = os.path.join(sys._MEIPASS, "static")
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    STATIC_DIR = os.path.join(BASE_DIR, "static")
DATA_DIR = os.path.join(BASE_DIR, "data")
DATA_FILE = os.path.join(DATA_DIR, "records.json")
PLANS_FILE = os.path.join(DATA_DIR, "plans.json")
CAL_FILE = os.path.join(DATA_DIR, "quota_cal.json")  # 跨设备配额校准值（随 git 同步）
COLLECT_INTERVAL = 600  # 每 10 分钟自动扫描一次本地日志

# 手动配额的周期定义：标签 + 周期秒数（按滚动窗口统计）
PLAN_PERIODS = {
    "5h":    ("5 小时窗口", 5 * 3600),
    "week":  ("每周", 7 * 86400),
    "month": ("每月", 30 * 86400),
}

_lock = threading.Lock()

# 供下拉列表使用的常见 Agent 名（实际数据以扫描结果为准）
KNOWN_AGENTS = ["Kimi Code", "Claude Code", "Codex CLI", "Cursor", "Cline", "Trae", "Gemini CLI", "Aider"]


def load_records():
    with _lock:
        if not os.path.exists(DATA_FILE):
            save_records([])
            return []
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except ValueError:
            return []


def save_records(records):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=1)


def seed_demo_data():
    """仅在使用 --demo 启动时生成演示数据。"""
    rng = random.Random(42)
    prices = load_prices(DATA_DIR)
    agents_models = {
        "Kimi Code": ["Kimi K2"], "Claude Code": ["Claude Sonnet 4.5"],
        "Cursor": ["GPT-5.2", "Gemini 3 Pro"], "Codex CLI": ["GPT-5.2"],
        "Cline": ["DeepSeek V3.2", "Qwen3 Coder"], "Trae": ["Doubao Seed 1.6"],
    }
    records, rid, today = [], 1, date.today()
    for d in range(45, 0, -1):
        day = today - timedelta(days=d)
        factor = 0.45 if day.weekday() >= 5 else 1.0
        for agent, models in agents_models.items():
            if rng.random() > 0.75 * factor:
                continue
            for model in models:
                if rng.random() > 0.55:
                    continue
                req = rng.randint(3, 40)
                inp = int(req * rng.uniform(1800, 9000))
                out = int(inp * rng.uniform(0.15, 0.45))
                cached = int(inp * rng.uniform(0.0, 0.35))
                records.append({
                    "id": rid, "date": day.isoformat(), "agent": agent, "model": model,
                    "requests": req, "input": inp, "output": out, "cached": cached,
                    "cost": calc_cost(prices, model, inp, out, cached), "source": "demo",
                })
                rid += 1
    save_records(records)


CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
}


def merged_records():
    prices = load_prices(DATA_DIR)
    manual = load_records()
    for r in manual:
        r.setdefault("source", "manual")
    auto = collector.load_auto_records(DATA_DIR, prices, calc_cost)
    return manual + auto, prices


# ---------- 价格表（models.dev 导入） ----------

MODELS_DEV_URL = "https://models.dev/api.json"
# 本地模型 id → models.dev 目录 id 的别名（订阅模型映射到同系列官方 API 参考价）
MODEL_ALIASES = {
    "k3": "kimi-k3",
    "k2p6": "kimi-k2.6",
    "k2p7": "kimi-k2.7-code",
    "kimi-for-coding": "kimi-k2.7-code",
    "kimi-for-coding-highspeed": "kimi-k2.7-code-highspeed",
}
# 目录提供方优先级（越靠前越官方）
PREFERRED_PROVIDERS = ["moonshotai", "moonshotai-cn", "zhipu", "deepseek", "openai",
                       "anthropic", "google", "qwen", "volcengine", "minimax", "kimi-for-coding"]

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
    try:
        fetched_at = float(cache.get("fetched_at") or 0) if cache else 0
    except (TypeError, ValueError):
        fetched_at = 0  # 非数字按过期处理
    fresh = cache and (time.time() - fetched_at < CATALOG_TTL)
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


# ---------- Plan（套餐配额）用量 ----------

def load_plans():
    try:
        with open(PLANS_FILE, "r", encoding="utf-8") as f:
            plans = json.load(f)
        return plans if isinstance(plans, list) else []
    except (OSError, ValueError):
        return []


def save_plans(plans):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(PLANS_FILE, "w", encoding="utf-8") as f:
        json.dump(plans, f, ensure_ascii=False, indent=1)


def _window_label(minutes):
    if minutes == 300:
        return "5 小时窗口"
    if minutes == 10080:
        return "每周窗口"
    if minutes >= 1440:
        return f"{minutes // 1440} 天窗口"
    return f"{minutes} 分钟窗口"


def _period_used(records, agent, span):
    """某 Agent 在当前滚动周期内采集到的 tokens 合计（含缓存，按天粒度）。"""
    from_day = (date.today() - timedelta(days=max(0, -(-span // 86400)) - 1)).isoformat()
    return sum(r["input"] + r["output"] + r["cached"]
               for r in records if r.get("agent") == agent and r.get("date", "") >= from_day)


def _load_cal():
    try:
        with open(CAL_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_cal(d):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(CAL_FILE, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)


def _calibrated_quota(agent_kind, window):
    """跨设备校准的窗口总额估算，返回 (quota, carried)。

    两条估算路径：
    1) 差分（优先）：同一窗口内 本机用量增量 ÷ 官方百分比增量 —— 只需观察区间内的
       本地数据，新设备/换设备也能快速校准，不依赖 git 同步历史；
    2) 时点（兜底）：本机窗口实测 ÷ 官方百分比 —— 需要本机日志覆盖整个窗口。
    新估算低于旧值 70% 时判定为日志不全，沿用旧校准（carried=True）；
    超过一个完整窗口仍未恢复才接受新值。
    """
    mins = int(window.get("window_minutes") or 0)
    pct = float(window.get("used_percent") or 0)
    measured = window.get("measured_tokens") or 0
    resets_at = window.get("resets_at")
    key = f"{agent_kind}|{window.get('label')}"
    now = time.time()
    span = mins * 60 if mins else 7 * 86400
    cal = _load_cal()
    st = cal.get(key) or {}
    changed = False

    # 差分锚点：窗口重置，或实测倒退（说明换了设备/日志被清）时重新锚定
    anchor = st.get("anchor") or {}
    if anchor.get("resets_at") != resets_at or measured < int(anchor.get("measured") or 0):
        anchor = {"resets_at": resets_at, "pct": pct, "measured": measured}
        st["anchor"] = anchor
        changed = True

    q_delta = None
    d_pct = pct - float(anchor.get("pct") or 0)
    d_used = measured - int(anchor.get("measured") or 0)
    if d_pct >= 1 and d_used > 0:
        q_delta = round(d_used / d_pct * 100)

    q_point = round(measured * 100 / pct) if 0 < pct < 100 and measured > 0 else None
    q_cand = q_delta or q_point

    q_old = st.get("quota")
    if q_cand and (not q_old or q_cand >= 0.7 * q_old or now - st.get("updated", 0) > span):
        st["quota"] = q_cand
        st["updated"] = int(now)
        changed = True
        q_out, carried = q_cand, False
    elif q_old:
        q_out, carried = q_old, True
    else:
        q_out, carried = q_cand, False
    if changed:
        cal[key] = st
        _save_cal(cal)
    return q_out, carried


def _estimate_window(agent_kind, window):
    """给官方窗口补本机实测 tokens，并按跨设备校准值估算总额/剩余。"""
    mins = int(window.get("window_minutes") or 0)
    resets_at = window.get("resets_at")
    start = (resets_at - mins * 60) if (resets_at and mins) else time.time() - mins * 60
    used = collector.usage_since(agent_kind, start) or {}
    measured = used.get("input", 0) + used.get("output", 0) + used.get("cached", 0)
    window["measured_tokens"] = measured
    q, carried = _calibrated_quota(agent_kind, window)
    pct = float(window.get("used_percent") or 0)
    window["est_quota"] = q
    window["est_remaining"] = round(q * (1 - pct / 100)) if q else None
    window["est_carried"] = carried
    return window


def compute_plans():
    """汇总 Plan 用量：Kimi/Codex 读官方数据并估算剩余；手动配额按采集数据统计。"""
    now = time.time()
    result = {"auto": [], "manual": [], "now": now}

    # Kimi：官方接口实时查询（百分比 + 重置时间），失败时展示错误提示
    kimi_card = quota.query_kimi()
    if not kimi_card.get("error"):
        for w in kimi_card.get("windows", []):
            _estimate_window("kimi", w)
        kimi_card["note"] = "官方接口 · 实时"
    result["auto"].append(kimi_card)

    # Codex：本地日志里的官方 rate_limits
    rl = collector.latest_rate_limits()
    if rl:
        limits = rl["rate_limits"]
        windows = []
        for kind in ("primary", "secondary"):
            w = limits.get(kind)
            if not w:
                continue
            mins = int(w.get("window_minutes") or 0)
            windows.append(_estimate_window("codex", {
                "label": _window_label(mins),
                "used_percent": float(w.get("used_percent") or 0),
                "resets_at": w.get("resets_at"),
                "window_minutes": mins,
            }))
        result["auto"].append({
            "agent": "Codex CLI",
            "plan_type": limits.get("plan_type") or "unknown",
            "updated": rl.get("timestamp"),
            "note": "官方限额 · 来自本地日志",
            "windows": windows,
        })

    records, _ = merged_records()
    for p in load_plans():
        label, span = PLAN_PERIODS.get(p.get("period"), PLAN_PERIODS["week"])
        used = _period_used(records, p.get("agent"), span)
        item = {**p, "period_label": label, "used": used}
        if p.get("mode") == "percent":
            # 百分比锚定：录入时的实测用量 ÷ 录入的百分比 ≈ 总额度，之后随采集数据自动推算
            anchor_pct = float(p.get("anchor_pct") or 0)
            anchor_used = int(p.get("anchor_used") or 0)
            est_quota = round(anchor_used * 100 / anchor_pct) if 0 < anchor_pct < 100 and anchor_used > 0 else None
            item["est_quota"] = est_quota
            if est_quota:
                item["pct"] = round(min(100.0, used / est_quota * 100), 1)
                item["remaining"] = max(0, est_quota - used)
            else:
                item["pct"] = round(anchor_pct, 1)
                item["remaining"] = None
        else:
            quota_tokens = max(1, int(p.get("quota") or 1))
            item["pct"] = round(min(100.0, used / quota_tokens * 100), 1)
            item["remaining"] = max(0, quota_tokens - used)
        result["manual"].append(item)
    return result


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # 静音访问日志
        pass

    def _send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path):
        ext = os.path.splitext(path)[1].lower()
        ctype = CONTENT_TYPES.get(ext, "application/octet-stream")
        try:
            with open(path, "rb") as f:
                body = f.read()
        except OSError:
            self._send_json({"error": "not found"}, 404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlparse(self.path)
        path = url.path
        if path == "/":
            self._send_file(os.path.join(STATIC_DIR, "index.html"))
        elif path == "/api/records":
            records, prices = merged_records()
            self._send_json({"records": records, "prices": prices, "agents": KNOWN_AGENTS})
        elif path == "/api/plans":
            try:
                self._send_json(compute_plans())
            except Exception as e:
                self._send_json({"error": str(e)}, 500)
        elif path == "/api/timeline":
            qs = parse_qs(url.query)
            day = qs.get("day", [""])[0]
            try:
                hour = qs.get("hour", [""])[0]
                hour = int(hour) if hour != "" else None
                if not day:
                    raise ValueError("day 不能为空")
                rows = collector.breakdown(day, hour)
            except (ValueError, TypeError):
                self._send_json({"error": "day/hour 参数不合法"}, 400)
                return
            prices = load_prices(DATA_DIR)
            for r in rows:
                r["cost"] = calc_cost(prices, r["model"], r["input"], r["output"], r["cached"])
            self._send_json({"rows": rows, "day": day, "hour": hour})
        elif path == "/api/prices":
            prices = load_prices(DATA_DIR)
            records, _ = merged_records()
            self._send_json({"prices": prices, "unpriced": unpriced_models(records, prices)})
        elif path.startswith("/static/"):
            rel = path[len("/static/"):]
            full = os.path.normpath(os.path.join(STATIC_DIR, rel))
            if not full.startswith(STATIC_DIR):
                self._send_json({"error": "forbidden"}, 403)
                return
            self._send_file(full)
        else:
            self._send_json({"error": "not found"}, 404)

    def do_POST(self):
        url = urlparse(self.path)
        if url.path == "/api/collect":
            try:
                summary = collector.collect_once(DATA_DIR)
            except Exception as e:  # 采集失败不影响主程序
                self._send_json({"error": str(e)}, 500)
                return
            self._send_json({"ok": True, "summary": summary})
            return
        if url.path == "/api/prices/import":
            try:
                result = import_models_dev()
            except Exception as e:
                self._send_json({"error": f"拉取 models.dev 失败: {e}"}, 500)
                return
            self._send_json({"ok": True, **result})
            return
        if url.path == "/api/prices":
            try:
                length = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
            except Exception:
                self._send_json({"error": "invalid json"}, 400)
                return
            model = str(payload.get("model", "")).strip()
            if not model:
                self._send_json({"error": "model 不能为空"}, 400)
                return
            def to_price(v):
                try:
                    return max(0.0, float(v))
                except (TypeError, ValueError):
                    return None
            inp = to_price(payload.get("input"))
            out = to_price(payload.get("output"))
            if inp is None or out is None:
                self._send_json({"error": "input/output 价格需为不小于 0 的数字"}, 400)
                return
            entry = {"input": inp, "output": out}
            cache = to_price(payload.get("cache_read")) if payload.get("cache_read") not in (None, "") else None
            if cache is not None:
                entry["cache_read"] = cache
            save_user_price(DATA_DIR, model, entry)
            self._send_json({"ok": True, "model": model.lower(), "price": entry})
            return
        if url.path == "/api/plans":
            try:
                length = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
            except Exception:
                self._send_json({"error": "invalid json"}, 400)
                return
            agent = str(payload.get("agent", "")).strip()
            period = str(payload.get("period", "")).strip()
            mode = str(payload.get("mode", "quota")).strip()
            if not agent or period not in PLAN_PERIODS:
                self._send_json({"error": "agent/period 不合法"}, 400)
                return
            plan = {
                "agent": agent,
                "period": period,
                "mode": "percent" if mode == "percent" else "quota",
            }
            if plan["mode"] == "percent":
                try:
                    pct = float(payload.get("pct") or 0)
                except (TypeError, ValueError):
                    pct = 0
                if not 0 < pct < 100:
                    self._send_json({"error": "百分比需在 0-100 之间"}, 400)
                    return
                # 锚定：记录录入时的实测周期用量，之后随采集数据自动推算剩余
                records, _ = merged_records()
                plan["anchor_pct"] = pct
                plan["anchor_used"] = _period_used(records, agent, PLAN_PERIODS[period][1])
                plan["anchor_at"] = int(time.time())
            else:
                try:
                    quota = int(payload.get("quota") or 0)
                except (TypeError, ValueError):
                    quota = 0
                if quota <= 0:
                    self._send_json({"error": "quota 不合法"}, 400)
                    return
                plan["quota"] = quota
            plans = load_plans()
            plan["id"] = max((p.get("id", 0) for p in plans), default=0) + 1
            name = str(payload.get("name", "")).strip() or f"{agent} · {PLAN_PERIODS[period][0]}配额"
            plan["name"] = name
            with _lock:
                plans.append(plan)
                save_plans(plans)
            self._send_json({"ok": True, "plan": plan})
            return
        if url.path != "/api/records":
            self._send_json({"error": "not found"}, 404)
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            self._send_json({"error": "invalid json"}, 400)
            return

        required = ["date", "agent", "model"]
        if any(not str(payload.get(k, "")).strip() for k in required):
            self._send_json({"error": "date/agent/model 不能为空"}, 400)
            return

        def to_int(v):
            try:
                return max(0, int(v))
            except (TypeError, ValueError):
                return 0

        records = load_records()
        new_id = max((r["id"] for r in records if isinstance(r.get("id"), int)), default=0) + 1
        input_t = to_int(payload.get("input"))
        output_t = to_int(payload.get("output"))
        cached_t = to_int(payload.get("cached"))
        model = str(payload["model"]).strip()
        cost = payload.get("cost")
        try:
            cost = float(cost)
        except (TypeError, ValueError):
            cost = calc_cost(load_prices(DATA_DIR), model, input_t, output_t, cached_t)

        rec = {
            "id": new_id,
            "date": str(payload["date"]).strip(),
            "agent": str(payload["agent"]).strip(),
            "model": model,
            "requests": to_int(payload.get("requests")) or 1,
            "input": input_t,
            "output": output_t,
            "cached": cached_t,
            "cost": round(cost, 4),
            "source": "manual",
        }
        with _lock:
            records.append(rec)
            save_records(records)
        self._send_json({"ok": True, "record": rec})

    def do_DELETE(self):
        url = urlparse(self.path)
        if url.path == "/api/plans":
            qs = parse_qs(url.query)
            try:
                pid = int(qs.get("id", [""])[0])
            except ValueError:
                self._send_json({"error": "id 不合法"}, 400)
                return
            plans = load_plans()
            kept = [p for p in plans if p.get("id") != pid]
            if len(kept) == len(plans):
                self._send_json({"error": "配额不存在"}, 404)
                return
            with _lock:
                save_plans(kept)
            self._send_json({"ok": True})
            return
        if url.path != "/api/records":
            self._send_json({"error": "not found"}, 404)
            return
        qs = parse_qs(url.query)
        try:
            rid = int(qs.get("id", [""])[0])
        except ValueError:
            self._send_json({"error": "自动采集的记录不能删除"}, 400)
            return
        records = load_records()
        before = len(records)
        with _lock:
            records = [r for r in records if r.get("id") != rid]
            if len(records) == before:
                self._send_json({"error": "记录不存在"}, 404)
                return
            save_records(records)
        self._send_json({"ok": True})


def collect_loop():
    """启动后立即采集一次，之后定时增量扫描。"""
    while True:
        try:
            collector.collect_once(DATA_DIR)
        except Exception:
            pass
        time.sleep(COLLECT_INTERVAL)


def find_free_port(preferred):
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", preferred))
        return preferred
    except OSError:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def main():
    if "--demo" in sys.argv:
        seed_demo_data()
        print("已生成演示数据")

    load_records()  # 确保数据文件存在
    threading.Thread(target=collect_loop, daemon=True).start()

    port = find_free_port(8399)
    url = f"http://127.0.0.1:{port}"
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    try:
        import webview
        webview.create_window("TokenLens", url, width=1440, height=900, min_size=(1100, 700))
        webview.start()  # 窗口关闭后返回
        server.shutdown()
        return
    except ImportError:
        pass

    # 无 pywebview 时回退为浏览器打开
    import webbrowser
    print("=" * 52)
    print("  TokenLens · AI / Agent Token 消耗看板")
    print(f"  已在浏览器中打开: {url}")
    print("  关闭本窗口即可停止服务")
    print("=" * 52)
    webbrowser.open(url)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
