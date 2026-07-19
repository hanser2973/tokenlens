# -*- coding: utf-8 -*-
"""HTTP 层安全与输入校验测试：Host/Origin 校验、静态目录防穿越、请求体上限、date 校验、原子存储。"""
import json
import os
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


if __name__ == "__main__":
    unittest.main()
