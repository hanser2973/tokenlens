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
import sys
import storage
from collections import namedtuple
from datetime import datetime, timedelta

STATE_FILE = "collector_state.json"
AUTO_FILE = "auto_usage.json"
MINUTES_FILE = "auto_minutes.json"
UNKNOWN_PROJECT = "未知项目"  # 取不到工作目录，以及升级前采集的老数据
SEEN_CAP = 50000  # Claude 消息去重集合上限
# 分钟桶只用于滚动窗口（5h/每周）的额度估算与多端同步，保留 10 天足够覆盖周窗口
MINUTES_RETENTION_DAYS = 10


def short_model(name):
    """accounts/fireworks/models/glm-5p2 -> glm-5p2；统一小写避免大小写拆行"""
    name = str(name or "").strip()
    if "/" in name:
        name = name.rsplit("/", 1)[-1]
    return name.lower() or "unknown"


def project_of(cwd):
    """工作目录 -> 项目名（取最后一段）。C:\\code\\tokenlens -> tokenlens。

    只留目录名而不是完整路径：完整路径是隐私（多端同步时会传出去），
    目录名足够区分项目，也正是用户脑子里对项目的称呼。
    """
    p = str(cwd or "").strip().replace("\\", "/").rstrip("/")
    if not p:
        return UNKNOWN_PROJECT
    name = p.rsplit("/", 1)[-1]
    return name or UNKNOWN_PROJECT


def kimi_project_of(path):
    """从 Kimi 会话路径取项目名：.../sessions/wd_<项目>_<hash>/session_xxx/...

    Kimi 的 wire.jsonl 里没有 cwd（翻遍 metadata/config.update 都没有），只有会话
    目录名带着工作目录的 slug，只能从这儿取。hash 是路径摘要，去掉。
    """
    parts = str(path).replace("\\", "/").split("/")
    for seg in parts:
        if seg.startswith("wd_"):
            body = seg[3:]
            return body.rsplit("_", 1)[0] or UNKNOWN_PROJECT  # 去掉尾部 hash
    return UNKNOWN_PROJECT


# ---------- 各工具日志解析 ----------

def make_kimi_parser(path):
    """Kimi Code: {"type":"usage.record","model":...,"usage":{...},"time":ms}，每条=一轮实际用量"""
    project = kimi_project_of(path)

    def parse(line):
        d = json.loads(line)
        if d.get("type") != "usage.record":
            return None
        u = d.get("usage") or {}
        ts = (d.get("time") or 0) / 1000
        if not ts:
            return None
        return {
            "ts": ts,
            "project": project,
            "model": short_model(d.get("model")),
            "input": int(u.get("inputOther") or 0) + int(u.get("inputCacheCreation") or 0),
            "output": int(u.get("output") or 0),
            "cached": int(u.get("inputCacheRead") or 0),
        }
    return parse


def make_claude_parser(state, path=""):
    """Claude Code: assistant 行的 message.usage；同一 message.id 可能出现多次（流式，
    以及 resume / 压缩后把历史消息复制进新会话文件），按 id 去重。

    seen 是 dict（值恒为 None），只借它的**插入顺序**当 LRU：命中时移到末尾，
    收尾按顺序裁剪，淘汰的一定是最久没再出现过的 id。
    """
    seen = state.setdefault("seen_claude", {})

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
                del seen[mid]      # 仍在被回放的老消息刷新到队尾，别先被裁掉
                seen[mid] = None
                return None
            seen[mid] = None
        try:
            ts = datetime.fromisoformat(str(d.get("timestamp", "")).replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
        return {
            "ts": ts,
            # 每行自带 cwd；缺失时退回目录名（~/.claude/projects/<路径转义> 形如 c--code-foo）
            "project": project_of(d.get("cwd")) if d.get("cwd") else _claude_dir_project(path),
            "model": short_model(m.get("model")),
            "input": int(u.get("input_tokens") or 0) + int(u.get("cache_creation_input_tokens") or 0),
            "output": int(u.get("output_tokens") or 0),
            "cached": int(u.get("cache_read_input_tokens") or 0),
        }
    return parse


def _claude_dir_project(path):
    """~/.claude/projects/c--code-pythoncode-tokenlens/xx.jsonl -> tokenlens"""
    parent = os.path.basename(os.path.dirname(str(path)))
    return parent.rsplit("-", 1)[-1] or UNKNOWN_PROJECT if parent else UNKNOWN_PROJECT


def make_codex_parser(path=""):
    """Codex CLI: session_meta / turn_context 记录当前模型与工作目录；
    token_count 的 last_token_usage 为单次调用增量"""
    cur = {"model": "unknown", "project": UNKNOWN_PROJECT}

    def parse(line):
        d = json.loads(line)
        t = d.get("type")
        if t in ("turn_context", "session_meta"):
            p = d.get("payload") or {}
            if p.get("model"):
                cur["model"] = p["model"]
            if p.get("cwd"):
                cur["project"] = project_of(p["cwd"])
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
            "project": cur["project"],
            "model": short_model(cur["model"]),
            "input": max(0, total_in - cached),
            "output": out,
            "cached": cached,
        }
    return parse


