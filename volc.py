# -*- coding: utf-8 -*-
"""火山方舟 Coding Plan 配额在线查询。

数据来源：火山引擎控制面 OpenAPI `GetCodingPlanUsage`（与火山方舟控制台
「订阅 → Coding Plan → 用量统计」同源，也和官方 arkcli `usage plan` 一致）：

    POST https://open.volcengineapi.com/?Action=GetCodingPlanUsage&Region=cn-beijing&Version=2024-01-01

认证是**火山引擎签名 V4（AK/SK）**，不是推理用的 Bearer API Key —— 数据面密钥会被
网关以 InvalidAuthorization 拒绝。AK/SK 在界面「方舟配额」里配置（存本地
data/settings.json），也可用环境变量 VOLCENGINE_ACCESS_KEY_ID /
VOLCENGINE_SECRET_ACCESS_KEY。

签名是 AWS SigV4 的火山变体，两处不能照搬标准 SigV4（对照官方签名示例与
cc-switch 的实现）：
1. canonical headers / SignedHeaders 用固定顺序 host;x-date;x-content-sha256;content-type，
   **不按字母序**；
2. algorithm 串是 `HMAC-SHA256`（无 AWS4 前缀），credential scope 以 `request` 结尾，
   签名密钥 kDate = HMAC(SK, date)（SK 不加 AWS4 前缀）。

接口只返回各窗口的已用**百分比**与重置时间（session / weekly / monthly），不返回
绝对 token 额度；TokenLens 的本地日志也无法把用量归因到方舟账号，所以这张卡片
只展示官方百分比，不做绝对额度估算，也不参与真实容量观测。
结果带 120s 内存缓存；保存新凭证后由 server 调 reset_cache() 立即生效。
"""
import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime

OPENAPI_HOST = "open.volcengineapi.com"
API_VERSION = "2024-01-01"
REGION = "cn-beijing"   # Agent/Coding Plan 目前在 cn-beijing
SERVICE = "ark"
ACTION = "GetCodingPlanUsage"
CONTENT_TYPE = "application/json; charset=utf-8"
# 火山特有：固定顺序，不排序（见模块 docstring）
SIGNED_HEADERS = "host;x-date;x-content-sha256;content-type"
CACHE_TTL = 120  # 秒（成功与失败都缓存，失败时避免后台轮询反复打网关）

_cache = {"at": 0.0, "data": None}


def reset_cache():
    """凭证变更后调用，下一次查询立刻用新凭证。"""
    _cache["at"] = 0.0
    _cache["data"] = None


def _uri_encode(s):
    """RFC3986：unreserved 之外全部 %XX 编码（大写十六进制）。"""
    out = []
    for b in s.encode("utf-8"):
        c = chr(b)
        if (b < 128 and c.isalnum()) or c in "-_.~":
            out.append(c)
        else:
            out.append("%{:02X}".format(b))
    return "".join(out)


def _canonical_query():
    pairs = sorted([("Action", ACTION), ("Region", REGION), ("Version", API_VERSION)])
    return "&".join("{}={}".format(_uri_encode(k), _uri_encode(v)) for k, v in pairs)


def sign_request(ak, sk, now=None):
    """构造签名头。返回 (url, headers)。now 可注入便于测试（UTC 时间戳）。"""
    canonical_query = _canonical_query()
    body = b""
    payload_hash = hashlib.sha256(body).hexdigest()
    ts = time.gmtime(now if now is not None else time.time())
    x_date = time.strftime("%Y%m%dT%H%M%SZ", ts)
    short_date = time.strftime("%Y%m%d", ts)

    canonical_headers = ("host:{}\nx-date:{}\nx-content-sha256:{}\ncontent-type:{}\n"
                         .format(OPENAPI_HOST, x_date, payload_hash, CONTENT_TYPE))
    canonical_request = "\n".join([
        "POST", "/", canonical_query, canonical_headers, SIGNED_HEADERS, payload_hash,
    ])
    scope = "{}/{}/{}/request".format(short_date, REGION, SERVICE)
    string_to_sign = "\n".join([
        "HMAC-SHA256", x_date, scope,
        hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
    ])

    def h(key, data):
        return hmac.new(key, data.encode("utf-8"), hashlib.sha256).digest()

    k_date = h(sk.encode("utf-8"), short_date)
    k_region = h(k_date, REGION)
    k_service = h(k_region, SERVICE)
    k_signing = h(k_service, "request")
    signature = hmac.new(k_signing, string_to_sign.encode("utf-8"),
                         hashlib.sha256).hexdigest()
    authorization = ("HMAC-SHA256 Credential={}/{}, SignedHeaders={}, Signature={}"
                     .format(ak, scope, SIGNED_HEADERS, signature))
    url = "https://{}/?{}".format(OPENAPI_HOST, canonical_query)
    headers = {
        "X-Date": x_date,
        "X-Content-Sha256": payload_hash,
        "Content-Type": CONTENT_TYPE,
        "Authorization": authorization,
    }
    return url, headers


def resolve_credentials(settings):
    """返回 (ak, sk, 来源说明)。settings 优先，环境变量兜底。"""
    ak = str((settings or {}).get("ark_ak") or "").strip()
    sk = str((settings or {}).get("ark_sk") or "").strip()
    if ak and sk:
        return ak, sk, "界面配置"
    ak = os.environ.get("VOLCENGINE_ACCESS_KEY_ID", "").strip()
    sk = os.environ.get("VOLCENGINE_SECRET_ACCESS_KEY", "").strip()
    if ak and sk:
        return ak, sk, "环境变量"
    return None, None, None


