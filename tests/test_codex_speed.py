import json
import os
import tempfile
import time
import unittest
from unittest import mock

import collector
import speed
import storage


def event(sub, **payload):
    return {"type": "event_msg", "payload": {"type": sub, **payload}}


def task(ts=100, output=1000, duration=30000, turn="t1", model="gpt-6.1-sol"):
    return [
        {"type": "session_meta", "payload": {"cwd": "C:/code/demo"}},
        event("task_started", turn_id=turn, started_at=ts),
        {"type": "turn_context", "payload": {"turn_id": turn, "model": model, "cwd": "C:/code/demo"}},
        {"type": "token_usage_record", "payload": {"turn_id": turn, "response_id": "r1",
            "usage": {"output_tokens": output, "reasoning_output_tokens": 700},
            "turn_token_usage": {"output_tokens": output}, "thread_token_usage": {"output_tokens": 999999}}},
        event("task_complete", turn_id=turn, started_at=ts, completed_at=ts + duration / 1000,
              duration_ms=duration, time_to_first_token_ms=2000),
    ]


def parse(events, state=None):
    return speed.codex_samples(map(json.dumps, events), "rollout.jsonl", state if state is not None else {})


class CodexSpeedTest(unittest.TestCase):
    def test_completed_task_has_task_throughput_not_generation_or_request_speed(self):
        s = parse(task())[0]
        self.assertEqual(s["basis"], "task")
        self.assertEqual(s["output"], 1000)
        self.assertEqual(s["task_seconds"], 30)
        self.assertAlmostEqual(s["output"] / s["task_seconds"], 33.333333, places=5)
        self.assertIsNone(s["request_seconds"])
        self.assertIsNone(s["stream_seconds"])
        self.assertEqual(s["ttft_seconds"], 2)
        self.assertEqual(s["model"], "gpt-6.1-sol")
        self.assertEqual(s["project"], "demo")

    def test_cumulative_usage_replaces_and_legacy_snapshots_are_ignored(self):
        events = task()
        first = events[3]
        first["payload"]["turn_token_usage"]["output_tokens"] = 100
        second = {"type": "token_usage_record", "payload": {"turn_id": "t1",
                  "turn_token_usage": {"output_tokens": 1000}}}
        legacy = event("token_count", info={"last_token_usage": {"output_tokens": 1000}})
        ss = parse(events[:4] + [legacy, legacy, second, second, legacy, events[-1], events[-1]])
        self.assertEqual(len(ss), 1)
        self.assertEqual(ss[0]["output"], 1000)

    def test_failed_cancelled_incomplete_zero_and_invalid_tasks_are_skipped(self):
        events = task()
        events[-1]["payload"]["error"] = {"message": "failed"}
        self.assertEqual(parse(events), [])
        events[-1] = event("turn_aborted", turn_id="t1", duration_ms=30000)
        self.assertEqual(parse(events), [])
        self.assertEqual(parse(task()[:-1]), [])
        self.assertEqual(parse(task(output=0)), [])
        events = task()
        events[-1]["payload"]["duration_ms"] = -1
        self.assertEqual(parse(events), [])
        events = task()
        events[3]["payload"]["turn_token_usage"]["output_tokens"] = "NaN"
        self.assertEqual(parse(events), [])

    def test_no_task_level_usage_does_not_guess_from_thread_or_last_request(self):
        events = task()
        events[3]["payload"].pop("turn_token_usage")
        events.insert(4, event("token_count", info={"last_token_usage": {"output_tokens": 1000}}))
        self.assertEqual(parse(events), [])

    def test_duration_falls_back_to_explicit_start_and_end(self):
        events = task()
        events[-1]["payload"].pop("duration_ms")
        self.assertEqual(parse(events)[0]["task_seconds"], 30)

    def test_missing_start_or_end_never_uses_another_tasks_clock(self):
        events = task()
        events[-1]["payload"].pop("duration_ms")
        events[-1]["payload"].pop("completed_at")
        self.assertEqual(parse(events), [])

    def test_multimodel_task_is_one_sample_with_no_false_model_attribution(self):
        events = task()
        context = {"type": "turn_context", "payload": {"turn_id": "t1", "model": "gpt-6-astra", "cwd": "C:/code/other"}}
        record = {"type": "token_usage_record", "payload": {"turn_id": "t1", "turn_token_usage": {"output_tokens": 1500}}}
        ss = parse(events[:4] + [context, record, events[-1]])
        self.assertEqual(len(ss), 1)
        self.assertEqual(ss[0]["model"], "多模型任务")
        self.assertEqual(ss[0]["project"], "多项目任务")
        self.assertEqual(ss[0]["output"], 1500)

    def test_interleaved_turns_keep_separate_usage_models_and_times(self):
        a, b = task(turn="a"), task(ts=200, output=500, turn="b", model="gpt-6-astra")
        ss = parse(a[:-1] + b[:-1] + [a[-1], b[-1]])
        self.assertEqual([(s["model"], s["output"]) for s in ss], [("gpt-6.1-sol", 1000), ("gpt-6-astra", 500)])

    def test_invalid_decreasing_cumulative_counter_is_skipped(self):
        events = task()
        decreasing = {"type": "token_usage_record", "payload": {"turn_id": "t1", "turn_token_usage": {"output_tokens": 100}}}
        self.assertEqual(parse(events[:4] + [decreasing, events[-1]]), [])

    def test_cross_scan_partial_line_and_restart_preserve_pending_turn(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "rollout.jsonl")
            events = task(ts=int(time.time()) - 30)
            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join(map(json.dumps, events[:-1])) + "\n")
            src = collector.Source("Codex CLI", [path], "codex", "jsonl")
            with mock.patch.object(collector, "SOURCES", [src]):
                speed.collect(d, force=True)
                self.assertEqual(storage.load_json(os.path.join(d, speed.SAMPLES_FILE), []), [])
                final = json.dumps(events[-1])
                with open(path, "a", encoding="utf-8") as f:
                    f.write(final[:25])
                speed.collect(d, force=True)
                self.assertEqual(storage.load_json(os.path.join(d, speed.SAMPLES_FILE), []), [])
                with open(path, "a", encoding="utf-8") as f:
                    f.write(final[25:] + "\n")
                speed.collect(d, force=True)
                speed.collect(d, force=True)
                samples = storage.load_json(os.path.join(d, speed.SAMPLES_FILE), [])
                self.assertEqual(len(samples), 1)
                self.assertEqual(samples[0]["task_seconds"], 30)

    def test_report_separates_weighted_task_speed_and_filters(self):
        with tempfile.TemporaryDirectory() as d:
            now = time.time()
            ss = [speed.sample("a", 1, "Codex CLI", "gpt-6.1-sol", "one", now - 1, 100, 10,
                               ttft_seconds=1, basis="task"),
                  speed.sample("a", 2, "Codex CLI", "gpt-6.1-sol", "two", now, 900, 20,
                               ttft_seconds=3, basis="task")]
            storage.save_json(os.path.join(d, speed.SAMPLES_FILE), ss)
            with mock.patch.object(speed, "collect"):
                row = speed.report(d)["rows"][0]
                self.assertEqual(row["task_tps"], 33.33)
                self.assertEqual(row["latest_task_tps"], 45)
                self.assertEqual(row["task_samples"], 2)
                self.assertEqual(row["measurement_basis"], "task")
                self.assertIsNone(row["request_tps"])
                self.assertIsNone(row["stream_tps"])
                self.assertEqual(row["ttft_seconds"], 2)
                self.assertEqual(speed.report(d, project="one")["rows"][0]["task_tps"], 10)
                self.assertEqual(speed.report(d, model="gpt-6-astra")["rows"], [])

    def test_replayed_task_in_another_rollout_is_not_counted_twice(self):
        with tempfile.TemporaryDirectory() as d:
            for name in ("original.jsonl", "forked.jsonl"):
                with open(os.path.join(d, name), "w", encoding="utf-8") as f:
                    f.write("\n".join(map(json.dumps, task(ts=int(time.time()) - 30))) + "\n")
            src = collector.Source("Codex CLI", [os.path.join(d, "*.jsonl")], "codex", "jsonl")
            with mock.patch.object(collector, "SOURCES", [src]):
                speed.collect(d, force=True)
                self.assertEqual(len(storage.load_json(os.path.join(d, speed.SAMPLES_FILE), [])), 1)


if __name__ == "__main__":
    unittest.main()
