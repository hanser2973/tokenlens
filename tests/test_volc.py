# -*- coding: utf-8 -*-
"""火山方舟 Coding Plan：签名构造、响应解析、凭证来源与缓存。"""
import hashlib
import hmac
import json
import os
import time
import unittest
from unittest import mock

import volc

AK = "AKLTtestaccesskey0000"
SK = "testsecretkey0000=="
NOW = 1782000000  # 固定时间戳，签名必须确定


def _expected_signature(ak, sk, now):
    """用另一套独立写法重算签名，和 volc.sign_request 对账。"""
    query = "Action=GetCodingPlanUsage&Region=cn-beijing&Version=2024-01-01"
    payload_hash = hashlib.sha256(b"").hexdigest()
    ts = time.gmtime(now)
    x_date = time.strftime("%Y%m%dT%H%M%SZ", ts)
    short_date = time.strftime("%Y%m%d", ts)
    headers = ("host:open.volcengineapi.com\nx-date:{}\nx-content-sha256:{}\n"
               "content-type:application/json; charset=utf-8\n").format(x_date, payload_hash)
    canonical = "\n".join(["POST", "/", query, headers,
                           "host;x-date;x-content-sha256;content-type", payload_hash])
    scope = "{}/cn-beijing/ark/request".format(short_date)
    sts = "\n".join(["HMAC-SHA256", x_date, scope,
                     hashlib.sha256(canonical.encode()).hexdigest()])
    k = hmac.new(sk.encode(), short_date.encode(), hashlib.sha256).digest()
    k = hmac.new(k, b"cn-beijing", hashlib.sha256).digest()
    k = hmac.new(k, b"ark", hashlib.sha256).digest()
    k = hmac.new(k, b"request", hashlib.sha256).digest()
    return hmac.new(k, sts.encode(), hashlib.sha256).hexdigest()


class UriEncodeTest(unittest.TestCase):
    def test_unreserved_passthrough(self):
        self.assertEqual(volc._uri_encode("abcXYZ019-_.~"), "abcXYZ019-_.~")

    def test_reserved_encoded_uppercase(self):
        self.assertEqual(volc._uri_encode("a/b+c d"), "a%2Fb%2Bc%20d")

    def test_utf8_encoded(self):
        self.assertEqual(volc._uri_encode("中"), "%E4%B8%AD")


class SignTest(unittest.TestCase):
    def test_canonical_query_sorted(self):
        self.assertEqual(volc._canonical_query(),
                         "Action=GetCodingPlanUsage&Region=cn-beijing&Version=2024-01-01")

    def test_signature_matches_independent_recompute(self):
        url, headers = volc.sign_request(AK, SK, now=NOW)
        self.assertTrue(url.startswith("https://open.volcengineapi.com/?"))
        self.assertIn(volc._canonical_query(), url)
        self.assertEqual(headers["X-Date"], time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(NOW)))
        self.assertEqual(headers["X-Content-Sha256"], hashlib.sha256(b"").hexdigest())
        auth = headers["Authorization"]
        self.assertTrue(auth.startswith(
            "HMAC-SHA256 Credential={}/{}/cn-beijing/ark/request, "
            "SignedHeaders=host;x-date;x-content-sha256;content-type, Signature="
            .format(AK, time.strftime("%Y%m%d", time.gmtime(NOW)))))
        sig = auth.rsplit("Signature=", 1)[1]
        self.assertEqual(sig, _expected_signature(AK, SK, NOW))

    def test_deterministic_and_time_sensitive(self):
        _, h1 = volc.sign_request(AK, SK, now=NOW)
        _, h2 = volc.sign_request(AK, SK, now=NOW)
        _, h3 = volc.sign_request(AK, SK, now=NOW + 60)
        self.assertEqual(h1["Authorization"], h2["Authorization"])
        self.assertNotEqual(h1["Authorization"], h3["Authorization"])


