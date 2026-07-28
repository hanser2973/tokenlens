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
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
import urllib.request

import collector
import quota
import storage
import sync
from pricing import load_prices, calc_cost, save_user_price

if getattr(sys, "frozen", False):
    # 打包成 exe 后：静态资源在 exe 内（sys._MEIPASS），数据文件放在 exe 同目录
    BASE_DIR = os.path.dirname(sys.executable)
    STATIC_DIR = os.path.join(sys._MEIPASS, "static")
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    STATIC_DIR = os.path.join(BASE_DIR, "static")
DATA_DIR = os.path.join(BASE_DIR, "data")
STATIC_PREFIX = os.path.normcase(STATIC_DIR) + os.sep  # 静态文件防目录穿越的比较前缀
DATA_FILE = os.path.join(DATA_DIR, "records.json")
PLANS_FILE = os.path.join(DATA_DIR, "plans.json")
CAL_FILE = os.path.join(DATA_DIR, "quota_cal.json")  # 配额校准值（本地保存，勿公开提交）
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")
INSTANCE_FILE = os.path.join(DATA_DIR, "instance.json")  # 已运行实例的端口，用于单实例检测
DEFAULT_PORT = 8399
APP_ID = "tokenlens"  # /api/ping 的标识，用来区分「端口被自己占了」和「被别的程序占了」
COLLECT_INTERVAL = 600  # 每 10 分钟自动扫描一次本地日志
COLLECT_FRESH_WINDOW = 30  # 秒：这段时间内刚采集过，时间线就直接用现成的分钟桶

# 多端模式：
#   off        单机使用，沿用原有行为
#   local_only 多台电脑共用一个账号，但没有共享目录可同步 —— 官方百分比仍然准确，
#              但由本机日志换算出的绝对 token 数会按本机用量占比系统性低估，
#              因此隐藏所有绝对估算，只展示可信的百分比与重置时间
#   sync       多端 + 已配置共享目录 —— 汇总各设备的采集结果，绝对值重新可信
MULTI_MODES = ("off", "local_only", "sync")
DEFAULT_SETTINGS = {"multi_device": "off", "sync_dir": ""}

# 手动配额的周期定义：标签 + 周期秒数（按滚动窗口统计）
PLAN_PERIODS = {
    "5h":    ("5 小时窗口", 5 * 3600),
    "week":  ("每周", 7 * 86400),
    "month": ("每月", 30 * 86400),
}

_lock = threading.Lock()
_cal_lock = threading.Lock()      # quota_cal.json 的读改写
_collect_lock = threading.Lock()  # 同一时刻只允许一次采集
_last_collect_at = 0.0

# 供下拉列表使用的常见 Agent 名（实际数据以扫描结果为准）
KNOWN_AGENTS = ["Kimi Code", "Claude Code", "Codex CLI", "Cursor", "Cline", "Trae", "Gemini CLI", "Aider"]


def load_records():
    with _lock:
        if not os.path.exists(DATA_FILE):
            save_records([])
            return []
        data = storage.load_json(DATA_FILE, [])
        return data if isinstance(data, list) else []


def save_records(records):
    storage.save_json(DATA_FILE, records, indent=1)


def seed_demo_data():
    """仅在使用 --demo 启动时生成演示数据。"""
    rng = random.Random(42)
    prices = load_prices(DATA_DIR)
    agents_models = {
        "Kimi Code": ["Kimi K2"], "Claude Code": ["Claude Sonnet 4.5"],
        "Cursor": ["GPT-5.2", "Gemini 3 Pro"], "Codex CLI": ["GPT-5.2"],
        "Cline": ["DeepSeek V3.2", "Qwen3 Coder"], "Trae": ["Doubao Seed 1.6"],
    }
    projects = ["tokenlens", "web-console", "data-pipeline", "mobile-app"]
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
                    "project": rng.choice(projects),
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


