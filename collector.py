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


# ---------- 存储 ----------

def _load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _save_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)


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
