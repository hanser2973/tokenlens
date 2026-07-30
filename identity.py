# -*- coding: utf-8 -*-
"""账号身份：从各 AI 工具的本地凭证里读出「这台机器登录的是哪个账号」。

为什么需要：sync.device_identity 生成的设备 id 是每台机器一个随机值，换台电脑就换
一个 id —— 汇总时无从判断两台机器是不是同一个人、同一个账号。而官方配额的百分比是
**账号级**的：把另一个账号的用量也加进来，「实测用量 ÷ 官方百分比」估出来的绝对额度
会凭空缩水，真实容量（capacity.py）更是直接作废。

只读，不碰凭证内容：这里取的是账号标识字段本身（accountUuid / account_id / userId），
不解析也不外传 access_token。

进同步目录的只有 sha256 前 16 位（account_hash）：同一账号在任何设备上算出的哈希一致，
足够做匹配；反推不回邮箱和 uuid，所以放进网盘也不会把身份带出去。邮箱之类的可读标签
只留在本机，供界面显示。
"""
import hashlib
import json
import os
import threading
import time

import storage

CLAUDE_CONFIG = os.path.expanduser("~/.claude.json")
CODEX_AUTH = os.path.expanduser("~/.codex/auth.json")
CACHE_FILE = "accounts.json"  # data/ 下：缓存只能联网拿到的账号 id（Kimi）

# 只有这三家能拿到账号标识：Cline / opencode 等是纯本地日志，压根没有账号概念
AGENT_OF_KIND = {"kimi": "Kimi Code", "claude": "Claude Code", "codex": "Codex CLI"}

CACHE_TTL = 60  # 秒：~/.claude.json 有几十 KB，每个请求重解析一遍不值当

_lock = threading.Lock()
_cache = {"at": 0.0, "dir": None, "data": None}


def account_hash(raw):
    """账号 id → 跨设备一致的短哈希。不加盐，否则两台机器算出来对不上。"""
    if not raw:
        return None
    return hashlib.sha256(str(raw).encode("utf-8")).hexdigest()[:16]


def mask(raw):
    """给界面看的账号 id：邮箱保留域名，其余只留头尾。"""
    s = str(raw or "")
    if not s:
        return ""
    if "@" in s:
        name, _, domain = s.partition("@")
        return (name[:2] + "***" if len(name) > 2 else name) + "@" + domain
    return s if len(s) <= 10 else f"{s[:4]}…{s[-4:]}"


def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def claude_account():
    """Claude Code：~/.claude.json 的 oauthAccount.accountUuid，登录邮箱作显示名。"""
    d = _read_json(CLAUDE_CONFIG)
    if not isinstance(d, dict):
        return None
    oa = d.get("oauthAccount") if isinstance(d.get("oauthAccount"), dict) else {}
    # userID 是本地生成的匿名 id，只在没登录 OAuth 时兜底 —— 它不跨设备一致，
    # 所以标 weak，匹配时不能拿它去否定一个对端
    uid, weak = oa.get("accountUuid"), False
    if not uid:
        uid, weak = d.get("userID"), True
    if not uid:
        return None
    return {"id": str(uid), "label": oa.get("emailAddress") or "",
            "source": "~/.claude.json", "weak": weak}


def codex_account():
    """Codex CLI：~/.codex/auth.json 的 tokens.account_id；API Key 模式退而用密钥哈希。"""
    d = _read_json(CODEX_AUTH)
    if not isinstance(d, dict):
        return None
    tokens = d.get("tokens") if isinstance(d.get("tokens"), dict) else {}
    acc = tokens.get("account_id")
    if acc:
        return {"id": str(acc), "label": "", "source": "~/.codex/auth.json", "weak": False}
    key = d.get("OPENAI_API_KEY")
    if key:
        # API Key 模式没有账号 id。密钥本身当然不能存，取它的哈希当身份：
        # 同一把 key 在各设备上一致，原文不出这个函数。
        return {"id": "apikey:" + account_hash(key), "label": "API Key",
                "source": "~/.codex/auth.json", "weak": False}
    return None


def _cache_path(data_dir):
    return os.path.join(data_dir, CACHE_FILE)


def _load_cache(data_dir):
    d = storage.load_json(_cache_path(data_dir), {})
    return d if isinstance(d, dict) else {}


def remember(data_dir, kind, account_id, label=""):
    """记下只能联网拿到的账号 id（Kimi 的 userId 只在配额接口的响应里）。

    没变化就不写盘：配额接口每 10 分钟查一次，没必要跟着写文件。
    """
    if not account_id:
        return None
    with _lock:
        cache = _load_cache(data_dir)
        old = cache.get(kind) if isinstance(cache.get(kind), dict) else {}
        if old.get("id") == str(account_id) and old.get("label") == (label or ""):
            return old
        entry = {"id": str(account_id), "label": label or ""}
        cache[kind] = entry
        storage.save_json(_cache_path(data_dir), cache, indent=1)
        _cache["at"] = 0.0  # 账号变了，别让读缓存再顶 60 秒
        return entry


def local_accounts(data_dir):
    """本机各工具登录的账号：{kind: {id, hash, label, agent, source, weak}}。

    读不到凭证的 kind 不会出现在结果里 —— 「不知道」和「不一样」必须分得开，
    调用方对未知账号要放行而不是排除。

    结果带 CACHE_TTL 秒缓存：配额页每次刷新都会走到这里，而登录态一天也变不了几次。
    """
    now = time.time()
    if (_cache["data"] is not None and _cache["dir"] == data_dir
            and now - _cache["at"] < CACHE_TTL):
        return {k: dict(v) for k, v in _cache["data"].items()}
    out = {}
    for kind, fn in (("claude", claude_account), ("codex", codex_account)):
        acc = fn()
        if acc:
            out[kind] = acc
    cached = _load_cache(data_dir).get("kimi")
    if isinstance(cached, dict) and cached.get("id"):
        out["kimi"] = {"id": str(cached["id"]), "label": cached.get("label") or "",
                       "source": "Kimi 配额接口", "weak": False}
    for kind, acc in out.items():
        acc["hash"] = account_hash(acc["id"])
        acc["agent"] = AGENT_OF_KIND.get(kind, kind)
    _cache.update({"at": now, "dir": data_dir, "data": {k: dict(v) for k, v in out.items()}})
    return out


def local_hashes(data_dir):
    """{kind: hash}，供同步导出与对端匹配使用（不含任何可读信息）。"""
    return {k: v["hash"] for k, v in local_accounts(data_dir).items() if v.get("hash")}


def display_accounts(data_dir):
    """给设置面板的账号列表：带工具名、脱敏 id、哈希前 8 位。"""
    rows = []
    for kind, acc in sorted(local_accounts(data_dir).items()):
        rows.append({
            "kind": kind,
            "agent": acc["agent"],
            "label": acc.get("label") or mask(acc["id"]),
            "hash": acc["hash"],
            "short": acc["hash"][:8],
            "source": acc.get("source", ""),
            "weak": bool(acc.get("weak")),
        })
    return rows