def load_settings():
    d = storage.load_json(SETTINGS_FILE, {})
    s = dict(DEFAULT_SETTINGS)
    if isinstance(d, dict):
        if d.get("multi_device") in MULTI_MODES:
            s["multi_device"] = d["multi_device"]
        if isinstance(d.get("sync_dir"), str):
            s["sync_dir"] = d["sync_dir"]
    # 声明了 sync 却没有可用目录时降级，避免展示「已合并」的假象
    if s["multi_device"] == "sync" and not s["sync_dir"]:
        s["multi_device"] = "local_only"
    return s


def save_settings(s):
    storage.save_json(SETTINGS_FILE, s, indent=2)


def settings_payload():
    """设置 + 本机身份 + 当前能读到的对端概况，供设置面板展示。"""
    s = load_settings()
    ident = sync.device_identity(DATA_DIR)
    peers = active_peers(s)
    return {
        **s,
        "device": ident,
        "peers": [{"device_name": p["device_name"], "device_id": p["device_id"],
                   "updated": p["updated"], "stale": p["stale"], "days": len(p["days"])}
                  for p in peers],
    }


def active_peers(settings=None):
    """sync 模式下返回其他设备的数据，其余模式返回空列表。"""
    s = settings or load_settings()
    if s["multi_device"] != "sync":
        return []
    return sync.read_peers(DATA_DIR, s["sync_dir"])


def merged_records(settings=None):
    prices = load_prices(DATA_DIR)
    manual = load_records()
    for r in manual:
        r.setdefault("source", "manual")
        # 手动录入可以不填项目；和采集不到工作目录的记录归到同一桶，别分裂成两个
        r.setdefault("project", collector.UNKNOWN_PROJECT)
    auto = collector.load_auto_records(DATA_DIR, prices, calc_cost)
    records = manual + auto + sync.peer_records(active_peers(settings), prices, calc_cost)
    maybe_auto_match(DATA_DIR, records, prices)  # 发现新模型时后台自动补价
    return records, prices


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
    """原子写入缓存（唯一临时文件 + os.replace），并发读写不会看到写了一半的文件。"""
    os.makedirs(data_dir, exist_ok=True)
    target = _catalog_cache_file(data_dir)
    tmp = f"{target}.{os.getpid()}.{threading.get_ident()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"fetched_at": int(time.time()), "index": index,
                   "unmatched": unmatched}, f, ensure_ascii=False)
    os.replace(tmp, target)


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


MODEL_BOUNDARY = "-._/: "  # 模型 id 里分隔系列与版本/变体的字符


def match_model(index, model):
    """按 别名 → 精确 → 边界对齐的前缀 匹配目录，返回价格 entry 或 None。

    只接受在分隔符处对齐的前缀匹配，两个方向的取法不同：
    - 目录 id 是我们模型名的前缀（'gpt-5.2' ⊂ 'gpt-5.2-2026-01-01'）：本机名更具体，
      取**最长**的目录 id，也就是最贴近的那个系列；
    - 我们的模型名是目录 id 的前缀（'gpt-5.2' ⊂ 'gpt-5.2-mini'）：目录里全是更具体的
      变体，取**最短**的那个，即最接近基础款。
    前者优先 —— 有精确到版本的系列匹配时，不该退回去猜变体。

    早先用的是「任意子串 + 取最短 id」，短 id 几乎是任何长模型名的子串，会稳定截胡：
    'gpt-5' 抢走 'gpt-5-mini-2025'（贵 5 倍）、'k2' 抢走 'kimi-k2.7-code-turbo'（错系列）。
    配错的价格还会被 auto_match_prices 写进 prices.json 且不再重试，用户完全无感。
    宁可匹配不到 —— 界面会打「未定价」徽标提示手动录入。
    """
    target = MODEL_ALIASES.get(model, model)
    entry = index.get(target)
    if entry:
        return entry
    cands = []
    for mid in index:
        if not mid:
            continue
        if target.startswith(mid) and target[len(mid)] in MODEL_BOUNDARY:
            cands.append((0, -len(mid), mid))  # 目录 id ⊂ 本机名：取最长
        elif mid.startswith(target) and mid[len(target)] in MODEL_BOUNDARY:
            cands.append((1, len(mid), mid))   # 本机名 ⊂ 目录 id：取最短
    if cands:
        return index[min(cands)[2]]
    return None