def _cline_models(path):
    """Cline 系任务目录里的 task_metadata.json：model_usage 按时间记了用过哪些模型。

    返回按时间升序的 [(ts秒, 模型名)]；读不到就空列表（模型记 unknown）。
    ui_messages.json 本身不带模型名，只能从这里对。
    """
    meta_path = os.path.join(os.path.dirname(path), "task_metadata.json")
    meta = storage.load_json(meta_path, None)
    if not isinstance(meta, dict):
        return []
    out = []
    for m in meta.get("model_usage") or []:
        if not isinstance(m, dict):
            continue
        mid = m.get("model_id") or m.get("modelId")
        try:
            ts = float(m.get("ts") or 0) / 1000
        except (TypeError, ValueError):
            continue
        if mid and ts:
            out.append((ts, short_model(mid)))
    out.sort()
    return out


def parse_cline_snapshot(data, path):
    """Cline / Roo Code / Kilo Code：tasks/<id>/ui_messages.json 是整份重写的消息数组，
    其中 say="api_req_started" 的 text 字段是一段 JSON 字符串，带本次请求的 token 数。

    这三个扩展同源（Roo / Kilo 都是 Cline 的分支），消息结构一致。
    工作目录没有稳定地写进这两个文件，所以项目维度记为未知。
    """
    if not isinstance(data, list):
        return []
    models = _cline_models(path)
    events = []
    for msg in data:
        if not isinstance(msg, dict) or msg.get("say") != "api_req_started":
            continue
        try:
            info = json.loads(msg.get("text") or "{}")
        except (ValueError, TypeError):
            continue
        if not isinstance(info, dict):
            continue

        def n(*keys):
            for k in keys:
                try:
                    v = int(info.get(k) or 0)
                except (TypeError, ValueError):
                    continue
                if v:
                    return v
            return 0

        inp, out = n("tokensIn"), n("tokensOut")
        cache_w, cache_r = n("cacheWrites"), n("cacheReads")
        if inp + out + cache_w + cache_r == 0:
            continue  # 请求还没回来时这些字段是空的
        try:
            ts = float(msg.get("ts") or 0) / 1000
        except (TypeError, ValueError):
            continue
        if not ts:
            continue
        model = "unknown"
        for mts, mid in models:  # 取该请求发生时正在用的那个模型
            if mts <= ts + 1:
                model = mid
        events.append({
            "ts": ts, "project": UNKNOWN_PROJECT, "model": model,
            "input": inp + cache_w,  # 缓存写入按输入计，与其他采集源口径一致
            "output": out, "cached": cache_r,
        })
    return events


