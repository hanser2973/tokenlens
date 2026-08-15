# -*- coding: utf-8 -*-
"""DeepSeek Harness 采集源：zstd 帧边界、用量解析、项目名、增量不重复计数。

dsh 的会话日志是「多个独立 zstd 帧首尾拼接」，帧边界即安全的增量偏移。这里造帧
不依赖任何压缩库 —— 手工拼一个只含 raw block 的合法帧就够钉住边界扫描的逻辑；
真正的解压走 zstdio 的后端，本机没装解压库时相关用例自动跳过。
"""
import json
import os
import tempfile
import unittest
from unittest import mock

import collector
import zstdio


def zframe(payload):
    """手工拼一个只含 raw block 的合法 zstd 帧（无校验和、无字典、不带解压后大小）。"""
    head = b"\x28\xb5\x2f\xfd\x00\x40"          # 魔数 + 描述符 0 + 窗口描述符
    block = ((len(payload) << 3) | 1).to_bytes(3, "little")  # last=1, type=0(raw)
    return head + block + payload


def usage_line(ts_ms=1786631676602, model="deepseek-v4-pro", inp=100, out=50,
               cache_r=20, cache_w=10, turn=1, step=1):
    return json.dumps({
        "type": "assistant/message", "seq": 483, "time": ts_ms,
        "data": {"turn": turn, "step": step,
                 "message": {"role": "assistant", "content": [],
                             "source": {"kind": "model", "provider": "deepseek", "model": model}},
                 "usage": {"inputTokens": inp, "outputTokens": out,
                           "cacheReadTokens": cache_r, "cacheWriteTokens": cache_w}}},
    ) + "\n"


def header_line(cwd="C:\\code\\pythoncode\\gpt-web-use"):
    return json.dumps({"type": "session", "version": 0, "id": "session-x",
                       "createdAt": 1786698708970, "cwd": cwd, "delegationDepth": 0}) + "\n"


class FrameBoundaryTest(unittest.TestCase):
    """帧边界只看帧头里的长度，不解压 —— 缺解压库时这部分逻辑照样要对。"""

    def test_prefix_covers_whole_complete_file(self):
        buf = zframe(b"a\n") + zframe(b"bb\n")
        self.assertEqual(zstdio.complete_prefix(buf), len(buf))
        self.assertEqual(len(zstdio.frames(buf)), 2)

    def test_partial_tail_frame_is_not_consumed(self):
        """尾帧还在写：只能推进到上一帧结束，否则下轮从帧中间读起，整段全丢。"""
        whole = zframe(b"a\n")
        buf = whole + zframe(b"bb\n")[:5]
        self.assertEqual(zstdio.complete_prefix(buf), len(whole))

    def test_garbage_stops_scan_instead_of_raising(self):
        self.assertEqual(zstdio.complete_prefix(b"not a zstd file at all"), 0)

    def test_skippable_frame_is_stepped_over(self):
        skippable = b"\x50\x2a\x4d\x18" + (4).to_bytes(4, "little") + b"xxxx"
        buf = skippable + zframe(b"a\n")
        self.assertEqual(zstdio.complete_prefix(buf), len(buf))


@unittest.skipUnless(zstdio.available(), "本机没有 zstd 解压库")
class DecodeTest(unittest.TestCase):
    def test_decodes_concatenated_frames_in_order(self):
        buf = zframe(b"one\n") + zframe(b"two\n")
        self.assertEqual(zstdio.decode(buf), b"one\ntwo\n")

    def test_decode_first_stops_at_one_frame(self):
        self.assertEqual(zstdio.decode_first(zframe(b"one\n") + zframe(b"two\n")), b"one\n")


class DshParserTest(unittest.TestCase):
    def test_parses_assistant_message_usage(self):
        ev = collector.make_dsh_parser("/x/session.jsonl")(usage_line())
        self.assertEqual(ev["input"], 110)     # inputTokens + cacheWriteTokens
        self.assertEqual(ev["output"], 50)
        self.assertEqual(ev["cached"], 20)
        self.assertEqual(ev["model"], "deepseek-v4-pro")

    def test_ignores_usage_chunk_to_avoid_double_count(self):
        """同一次调用 assistant/chunk 也记一遍 usage；两边都收就是翻倍。"""
        chunk = json.dumps({"type": "assistant/chunk", "seq": 481, "time": 1786631676599,
                            "data": {"turn": 1, "step": 1,
                                     "chunk": {"type": "usage",
                                               "usage": {"inputTokens": 1208, "outputTokens": 536,
                                                         "cacheReadTokens": 6912}}}})
        self.assertIsNone(collector.make_dsh_parser("/x/session.jsonl")(chunk))

    def test_skips_events_without_usage(self):
        parse = collector.make_dsh_parser("/x/session.jsonl")
        self.assertIsNone(parse(json.dumps({"type": "tool/call", "time": 1786631676599, "data": {}})))
        self.assertIsNone(parse(usage_line(inp=0, out=0, cache_r=0, cache_w=0)))

    def test_unknown_model_when_source_missing(self):
        line = json.dumps({"type": "assistant/message", "time": 1786631676602,
                           "data": {"usage": {"inputTokens": 5, "outputTokens": 1}}})
        self.assertEqual(collector.make_dsh_parser("/x/session.jsonl")(line)["model"], "unknown")


