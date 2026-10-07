# -*- coding: utf-8 -*-
"""只读采集模型调用与 Codex 任务耗时；任务吞吐和模型生成速度分开统计。"""
import hashlib
import json
import math
import os
import sqlite3
import threading
import time
from datetime import datetime

import collector
import storage

STATE_FILE = "speed_state.json"
SAMPLES_FILE = "speed_samples.json"
RETENTION_DAYS = 30
MAX_SAMPLES = 20000
REFRESH_SECONDS = 15
_lock = threading.Lock()
_last_scan = {}


def number(value):
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def sample(path, key, agent, model, project, ts, output, request_seconds, stream_seconds=None, ttft_seconds=None, basis="request"):
    ts, output, request_seconds = number(ts), number(output), number(request_seconds)
    if not ts or not output or output <= 0 or not request_seconds or request_seconds <= 0:
        return None
    stream_seconds = number(stream_seconds)
    # 与 Kimi CLI 一样，不用不足 50ms 的 decode 窗口推算 tok/s。
    if stream_seconds is None or stream_seconds < 0.05:
        stream_seconds = None
    ttft_seconds = number(ttft_seconds)
    if ttft_seconds is not None and ttft_seconds < 0:
        ttft_seconds = None
    return {
        "id": hashlib.sha256((path + "|" + str(key)).encode()).hexdigest()[:24],
        "agent": agent, "model": collector.short_model(model),
        "project": project, "ts": ts, "output": int(output),
        "basis": basis,
        "request_seconds": request_seconds if basis == "request" else None,
        "task_seconds": request_seconds if basis == "task" else None,
        "stream_seconds": stream_seconds if basis == "request" else None,
        "ttft_seconds": ttft_seconds,
    }


def kimi_samples(lines, path, pending):
    """step.end 的流式计时排除工具执行；旧日志只计算单次请求速度。"""
    result = []
    project = collector.kimi_project_of(path)

    def finish(event=None):
        event = event or {}
        usage = event.get("usage") or pending.get("usage") or {}
        end = pending.get("end")
        start = pending.get("start")
        stream_ms = number(event.get("llmStreamDurationMs"))
        ttft_ms = number(event.get("llmFirstTokenLatencyMs"))
        request_s = ((stream_ms + ttft_ms) / 1000
                     if stream_ms is not None and stream_ms >= 0 and ttft_ms is not None and ttft_ms >= 0
                     else (end - start) / 1000 if end and start else None)
        s = sample(path, event.get("uuid") or end, "Kimi Code", pending.get("model"),
                   project, (end or 0) / 1000, usage.get("output"), request_s,
                   stream_ms / 1000 if stream_ms is not None else None,
                   ttft_ms / 1000 if ttft_ms is not None else None)
        if s:
            result.append(s)

    for line in lines:
        try:
            d = json.loads(line)
            if not isinstance(d, dict):
                continue
            typ = d.get("type")
            if typ == "llm.request":
                # 老版本没有 step.end 计时；下一个请求开始才结算前一个，支持跨扫描续接。
                if pending.get("usage"):
                    finish()
                pending.clear()
                pending.update(start=number(d.get("time")), model=d.get("model"))
            elif typ == "usage.record" and d.get("usageScope") != "session":
                pending.update(usage=d.get("usage") or {}, end=number(d.get("time")), model=d.get("model"))
            elif typ == "context.append_loop_event":
                event = d.get("event") or {}
                if event.get("type") == "step.end":
                    if not pending.get("end"):
                        pending["end"] = number(d.get("time"))
                    if event.get("finishReason") not in ("error", "aborted", "interrupted", "cancelled"):
                        finish(event)
                    pending.clear()
            elif typ in ("turn.step.interrupted", "turn.cancel"):
                pending.clear()
        except (ValueError, TypeError, AttributeError):
            continue
    return result