def parse_opencode_snapshot(data, path):
    """opencode：storage/message/<会话>/<消息>.json，一个文件就是一条消息。

    assistant 消息带 tokens {input, output, cache:{read, write}} 与 modelID，
    path.cwd 给出工作目录，项目维度可用。
    """
    if not isinstance(data, dict) or data.get("role") != "assistant":
        return []
    tok = data.get("tokens")
    if not isinstance(tok, dict):
        return []
    cache = tok.get("cache") if isinstance(tok.get("cache"), dict) else {}

    def n(v):
        try:
            return max(0, int(v or 0))
        except (TypeError, ValueError):
            return 0

    inp, out = n(tok.get("input")), n(tok.get("output"))
    cache_r, cache_w = n(cache.get("read")), n(cache.get("write"))
    if inp + out + cache_r + cache_w == 0:
        return []
    t = data.get("time") if isinstance(data.get("time"), dict) else {}
    try:
        ts = float(t.get("completed") or t.get("created") or 0) / 1000
    except (TypeError, ValueError):
        return []
    if not ts:
        return []
    p = data.get("path") if isinstance(data.get("path"), dict) else {}
    return [{
        "ts": ts,
        "project": project_of(p.get("cwd") or p.get("root")),
        "model": short_model(data.get("modelID") or data.get("modelId")),
        "input": inp + cache_w,
        "output": out + n(tok.get("reasoning")),
        "cached": cache_r,
    }]


SNAPSHOT_PARSERS = {"cline": parse_cline_snapshot, "opencode": parse_opencode_snapshot}


# ---------- 采集源注册表 ----------
# mode 决定怎么扫：
#   jsonl    追加写的 jsonl，按字节偏移增量读（Kimi / Claude / Codex）
#   snapshot 整份重写的 JSON 文件，按 mtime 判断是否要重读，靠时间戳去重
#            （Cline 系扩展的 ui_messages.json、opencode 的单条消息 JSON）
Source = namedtuple("Source", "agent patterns kind mode")


def _vscode_globalstorage(ext_id):
    """VS Code 系编辑器里某个扩展的 tasks 目录。Cursor / Windsurf / Trae 都是 VS Code
    的衍生版，装同一个扩展时目录结构一致，所以一并扫。"""
    home = os.path.expanduser("~")
    if sys.platform == "win32":
        bases = [os.environ.get("APPDATA") or os.path.join(home, "AppData", "Roaming")]
    elif sys.platform == "darwin":
        bases = [os.path.join(home, "Library", "Application Support")]
    else:
        bases = [os.path.join(home, ".config")]
    apps = ["Code", "Code - Insiders", "VSCodium", "Cursor", "Windsurf", "Trae", "Trae CN"]
    return [os.path.join(b, a, "User", "globalStorage", ext_id, "tasks", "*", "ui_messages.json")
            for b in bases for a in apps]


SOURCES = [
    Source("Kimi Code",   ["~/.kimi-code/sessions/*/*/agents/*/wire.jsonl"], "kimi",   "jsonl"),
    Source("Claude Code", ["~/.claude/projects/*/*.jsonl"],                  "claude", "jsonl"),
    Source("Codex CLI",   ["~/.codex/sessions/**/rollout-*.jsonl"],          "codex",  "jsonl"),
    # Cline 与它的两个分支共用同一套 tasks/ui_messages.json 结构
    Source("Cline",       _vscode_globalstorage("saoudrizwan.claude-dev"),     "cline", "snapshot"),
    Source("Roo Code",    _vscode_globalstorage("rooveterinaryinc.roo-cline"), "cline", "snapshot"),
    Source("Kilo Code",   _vscode_globalstorage("kilocode.kilo-code"),         "cline", "snapshot"),
    Source("opencode",    ["~/.local/share/opencode/storage/message/*/*.json",
                           "~/AppData/Local/opencode/storage/message/*/*.json"],
           "opencode", "snapshot"),
]

_AGENT_KINDS = {s.agent: s.kind for s in SOURCES}


def kind_patterns(kind):
    """某 kind 下所有采集源的 glob 模式（Cline 系有多个编辑器目录）。"""
    return [p for s in SOURCES if s.kind == kind for p in s.patterns]


def source_files(src):
    """展开一个采集源的全部日志文件路径。"""
    return [f for p in src.patterns
            for f in glob.glob(os.path.expanduser(p), recursive=True)]