class DshProjectTest(unittest.TestCase):
    """项目名优先读会话头里的 cwd：目录名是把分隔符换成 '-' 压出来的，
    项目名自带连字符时（gpt-web-use）拆不回来。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = os.path.join(self.tmp.name, "--C-code-pythoncode-gpt-web-use--", "session-1")
        os.makedirs(self.dir)

    def test_reads_cwd_from_raw_header(self):
        path = os.path.join(self.dir, "session.jsonl")
        with open(path, "wb") as f:
            f.write((header_line() + usage_line()).encode("utf-8"))
        self.assertEqual(collector._dsh_project(path), "gpt-web-use")

    @unittest.skipUnless(zstdio.available(), "本机没有 zstd 解压库")
    def test_reads_cwd_from_compressed_header_frame(self):
        path = os.path.join(self.dir, "session.jsonl.zstd")
        with open(path, "wb") as f:
            f.write(zframe(header_line().encode("utf-8")) + zframe(usage_line().encode("utf-8")))
        self.assertEqual(collector._dsh_project(path), "gpt-web-use")

    def test_falls_back_to_directory_name_without_header(self):
        path = os.path.join(self.dir, "session.jsonl")
        with open(path, "wb") as f:
            f.write(usage_line().encode("utf-8"))   # 头帧已被增量读跳过 / 文件没有头
        self.assertEqual(collector._dsh_project(path), "use")  # 目录名拆不回连字符

    def test_sessions_without_cwd_are_unknown_project(self):
        d = os.path.join(self.tmp.name, "_no-cwd_", "session-2")
        os.makedirs(d)
        path = os.path.join(d, "session.jsonl")
        with open(path, "wb") as f:
            f.write(usage_line().encode("utf-8"))
        self.assertEqual(collector._dsh_project(path), collector.UNKNOWN_PROJECT)


class DshIncrementalTest(unittest.TestCase):
    """追加写 + 按帧偏移增量读：重扫不重复计数，新增的帧要收进来。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.dir = os.path.join(self.tmp, "sessions", "--C-code-my--", "session-1")
        os.makedirs(self.dir)
        self.path = os.path.join(self.dir, "session.jsonl.zstd")
        src = collector.Source("DeepSeek Harness",
                               [os.path.join(self.tmp, "sessions", "*", "*", "session.jsonl.zstd")],
                               "dsh", "zstd")
        p = mock.patch.object(collector, "SOURCES", [src])
        p.start()
        self.addCleanup(p.stop)

    def _append(self, data):
        with open(self.path, "ab") as f:
            f.write(data)

    def _total(self):
        auto = collector._load_json(os.path.join(self.tmp, collector.AUTO_FILE), {})
        return sum(v["input"] + v["output"] + v["cached"] for v in auto.values())

    @unittest.skipUnless(zstdio.available(), "本机没有 zstd 解压库")
    def test_appended_frame_counted_once(self):
        self._append(zframe(header_line("C:\\code\\my").encode("utf-8")))
        self._append(zframe(usage_line().encode("utf-8")))
        self.assertEqual(collector.collect_once(self.tmp)["DeepSeek Harness"]["events"], 1)
        self.assertEqual(self._total(), 180)
        self.assertEqual(collector.collect_once(self.tmp)["DeepSeek Harness"]["events"], 0)
        self._append(zframe(usage_line(ts_ms=1786631700000, inp=1, out=2,
                                       cache_r=3, cache_w=0).encode("utf-8")))
        self.assertEqual(collector.collect_once(self.tmp)["DeepSeek Harness"]["events"], 1)
        self.assertEqual(self._total(), 186)

    @unittest.skipUnless(zstdio.available(), "本机没有 zstd 解压库")
    def test_half_written_frame_is_picked_up_next_round(self):
        """半个帧不能推进偏移，否则它写完后永远没人读。"""
        self._append(zframe(header_line("C:\\code\\my").encode("utf-8")))
        full = zframe(usage_line().encode("utf-8"))
        self._append(full[:6])
        self.assertEqual(collector.collect_once(self.tmp)["DeepSeek Harness"]["events"], 0)
        self._append(full[6:])
        self.assertEqual(collector.collect_once(self.tmp)["DeepSeek Harness"]["events"], 1)
        self.assertEqual(self._total(), 180)

    def test_without_zstd_backend_source_is_skipped_not_consumed(self):
        """缺解压库时必须整源跳过：若照常推进偏移，装上库之后这些帧再也读不到了。"""
        self._append(zframe(header_line("C:\\code\\my").encode("utf-8"))
                     + zframe(usage_line().encode("utf-8")))
        with mock.patch.object(zstdio, "_decompress", None):
            self.assertNotIn("DeepSeek Harness", collector.collect_once(self.tmp))
            self.assertEqual(self._total(), 0)
            self.assertTrue(any("zstandard" in n for n in collector.scan_notes()))
        state = collector._load_json(os.path.join(self.tmp, collector.STATE_FILE), {})
        self.assertEqual(state.get("offsets", {}), {})


if __name__ == "__main__":
    unittest.main()
