import json
import os
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime
from unittest import mock

import collector
import speed
import storage


def kimi_call(ts, output=100, stream_ms=2000, ttft_ms=1000, uid="one"):
    return [
        {"type": "llm.request", "model": "k3", "time": ts},
        {"type": "usage.record", "model": "kimi-code/k3", "time": ts + 3000,
         "usageScope": "turn", "usage": {"output": output}},
        {"type": "context.append_loop_event", "time": ts + 9000,
         "event": {"type": "step.end", "uuid": uid, "usage": {"output": output},
                   "llmStreamDurationMs": stream_ms, "llmFirstTokenLatencyMs": ttft_ms}},
    ]


class SpeedTest(unittest.TestCase):
    def test_stream_speed_excludes_wait_and_tools(self):
        s = speed.kimi_samples(map(json.dumps, kimi_call(10000)), "wd_demo_hash/wire.jsonl", {})[0]
        self.assertEqual(s["output"] / s["stream_seconds"], 50)
        self.assertAlmostEqual(s["output"] / s["request_seconds"], 100 / 3)
        self.assertEqual(s["ttft_seconds"], 1)
        self.assertEqual(s["project"], "demo")

    def test_cross_scan_state_and_partial_line_do_not_duplicate(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "wire.jsonl")
            events = kimi_call(time.time() * 1000)
            with open(path, "w", encoding="utf-8") as f:
                f.write(json.dumps(events[0]) + "\n" + json.dumps(events[1]) + "\n")
            src = collector.Source("Kimi Code", [path], "kimi", "jsonl")
            with mock.patch.object(collector, "SOURCES", [src]):
                speed.collect(d, force=True)
                self.assertEqual(storage.load_json(os.path.join(d, speed.SAMPLES_FILE), []), [])
                final = json.dumps(events[2])
                with open(path, "a", encoding="utf-8") as f:
                    f.write(final[:20])
                speed.collect(d, force=True)
                self.assertEqual(storage.load_json(os.path.join(d, speed.SAMPLES_FILE), []), [])
                with open(path, "a", encoding="utf-8") as f:
                    f.write(final[20:] + "\n")
                speed.collect(d, force=True)
                speed.collect(d, force=True)
                self.assertEqual(len(storage.load_json(os.path.join(d, speed.SAMPLES_FILE), [])), 1)

    def test_retry_uses_latest_request_start(self):
        lines = [{"type": "llm.request", "time": 1000}, *kimi_call(10000)]
        self.assertEqual(speed.kimi_samples(map(json.dumps, lines), "wire", {})[0]["request_seconds"], 3)

    def test_legacy_logs_only_report_request_speed(self):
        events = kimi_call(10000)
        events[2]["event"] = {"type": "step.end"}
        s = speed.kimi_samples(map(json.dumps, events), "wire", {})[0]
        self.assertEqual(s["request_seconds"], 3)
        self.assertIsNone(s["stream_seconds"])

    def test_invalid_and_tiny_timings_are_not_fake_stream_speeds(self):
        self.assertIsNone(speed.sample("a", 1, "a", "m", "p", 1, 10, -1))
        self.assertIsNone(speed.sample("a", 1, "a", "m", "p", 1, 10, float("nan")))
        self.assertIsNone(speed.sample("a", 1, "a", "m", "p", 1, 10, 1, .001)["stream_seconds"])

    def test_cancel_and_cumulative_records_are_not_counted(self):
        events = kimi_call(10000)
        events[1]["usageScope"] = "session"
        events[2]["event"].pop("usage")
        self.assertEqual(speed.kimi_samples(map(json.dumps, events), "wire", {}), [])
        events = kimi_call(10000)
        events[2]["event"]["finishReason"] = "aborted"
        self.assertEqual(speed.kimi_samples(map(json.dumps, events), "wire", {}), [])
        parse = collector.make_kimi_parser("wire")
        self.assertIsNone(parse(json.dumps({"type": "usage.record", "usageScope": "session", "time": 1000})))

    def test_weighted_average_filters_and_latest(self):
        with tempfile.TemporaryDirectory() as d:
            now = time.time()
            ss = [speed.sample("a", 1, "Kimi Code", "k3", "one", now - 1, 100, 3, 2, 1),
                  speed.sample("a", 2, "Kimi Code", "k3", "two", now, 900, 10, 9, 1)]
            storage.save_json(os.path.join(d, speed.SAMPLES_FILE), ss)
            with mock.patch.object(speed, "collect"):
                row = speed.report(d)["rows"][0]
                self.assertEqual(row["stream_tps"], 90.91)
                self.assertEqual(row["request_tps"], 76.92)
                self.assertEqual(row["latest_stream_tps"], 100)
                self.assertEqual(speed.report(d, project="one")["rows"][0]["stream_tps"], 50)
                self.assertEqual(speed.report(d, agent="Codex CLI")["rows"], [])
                self.assertEqual(speed.report(d, date_to="2000-01-01")["rows"], [])

    def test_opencode_and_zcode_request_timings(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "message.json")
            storage.save_json(path, {"role": "assistant", "modelID": "m", "time": {"created": 1000, "completed": 3000},
                                     "tokens": {"output": 90, "reasoning": 10}, "path": {"cwd": "C:/code/demo"}})
            s = speed.opencode_samples(path)[0]
            self.assertEqual(s["output"] / s["request_seconds"], 50)
            self.assertIsNone(s["stream_seconds"])
            failed = storage.load_json(path, {})
            failed["error"] = {"name": "MessageAbortedError"}
            storage.save_json(path, failed)
            self.assertEqual(speed.opencode_samples(path), [])
            db = os.path.join(d, "db.sqlite")
            with sqlite3.connect(db) as con:
                con.execute("CREATE TABLE session (id TEXT, directory TEXT)")
                con.execute("INSERT INTO session VALUES ('s', 'C:/code/demo')")
                con.execute("CREATE TABLE model_usage (logical_request_id TEXT, model_id TEXT, started_at INTEGER, completed_at INTEGER, output_tokens INTEGER, status TEXT, session_id TEXT)")
                con.execute("INSERT INTO model_usage VALUES ('r', 'm', 1000, 3000, 100, 'completed', 's')")
                con.execute("INSERT INTO model_usage VALUES ('failed', 'm', 1000, 3000, 100, 'failed', 's')")
            con.close()
            ss = speed.zcode_samples(db)
            self.assertEqual(len(ss), 1)
            self.assertEqual(ss[0]["output"] / ss[0]["request_seconds"], 50)


if __name__ == "__main__":
    unittest.main()
