# -*- coding: utf-8 -*-
"""官方订阅配额在线查询（Kimi For Coding）。

数据来源（与 cc-switch 相同）：
- GET https://api.kimi.com/coding/v1/usages ，Bearer 用 Kimi CLI 的 OAuth access_token
- 凭证位于 ~/.kimi-code/credentials/kimi-code.json；过期时用 refresh_token 刷新并原子回写
  （client_id 取自 kimi-code 开源仓库 packages/oauth/src/constants.ts）

注意：接口只返回百分比与重置时间，不返回绝对 token 额度；绝对额度由
collector.usage_since 实测值 ÷ 百分比估算（真实容量另见 capacity.py）。
响应里的 user.userId 是账号唯一 id，交给 identity.py 做多端账号匹配。
结果带 120s 内存缓存。
"""
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

CRED_FILE = os.path.expanduser("~/.kimi-code/credentials/kimi-code.json")
USAGES_URL = "https://api.kimi.com/coding/v1/usages"
TOKEN_URL = "https://auth.kimi.com/api/oauth/token"
CLIENT_ID = "17e5f671-d194-4dfb-9706-5516cb48c098"
CACHE_TTL = 120  # 秒

_cache = {"at": 0.0, "data": None}


def _http_json(url, headers=None, data=None, timeout=15):
    req = urllib.request.Request(url, data=data, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _load_cred():
    try:
        with open(CRED_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _save_cred(cred):
    """原子回写，避免半截文件损坏 CLI 登录态。"""
    tmp = CRED_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cred, f, ensure_ascii=False, indent=2)
    os.chmod(tmp, 0o600)  # POSIX 下避免 OAuth 凭证被其他用户读取
    os.replace(tmp, CRED_FILE)


def _access_token():
    """返回 (token, None) 或 (None, 错误信息)。过期时先刷新并回写。"""
    cred = _load_cred()
    if not cred or not cred.get("access_token"):
        return None, "未找到 Kimi CLI 凭证（请先运行 Kimi CLI 登录）"
    if time.time() < float(cred.get("expires_at") or 0) - 60:
        return cred["access_token"], None
    refresh = cred.get("refresh_token")
    if not refresh:
        return None, "凭证已过期且无 refresh_token，请在 Kimi CLI 重新登录"
    body = urllib.parse.urlencode({
        "client_id": CLIENT_ID,
        "grant_type": "refresh_token",
        "refresh_token": refresh,
    }).encode()
    try:
        data = _http_json(TOKEN_URL, data=body,
                          headers={"Content-Type": "application/x-www-form-urlencoded"})
    except Exception as e:
        return None, f"凭证刷新失败：{e}"
    if not isinstance(data, dict) or not data.get("access_token"):
        return None, "凭证刷新被拒绝，请在 Kimi CLI 重新登录"
    cred["access_token"] = data["access_token"]
    cred["refresh_token"] = data.get("refresh_token") or refresh
    if data.get("expires_in"):
        cred["expires_in"] = int(data["expires_in"])
    cred["expires_at"] = int(data.get("expires_at") or (time.time() + int(data.get("expires_in") or 3600)))
    try:
        _save_cred(cred)
    except OSError:
        pass  # 回写失败不影响本次使用
    return cred["access_token"], None


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _pct(detail):
    """从一段额度明细里算已用百分比。

    接口按 proto3 的习惯省略零值字段：没用过的窗口只给 {limit, remaining}，
    用满的窗口只给 {limit, used}。所以两条路都要认 —— 只看 used 的话，
    「有 remaining 没 used」会被当成 0%，真实容量的整轮观测会从错误的起点开始。
    """
    detail = detail or {}
    limit = _num(detail.get("limit"))
    if not limit or limit <= 0:
        return 0.0
    used = _num(detail.get("used"))
    if used is None:
        remaining = _num(detail.get("remaining"))
        if remaining is None:
            return 0.0
        used = limit - remaining
    return round(max(0.0, min(used / limit * 100, 100.0)), 1)


def _iso_to_ts(s):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def _window_label(minutes):
    if minutes == 300:
        return "5 小时窗口"
    if minutes >= 10080:
        return "每周窗口"
    if minutes >= 1440:
        return f"{minutes // 1440} 天窗口"
    return f"{minutes} 分钟窗口"


def query_kimi():
    """查询 Kimi 官方配额。成功返回卡片 dict，失败返回 {"error": ...}。"""
    now = time.time()
    if _cache["data"] is not None and now - _cache["at"] < CACHE_TTL:
        return _cache["data"]

    token, err = _access_token()
    if not token:
        return {"agent": "Kimi Code", "error": err}
    try:
        body = _http_json(USAGES_URL, headers={
            "Authorization": "Bearer " + token,
            "Accept": "application/json",
        })
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return {"agent": "Kimi Code", "error": "凭证已过期，请在 Kimi CLI 中重新登录"}
        return {"agent": "Kimi Code", "error": f"配额接口错误 HTTP {e.code}"}
    except Exception as e:
        return {"agent": "Kimi Code", "error": f"配额接口网络错误：{e}"}

    windows = []
    for item in body.get("limits") or []:
        detail = item.get("detail") or {}
        win = item.get("window") or {}
        mins = int(win.get("duration") or 0)
        windows.append({
            "label": _window_label(mins),
            "used_percent": _pct(detail),
            "resets_at": _iso_to_ts(detail.get("resetTime")),
            "window_minutes": mins,
        })
    usage = body.get("usage") or {}
    if usage:
        windows.append({
            "label": "每周窗口",
            "used_percent": _pct(usage),
            "resets_at": _iso_to_ts(usage.get("resetTime")),
            "window_minutes": 10080,
        })

    user = body.get("user") or {}
    level = (user.get("membership") or {}).get("level") or ""
    card = {
        "agent": "Kimi Code",
        # 账号唯一 id：多端汇总时用来确认各设备是不是同一个账号（见 identity.py）
        "account_id": str(user.get("userId") or "") or None,
        "plan_type": level.replace("LEVEL_", "") or "unknown",
        "updated": datetime.now().astimezone().isoformat(timespec="seconds"),
        "windows": windows,
    }
    _cache["at"] = now
    _cache["data"] = card
    return card