def kind_for_agent(agent):
    """'Kimi Code' -> 'kimi'；不是自动采集的 Agent（手动录入的）返回 None。"""
    return _AGENT_KINDS.get(agent)


def _parser_for(kind, state, path=""):
    if kind == "kimi":
        return make_kimi_parser(path)
    if kind == "claude":
        return make_claude_parser(state, path)
    return make_codex_parser(path)


def parse_chunk(src, path, chunk, state):
    """把一个文件的字节内容解析成事件列表，屏蔽两种存放方式的差异。

    jsonl 逐行解析；snapshot 整份 JSON 交给对应解析器。解析失败一律当作没有事件，
    不让某个工具的格式变动拖垮整轮采集。
    """
    if src.mode == "snapshot":
        parser = SNAPSHOT_PARSERS.get(src.kind)
        if not parser:
            return []
        try:
            return parser(json.loads(chunk.decode("utf-8", errors="replace")), path)
        except Exception:
            return []
    parse = _parser_for(src.kind, state, path)
    events = []
    for raw in chunk.split(b"\n"):
        raw = raw.strip()
        if not raw:
            continue
        try:
            ev = parse(raw.decode("utf-8", errors="replace"))
        except ValueError:
            continue
        if ev:
            events.append(ev)
    return events


def _scan_events(sources, since_ts):
    """重扫原始日志，产出 (agent, 事件) 流。跳过 mtime 明显过老的文件（留 1h 余量）。"""
    for src in sources:
        state = {}
        for path in source_files(src):
            try:
                if os.path.getmtime(path) < since_ts - 3600:
                    continue
                with open(path, "rb") as f:
                    chunk = f.read()
            except OSError:
                continue
            for ev in parse_chunk(src, path, chunk, state):
                yield src.agent, ev


def usage_since(kind, since_ts):
    """精确汇总某工具 since_ts 之后的事件级用量（用于套餐窗口估算）。没有该工具返回 None。"""
    sources = [s for s in SOURCES if s.kind == kind]
    if not sources:
        return None
    total = {"input": 0, "output": 0, "cached": 0, "requests": 0}
    for _, ev in _scan_events(sources, since_ts):
        if ev["ts"] < since_ts:
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
    for agent, ev in _scan_events(SOURCES, day_start):
        if not (day_start <= ev["ts"] < day_end):
            continue
        dt = datetime.fromtimestamp(ev["ts"])
        if hour is not None and dt.hour != hour:
            continue
        b = f"{dt.hour:02d}" if hour is None else f"{dt.hour:02d}:{dt.minute:02d}"
        slot = buckets.setdefault((b, agent, ev["model"]),
                                  {"input": 0, "output": 0, "cached": 0, "requests": 0})
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
    for pattern in kind_patterns("codex"):
        for path in glob.glob(os.path.expanduser(pattern), recursive=True):
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

def _prune_minutes(minutes, today=None):
    """丢弃早于保留期的分钟桶，避免文件无限增长。就地修改并返回同一个 dict。"""
    today = today or datetime.now().date()
    cutoff = (today - timedelta(days=MINUTES_RETENTION_DAYS)).isoformat()
    for key in [k for k in minutes if k[:10] < cutoff]:
        del minutes[key]
    return minutes


def _collect_jsonl(src, path, offsets, state, record):
    """追加写的 jsonl：从上次的字节偏移往后读，只处理完整行。返回新增事件数。"""
    try:
        size = os.path.getsize(path)
    except OSError:
        return 0
    offset = offsets.get(path, 0)
    if offset > size:  # 文件被截断/重建
        offset = 0
    if offset == size:
        return 0
    try:
        with open(path, "rb") as f:
            f.seek(offset)
            chunk = f.read()
    except OSError:
        return 0
    last_nl = chunk.rfind(b"\n")
    if last_nl < 0:
        return 0  # 没有完整行（可能正在写入）
    offsets[path] = offset + last_nl + 1
    events = parse_chunk(src, path, chunk[:last_nl + 1], state)
    for ev in events:
        record(src.agent, ev)
    return len(events)


