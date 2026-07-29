# -*- coding: utf-8 -*-
"""HTTP 层安全与输入校验测试：Host/Origin 校验、静态目录防穿越、请求体上限、date 校验、原子存储。"""
import base64
import json
import os
import socket
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock

import server
import storage


class StorageTest(unittest.TestCase):
    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "sub", "x.json")
            storage.save_json(p, {"a": 1}, indent=1)
            self.assertEqual(storage.load_json(p, {}), {"a": 1})

    def test_missing_returns_default(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(storage.load_json(os.path.join(d, "nope.json"), {"x": 2}), {"x": 2})

    def test_corrupt_backed_up(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "x.json")
            with open(p, "w", encoding="utf-8") as f:
                f.write("{broken")
            self.assertEqual(storage.load_json(p, []), [])
            self.assertFalse(os.path.exists(p))
            self.assertEqual(len([f for f in os.listdir(d) if ".corrupt-" in f]), 1)


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        # 数据文件重定向到临时目录，避免测试碰真实数据
        cls.saved = {k: getattr(server, k) for k in ("DATA_DIR", "DATA_FILE", "PLANS_FILE", "CAL_FILE")}
        server.DATA_DIR = cls.tmp.name
        server.DATA_FILE = os.path.join(cls.tmp.name, "records.json")
        server.PLANS_FILE = os.path.join(cls.tmp.name, "plans.json")
        server.CAL_FILE = os.path.join(cls.tmp.name, "quota_cal.json")
        cls.patcher = mock.patch.object(server, "maybe_auto_match", lambda *a, **k: None)
        cls.patcher.start()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.patcher.stop()
        for k, v in cls.saved.items():
            setattr(server, k, v)
        cls.tmp.cleanup()

    def req(self, path, method="GET", headers=None, body=None):
        r = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}",
                                   method=method, headers=headers or {}, data=body)
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode("utf-8"))

    def test_get_ok(self):
        status, data = self.req("/api/records")
        self.assertEqual(status, 200)
        self.assertIn("records", data)

    def test_foreign_host_rejected(self):
        status, _ = self.req("/api/records", headers={"Host": "evil.com"})
        self.assertEqual(status, 403)

    def test_foreign_origin_post_rejected(self):
        body = json.dumps({"date": "2026-07-01", "agent": "a", "model": "m"}).encode()
        status, _ = self.req("/api/records", method="POST", body=body,
                             headers={"Origin": "https://evil.com",
                                      "Content-Type": "application/json"})
        self.assertEqual(status, 403)

    def test_static_traversal_blocked(self):
        status, _ = self.req("/static/../server.py")
        self.assertEqual(status, 403)

    def test_body_too_large(self):
        status, _ = self.req("/api/prices", method="POST", body=b" " * (server.MAX_BODY + 1),
                             headers={"Content-Type": "application/json"})
        self.assertEqual(status, 413)

    def test_bad_date_rejected(self):
        body = json.dumps({"date": "not-a-date", "agent": "a", "model": "m"}).encode()
        status, _ = self.req("/api/records", method="POST", body=body,
                             headers={"Content-Type": "application/json"})
        self.assertEqual(status, 400)
        self.assertEqual(server.load_records(), [])

    def test_post_record_ok(self):
        body = json.dumps({"date": "2026-07-01", "agent": "a", "model": "m",
                           "input": 10, "output": 5}).encode()
        status, data = self.req("/api/records", method="POST", body=body,
                                headers={"Content-Type": "application/json"})
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        recs = server.load_records()
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["source"], "manual")

    # ---------- 背景壁纸 ----------
    # 端点直接落盘二进制，必须挡住非图片内容，别把 data/ 变成任意写入口

    def raw(self, path, method="GET", headers=None, body=None):
        """壁纸 GET 返回的是图片，不能按 JSON 解析，单独走一个原始版本。"""
        r = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}",
                                   method=method, headers=headers or {}, data=body)
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                return resp.status, resp.headers.get("Content-Type"), resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers.get("Content-Type"), e.read()

    @staticmethod
    def _jpeg():
        return b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"\x00" * 32 + b"\xff\xd9"

    def _put_wallpaper(self, raw_bytes):
        body = json.dumps({"data": base64.b64encode(raw_bytes).decode()}).encode()
        return self.req("/api/wallpaper", method="POST", body=body,
                        headers={"Content-Type": "application/json"})

    def test_wallpaper_roundtrip(self):
        self.addCleanup(lambda: self.req("/api/wallpaper", method="DELETE"))
        status, data = self._put_wallpaper(self._jpeg())
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertTrue(os.path.exists(server.wallpaper_path()))

        status, ctype, body = self.raw("/api/wallpaper")
        self.assertEqual(status, 200)
        self.assertEqual(ctype, "image/jpeg")
        self.assertEqual(body, self._jpeg())

        status, _ = self.req("/api/wallpaper", method="DELETE")
        self.assertEqual(status, 200)
        self.assertFalse(os.path.exists(server.wallpaper_path()))
        self.assertEqual(self.raw("/api/wallpaper")[0], 404)

    def test_wallpaper_rejects_non_jpeg(self):
        status, data = self._put_wallpaper(b"<?php system($_GET[0]); ?>")
        self.assertEqual(status, 400)
        self.assertIn("JPEG", data["error"])
        self.assertFalse(os.path.exists(server.wallpaper_path()))

    def test_wallpaper_rejects_bad_base64(self):
        body = json.dumps({"data": "not base64!!"}).encode()
        status, _ = self.req("/api/wallpaper", method="POST", body=body,
                             headers={"Content-Type": "application/json"})
        self.assertEqual(status, 400)

    def test_wallpaper_delete_is_idempotent(self):
        self.assertEqual(self.req("/api/wallpaper", method="DELETE")[0], 200)

    def test_wallpaper_foreign_origin_rejected(self):
        body = json.dumps({"data": base64.b64encode(self._jpeg()).decode()}).encode()
        status, _ = self.req("/api/wallpaper", method="POST", body=body,
                             headers={"Origin": "https://evil.com",
                                      "Content-Type": "application/json"})
        self.assertEqual(status, 403)

    # ---------- 单实例检测 ----------
    # 双开会让两个进程共用同一份 collector_state.json，交错读改写 offsets 导致漏扫/重复计数

    def test_ping_identifies_app(self):
        status, data = self.req("/api/ping")
        self.assertEqual(status, 200)
        self.assertEqual(data["app"], server.APP_ID)

    def test_probe_finds_running_instance(self):
        self.assertIsNotNone(server.probe_instance(self.port))

    def test_probe_ignores_closed_port(self):
        self.assertIsNone(server.probe_instance(_closed_port(), timeout=0.5))

    def test_running_instance_reads_recorded_port(self):
        """上次没抢到默认端口时，靠 instance.json 记的端口找到自己。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "instance.json")
            storage.save_json(path, {"port": self.port, "pid": 1})
            with mock.patch.object(server, "INSTANCE_FILE", path), \
                 mock.patch.object(server, "DEFAULT_PORT", _closed_port()):
                self.assertEqual(server.running_instance(), {"port": self.port})

    def test_running_instance_none_when_nothing_listening(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(server, "INSTANCE_FILE",
                                   os.path.join(tmp, "instance.json")), \
                 mock.patch.object(server, "DEFAULT_PORT", _closed_port()), \
                 mock.patch.object(server, "probe_instance", return_value=None):
                self.assertIsNone(server.running_instance())


def _closed_port():
    """绑一个端口再放掉，拿到一个几乎肯定没人监听的号。"""
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


if __name__ == "__main__":
    unittest.main()