class ParseWindowsTest(unittest.TestCase):
    def test_full_payload(self):
        result = {"QuotaUsage": [
            {"Level": "monthly", "Percent": 0.09, "ResetTime": 1800000000},
            {"Level": "session", "Percent": 1.38, "ResetTime": 1790000000},
            {"Level": "weekly", "Percent": 0.18, "ResetTime": 1795000000},
        ]}
        wins = volc.parse_windows(result)
        self.assertEqual([w["label"] for w in wins], ["当前会话", "每周窗口", "每月窗口"])
        self.assertEqual(wins[0]["used_percent"], 1.38)
        self.assertEqual(wins[0]["resets_at"], 1790000000)
        self.assertTrue(all(w["percent_only"] for w in wins))

    def test_non_positive_reset_is_none(self):
        wins = volc.parse_windows({"QuotaUsage": [
            {"Level": "session", "Percent": 0, "ResetTime": -1}]})
        self.assertIsNone(wins[0]["resets_at"])

    def test_millis_reset_and_string_percent(self):
        wins = volc.parse_windows({"QuotaUsage": [
            {"Level": "weekly", "Percent": "12.5", "ResetTime": 1795000000000}]})
        self.assertEqual(wins[0]["used_percent"], 12.5)
        self.assertEqual(wins[0]["resets_at"], 1795000000)

    def test_fallback_field_names(self):
        wins = volc.parse_windows({"Usages": [
            {"Type": "monthly", "UsedPercent": 33.3, "ResetTimestamp": 1800000000}]})
        self.assertEqual(len(wins), 1)
        self.assertEqual(wins[0]["label"], "每月窗口")

    def test_empty_means_not_subscribed(self):
        self.assertEqual(volc.parse_windows({}), [])
        self.assertEqual(volc.parse_windows({"QuotaUsage": []}), [])

    def test_unknown_level_skipped(self):
        wins = volc.parse_windows({"QuotaUsage": [
            {"Level": "daily", "Percent": 5}, {"Level": "weekly", "Percent": 6}]})
        self.assertEqual([w["label"] for w in wins], ["每周窗口"])

    def test_percent_clamped(self):
        wins = volc.parse_windows({"QuotaUsage": [{"Level": "session", "Percent": 120}]})
        self.assertEqual(wins[0]["used_percent"], 100.0)


