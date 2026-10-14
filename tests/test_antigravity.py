# -*- coding: utf-8 -*-
"""Antigravity accounting: wire format, real turn times, readonly WAL and dedup."""
import os
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import antigravity
import collector
import server


def _varint(value):
    result = bytearray()
    while value > 127:
        result.append((value & 127) | 128)
        value >>= 7
    result.append(value)
    return bytes(result)


def _number(field, value):
    return _varint(field << 3) + _varint(value)


def _bytes(field, value):
    if isinstance(value, str):
        value = value.encode("utf-8")
    return _varint((field << 3) | 2) + _varint(len(value)) + value


def _time(seconds, nanos=0):
    return _number(1, seconds) + _number(2, nanos)


def _generation(response="r1", model="gemini-3.1-pro-high", label="",
                system=100, inp=200, cached=300, output=40, thinking=10,
                total=True, seconds=1791936000, nanos=0):
    usage = (_number(1, system) + _number(2, inp) + _number(5, cached)
             + _number(9, output) + _number(10, thinking) + _bytes(11, response))
    if total:
        usage += _number(3, output + thinking)
    chat = _bytes(4, usage) + _bytes(19, model) + _bytes(21, label)
    if seconds is not None:
        chat += _bytes(9, _bytes(4, _time(seconds, nanos)))
    else:
        chat += _bytes(9, _number(2, (1 << 64) - 1) + _bytes(10, b"not a timestamp"))
    return _bytes(1, chat)


def _step(response, seconds, gen_index=None, nanos=0):
    meta = _bytes(1, _time(seconds, nanos)) + _bytes(9, _bytes(11, response))
    if gen_index is not None:
        meta += _bytes(20, _number(3, gen_index))
    return meta


def _db(path, rows, steps=None, workspace="file:///C:/code/my-project", conversation="session-1"):
    con = sqlite3.connect(str(path))
    try:
        con.executescript("""
            CREATE TABLE gen_metadata (idx INTEGER PRIMARY KEY, data BLOB, size INTEGER);
            CREATE TABLE trajectory_meta (trajectory_id TEXT);
            CREATE TABLE trajectory_metadata_blob (id TEXT, data BLOB);
        """)
        con.execute("INSERT INTO trajectory_meta VALUES (?)", (conversation,))
        meta = _bytes(2, _time(1700000000))  # Session creation must never date a turn.
        if workspace:
            meta += _bytes(1, _bytes(1, workspace))
        con.execute("INSERT INTO trajectory_metadata_blob VALUES ('main', ?)", (meta,))
        con.executemany("INSERT INTO gen_metadata VALUES (?, ?, 0)", rows)
        if steps is not None:
            con.execute("CREATE TABLE steps (idx INTEGER PRIMARY KEY, step_type INTEGER, metadata BLOB)")
            con.executemany("INSERT INTO steps VALUES (?, ?, ?)", steps)
        con.commit()
    finally:
        con.close()


class AntigravityParserTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "conversation #中文.db"
        self.addCleanup(antigravity._scan_issues.pop, str(self.path), None)

    def test_counts_system_input_cache_and_total_output_once(self):
        _db(self.path, [(0, _generation())])
        ev, = antigravity.read_events(self.path)
        self.assertEqual((ev["input"], ev["output"], ev["cached"]), (300, 50, 300))
        self.assertEqual(ev["model"], "gemini-3.1-pro-high")
        self.assertEqual(ev["project"], "my-project")
        self.assertEqual(ev["ts"], 1791936000)

    def test_missing_total_uses_output_plus_thinking(self):
        _db(self.path, [(0, _generation(total=False))])
        self.assertEqual(antigravity.read_events(self.path)[0]["output"], 50)

    def test_output_only_and_thinking_only_are_generations(self):
        _db(self.path, [(0, _generation(system=0, inp=0, cached=0, output=0)),
                        (1, _generation(response="r2", system=0, inp=0, cached=0, thinking=0))])
        self.assertEqual([e["output"] for e in antigravity.read_events(self.path)], [10, 40])

    def test_modern_time_joins_response_not_step_row_index(self):
        _db(self.path, [(0, _generation(seconds=None)),
                        (1, _generation(response="r2", seconds=None))], steps=[
            (0, 1, _step("", 1700000000)),
            (8, 15, _step("r1", 1791936000, nanos=500000000)),
            (12, 15, _step("r2", 1792022400)),
        ])
        self.assertEqual([e["ts"] for e in antigravity.read_events(self.path)],
                         [1791936000.5, 1792022400])

    def test_generation_index_join_handles_gaps_and_missing_response(self):
        _db(self.path, [(7, _generation(response="", seconds=None))], steps=[
            (35, 15, _step("", 1791936000, gen_index=7)),
        ])
        ev, = antigravity.read_events(self.path)
        self.assertEqual(ev["ts"], 1791936000)
        self.assertEqual(ev["_rid"], "generation:session-1:7")

    def test_explicit_generation_time_takes_priority(self):
        _db(self.path, [(0, _generation())], steps=[(8, 15, _step("r1", 1792022400))])
        self.assertEqual(antigravity.read_events(self.path)[0]["ts"], 1791936000)

    def test_missing_time_is_skipped_with_notice_not_dated_to_session(self):
        _db(self.path, [(0, _generation(seconds=None))])
        self.assertEqual(antigravity.read_events(self.path), [])
        self.assertIn("调用时间", antigravity.scan_notes([self.path])[0])

    def test_conflicting_response_time_is_not_guessed(self):
        _db(self.path, [(0, _generation(seconds=None))], steps=[
            (3, 15, _step("r1", 1791936000)), (4, 15, _step("r1", 1792022400)),
        ])
        self.assertEqual(antigravity.read_events(self.path), [])

    def test_bad_nanos_are_rejected(self):
        _db(self.path, [(0, _generation(nanos=1000000000))])
        self.assertEqual(antigravity.read_events(self.path), [])

    def test_bookkeeping_and_zero_usage_are_skipped(self):
        _db(self.path, [(0, _generation(model="", system=100, inp=0, cached=0, output=0, thinking=0)),
                        (1, _generation(system=0, inp=0, cached=0, output=0, thinking=0))])
        self.assertEqual(antigravity.read_events(self.path), [])

    def test_recorded_label_is_preserved_when_model_id_missing(self):
        _db(self.path, [(0, _generation(model="", label="Claude Opus 4.6 (Thinking)"))])
        self.assertEqual(antigravity.read_events(self.path)[0]["model"], "claude opus 4.6 (thinking)")

    def test_percent_encoded_workspace_and_missing_workspace(self):
        _db(self.path, [(0, _generation())], workspace="file:///C:/code/%E4%B8%AD%E6%96%87%20project/")
        self.assertEqual(antigravity.read_events(self.path)[0]["project"], "中文 project")
        con = sqlite3.connect(str(self.path))
        try:
            con.execute("DELETE FROM trajectory_metadata_blob")
            con.commit()
        finally:
            con.close()
        self.assertEqual(antigravity.read_events(self.path)[0]["project"], collector.UNKNOWN_PROJECT)

    def test_duplicates_within_db_are_collapsed(self):
        _db(self.path, [(0, _generation()), (1, _generation())])
        self.assertEqual(len(antigravity.read_events(self.path)), 1)

    def test_corrupt_overflow_oversized_rows_do_not_block_good_row(self):
        _db(self.path, [(0, b"\x0a\xff"),
                        (1, _generation(inp=(1 << 64) - 1)),
                        (2, b"x" * (antigravity.MAX_BLOB_BYTES + 1)),
                        (3, _generation())])
        self.assertEqual(len(antigravity.read_events(self.path)), 1)
        self.assertIn("3 条", antigravity.scan_notes([self.path])[0])

    def test_missing_corrupt_or_unsupported_database_does_not_crash(self):
        self.assertEqual(antigravity.read_events(self.path), [])
        self.assertFalse(self.path.exists())
        self.path.write_bytes(b"not sqlite")
        self.assertEqual(antigravity.read_events(self.path), [])
        self.path.unlink()
        con = sqlite3.connect(str(self.path))
        con.close()
        self.assertEqual(antigravity.read_events(self.path), [])
        self.assertIn("不受支持", antigravity.scan_notes([self.path])[0])

    def test_committed_wal_is_read_without_writing_database(self):
        _db(self.path, [])
        con = sqlite3.connect(str(self.path))
        try:
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("INSERT INTO gen_metadata VALUES (0, ?, 0)", (_generation(),))
            con.commit()
            before = self.path.read_bytes()
            self.assertEqual(len(antigravity.read_events(self.path)), 1)
            self.assertEqual(self.path.read_bytes(), before)
            con.execute("INSERT INTO gen_metadata VALUES (1, ?, 0)", (_generation(response="r2"),))
            self.assertEqual(len(antigravity.read_events(self.path)), 1)  # Uncommitted row excluded.
        finally:
            con.close()

    def test_custom_root_discovery(self):
        with mock.patch.dict(os.environ, {"GEMINI_CLI_HOME": self.tmp.name}):
            self.assertEqual(antigravity.patterns(),
                             [os.path.join(self.tmp.name, "antigravity*", "conversations", "*.db")])

    def test_wire_reader_rejects_truncation_overflow_and_bad_tags(self):
        for blob in (b"\x80", b"\x00", b"\x0a\x05a", b"\x0b", b"\x09x", b"\x0dxx",
                     b"\x08" + b"\xff" * 9 + b"\x02"):
            with self.subTest(blob=blob), self.assertRaises(ValueError):
                antigravity._fields(blob)
        self.assertEqual(antigravity._fields(b"\x09" + b"x" * 8 + _number(2, 10))[2], 10)


class AntigravityCollectionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "one.db"
        self.copy = Path(self.tmp.name) / "copy.db"
        self.data = str(Path(self.tmp.name) / "data")
        _db(self.path, [(0, _generation())])
        shutil.copyfile(str(self.path), str(self.copy))
        src = collector.Source("Antigravity", [str(self.path), str(self.copy)], "antigravity", "sqlite")
        patcher = mock.patch.object(collector, "SOURCES", [src])
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(antigravity._scan_issues.pop, str(self.path), None)
        self.addCleanup(antigravity._scan_issues.pop, str(self.copy), None)

    def test_collection_dedups_copies_and_restarts_then_collects_new_row(self):
        self.assertEqual(collector.collect_once(self.data)["Antigravity"]["events"], 1)
        self.assertEqual(collector.collect_once(self.data)["Antigravity"]["events"], 0)
        con = sqlite3.connect(str(self.path))
        try:
            con.execute("INSERT INTO gen_metadata VALUES (1, ?, 0)", (_generation(response="r2"),))
            con.commit()
        finally:
            con.close()
        self.assertEqual(collector.collect_once(self.data)["Antigravity"]["events"], 1)
        records = collector.load_auto_records(self.data, {}, lambda *args: 0)
        self.assertEqual(sum(r["requests"] for r in records), 2)
        self.assertEqual(sum(r["input"] + r["output"] + r["cached"] for r in records), 1300)
        self.assertTrue(all(r["project"] == "my-project" for r in records))

    def test_raw_timeline_and_window_summary_dedup_copies(self):
        rows = list(collector._scan_events(collector.SOURCES, 0))
        self.assertEqual(len(rows), 1)
        self.assertEqual(collector.usage_since("antigravity", 0)["requests"], 1)
        day = collector.datetime.fromtimestamp(rows[0][1]["ts"]).strftime("%Y-%m-%d")
        self.assertEqual(sum(r["requests"] for r in collector.breakdown(day)), 1)

    def test_fallback_identity_separates_conversations(self):
        self.copy.unlink()
        self.path.unlink()
        _db(self.path, [(0, _generation(response=""))], conversation="one")
        _db(self.copy, [(0, _generation(response=""))], conversation="two")
        self.assertEqual(collector.collect_once(self.data)["Antigravity"]["events"], 2)

    def test_registered_agent_and_scan_diagnostics(self):
        self.assertIn("Antigravity", server.KNOWN_AGENTS)
        self.assertEqual(collector.kind_for_agent("Antigravity"), "antigravity")
        self.path.unlink()
        _db(self.path, [(0, _generation(seconds=None))])
        collector.collect_once(self.data)
        self.assertTrue(any("Antigravity" in note and "调用时间" in note for note in collector.scan_notes()))


if __name__ == "__main__":
    unittest.main()