def _codex_timestamp(value):
    """任务 started_at/completed_at 为 Unix 秒；外层 timestamp 为 ISO 时间。"""
    n = number(value)
    if n is not None:
        return n
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def codex_samples(lines, path, pending):
    """仅结算成功完成的整轮任务，使用 turn_token_usage，忽略旧 token_count 快照。"""
    result = []
    turns = pending.setdefault("turns", {})
    for line in lines:
        try:
            d = json.loads(line)
            if not isinstance(d, dict) or not isinstance(d.get("payload"), dict):
                continue
            p = d["payload"]
            typ, sub = d.get("type"), p.get("type")
            if typ == "session_meta":
                pending["project"] = collector.project_of(p.get("cwd"))
                continue
            turn_id = p.get("turn_id") or pending.get("current_turn")
            if typ == "event_msg" and sub == "task_started" and turn_id:
                pending["current_turn"] = turn_id
                st = turns.setdefault(turn_id, {})
                st.setdefault("start", _codex_timestamp(p.get("started_at") or d.get("timestamp")))
                st.setdefault("model", pending.get("model", "unknown"))
                st.setdefault("project", pending.get("project", collector.UNKNOWN_PROJECT))
            elif typ == "turn_context":
                if p.get("model"):
                    pending["model"] = collector.short_model(p["model"])
                if p.get("cwd"):
                    pending["project"] = collector.project_of(p["cwd"])
                if turn_id and turn_id in turns:
                    turns[turn_id].update(model=pending.get("model", "unknown"),
                                          project=pending.get("project", collector.UNKNOWN_PROJECT))
            elif typ == "token_usage_record" and turn_id in turns:
                st = turns[turn_id]
                usage = p.get("turn_token_usage")
                if not isinstance(usage, dict):
                    continue
                output = number(usage.get("output_tokens"))
                if output is None or output < 0:
                    st["invalid_usage"] = True
                    continue
                if st.get("output") is not None and output < st["output"]:
                    st["invalid_usage"] = True
                # 同一任务累计值每次覆盖，不累加，不加 reasoning_output_tokens（已包含在 output 中）。
                st["output"] = output
                models = st.setdefault("models", [])
                model = st.get("model", "unknown")
                if model not in models:
                    models.append(model)
                projects = st.setdefault("projects", [])
                project = st.get("project", collector.UNKNOWN_PROJECT)
                if project not in projects:
                    projects.append(project)
            elif typ == "event_msg" and sub in ("task_complete", "turn_aborted"):
                st = turns.pop(turn_id, None)
                if pending.get("current_turn") == turn_id:
                    pending.pop("current_turn", None)
                if not st or st.get("invalid_usage") or sub == "turn_aborted" or p.get("error"):
                    continue
                end = _codex_timestamp(p.get("completed_at") or d.get("timestamp"))
                start = _codex_timestamp(p.get("started_at")) or st.get("start")
                duration_ms = number(p.get("duration_ms"))
                duration = duration_ms / 1000 if duration_ms is not None else (end - start if end and start else None)
                ttft_ms = number(p.get("time_to_first_token_ms"))
                models, projects = st.get("models", []), st.get("projects", [])
                model = models[0] if len(models) == 1 else "多模型任务" if models else st.get("model")
                project = projects[0] if len(projects) == 1 else "多项目任务" if projects else st.get("project")
                # 分叉/回放的 rollout 可携带相同历史任务，按全局 turn_id 去重，避免跨文件翻倍。
                s = sample("codex-task", turn_id, "Codex CLI", model, project or collector.UNKNOWN_PROJECT,
                           end, st.get("output"), duration,
                           ttft_seconds=ttft_ms / 1000 if ttft_ms is not None else None, basis="task")
                if s:
                    result.append(s)
        except (ValueError, TypeError, AttributeError):
            continue
    # 已完成的任务立即移除；只保留最近的未完成任务，避免旧日志状态无限增长。
    while len(turns) > 128:
        del turns[next(iter(turns))]
    return result


def zcode_samples(path):
    try:
        con = sqlite3.connect("file:%s?mode=ro" % path.replace("\\", "/"), uri=True)
        try:
            rows = con.execute("""SELECT m.logical_request_id, m.model_id, m.started_at,
                m.completed_at, m.output_tokens, s.directory FROM model_usage m
                LEFT JOIN session s ON s.id = m.session_id
                WHERE m.status = 'completed' OR m.status IS NULL OR m.status = ''""").fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return []
    result = []
    for rid, model, start, end, output, cwd in rows:
        start, end = number(start), number(end)
        if start is None or end is None:
            continue
        s = sample(path, rid or end, "ZCode", model, collector.project_of(cwd),
                   end / 1000, output, (end - start) / 1000)
        if s:
            result.append(s)
    return result


def opencode_samples(path):
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        t, tokens = d.get("time") or {}, d.get("tokens") or {}
        start, end = number(t.get("created")), number(t.get("completed"))
        if d.get("role") != "assistant" or d.get("error") or start is None or end is None:
            return []
        output = (number(tokens.get("output")) or 0) + (number(tokens.get("reasoning")) or 0)
        s = sample(path, d.get("id") or end, "opencode", d.get("modelID") or d.get("modelId"),
                   collector.project_of((d.get("path") or {}).get("cwd")),
                   end / 1000, output, (end - start) / 1000)
        return [s] if s else []
    except (OSError, ValueError, TypeError, AttributeError):
        return []