class CredentialsTest(unittest.TestCase):
    def test_settings_first(self):
        with mock.patch.dict(os.environ, {"VOLCENGINE_ACCESS_KEY_ID": "envak",
                                          "VOLCENGINE_SECRET_ACCESS_KEY": "envsk"}):
            ak, sk, src = volc.resolve_credentials({"ark_ak": " fileak ", "ark_sk": " filesk "})
        self.assertEqual((ak, sk, src), ("fileak", "filesk", "界面配置"))

    def test_env_fallback(self):
        with mock.patch.dict(os.environ, {"VOLCENGINE_ACCESS_KEY_ID": "envak",
                                          "VOLCENGINE_SECRET_ACCESS_KEY": "envsk"}):
            ak, sk, src = volc.resolve_credentials({})
        self.assertEqual((ak, sk, src), ("envak", "envsk", "环境变量"))

    def test_none(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(volc.resolve_credentials({}), (None, None, None))
            self.assertEqual(volc.resolve_credentials({"ark_ak": "x"}), (None, None, None))


class _FakeResp:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class QueryTest(unittest.TestCase):
    def setUp(self):
        volc.reset_cache()
        self.addCleanup(volc.reset_cache)

    def _query(self, payload, settings=None):
        with mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch.object(volc.urllib.request, "urlopen",
                               return_value=_FakeResp(payload)) as m:
            card = volc.query_ark(settings or {"ark_ak": AK, "ark_sk": SK})
        return card, m

    def test_no_credentials_returns_guidance_without_network(self):
        with mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch.object(volc.urllib.request, "urlopen",
                               side_effect=AssertionError("不该发请求")):
            card = volc.query_ark({})
        self.assertIn("未配置", card["error"])

    def test_success_card(self):
        payload = {"ResponseMetadata": {}, "Result": {"QuotaUsage": [
            {"Level": "session", "Percent": 1.38, "ResetTime": 1790000000},
            {"Level": "weekly", "Percent": 0.18, "ResetTime": 1795000000},
        ]}}
        card, m = self._query(payload)
        self.assertNotIn("error", card)
        self.assertEqual(card["agent"], "火山方舟")
        self.assertEqual(card["plan_type"], "Coding Plan")
        self.assertEqual(len(card["windows"]), 2)
        req = m.call_args[0][0]
        self.assertEqual(req.get_method(), "POST")
        self.assertIn("Action=GetCodingPlanUsage", req.full_url)
        self.assertIn("Credential=", req.headers["Authorization"])

    def test_business_error_envelope(self):
        payload = {"ResponseMetadata": {"Error": {"Code": "InvalidAccessKeyId",
                                                  "Message": "bad key"}}}
        card, _ = self._query(payload)
        self.assertIn("拒绝", card["error"])
        self.assertIn("InvalidAccessKeyId", card["error"])

    def test_not_subscribed(self):
        card, _ = self._query({"ResponseMetadata": {}, "Result": {"QuotaUsage": []}})
        self.assertIn("未查询到生效的 Coding Plan 订阅", card["error"])

    def test_result_cached(self):
        payload = {"Result": {"QuotaUsage": [
            {"Level": "session", "Percent": 1, "ResetTime": 1790000000}]}}
        self._query(payload)
        # 第二次调用不再发请求（mock 已随 _query 退出而还原）
        card = volc.query_ark({"ark_ak": AK, "ark_sk": SK})
        self.assertEqual(card["windows"][0]["used_percent"], 1)
        # reset_cache 后会重新查询
        volc.reset_cache()
        card, _ = self._query({"Result": {"QuotaUsage": [
            {"Level": "session", "Percent": 2, "ResetTime": 1790000000}]}})
        self.assertEqual(card["windows"][0]["used_percent"], 2)


class ServerApiTest(unittest.TestCase):
    """AK/SK 的存取：写进 settings.json，但任何 API 响应都不得带出 SK 原文。"""

    @classmethod
    def setUpClass(cls):
        import tempfile
        import threading
        from http.server import ThreadingHTTPServer
        import server

        cls.tmp = tempfile.TemporaryDirectory()
        d = cls.tmp.name
        cls.saved = {k: getattr(server, k) for k in
                     ("DATA_DIR", "DATA_FILE", "PLANS_FILE", "CAL_FILE", "SETTINGS_FILE")}
        server.DATA_DIR = d
        server.DATA_FILE = os.path.join(d, "records.json")
        server.PLANS_FILE = os.path.join(d, "plans.json")
        server.CAL_FILE = os.path.join(d, "quota_cal.json")
        server.SETTINGS_FILE = os.path.join(d, "settings.json")
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        import server
        cls.httpd.shutdown()
        cls.httpd.server_close()
        for k, v in cls.saved.items():
            setattr(server, k, v)
        cls.tmp.cleanup()

    def setUp(self):
        import server
        if os.path.exists(server.SETTINGS_FILE):
            os.remove(server.SETTINGS_FILE)
        volc.reset_cache()

    def req(self, path, method="GET", body=None):
        import urllib.request
        r = urllib.request.Request("http://127.0.0.1:{}{}".format(self.port, path),
                                   method=method, data=body,
                                   headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode("utf-8"))

    def post_settings(self, payload):
        return self.req("/api/settings", method="POST",
                        body=json.dumps(payload).encode("utf-8"))

    def test_save_masks_and_never_leaks_sk(self):
        status, data = self.post_settings({"ark_ak": AK, "ark_sk": SK})
        self.assertEqual(status, 200)
        self.assertTrue(data["ark"]["configured"])
        self.assertTrue(data["ark"]["ak_masked"])
        self.assertNotIn(SK, json.dumps(data, ensure_ascii=False))
        # 落盘的是原文（本地凭证），GET 响应里仍然不能出现 SK
        import server
        on_disk = server.storage.load_json(server.SETTINGS_FILE, {})
        self.assertEqual(on_disk["ark_sk"], SK)
        status, data = self.req("/api/settings")
        self.assertNotIn(SK, json.dumps(data, ensure_ascii=False))

    def test_partial_update_merges_with_saved(self):
        self.post_settings({"ark_ak": AK, "ark_sk": SK})
        status, _ = self.post_settings({"ark_ak": "AKLTnew0000"})
        self.assertEqual(status, 200)
        import server
        on_disk = server.storage.load_json(server.SETTINGS_FILE, {})
        self.assertEqual(on_disk["ark_ak"], "AKLTnew0000")
        self.assertEqual(on_disk["ark_sk"], SK)

    def test_half_pair_without_saved_rejected(self):
        status, data = self.post_settings({"ark_ak": AK})
        self.assertEqual(status, 400)
        self.assertIn("同时", data["error"])

    def test_clear(self):
        self.post_settings({"ark_ak": AK, "ark_sk": SK})
        status, data = self.post_settings({"ark_clear": True})
        self.assertEqual(status, 200)
        self.assertFalse(data["ark"]["configured"])
        import server
        on_disk = server.storage.load_json(server.SETTINGS_FILE, {})
        self.assertEqual(on_disk.get("ark_ak"), "")
        self.assertEqual(on_disk.get("ark_sk"), "")

    def test_multi_device_save_preserves_ark_keys(self):
        self.post_settings({"ark_ak": AK, "ark_sk": SK})
        status, data = self.post_settings({"multi_device": "off"})
        self.assertEqual(status, 200)
        self.assertTrue(data["ark"]["configured"])

    def test_env_credentials_reported(self):
        with mock.patch.dict(os.environ, {"VOLCENGINE_ACCESS_KEY_ID": "envak",
                                          "VOLCENGINE_SECRET_ACCESS_KEY": "envsk"}):
            status, data = self.req("/api/settings")
        self.assertTrue(data["ark"]["configured"])
        self.assertEqual(data["ark"]["source"], "环境变量")


if __name__ == "__main__":
    unittest.main()