def _collect_snapshot(src, path, snap_ts, snap_mtime, record):
    """整份重写的 JSON：mtime 没变就跳过，变了就重读整份、只收时间戳比上次新的事件。

    这类文件（Cline 的 ui_messages.json）每次都是全量重写，没有稳定的字节偏移可用；
    靠「已处理到哪个时间戳」去重，重读同一份文件不会重复计数。
    """
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return 0
    if snap_mtime.get(path) == mtime:
        return 0
    try:
        with open(path, "rb") as f:
            chunk = f.read()
    except OSError:
        return 0
    snap_mtime[path] = mtime
    try:
        last = float(snap_ts.get(path) or 0)
    except (TypeError, ValueError):
        last = 0
    n, newest = 0, last
    for ev in parse_chunk(src, path, chunk, {}):
        if ev["ts"] <= last:
            continue
        record(src.agent, ev)
        newest = max(newest, ev["ts"])
        n += 1
    snap_ts[path] = newest
    return n


def collect_once(data_dir):
    """增量扫描一次，返回 {agent: {"files": n, "events": n}} 摘要。"""
    state_path = os.path.join(data_dir, STATE_FILE)
    auto_path = os.path.join(data_dir, AUTO_FILE)
    minutes_path = os.path.join(data_dir, MINUTES_FILE)
    state = _load_json(state_path, {})
    if isinstance(state.get("seen_claude"), list):
        # JSON 里存的是保序列表，还原成 dict 继续当 LRU 用
        state["seen_claude"] = dict.fromkeys(state["seen_claude"])
    offsets = state.setdefault("offsets", {})
    auto = _load_json(auto_path, {})
    minutes = _load_json(minutes_path, {})

    snap_ts = state.setdefault("snapshot_ts", {})
    snap_mtime = state.setdefault("snapshot_mtime", {})

    def record(agent, ev):
        dt = datetime.fromtimestamp(ev["ts"])
        day = dt.strftime("%Y-%m-%d")
        project = ev.get("project") or UNKNOWN_PROJECT
        # 日聚合带项目维度；分钟桶不带 —— 它只服务滚动窗口与多端同步，
        # 加一维只会让桶数翻几倍、同步文件变大，换不来任何东西。
        for store, key in ((auto, f"{day}|{agent}|{ev['model']}|{project}"),
                           (minutes, f"{dt.strftime('%Y-%m-%dT%H:%M')}|{agent}|{ev['model']}")):
            slot = store.setdefault(key, {"input": 0, "output": 0, "cached": 0, "requests": 0})
            slot["input"] += ev["input"]
            slot["output"] += ev["output"]
            slot["cached"] += ev["cached"]
            slot["requests"] += 1

    summary = {}
    for src in SOURCES:
        files = source_files(src)
        events = 0
        for path in files:
            if src.mode == "snapshot":
                events += _collect_snapshot(src, path, snap_ts, snap_mtime, record)
            else:
                events += _collect_jsonl(src, path, offsets, state, record)
        if files:
            summary[src.agent] = {"files": len(files), "events": events}

    if isinstance(state.get("seen_claude"), dict):
        # 按插入顺序保留最近 SEEN_CAP 个。早先这里是 sorted(...)[-CAP:]，
        # message id 是随机串，按字典序排完取尾部等于随机丢一半 —— 被丢掉的 id
        # 在 resume/压缩生成的新会话文件里再出现时会被重复计数。
        state["seen_claude"] = list(state["seen_claude"])[-SEEN_CAP:]
    _save_json(state_path, state)
    _save_json(auto_path, auto)
    _save_json(minutes_path, _prune_minutes(minutes))
    return summary


def load_minutes(data_dir):
    """读取分钟桶 {'YYYY-MM-DDTHH:MM|agent|model': {...}}，用于滚动窗口统计与多端同步。"""
    data = _load_json(os.path.join(data_dir, MINUTES_FILE), {})
    return data if isinstance(data, dict) else {}


