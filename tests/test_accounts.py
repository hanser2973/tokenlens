# -*- coding: utf-8 -*-
"""账号唯一 id：识别、导出哈希、按账号过滤对端。

设备 id 是每台机器随机生成的，换设备就换一个，认不出「这两台是同一个账号」。
官方配额百分比却是账号级的 —— 分子混进别的账号的用量，估算和真实容量都会偏。
"""
import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from unittest import mock

import collector
import identity
import server
import storage
import sync


def _minute_key(dt, agent="Kimi Code", model="k2p7"):
    return f"{dt.strftime('%Y-%m-%dT%H:%M')}|{agent}|{model}"


def _slot(n):
    return {"input": n, "output": n, "cached": n, "requests": 1}


def _write(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f)


class LocalAccountTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.claude = os.path.join(self.tmp.name, "claude.json")
        self.codex = os.path.join(self.tmp.name, "auth.json")
        for p in (mock.patch.object(identity, "CLAUDE_CONFIG", self.claude),
                  mock.patch.object(identity, "CODEX_AUTH", self.codex)):
            p.start()
            self.addCleanup(p.stop)

    def test_hash_is_stable_and_short(self):
        h = identity.account_hash("user-123")
        self.assertEqual(h, identity.account_hash("user-123"))
        self.assertEqual(len(h), 16)
        self.assertNotIn("user-123", h)  # 反推不回原文

    def test_no_credentials_means_unknown_not_empty_string(self):
        """读不到就不要出现在结果里：「不知道」和「不一样」必须分得开。"""
        self.assertEqual(identity.local_accounts(self.tmp.name), {})

    def test_claude_prefers_oauth_uuid(self):
        _write(self.claude, {"userID": "local-anon",
                             "oauthAccount": {"accountUuid": "uuid-1", "emailAddress": "me@x.com"}})
        acc = identity.claude_account()
        self.assertEqual(acc["id"], "uuid-1")
        self.assertEqual(acc["label"], "me@x.com")
        self.assertFalse(acc["weak"])

    def test_claude_falls_back_to_local_user_id(self):
        """没登录 OAuth 时只有本地匿名 id —— 它不跨设备一致，要标成 weak。"""
        _write(self.claude, {"userID": "local-anon"})
        acc = identity.claude_account()
        self.assertEqual(acc["id"], "local-anon")
        self.assertTrue(acc["weak"])

    def test_codex_account_id(self):
        _write(self.codex, {"tokens": {"account_id": "acc-9", "access_token": "secret"}})
        self.assertEqual(identity.codex_account()["id"], "acc-9")

    def test_codex_api_key_mode_never_stores_the_key(self):
        _write(self.codex, {"auth_mode": "apikey", "OPENAI_API_KEY": "sk-super-secret"})
        acc = identity.codex_account()
        self.assertTrue(acc["id"].startswith("apikey:"))
        self.assertNotIn("sk-super-secret", acc["id"])

    def test_broken_credential_file_is_not_fatal(self):
        with open(self.claude, "w", encoding="utf-8") as f:
            f.write("{not json")
        self.assertIsNone(identity.claude_account())

    def test_kimi_id_is_remembered_across_restarts(self):
        """Kimi 的 userId 只在配额接口里出现，离线时要能从缓存里拿回来。"""
        identity.remember(self.tmp.name, "kimi", "kimi-user-1", label="")
        accs = identity.local_accounts(self.tmp.name)
        self.assertEqual(accs["kimi"]["id"], "kimi-user-1")
        self.assertEqual(accs["kimi"]["agent"], "Kimi Code")
        self.assertEqual(identity.local_hashes(self.tmp.name)["kimi"],
                         identity.account_hash("kimi-user-1"))

    def test_remember_skips_write_when_unchanged(self):
        identity.remember(self.tmp.name, "kimi", "kimi-user-1")
        path = os.path.join(self.tmp.name, identity.CACHE_FILE)
        before = os.stat(path).st_mtime_ns
        identity.remember(self.tmp.name, "kimi", "kimi-user-1")
        self.assertEqual(os.stat(path).st_mtime_ns, before)

    def test_mask_keeps_domain_only(self):
        self.assertEqual(identity.mask("someone@example.com"), "so***@example.com")
        self.assertEqual(identity.mask("0123456789abcdef"), "0123…cdef")