def collect(data_dir, force=False):
    """独立偏移支持已有日志回填；只读已完成记录，半行留待下次扫描。"""
    with _lock:
        now = time.time()
        if not force and now - _last_scan.get(data_dir, 0) < REFRESH_SECONDS:
            return
        state = storage.load_json(os.path.join(data_dir, STATE_FILE), {})
        samples = storage.load_json(os.path.join(data_dir, SAMPLES_FILE), [])
        state = state if isinstance(state, dict) else {}
        offsets = state.setdefault("offsets", {})
        pending = state.setdefault("pending", {})
        mtimes = state.setdefault("mtimes", {})
        by_id = {s["id"]: s for s in samples if isinstance(s, dict) and s.get("id")}
        for src in collector.SOURCES:
            if src.kind not in ("kimi", "codex", "zcode", "opencode"):
                continue
            for path in collector.source_files(src):
                try:
                    if src.kind in ("kimi", "codex"):
                        size = os.path.getsize(path)
                        offset = offsets.get(path, 0)
                        if size < offset:
                            offset = 0
                            pending.pop(path, None)
                        if size == offset:
                            continue
                        with open(path, "rb") as f:
                            f.seek(offset)
                            chunk = f.read()
                        used = chunk.rfind(b"\n") + 1
                        parse = kimi_samples if src.kind == "kimi" else codex_samples
                        found = parse(chunk[:used].splitlines(), path, pending.setdefault(path, {}))
                        offsets[path] = offset + used
                    elif src.kind == "zcode":
                        # WAL 写入不保证主库 mtime 更新，不能借 mtime 跳过 SQLite。
                        found = zcode_samples(path)
                    else:
                        mtime = os.stat(path).st_mtime_ns
                        if mtimes.get(path) == mtime:
                            continue
                        found = opencode_samples(path)
                        mtimes[path] = mtime
                    by_id.update((s["id"], s) for s in found)
                except OSError:
                    continue
        kept = sorted((s for s in by_id.values() if s["ts"] >= now - RETENTION_DAYS * 86400),
                      key=lambda s: s["ts"])[-MAX_SAMPLES:]
        storage.save_json(os.path.join(data_dir, SAMPLES_FILE), kept)
        storage.save_json(os.path.join(data_dir, STATE_FILE), state)
        _last_scan[data_dir] = now


def report(data_dir, date_from="", date_to="", agent="all", model="all", project="all"):
    collect(data_dir)
    groups = {}
    samples = storage.load_json(os.path.join(data_dir, SAMPLES_FILE), [])
    for s in samples:
        day = datetime.fromtimestamp(s["ts"]).strftime("%Y-%m-%d")
        if date_from and day < date_from or date_to and day > date_to:
            continue
        if any(v != "all" and s.get(k) != v for k, v in (("agent", agent), ("model", model), ("project", project))):
            continue
        groups.setdefault((s["agent"], s["model"]), []).append(s)
    rows = []
    for (a, m), ss in groups.items():
        latest = max(ss, key=lambda s: s["ts"])
        stream = [s for s in ss if s.get("stream_seconds")]
        requests = [s for s in ss if s.get("request_seconds") and s.get("basis") != "task"]
        tasks = [s for s in ss if s.get("task_seconds") and s.get("basis") == "task"]
        ttfts = [s["ttft_seconds"] for s in ss if s.get("ttft_seconds") is not None]
        rows.append({
            "agent": a, "model": m, "samples": len(ss), "stream_samples": len(stream),
            "request_samples": len(requests), "task_samples": len(tasks),
            "measurement_basis": "task" if tasks and not requests else "request",
            "stream_tps": round(sum(s["output"] for s in stream) / sum(s["stream_seconds"] for s in stream), 2) if stream else None,
            "request_tps": round(sum(s["output"] for s in requests) / sum(s["request_seconds"] for s in requests), 2) if requests else None,
            "task_tps": round(sum(s["output"] for s in tasks) / sum(s["task_seconds"] for s in tasks), 2) if tasks else None,
            "latest_stream_tps": round(latest["output"] / latest["stream_seconds"], 2) if latest.get("stream_seconds") else None,
            "latest_request_tps": round(latest["output"] / latest["request_seconds"], 2) if latest.get("request_seconds") else None,
            "latest_task_tps": round(latest["output"] / latest["task_seconds"], 2) if latest.get("task_seconds") else None,
            "ttft_seconds": round(sum(ttfts) / len(ttfts), 3) if ttfts else None,
            "last_seen": latest["ts"],
        })
    return {"rows": sorted(rows, key=lambda r: r["last_seen"], reverse=True), "now": time.time(),
            "retention_days": RETENTION_DAYS, "max_samples": MAX_SAMPLES, "scope": "local",
            "refresh_seconds": REFRESH_SECONDS}
