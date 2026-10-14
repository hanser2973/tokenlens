# -*- coding: utf-8 -*-
"""Read Antigravity IDE/CLI generation accounting from local SQLite stores.

The protobuf layout is undocumented. Field numbers were checked against local
databases and the parsers in junhoyeo/tokscale and robinebers/openusage:
https://github.com/junhoyeo/tokscale/blob/main/crates/tokscale-core/src/sessions/antigravity_cli.rs
Only recorded usage and correlated generation timestamps are accepted. Neither
conversation text, credentials, file mtimes nor session creation times are used
to estimate tokens or date a generation.
"""
import os
import sqlite3
from pathlib import Path
from urllib.parse import unquote, urlsplit

MAX_BLOB_BYTES = 1 << 20
MAX_COUNTER = (1 << 53) - 1  # Exact integers in the dashboard's JavaScript.
_scan_issues = {}


def patterns():
    root = os.environ.get("GEMINI_CLI_HOME") or os.path.expanduser("~/.gemini")
    return [os.path.join(root, "antigravity*", "conversations", "*.db")]


def _varint(data, offset):
    value = 0
    for index in range(10):
        if offset >= len(data):
            raise ValueError("truncated varint")
        byte = data[offset]
        offset += 1
        if index == 9 and byte > 1:
            raise ValueError("varint overflow")
        value |= (byte & 127) << (7 * index)
        if not byte & 128:
            return value, offset
    raise ValueError("unterminated varint")


def _fields(data):
    """A bounded wire reader; length-delimited fields stay as memoryview slices."""
    if not isinstance(data, (bytes, memoryview)):
        return {}
    data = memoryview(data)
    fields, offset = {}, 0
    while offset < len(data):
        tag, offset = _varint(data, offset)
        number, wire = tag >> 3, tag & 7
        if not 0 < number < (1 << 29):
            raise ValueError("invalid field")
        if wire == 0:
            value, offset = _varint(data, offset)
        elif wire in (1, 2, 5):
            if wire == 2:
                size, offset = _varint(data, offset)
            else:
                size = 8 if wire == 1 else 4
            end = offset + size
            if end > len(data):
                raise ValueError("truncated field")
            value, offset = data[offset:end], end
        else:
            raise ValueError("unsupported wire type")
        fields.setdefault(number, value)
    return fields


def _message(fields, number):
    return _fields(fields.get(number))


def _text(fields, number):
    value = fields.get(number)
    if not isinstance(value, memoryview):
        return ""
    return bytes(value).decode("utf-8").strip()


def _counter(fields, number):
    value = fields.get(number, 0)
    if not isinstance(value, int) or not 0 <= value <= MAX_COUNTER:
        raise ValueError("invalid token count")
    return value


def _timestamp(fields):
    seconds, nanos = fields.get(1), fields.get(2, 0)
    if (isinstance(seconds, int) and 0 < seconds < 253402300800
            and isinstance(nanos, int) and 0 <= nanos < 1000000000):
        return seconds + nanos / 1000000000
    return None


def _unique(mapping, key, value):
    if key not in mapping:
        mapping[key] = value
    elif mapping[key] != value:
        mapping[key] = None  # Conflicting matches cannot date a generation.


def _step_times(con):
    by_response, by_index = {}, {}
    try:
        rows = con.execute(
            "SELECT metadata FROM steps WHERE step_type = 15 "
            "AND length(metadata) <= ?", (MAX_BLOB_BYTES,))
        for (blob,) in rows:
            try:
                meta = _fields(blob)
                ts = _timestamp(_message(meta, 1))
                if ts is None:
                    continue
                response = _text(_message(meta, 9), 11)
                if response:
                    _unique(by_response, response, ts)
                index = _message(meta, 20).get(3)
                if isinstance(index, int):
                    _unique(by_index, index, ts)
            except (ValueError, UnicodeError):
                continue
    except sqlite3.Error:
        pass  # Older databases can contain only embedded generation timestamps.
    return by_response, by_index