def unpriced_models(records, prices):
    """记录中出现过、但价格表里没有的模型（小写，排序）。"""
    return sorted({str(r.get("model", "")).lower() for r in records if r.get("model")}
                  - set(prices))


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
    plans = storage.load_json(PLANS_FILE, [])
    return plans if isinstance(plans, list) else []


def save_plans(plans):
    storage.save_json(PLANS_FILE, plans, indent=1)


def _window_label(minutes):
    if minutes == 300:
        return "5 小时窗口"
    if minutes == 10080:
        return "每周窗口"
    if minutes >= 1440:
        return f"{minutes // 1440} 天窗口"
    return f"{minutes} 分钟窗口"


def _period_used(records, agent, span, peers=None):
    """某 Agent 在当前滚动周期内的 tokens 合计（含缓存）。

    span 不足一天时按**真正的滚动窗口**统计：用分钟桶从 now-span 起算。早先这里一律
    按自然日截断，5 小时窗口实际统计的是「今天 00:00 至今」，晚上查看会把十几个小时的
    用量算进 5 小时窗口，严重高估。分钟桶只来自自动采集，手动录入的记录没有时刻，
    无法参与滚动窗口统计。
    span ≥ 一天时仍按自然日统计；此时 records 在 sync 模式下已含对端数据。
    """
    if span < 86400:
        start = time.time() - span
        local = collector.minutes_usage_since(collector.load_minutes(DATA_DIR), start, agent=agent)
        remote = sync.peer_usage_since(peers or [], start, agent=agent)
        return sum(local[f] + remote[f] for f in ("input", "output", "cached"))
    from_day = (date.today() - timedelta(days=span // 86400 - 1)).isoformat()
    return sum(r.get("input", 0) + r.get("output", 0) + r.get("cached", 0)
               for r in records if r.get("agent") == agent and r.get("date", "") >= from_day)


def _load_cal():
    d = storage.load_json(CAL_FILE, {})
    return d if isinstance(d, dict) else {}


def _save_cal(d):
    storage.save_json(CAL_FILE, d, indent=1)


def _cal_key(agent_kind, window):
    """校准状态在 quota_cal.json 里的键，同时也是前端开关按钮传回来的标识。"""
    return f"{agent_kind}|{window.get('label')}"


def _calibrated_quota(key, window):
    """窗口总额估算，返回 (quota, carried)。

    两条估算路径：
    1) 差分（优先）：同一窗口内 实测用量增量 ÷ 官方百分比增量 —— 只需观察区间内的
       数据，换设备后也能快速重新锚定，不依赖历史同步；
    2) 时点（兜底）：窗口实测 ÷ 官方百分比 —— 需要日志覆盖整个窗口。
    新估算低于旧值 70% 时判定为日志不全，沿用旧校准（carried=True，UI 显示「沿用上次
    校准值」）；超过一个完整窗口仍未恢复才接受新值。

    st["manual"] 为真表示用户手动点过「开始估计」：此时**只认差分路径**。手动重启的
    语义就是「从这一刻起重新锚定」，而时点法会把重启之前那段用量一起除进去 —— 那段
    正是用户判断为不干净（别的设备也在跑、日志缺了一截）才要重来的，不能再用。
    代价是重启后要等官方百分比涨 1% 才出得来数，UI 会显示「正在校准」。

    前提：实测用量必须与官方百分比同口径。官方百分比是**账号级**的，所以只有单机使用、
    或多端已开启 sync 合并时，这里算出来的绝对额度才可信。多端未合并时调用方会跳过
    本函数（见 _estimate_window 的 hide_absolute），而不是给出一个按本机占比缩水的数。
    """
    mins = int(window.get("window_minutes") or 0)
    pct = float(window.get("used_percent") or 0)
    measured = window.get("measured_tokens") or 0
    resets_at = window.get("resets_at")
    now = time.time()
    span = mins * 60 if mins else 7 * 86400
    with _cal_lock:
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

        q_point = None
        if not st.get("manual") and 0 < pct < 100 and measured > 0:
            q_point = round(measured * 100 / pct)
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


def set_estimation(key, enabled):
    """开关某个窗口的额度估计，返回该窗口的新校准状态。

    开启 = 从此刻重新锚定：整条校准记录清空重建（manual=True），旧的 quota / anchor
    一并丢掉。多端环境下前一段观测可能混了别的设备的数据，留着反而把新估算带偏。
    关闭 = 只留官方百分比与重置时间，不再输出任何绝对 token 数。
    """
    with _cal_lock:
        cal = _load_cal()
        cal[key] = ({"enabled": True, "manual": True, "started_at": int(time.time())}
                    if enabled else {"enabled": False})
        _save_cal(cal)
        return cal[key]


def _estimate_window(agent_kind, window, peers=None, hide_absolute=False):
    """给官方窗口补实测 tokens，并按校准值估算总额/剩余。

    实测 = 本机原始日志（事件级精确）+ 各对端分钟桶（误差 < 60s）。只有当实测覆盖
    整个账号时，除以官方百分比得到的绝对额度才有意义；hide_absolute 为真时只保留
    百分比与实测量，不输出会误导人的绝对值。

    用户也可以自己关掉某个窗口的估计（est_enabled=False）—— 多端场景下这台机器的
    观测口径未必干净，与其看一个不知道偏了多少的数，不如只看官方百分比。
    """
    mins = int(window.get("window_minutes") or 0)
    resets_at = window.get("resets_at")
    start = (resets_at - mins * 60) if (resets_at and mins) else time.time() - mins * 60
    used = collector.usage_since(agent_kind, start) or {}
    local = used.get("input", 0) + used.get("output", 0) + used.get("cached", 0)
    remote_parts = sync.peer_usage_since(peers or [], start, kind=agent_kind)
    remote = remote_parts["input"] + remote_parts["output"] + remote_parts["cached"]
    window["measured_local"] = local
    window["measured_remote"] = remote
    window["measured_tokens"] = local + remote
    window["account_wide"] = bool(peers)

    key = _cal_key(agent_kind, window)
    st = _load_cal().get(key) or {}
    enabled = bool(st.get("enabled", True))  # 没设置过的默认开启，保持既有行为
    # 这些键必须每次全部重写：Kimi 的卡片有 120s 内存缓存，window 是被复用的同一个
    # dict，分支里漏写的键会留着上一轮的值（停止估计后再开启，UI 仍显示「已停止」）
    window.update({
        "cal_key": key,
        "est_enabled": enabled,
        "est_since": st.get("started_at"),
        "est_quota": None,
        "est_remaining": None,
        "est_carried": False,
        "est_calibrating": False,
        "absolute_hidden": hide_absolute,
        "est_paused": not enabled and not hide_absolute,
    })
    if hide_absolute or not enabled:
        return window
    q, carried = _calibrated_quota(key, window)
    pct = float(window.get("used_percent") or 0)
    window["est_quota"] = q
    window["est_remaining"] = round(q * (1 - pct / 100)) if q else None
    window["est_carried"] = carried
    # 手动重启后差分还没攒够数据：区别于「压根没数据」，UI 要说清在等什么
    window["est_calibrating"] = bool(st.get("manual") and not q)
    return window


def compute_plans():
    """汇总 Plan 用量：Kimi/Codex 读官方数据并估算剩余；手动配额按采集数据统计。"""
    now = time.time()
    settings = load_settings()
    peers = active_peers(settings)
    # 多端但没合并数据时，任何由本机日志换算出的绝对 token 数都会按本机用量占比缩水
    hide = settings["multi_device"] == "local_only"
    result = {
        "auto": [], "manual": [], "now": now,
        "multi_device": settings["multi_device"],
        "peers": [{"device_name": p["device_name"], "updated": p["updated"], "stale": p["stale"]}
                  for p in peers],
    }

    # Kimi：官方接口实时查询（百分比 + 重置时间，账号级，多端下同样准确）
    kimi_card = quota.query_kimi()
    if not kimi_card.get("error"):
        for w in kimi_card.get("windows", []):
            _estimate_window("kimi", w, peers, hide)
        kimi_card["note"] = "官方接口 · 实时"
    result["auto"].append(kimi_card)

    # Codex：官方 rate_limits 快照，多端时取各设备中最新的一份
    rl, rl_source = sync.newest_rate_limits(collector.latest_rate_limits(), peers)
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
            }, peers, hide))
        result["auto"].append({
            "agent": "Codex CLI",
            "plan_type": limits.get("plan_type") or "unknown",
            "updated": rl.get("timestamp"),
            "note": f"官方限额 · 来自 {rl_source} 的日志" if rl_source else "官方限额 · 来自本地日志",
            "snapshot_device": rl_source,
            "windows": windows,
        })

    records, _ = merged_records(settings)
    for p in load_plans():
        label, span = PLAN_PERIODS.get(p.get("period"), PLAN_PERIODS["week"])
        used = _period_used(records, p.get("agent"), span, peers)
        item = {**p, "period_label": label, "used": used,
                "account_wide": settings["multi_device"] != "local_only"}
        if p.get("mode") == "percent":
            # 百分比锚定：录入时的实测用量 ÷ 录入的百分比 ≈ 总额度，之后随采集数据自动推算
            # 多端未合并时 est_quota 会缩水，但 used 与 est_quota 同为本机口径，误差在
            # 百分比上互相抵消 —— 所以百分比仍可用，只隐藏绝对值。
            anchor_pct = float(p.get("anchor_pct") or 0)
            anchor_used = int(p.get("anchor_used") or 0)
            est_quota = round(anchor_used * 100 / anchor_pct) if 0 < anchor_pct < 100 and anchor_used > 0 else None
            if est_quota:
                item["pct"] = round(min(100.0, used / est_quota * 100), 1)
                item["remaining"] = max(0, est_quota - used)
            else:
                item["pct"] = round(anchor_pct, 1)
                item["remaining"] = None
            item["est_quota"] = est_quota
            if hide:
                item["est_quota"] = item["remaining"] = None
                item["absolute_hidden"] = True
        else:
            # 固定配额：quota 是账号级的，used 在多端未合并时只有本机 ——
            # 这会低估已用、高估剩余，是唯一朝危险方向偏的路径，必须显式告警。
            quota_tokens = max(1, int(p.get("quota") or 1))
            item["pct"] = round(min(100.0, used / quota_tokens * 100), 1)
            item["remaining"] = max(0, quota_tokens - used)
            item["understated"] = hide
        result["manual"].append(item)
    return result


