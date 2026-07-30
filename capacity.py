# -*- coding: utf-8 -*-
"""真实容量：一个配额窗口从满额跑到用完，实际消耗掉多少 token。

和「估算总额」的区别：
- 估算总额（server._calibrated_quota）是外推 —— 实测用量 ÷ 官方百分比。百分比只有
  一位小数，用得少的时候分母很小，5% 的观测误差会被放大成几倍的额度误差；官方的
  百分比也未必与 token 严格线性（不同模型的权重不一样）。
- 真实容量不外推：只认一整轮完整观测 —— 窗口从 0% 一路看到 100%，期间实测到的
  token 总量就是这个窗口真实装得下多少。一轮 5 小时窗口跑满就能出一个数，周窗口
  则要一整周。

采样条件（缺一不可，任何一条不满足这一轮就作废，等下一轮窗口重置重来）：
1. 从满额开始：本轮第一次看到这个窗口时，官方已用 ≤ START_PCT
2. 口径覆盖整个账号：单机使用，或多端已合并且各对端的分钟桶覆盖到窗口起点
3. 真的跑到用完：官方已用百分比 ≥ FULL_PCT
4. 全程同一个账号：中途换账号（重新登录成另一个号）作废

样本带上账号哈希与套餐等级：换账号、升套餐之后额度本来就变了，旧样本不该混进来。
"""
import os
import threading
import time

import storage

FILE = "capacity.json"
START_PCT = 5.0    # 本轮首次观测时已用不超过这个百分比，才算「从满额开始」
FULL_PCT = 99.0    # 官方已用到这里就认为窗口跑满（不少接口封顶就停在 99.x）
MAX_SAMPLES = 12   # 每个窗口保留多少个历史样本

_lock = threading.Lock()


def _path(data_dir):
    return os.path.join(data_dir, FILE)


def load(data_dir):
    d = storage.load_json(_path(data_dir), {})
    return d if isinstance(d, dict) else {}


def _new_cycle(resets_at, pct, tokens, account, now):
    return {
        "resets_at": resets_at,
        "started_at": int(now),
        "started_pct": pct,
        "started_tokens": tokens,
        "from_full": pct <= START_PCT,
        "account": account,
        "covered": True,
        "blocked": "",
        "sampled": False,
    }


def observe(data_dir, key, window, covered=True, blocked="", account=None, plan_type=None):
    """记一次窗口观测，返回这个窗口的真实容量摘要。

    window 用到 resets_at / used_percent / window_minutes / measured_tokens 四个字段。
    covered=False 表示这次实测口径盖不住整个账号（多端未同步、对端日志缺一截、
    用户手动停了估计），blocked 是给界面看的原因。
    """
    resets_at = window.get("resets_at")
    try:
        pct = float(window.get("used_percent") or 0)
    except (TypeError, ValueError):
        pct = 0.0
    tokens = int(window.get("measured_tokens") or 0)
    now = time.time()

    with _lock:
        allst = load(data_dir)
        st = allst.get(key) if isinstance(allst.get(key), dict) else {}
        cycle = st.get("cycle") if isinstance(st.get("cycle"), dict) else None
        before = dict(cycle) if cycle else None

        if not cycle or cycle.get("resets_at") != resets_at:
            cycle = _new_cycle(resets_at, pct, tokens, account, now)
        elif account and cycle.get("account") and cycle["account"] != account:
            # 本轮中途换了账号：前半段是别人的用量，这一轮的总量已经没有意义
            cycle["covered"] = False
            cycle["blocked"] = "本轮中途切换了账号"
            cycle["account"] = account
        elif account and not cycle.get("account"):
            cycle["account"] = account

        # 覆盖状态每轮重算：多端设置、对端日志覆盖是会中途变的，脏过一次就不能再洗白
        if not covered:
            cycle["covered"] = False
            cycle["blocked"] = blocked or "实测口径未覆盖整个账号"

        cycle["pct"] = pct
        cycle["tokens"] = tokens
        cycle["at"] = int(now)

        eligible = bool(cycle.get("from_full")) and bool(cycle.get("covered"))
        sampled_now = False
        if eligible and not cycle.get("sampled") and pct >= FULL_PCT and tokens > 0:
            sampled_now = True
            samples = [s for s in (st.get("samples") or []) if isinstance(s, dict)]
            samples.append({
                "resets_at": resets_at,
                "tokens": tokens,
                "pct": pct,
                "at": int(now),
                "started_pct": cycle.get("started_pct"),
                "window_minutes": int(window.get("window_minutes") or 0),
                "account": cycle.get("account"),
                "plan_type": plan_type,
            })
            st["samples"] = samples[-MAX_SAMPLES:]
            cycle["sampled"] = True

        st["cycle"] = cycle
        # 没变化就不写盘：配额接口有缓存，界面刷新时常拿到完全相同的一组数
        if sampled_now or before != cycle:
            allst[key] = st
            storage.save_json(_path(data_dir), allst, indent=1)
    return summary(st, account)


def _blocked_reason(cycle):
    if not cycle:
        return "还没观测到这个窗口"
    if not cycle.get("from_full"):
        pct = cycle.get("started_pct")
        return f"本轮从已用 {pct}% 起才开始观测，等下一轮窗口重置" if pct is not None else "本轮不是从满额开始"
    if not cycle.get("covered"):
        return cycle.get("blocked") or "实测口径未覆盖整个账号"
    return ""


def summary(st, account=None):
    """真实容量摘要。只认与当前账号一致的样本 —— 换了账号，旧额度就作不了数。

    样本里没有 account 的是升级前记的老数据，一律保留（当时只有单账号）。
    """
    st = st if isinstance(st, dict) else {}
    samples = [s for s in (st.get("samples") or [])
               if isinstance(s, dict) and s.get("tokens")]
    if account:
        samples = [s for s in samples if not s.get("account") or s["account"] == account]
    cycle = st.get("cycle") if isinstance(st.get("cycle"), dict) else None

    out = {
        "samples": len(samples),
        "tokens": None, "at": None, "avg": None, "min": None, "max": None,
        "plan_type": None,
        "tracking": {
            "pct": (cycle or {}).get("pct"),
            "tokens": (cycle or {}).get("tokens"),
            "started_pct": (cycle or {}).get("started_pct"),
            "started_at": (cycle or {}).get("started_at"),
            "eligible": bool(cycle and cycle.get("from_full") and cycle.get("covered")),
            "sampled": bool(cycle and cycle.get("sampled")),
            "blocked": _blocked_reason(cycle),
        },
    }
    if samples:
        vals = [int(s["tokens"]) for s in samples]
        latest = samples[-1]
        out.update({
            "tokens": int(latest["tokens"]),
            "at": latest.get("at"),
            "avg": round(sum(vals) / len(vals)),
            "min": min(vals),
            "max": max(vals),
            "plan_type": latest.get("plan_type"),
        })
    return out


def get(data_dir, key, account=None):
    """只读地取某个窗口的真实容量摘要（不记录观测）。"""
    return summary(load(data_dir).get(key), account)
