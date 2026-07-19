# -*- coding: utf-8 -*-
"""
本地 AI 工具 token 用量采集器。

原理：各 AI 编程工具会把会话记录（含模型名与 token 用量）写入本地 jsonl 日志，
本模块增量扫描这些文件，聚合成 日 × Agent × 模型 的用量，存到 data/auto_usage.json。

目前支持：
- Kimi Code   ~/.kimi-code/sessions/*/*/agents/*/wire.jsonl   （usage.record 事件）
- Claude Code ~/.claude/projects/*/*.jsonl                    （assistant 消息 usage）
- Codex CLI   ~/.codex/sessions/**/rollout-*.jsonl            （token_count 事件）

只读扫描，不修改任何工具的数据。Cursor 等工具不在本地记录 token 明细，无法自动统计。
"""
import glob
import json
import os
import storage
from datetime import datetime

STATE_FILE = "collector_state.json"
AUTO_FILE = "auto_usage.json"
SEEN_CAP = 50000  # Claude 消息去重集合上限


def short_model(name):
    """accounts/fireworks/models/glm-5p2 -> glm-5p2；统一小写避免大小写拆行"""
    name = str(name or "").strip()
    if "/" in name:
        name = name.rsplit("/", 1)[-1]
    return name.lower() or "unknown"


# ---------- 各工具日志解析 ----------

def parse_kimi(line):
    """Kimi Code: {"type":"usage.record","model":...,"usage":{...},"time":ms}，每条=一轮实际用量"""
    d = json.loads(line)
    if d.get("type") != "usage.record":
        return None
    u = d.get("usage") or {}
    ts = (d.get("time") or 0) / 1000
    if not ts:
        return None
    return {
        "ts": ts,
        "model": short_model(d.get("model")),
        "input": int(u.get("inputOther") or 0) + int(u.get("inputCacheCreation") or 0),
        "output": int(u.get("output") or 0),
        "cached": int(u.get("inputCacheRead") or 0),
    }