# 仅服务本机请求：Host 必须是本机地址；写操作额外要求 Origin（如有）也是本机
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
MAX_BODY = 1 << 20  # JSON 请求体上限 1 MB


def _host_name(value):
    """从 Host 头取主机名（去端口、去 IPv6 方括号）。"""
    v = (value or "").strip().lower()
    if v.startswith("["):
        return v[1:].split("]", 1)[0]
    return v.split(":", 1)[0]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # 静音访问日志
        pass

    def _host_ok(self, writes=False):
        """Host 校验（防 DNS rebinding 读数据）；写操作再校验 Origin（挡跨站表单 CSRF）。"""
        if _host_name(self.headers.get("Host")) not in LOCAL_HOSTS:
            return False
        if writes:
            origin = self.headers.get("Origin")
            if origin and (urlparse(origin).hostname or "").lower() not in LOCAL_HOSTS:
                return False
        return True

    def _read_json(self):
        """读取 JSON 请求体；成功返回 dict，失败返回 None 并已发送 4xx 响应。"""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0:
            self._send_json({"error": "invalid Content-Length"}, 400)
        elif length > MAX_BODY:
            self._send_json({"error": "请求体过大"}, 413)
        else:
            try:
                data = json.loads(self.rfile.read(length).decode("utf-8"))
                if isinstance(data, dict):
                    return data
            except Exception:
                pass
            self._send_json({"error": "invalid json"}, 400)
        return None

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
        if not self._host_ok():
            self._send_json({"error": "forbidden"}, 403)
            return
        url = urlparse(self.path)
        path = url.path
        if path == "/":
            self._send_file(os.path.join(STATIC_DIR, "index.html"))
        elif path == "/api/records":
            records, prices = merged_records()
            self._send_json({"records": records, "prices": prices, "agents": KNOWN_AGENTS,
                             "unpriced": unpriced_models(records, prices)})
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
                rows = timeline_rows(day, hour)
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
        elif path == "/api/ping":
            # 单实例检测用：新进程靠它确认端口上跑的是不是另一个 TokenLens
            self._send_json({"app": APP_ID, "pid": os.getpid()})
        elif path == "/api/settings":
            self._send_json(settings_payload())
        elif path.startswith("/static/"):
            rel = path[len("/static/"):]
            full = os.path.normpath(os.path.join(STATIC_DIR, rel))
            if not os.path.normcase(full).startswith(STATIC_PREFIX):
                self._send_json({"error": "forbidden"}, 403)
                return
            self._send_file(full)
        else:
            self._send_json({"error": "not found"}, 404)

    def do_POST(self):
        if not self._host_ok(writes=True):
            self._send_json({"error": "forbidden"}, 403)
            return
        url = urlparse(self.path)
        if url.path == "/api/collect":
            try:
                summary, shared = collect_and_share()
            except Exception as e:  # 采集失败不影响主程序
                self._send_json({"error": str(e)}, 500)
                return
            self._send_json({"ok": True, "summary": summary, "shared": shared})
            return
        if url.path == "/api/settings":
            payload = self._read_json()
            if payload is None:
                return
            s = load_settings()
            mode = payload.get("multi_device", s["multi_device"])
            if mode not in MULTI_MODES:
                self._send_json({"error": "multi_device 取值不合法"}, 400)
                return
            sync_dir = str(payload.get("sync_dir", s["sync_dir"]) or "").strip()
            if mode == "sync":
                if not sync_dir:
                    self._send_json({"error": "选择「多端合并」需要填写共享目录"}, 400)
                    return
                # 立刻验证目录可写，别等到后台采集时才静默失败
                try:
                    os.makedirs(sync_dir, exist_ok=True)
                    probe = os.path.join(sync_dir, ".tokenlens-write-test")
                    with open(probe, "w", encoding="utf-8") as f:
                        f.write("ok")
                    os.remove(probe)
                except OSError as e:
                    self._send_json({"error": f"共享目录不可写：{e}"}, 400)
                    return
            if payload.get("device_name"):
                sync.rename_device(DATA_DIR, payload["device_name"])
            save_settings({"multi_device": mode, "sync_dir": sync_dir})
            shared = None
            if mode == "sync":
                try:
                    shared = sync.export(DATA_DIR, sync_dir)
                except OSError as e:
                    self._send_json({"error": f"写入共享目录失败：{e}"}, 400)
                    return
            self._send_json({"ok": True, "shared": shared, **settings_payload()})
            return
        if url.path == "/api/collect/rebuild":
            try:
                summary, backups = rebuild_collection()
            except Exception as e:
                self._send_json({"error": f"重建失败：{e}"}, 500)
                return
            self._send_json({"ok": True, "summary": summary, "backups": backups})
            return
        if url.path == "/api/quota/estimate":
            payload = self._read_json()
            if payload is None:
                return
            key = str(payload.get("key", "")).strip()
            if not key or "|" not in key or len(key) > 200:
                self._send_json({"error": "key 不合法"}, 400)
                return
            enabled = bool(payload.get("enabled"))
            state = set_estimation(key, enabled)
            self._send_json({"ok": True, "key": key, **state})
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
            payload = self._read_json()
            if payload is None:
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
            with _lock:
                save_user_price(DATA_DIR, model, entry)
            self._send_json({"ok": True, "model": model.lower(), "price": entry})
            return
        if url.path == "/api/plans":
            payload = self._read_json()
            if payload is None:
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
                st = load_settings()
                peers = active_peers(st)
                records, _ = merged_records(st)
                plan["anchor_pct"] = pct
                plan["anchor_used"] = _period_used(records, agent, PLAN_PERIODS[period][1], peers)
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
        payload = self._read_json()
        if payload is None:
            return

        required = ["date", "agent", "model"]
        if any(not str(payload.get(k, "")).strip() for k in required):
            self._send_json({"error": "date/agent/model 不能为空"}, 400)
            return
        day = str(payload["date"]).strip()
        try:
            datetime.strptime(day, "%Y-%m-%d")
        except ValueError:
            self._send_json({"error": "date 格式需为 YYYY-MM-DD"}, 400)
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
            "date": day,
            "agent": str(payload["agent"]).strip(),
            "model": model,
            "project": str(payload.get("project", "")).strip()[:60] or collector.UNKNOWN_PROJECT,
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
        if not self._host_ok(writes=True):
            self._send_json({"error": "forbidden"}, 403)
            return
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