def minutes_usage_since(minutes, start_ts, kind=None, agent=None):
    """汇总分钟桶中 >= start_ts 的用量。kind/agent 任一给定则只统计该 Agent。

    分钟桶按本地时间字符串存储，边界那一分钟整体计入，误差 < 60s。
    """
    total = {"input": 0, "output": 0, "cached": 0, "requests": 0}
    start_key = datetime.fromtimestamp(start_ts).strftime("%Y-%m-%dT%H:%M")
    for key, v in minutes.items():
        parts = key.split("|")
        if len(parts) != 3 or parts[0] < start_key:
            continue
        if agent is not None and parts[1] != agent:
            continue
        if kind is not None and kind_for_agent(parts[1]) != kind:
            continue
        for f in total:
            total[f] += int(v.get(f) or 0)
    return total


def minutes_coverage_start(minutes):
    """分钟桶覆盖到的最早日期 'YYYY-MM-DD'；没有任何数据返回 None。

    分钟桶只保留最近 MINUTES_RETENTION_DAYS 天，且首次采集之前的历史也不会有，
    所以「能覆盖到哪天」要按实际数据算，不能按保留期硬推。
    """
    days = [k[:10] for k in minutes if len(k) >= 10]
    return min(days) if days else None


def breakdown_from_minutes(minutes, day, hour=None):
    """用分钟桶拼出与 breakdown() 同结构的明细行；覆盖不到该日期时返回 None。

    快路径：breakdown() 要把当天动过的日志整份读进内存（Claude 的会话文件动辄几百 MB），
    而分钟桶是增量采集时顺带累加的，同样的 agent×model 精度，直接查表即可。
    """
    start = minutes_coverage_start(minutes)
    if start is None or day < start:
        return None  # 超出分钟桶覆盖范围，交给调用方回退到重扫日志
    prefix = day + "T"
    want_hour = f"{hour:02d}" if hour is not None else None
    buckets = {}
    for key, v in minutes.items():
        if not key.startswith(prefix):
            continue
        parts = key.split("|")
        if len(parts) != 3:
            continue
        stamp, agent, model = parts
        hh, mm = stamp[11:13], stamp[14:16]
        if want_hour is not None and hh != want_hour:
            continue
        slot = buckets.setdefault((hh if want_hour is None else f"{hh}:{mm}", agent, model),
                                  {"input": 0, "output": 0, "cached": 0, "requests": 0})
        for f in slot:
            slot[f] += int(v.get(f) or 0)
    rows = [{"bucket": b, "agent": a, "model": m, **v} for (b, a, m), v in buckets.items()]
    rows.sort(key=lambda r: (r["bucket"], r["agent"], r["model"]))
    return rows


def split_auto_key(key):
    """拆日聚合的键，返回 (day, agent, model, project)；格式不对返回 None。

    加项目维度之前是 3 段（日期|Agent|模型），老数据里还留着，项目记为未知。
    想把历史数据也补上项目，用界面上的「重建采集数据」重扫一遍即可。
    """
    parts = key.split("|")
    if len(parts) == 4:
        return parts[0], parts[1], parts[2], parts[3]
    if len(parts) == 3:
        return parts[0], parts[1], parts[2], UNKNOWN_PROJECT
    return None


def load_auto_records(data_dir, prices, calc_cost_fn):
    """读取自动聚合数据，转成与手动记录相同的结构（source=auto）。"""
    auto = _load_json(os.path.join(data_dir, AUTO_FILE), {})
    rows = []
    for key, v in auto.items():
        parsed = split_auto_key(key)
        if not parsed:
            continue
        day, agent, model, project = parsed
        rows.append({
            "id": "auto-" + key,
            "date": day,
            "agent": agent,
            "model": model,
            "project": project,
            "requests": v.get("requests", 0),
            "input": v.get("input", 0),
            "output": v.get("output", 0),
            "cached": v.get("cached", 0),
            "cost": calc_cost_fn(prices, model, v.get("input", 0), v.get("output", 0), v.get("cached", 0)),
            "source": "auto",
        })
    return rows