def make_claude_parser(state):
    """Claude Code: assistant 行的 message.usage；同一 message.id 可能出现多次（流式），按 id 去重"""
    seen = state.setdefault("seen_claude", set())

    def parse(line):
        d = json.loads(line)
        m = d.get("message")
        if not isinstance(m, dict) or not m.get("usage"):
            return None
        u = m["usage"]
        total = (u.get("input_tokens") or 0) + (u.get("output_tokens") or 0) \
            + (u.get("cache_read_input_tokens") or 0) + (u.get("cache_creation_input_tokens") or 0)
        if total == 0:
            return None
        mid = m.get("id") or d.get("uuid")
        if mid:
            if mid in seen:
                return None
            seen.add(mid)
        try:
            ts = datetime.fromisoformat(str(d.get("timestamp", "")).replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
        return {
            "ts": ts,
            "model": short_model(m.get("model")),
            "input": int(u.get("input_tokens") or 0) + int(u.get("cache_creation_input_tokens") or 0),
            "output": int(u.get("output_tokens") or 0),
            "cached": int(u.get("cache_read_input_tokens") or 0),
        }
    return parse


def make_codex_parser():
    """Codex CLI: turn_context 记录当前模型；token_count 的 last_token_usage 为单次调用增量"""
    current_model = {"name": "unknown"}

    def parse(line):
        d = json.loads(line)
        t = d.get("type")
        if t == "turn_context":
            m = (d.get("payload") or {}).get("model")
            if m:
                current_model["name"] = m
            return None
        if t != "event_msg" or (d.get("payload") or {}).get("type") != "token_count":
            return None
        u = ((d["payload"].get("info") or {}).get("last_token_usage")) or {}
        total_in = int(u.get("input_tokens") or 0)
        cached = int(u.get("cached_input_tokens") or 0)
        out = int(u.get("output_tokens") or 0)
        if total_in + cached + out == 0:
            return None
        try:
            ts = datetime.fromisoformat(str(d.get("timestamp", "")).replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
        return {
            "ts": ts,
            "model": short_model(current_model["name"]),
            "input": max(0, total_in - cached),
            "output": out,
            "cached": cached,
        }
    return parse


SOURCES = [
    ("Kimi Code",   "~/.kimi-code/sessions/*/*/agents/*/wire.jsonl", "kimi"),
    ("Claude Code", "~/.claude/projects/*/*.jsonl",                  "claude"),
    ("Codex CLI",   "~/.codex/sessions/**/rollout-*.jsonl",          "codex"),
]

_KIND_PATTERNS = {kind: pattern for _, pattern, kind in SOURCES}


def _parser_for(kind, state):
    if kind == "kimi":
        return parse_kimi
    if kind == "claude":
        return make_claude_parser(state)
    return make_codex_parser()


def usage_since(kind, since_ts):
    """精确汇总某工具 since_ts 之后的事件级用量（用于套餐窗口估算）。没有该工具返回 None。"""
    pattern = _KIND_PATTERNS.get(kind)
    if not pattern:
        return None
    total = {"input": 0, "output": 0, "cached": 0, "requests": 0}
    state = {}
    for path in glob.glob(os.path.expanduser(pattern), recursive=True):
        try:
            if os.path.getmtime(path) < since_ts - 3600:  # 留 1h 余量，过老的文件直接跳过
                continue
            with open(path, "rb") as f:
                chunk = f.read()
        except OSError:
            continue
        parse = _parser_for(kind, state)
        for raw in chunk.split(b"\n"):
            raw = raw.strip()
            if not raw:
                continue
            try:
                ev = parse(raw.decode("utf-8", errors="replace"))
            except ValueError:
                continue
            if not ev or ev["ts"] < since_ts:
                continue
            total["input"] += ev["input"]
            total["output"] += ev["output"]
            total["cached"] += ev["cached"]
            total["requests"] += 1
    return total


def breakdown(day, hour=None):
    """扫描原始日志，返回指定日期按小时（或指定小时按分钟）分桶的明细行。

    day: 'YYYY-MM-DD'；hour: 0-23 或 None。
    返回 [{bucket, agent, model, input, output, cached, requests}]，
    bucket 形如 '13'（按小时）或 '13:42'（按分钟）。只含自动采集数据。
    """
    day_start = datetime.strptime(day, "%Y-%m-%d").timestamp()
    day_end = day_start + 86400
    buckets = {}
    for agent, pattern, kind in SOURCES:
        state = {}
        for path in glob.glob(os.path.expanduser(pattern), recursive=True):
            try:
                if os.path.getmtime(path) < day_start - 3600:
                    continue
                with open(path, "rb") as f:
                    chunk = f.read()
            except OSError:
                continue
            parse = _parser_for(kind, state)
            for raw in chunk.split(b"\n"):
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    ev = parse(raw.decode("utf-8", errors="replace"))
                except ValueError:
                    continue
                if not ev or not (day_start <= ev["ts"] < day_end):
                    continue
                dt = datetime.fromtimestamp(ev["ts"])
                if hour is not None and dt.hour != hour:
                    continue
                b = f"{dt.hour:02d}" if hour is None else f"{dt.hour:02d}:{dt.minute:02d}"
                key = (b, agent, ev["model"])
                slot = buckets.setdefault(key, {"input": 0, "output": 0, "cached": 0, "requests": 0})
                slot["input"] += ev["input"]
                slot["output"] += ev["output"]
                slot["cached"] += ev["cached"]
                slot["requests"] += 1
    rows = [
        {"bucket": b, "agent": a, "model": m, **v}
        for (b, a, m), v in buckets.items()
    ]
    rows.sort(key=lambda r: (r["bucket"], r["agent"], r["model"]))
    return rows


def latest_rate_limits(max_files=15, tail=1 << 20):
    """从 Codex 日志尾部找最新的 rate_limits（套餐窗口用量百分比）。找不到返回 None。"""
    files = []
    for path in glob.glob(os.path.expanduser(_KIND_PATTERNS["codex"]), recursive=True):
        try:
            files.append((os.path.getmtime(path), path))
        except OSError:
            continue
    files.sort(reverse=True)
    for _, path in files[:max_files]:
        try:
            size = os.path.getsize(path)
            with open(path, "rb") as f:
                f.seek(max(0, size - tail))
                chunk = f.read()
        except OSError:
            continue
        for raw in reversed(chunk.split(b"\n")):
            if b"rate_limits" not in raw:
                continue
            try:
                d = json.loads(raw.decode("utf-8", errors="replace"))
            except ValueError:
                continue
            rl = (d.get("payload") or {}).get("rate_limits")
            if rl:
                return {"rate_limits": rl, "timestamp": d.get("timestamp")}
    return None


# ---------- 存储 ----------

def _load_json(path, default):
    return storage.load_json(path, default)


def _save_json(path, obj):
    storage.save_json(path, obj)


# ---------- 主流程 ----------

def collect_once(data_dir):
    """增量扫描一次，返回 {agent: {"files": n, "events": n}} 摘要。"""
    state_path = os.path.join(data_dir, STATE_FILE)
    auto_path = os.path.join(data_dir, AUTO_FILE)
    state = _load_json(state_path, {})
    if isinstance(state.get("seen_claude"), list):
        state["seen_claude"] = set(state["seen_claude"])
    offsets = state.setdefault("offsets", {})
    auto = _load_json(auto_path, {})

    summary = {}
    for agent, pattern, kind in SOURCES:
        files = glob.glob(os.path.expanduser(pattern), recursive=True)
        events = 0
        claude_parser = make_claude_parser(state) if kind == "claude" else None
        for path in files:
            if kind == "kimi":
                parse = parse_kimi
            elif kind == "claude":
                parse = claude_parser
            else:
                parse = make_codex_parser()  # “当前模型”按文件独立跟踪

            try:
                size = os.path.getsize(path)
            except OSError:
                continue
            offset = offsets.get(path, 0)
            if offset > size:  # 文件被截断/重建
                offset = 0
            if offset == size:
                continue
            try:
                with open(path, "rb") as f:
                    f.seek(offset)
                    chunk = f.read()
            except OSError:
                continue
            last_nl = chunk.rfind(b"\n")
            if last_nl < 0:
                continue  # 没有完整行（可能正在写入）
            offsets[path] = offset + last_nl + 1
            for raw in chunk[:last_nl + 1].split(b"\n"):
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    ev = parse(raw.decode("utf-8", errors="replace"))
                except ValueError:
                    continue
                if not ev:
                    continue
                day = datetime.fromtimestamp(ev["ts"]).strftime("%Y-%m-%d")
                key = f"{day}|{agent}|{ev['model']}"
                slot = auto.setdefault(key, {"input": 0, "output": 0, "cached": 0, "requests": 0})
                slot["input"] += ev["input"]
                slot["output"] += ev["output"]
                slot["cached"] += ev["cached"]
                slot["requests"] += 1
                events += 1
        if files:
            summary[agent] = {"files": len(files), "events": events}

    if isinstance(state.get("seen_claude"), set):
        state["seen_claude"] = sorted(state["seen_claude"])[-SEEN_CAP:]
    _save_json(state_path, state)
    _save_json(auto_path, auto)
    return summary


def load_auto_records(data_dir, prices, calc_cost_fn):
    """读取自动聚合数据，转成与手动记录相同的结构（source=auto）。"""
    auto = _load_json(os.path.join(data_dir, AUTO_FILE), {})
    rows = []
    for key, v in auto.items():
        try:
            day, agent, model = key.split("|")
        except ValueError:
            continue
        rows.append({
            "id": "auto-" + key,
            "date": day,
            "agent": agent,
            "model": model,
            "requests": v.get("requests", 0),
            "input": v.get("input", 0),
            "output": v.get("output", 0),
            "cached": v.get("cached", 0),
            "cost": calc_cost_fn(prices, model, v.get("input", 0), v.get("output", 0), v.get("cached", 0)),
            "source": "auto",
        })
    return rows