class PeerAccountFilterTest(unittest.TestCase):
    def _peer(self, name, accounts, minutes=None):
        return {"device_id": name, "device_name": name, "accounts": accounts,
                "minutes": minutes or {}}

    def test_same_account_kept_other_dropped_unknown_kept(self):
        peers = [self._peer("台式机", {"kimi": "AAA"}),
                 self._peer("同事机", {"kimi": "BBB"}),
                 self._peer("老版本", {})]
        kept, unverified, other = sync.peers_for_kind(peers, "kimi", "AAA")
        self.assertEqual([p["device_name"] for p in kept], ["台式机", "老版本"])
        self.assertEqual(unverified, ["老版本"])
        self.assertEqual(other, ["同事机"])

    def test_unknown_local_account_keeps_everyone(self):
        """本机拿不到账号时无从比较，只能放行 —— 不能凭空排除对端。"""
        peers = [self._peer("台式机", {"kimi": "AAA"}), self._peer("同事机", {"kimi": "BBB"})]
        kept, unverified, other = sync.peers_for_kind(peers, "kimi", None)
        self.assertEqual(len(kept), 2)
        self.assertEqual(other, [])
        self.assertEqual(unverified, [])

    def test_filter_is_per_tool(self):
        """同一台设备可能 Claude 同账号、Codex 换了账号，按 kind 分别判断。"""
        peers = [self._peer("台式机", {"claude": "AAA", "codex": "ZZZ"})]
        self.assertEqual(len(sync.peers_for_kind(peers, "claude", "AAA")[0]), 1)
        self.assertEqual(len(sync.peers_for_kind(peers, "codex", "AAA")[0]), 0)

    def test_coverage_start_takes_the_latest_peer(self):
        now = datetime.now()
        peers = [self._peer("a", {}, {_minute_key(now - timedelta(days=3)): _slot(1)}),
                 self._peer("b", {}, {_minute_key(now - timedelta(days=1)): _slot(1)})]
        self.assertEqual(sync.peers_coverage_start(peers),
                         (now - timedelta(days=1)).date().isoformat())

    def test_coverage_none_when_a_peer_has_no_minutes(self):
        self.assertIsNone(sync.peers_coverage_start([self._peer("a", {}, {})]))
        self.assertIsNone(sync.peers_coverage_start([{"device_name": "a"}]))


class SyncExportsAccountsTest(unittest.TestCase):
    def setUp(self):
        self.a = tempfile.TemporaryDirectory()
        self.b = tempfile.TemporaryDirectory()
        self.shared = tempfile.TemporaryDirectory()
        for t in (self.a, self.b, self.shared):
            self.addCleanup(t.cleanup)

    def test_export_carries_hash_not_raw_id(self):
        identity.remember(self.a.name, "kimi", "kimi-user-1")
        with mock.patch.object(identity, "CLAUDE_CONFIG", os.path.join(self.a.name, "nope")), \
             mock.patch.object(identity, "CODEX_AUTH", os.path.join(self.a.name, "nope")):
            sync.export(self.a.name, self.shared.name)
            peers = sync.read_peers(self.b.name, self.shared.name)
        self.assertEqual(peers[0]["accounts"], {"kimi": identity.account_hash("kimi-user-1")})
        raw = storage.load_json(
            os.path.join(self.shared.name, os.listdir(self.shared.name)[0]), {})
        self.assertNotIn("kimi-user-1", json.dumps(raw))  # 原始 id 不出本机


class WindowExcludesOtherAccountTest(unittest.TestCase):
    """配额窗口的实测口径必须只含同账号的设备。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for p in (mock.patch.object(server, "DATA_DIR", self.tmp.name),
                  mock.patch.object(server, "CAL_FILE",
                                    os.path.join(self.tmp.name, "quota_cal.json"))):
            p.start()
            self.addCleanup(p.stop)

    def test_other_account_usage_is_not_counted(self):
        now = datetime.now()
        peers = [
            {"device_name": "我的台式机", "accounts": {"kimi": "AAA"},
             "minutes": {_minute_key(now - timedelta(minutes=5)): _slot(10)}},
            {"device_name": "同事的机器", "accounts": {"kimi": "BBB"},
             "minutes": {_minute_key(now - timedelta(minutes=5)): _slot(100)}},
        ]
        w = {"label": "5 小时窗口", "used_percent": 30.0,
             "resets_at": time.time() + 3600, "window_minutes": 300}
        with mock.patch.object(collector, "usage_since",
                               return_value={"input": 0, "output": 0, "cached": 0}):
            out = server._estimate_window("kimi", w, peers=peers, account="AAA")
        self.assertEqual(out["measured_remote"], 30)          # 只有同账号那台的 10+10+10
        self.assertEqual(out["peers_other_account"], ["同事的机器"])
        self.assertEqual(out["est_quota"], 100)               # 30 tokens / 30%

    def test_peer_without_coverage_blocks_real_capacity(self):
        """对端的分钟桶没覆盖到窗口起点 → 少数了一截，不能当真实容量。"""
        peers = [{"device_name": "台式机", "accounts": {"kimi": "AAA"}, "minutes": {}}]
        w = {"label": "5 小时窗口", "used_percent": 0.0,
             "resets_at": time.time() + 3600, "window_minutes": 300}
        with mock.patch.object(collector, "usage_since",
                               return_value={"input": 0, "output": 0, "cached": 0}):
            out = server._estimate_window("kimi", w, peers=peers, account="AAA")
        self.assertFalse(out["real_capacity"]["tracking"]["eligible"])
        self.assertIn("窗口起点", out["real_capacity"]["tracking"]["blocked"])


if __name__ == "__main__":
    unittest.main()