def _response_error(body):
    """火山 OpenAPI 的错误信封：ResponseMetadata.Error（业务错误也常以 200 返回）。"""
    err = (body.get("ResponseMetadata") or {}).get("Error") or body.get("Error")
    if not isinstance(err, dict):
        return None
    code = str(err.get("Code") or "")
    msg = str(err.get("Message") or "")
    return (code, msg) if (code or msg) else None


def _is_auth_error(code):
    c = code.lower()
    return any(k in c for k in ("auth", "signature", "denied", "credential",
                                "token", "forbidden", "unauthorized", "accesskey"))


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _reset_ts(v):
    """ResetTime 兼容秒 / 毫秒 / ISO 字符串；<=0（无活跃窗口的哨兵）视为无。"""
    n = _num(v)
    if n is not None:
        if n <= 0:
            return None
        return n / 1000 if n >= 1e12 else n
    if isinstance(v, str):
        try:
            return datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


# Level →（窗口标签, 排序权重）。实测字段名是 Level，其余作防御式 fallback。
_LEVELS = {
    "session": ("当前会话", 0), "5h": ("当前会话", 0),
    "fivehour": ("当前会话", 0), "five_hour": ("当前会话", 0),
    "weekly": ("每周窗口", 1), "week": ("每周窗口", 1), "7d": ("每周窗口", 1),
    "monthly": ("每月窗口", 2), "month": ("每月窗口", 2),
}


def parse_windows(result):
    """把 GetCodingPlanUsage 的 Result 解析成窗口列表（只认官方百分比）。

    官方文档未给出逐字段规格：按 arkcli 描述，CodingPlan 后端只回 Percent，
    重置时间为秒级。这里宽松匹配数组名（QuotaUsage/Usages/Details）与字段名，
    命中即用、未命中跳过；空数组 = 未订阅。
    """
    arr = None
    for key in ("QuotaUsage", "Usages", "Details"):
        if isinstance(result.get(key), list):
            arr = result[key]
            break
    if not arr:
        return []
    windows = []
    for item in arr:
        if not isinstance(item, dict):
            continue
        label = ""
        for key in ("Level", "Type", "Period", "Label", "Window"):
            if item.get(key):
                label = str(item[key])
                break
        meta = _LEVELS.get(label.lower())
        if not meta:
            continue
        pct = None
        for key in ("Percent", "UsedPercent", "UsagePercent"):
            pct = _num(item.get(key))
            if pct is not None:
                break
        resets_at = None
        for key in ("ResetTime", "ResetTimestamp", "NextRefreshTime"):
            if item.get(key) is not None:
                resets_at = _reset_ts(item.get(key))
                break
        windows.append({
            "label": meta[0],
            "used_percent": round(max(0.0, min(pct or 0.0, 100.0)), 2),
            "resets_at": resets_at,
            "percent_only": True,  # 接口不返回绝对额度，本地日志也无法归因到方舟账号
            "_order": meta[1],
        })
    windows.sort(key=lambda w: w["_order"])
    for w in windows:
        w.pop("_order", None)
    return windows


def query_ark(settings=None):
    """查询火山方舟 Coding Plan 配额。成功返回卡片 dict，失败返回 {"error": ...}。"""
    now = time.time()
    if _cache["data"] is not None and now - _cache["at"] < CACHE_TTL:
        return _cache["data"]

    ak, sk, source = resolve_credentials(settings)
    if not ak:
        card = {"agent": "火山方舟", "error":
                "未配置火山引擎 AK/SK：点 Plan 页右上「方舟配额」填写"
                "（控制台「访问控制 → 密钥管理」创建）"}
        _cache.update({"at": now, "data": card})
        return card

    url, headers = sign_request(ak, sk)
    req = urllib.request.Request(url, data=b"", headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = b""
        try:
            raw = e.read()
        except Exception:
            pass
        try:
            env = json.loads(raw.decode("utf-8"))
        except ValueError:
            env = {}
        err = _response_error(env) if isinstance(env, dict) else None
        if err and _is_auth_error(err[0]):
            card = {"agent": "火山方舟", "error":
                    "凭证被网关拒绝（{}）：请检查 AccessKey ID / Secret 是否正确，"
                    "且账号有方舟 OpenAPI 调用权限".format(err[0])}
        elif err:
            card = {"agent": "火山方舟", "error":
                    "配额接口错误 HTTP {}（{}）：{}".format(e.code, err[0], err[1])}
        else:
            card = {"agent": "火山方舟", "error": "配额接口错误 HTTP {}".format(e.code)}
        _cache.update({"at": now, "data": card})
        return card
    except Exception as e:
        card = {"agent": "火山方舟", "error": "配额接口网络错误：{}".format(e)}
        _cache.update({"at": now, "data": card})
        return card

    err = _response_error(body) if isinstance(body, dict) else None
    if err:
        if _is_auth_error(err[0]):
            card = {"agent": "火山方舟", "error":
                    "凭证被网关拒绝（{}）：请检查 AccessKey ID / Secret 是否正确，"
                    "且账号有方舟 OpenAPI 调用权限".format(err[0])}
        else:
            card = {"agent": "火山方舟", "error":
                    "配额接口返回错误（{}）：{}".format(err[0], err[1])}
        _cache.update({"at": now, "data": card})
        return card

    result = body.get("Result") if isinstance(body, dict) else None
    windows = parse_windows(result or {})
    if not windows:
        card = {"agent": "火山方舟", "error":
                "未查询到生效的 Coding Plan 订阅（或该 AK/SK 所属账号未订阅）"}
        _cache.update({"at": now, "data": card})
        return card

    card = {
        "agent": "火山方舟",
        "plan_type": "Coding Plan",
        "updated": datetime.now().astimezone().isoformat(timespec="seconds"),
        "note": "火山引擎 OpenAPI · 实时 · 凭证来自{}".format(source),
        "windows": windows,
    }
    _cache.update({"at": now, "data": card})
    return card