def collect_and_share():
    """采集一次；sync 模式下顺带把本机结果写进共享目录。返回采集摘要。

    导出失败（网盘未挂载、目录被删）不影响采集本身，只在返回值里带上错误。
    _collect_lock 让后台定时线程、手动「同步本地数据」、时间线的按需采集三者互斥，
    否则会同时读改写 offsets / auto_usage，互相覆盖导致漏扫或重扫。
    """
    global _last_collect_at
    with _collect_lock:
        summary = collector.collect_once(DATA_DIR)
        _last_collect_at = time.time()
    s = load_settings()
    shared = None
    if s["multi_device"] == "sync" and s["sync_dir"]:
        try:
            shared = sync.export(DATA_DIR, s["sync_dir"])
        except OSError as e:
            shared = f"导出失败：{e}"
    return summary, shared


def rebuild_collection():
    """清空采集状态并全量重扫一遍本机日志，返回 (采集摘要, 备份文件名列表)。

    什么时候需要：采集状态出过问题（漏扫、重复计数），或者想让升级前采集的老数据
    补上新增的维度（比如项目名）—— 旧数据里没有的字段，只能靠重扫原始日志补回来。

    三个文件不是删除而是改名备份：万一某个工具的日志已经被它自己清理掉了，
    重扫会比原来少一截，留着备份至少还能捞回来。手动录入的记录存在另一个文件，不受影响。
    """
    stamp = int(time.time())
    backups = []
    with _collect_lock:
        for name in (collector.STATE_FILE, collector.AUTO_FILE, collector.MINUTES_FILE):
            path = os.path.join(DATA_DIR, name)
            if not os.path.exists(path):
                continue
            backup = f"{path}.bak-{stamp}"
            try:
                os.replace(path, backup)
                backups.append(os.path.basename(backup))
            except OSError:
                pass
        summary = collector.collect_once(DATA_DIR)
    global _last_collect_at
    _last_collect_at = time.time()
    s = load_settings()
    if s["multi_device"] == "sync" and s["sync_dir"]:
        try:
            sync.export(DATA_DIR, s["sync_dir"])
        except OSError:
            pass  # 导出失败不影响重建本身
    return summary, backups