def _project(con):
    try:
        row = con.execute(
            "SELECT data FROM trajectory_metadata_blob WHERE length(data) <= ? LIMIT 1",
            (MAX_BLOB_BYTES,)).fetchone()
        uri = _text(_message(_fields(row[0]), 1), 1) if row else ""
        parsed = urlsplit(uri)
        if parsed.scheme != "file":
            return "未知项目"
        path = unquote(parsed.path).replace("\\", "/").rstrip("/")
        return path.rsplit("/", 1)[-1] or "未知项目"
    except (sqlite3.Error, ValueError, UnicodeError):
        return "未知项目"


def _generation(blob, index, conversation, project, by_response, by_index):
    chat = _message(_fields(blob), 1)
    usage = _message(chat, 4)
    if not usage:
        return None
    model = _text(chat, 19) or _text(chat, 21)
    system, new_input = _counter(usage, 1), _counter(usage, 2)
    cached = _counter(usage, 5)
    # #3 is the total output, already including #10 thinking. Some versions
    # omit #3 and record just its components (#9 text + #10 thinking).
    output = (_counter(usage, 3) if 3 in usage
              else _counter(usage, 9) + _counter(usage, 10))
    if not new_input + output + cached and not model:
        return None  # System-prompt bookkeeping, not a model generation.
    inp = system + new_input
    if not inp + output + cached:
        return None
    if inp + output + cached > MAX_COUNTER:
        raise ValueError("token count overflow")
    response = _text(usage, 11)
    ts = _timestamp(_message(_message(chat, 9), 4))
    if ts is None:
        ts = by_response.get(response) or by_index.get(index)
    if ts is None:
        raise ValueError("missing generation timestamp")
    # No guessed alias: keep the recorded model when its API name is unknown.
    model = model.rsplit("/", 1)[-1].lower() or "unknown"
    return {"ts": ts, "project": project, "model": model,
            "input": inp, "output": output, "cached": cached,
            "_rid": "response:" + response if response
                    else "generation:%s:%s" % (conversation, index)}


def read_events(path):
    issues = {"invalid": 0, "undated": 0, "oversized": 0, "unreadable": 0}
    _scan_issues[str(path)] = issues
    events, seen = [], set()
    try:
        con = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro",
                              uri=True, timeout=1)
    except (sqlite3.Error, OSError, ValueError):
        issues["unreadable"] = 1
        return events
    try:
        con.execute("BEGIN")  # One consistent snapshot, including committed WAL rows.
        project = _project(con)
        by_response, by_index = _step_times(con)
        conversation = Path(path).stem
        try:
            row = con.execute("SELECT trajectory_id FROM trajectory_meta LIMIT 1").fetchone()
            if row and row[0]:
                conversation = str(row[0])
        except sqlite3.Error:
            pass
        # CASE avoids bringing cumulative prompt/media BLOBs into Python memory.
        rows = con.execute(
            "SELECT idx, CASE WHEN length(data) <= ? THEN data ELSE NULL END "
            "FROM gen_metadata WHERE data IS NOT NULL ORDER BY idx", (MAX_BLOB_BYTES,))
        for index, blob in rows:
            if blob is None:
                issues["oversized"] += 1
                continue
            try:
                ev = _generation(blob, index, conversation, project, by_response, by_index)
            except (ValueError, UnicodeError) as exc:
                issues["undated" if str(exc) == "missing generation timestamp" else "invalid"] += 1
                continue
            if ev and ev["_rid"] not in seen:
                seen.add(ev["_rid"])
                events.append(ev)
    except sqlite3.Error:
        issues["unreadable"] = 1
        return []
    finally:
        con.close()
    return events


def scan_notes(paths):
    totals = {key: sum(_scan_issues.get(str(path), {}).get(key, 0) for path in paths)
              for key in ("invalid", "undated", "oversized", "unreadable")}
    notes = []
    if totals["undated"]:
        notes.append("Antigravity 有 %s 条用量缺少可关联的调用时间，已跳过；不按会话创建时间估算" % totals["undated"])
    if totals["invalid"] or totals["oversized"]:
        notes.append("Antigravity 已跳过 %s 条格式异常或超过 1 MiB 的用量记录" %
                     (totals["invalid"] + totals["oversized"]))
    if totals["unreadable"]:
        notes.append("Antigravity 有 %s 个会话数据库暂时无法读取或格式不受支持" % totals["unreadable"])
    return notes