def collect_if_stale():
    """距上次采集超过 COLLECT_FRESH_WINDOW 就先增量采集一次。

    时间线走分钟桶快路径，而分钟桶只在采集时更新；后台采集是 10 分钟一次，
    直接查表会看不到最近几分钟的用量。增量采集只读日志新增的尾部，很便宜。
    """
    if time.time() - _last_collect_at < COLLECT_FRESH_WINDOW:
        return
    try:
        collect_and_share()
    except Exception:
        pass  # 采集失败不该让时间线整个报错，用已有的桶先渲染


def timeline_rows(day, hour=None):
    """明细时间线：优先用分钟桶（快），覆盖不到的日期回退为重扫原始日志。"""
    collect_if_stale()
    rows = collector.breakdown_from_minutes(collector.load_minutes(DATA_DIR), day, hour)
    return rows if rows is not None else collector.breakdown(day, hour)


def collect_loop():
    """启动后立即采集一次，之后定时增量扫描。"""
    while True:
        try:
            collect_and_share()
        except Exception:
            pass
        time.sleep(COLLECT_INTERVAL)


def _port_free(port):
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def find_free_port(preferred):
    """优先用 preferred，被占则让系统分配一个。"""
    if _port_free(preferred):
        return preferred
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def probe_instance(port, timeout=1.5):
    """端口上跑的是另一个 TokenLens 就返回它的 /api/ping 响应，否则 None。"""
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/ping",
                                     headers={"Host": f"127.0.0.1:{port}"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            d = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None
    return d if isinstance(d, dict) and d.get("app") == APP_ID else None


def running_instance():
    """已有实例在跑就返回 {'port': ...}，否则 None。

    双开会让两个进程共用同一份 collector_state.json 与 auto_usage.json，而 _lock
    只是线程锁不跨进程，交错读改写 offsets 会漏扫或重复计数 —— 所以这里必须拦住。
    先查 instance.json 记的端口（上次可能没抢到默认端口），再兜底查默认端口。
    """
    ports = []
    d = storage.load_json(INSTANCE_FILE, {})
    if isinstance(d, dict) and isinstance(d.get("port"), int):
        ports.append(d["port"])
    if DEFAULT_PORT not in ports:
        ports.append(DEFAULT_PORT)
    for p in ports:
        if probe_instance(p):
            return {"port": p}
    return None


def _write_instance(port):
    try:
        storage.save_json(INSTANCE_FILE, {"port": port, "pid": os.getpid(),
                                          "started_at": int(time.time())})
    except OSError:
        pass  # 记不上只是退化成「只探默认端口」，不该拦住启动


def _clear_instance():
    try:
        os.remove(INSTANCE_FILE)
    except OSError:
        pass


def main():
    running = running_instance()
    if running:
        # 已经有一个在跑：打开它的界面，别再起第二个采集进程
        import webbrowser
        url = f"http://127.0.0.1:{running['port']}"
        print(f"TokenLens 已在运行，已为你打开 {url}")
        webbrowser.open(url)
        return

    if "--demo" in sys.argv:
        seed_demo_data()
        print("已生成演示数据")

    load_records()  # 确保数据文件存在
    threading.Thread(target=collect_loop, daemon=True).start()

    port = find_free_port(DEFAULT_PORT)
    url = f"http://127.0.0.1:{port}"
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    _write_instance(port)
    try:
        _run_ui(server, url)
    finally:
        _clear_instance()


def _run_ui(server, url):
    try:
        import webview
    except ImportError:
        webview = None
    if webview is not None:
        try:
            webview.create_window("TokenLens", url, width=1440, height=900, min_size=(1100, 700))
            webview.start()  # 窗口关闭后返回
            server.shutdown()
            return
        except Exception as e:
            # 装了 pywebview 但底层 WebView2 / GTK 运行时缺失时 start() 会抛，
            # 这类失败不该让整个程序崩掉，退回浏览器照样能用
            print(f"原生窗口启动失败（{e}），改用浏览器打开")

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
