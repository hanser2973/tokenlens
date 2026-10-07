import unittest
import urllib.error
from unittest import mock

import quota
import server


class KimiQuotaTest(unittest.TestCase):
    def setUp(self):
        self.cache = quota._cache.copy()
        quota._cache.update(at=0, data=None)

    def tearDown(self):
        quota._cache.update(self.cache)

    def test_new_monthly_windows_and_profile(self):
        card = quota.parse_kimi_usage({"usages": {
            "limit_5h": {"used_ratio": 0},
            "limit_month_total": {"used_ratio": "0.4427", "reset_time": "2026-10-25T00:00:00Z"},
            "limit_month_code": {"used_ratio": 0.4247},
        }}, {"user_id": "account", "user_level_name": "Pro"})
        self.assertEqual(card["plan_type"], "Pro")
        self.assertEqual(card["account_id"], "account")
        total = next(w for w in card["windows"] if w["id"] == "limit_month_total")
        self.assertEqual(total["used_percent"], 44.27)
        self.assertTrue(total["percent_only"])
        self.assertTrue(total["resets_at"])
        self.assertNotIn("每周窗口", [w["label"] for w in card["windows"]])

    def test_legacy_percent_and_plan_remain_supported(self):
        card = quota.parse_kimi_usage({
            "limits": [{"window": {"duration": 300}, "detail": {"limit": "100", "remaining": "80"}}],
            "usage": {"limit": "100", "used": "30"},
            "user": {"userId": "old", "membership": {"level": "LEVEL_ALLEGRETTO"}},
        })
        self.assertEqual([w["used_percent"] for w in card["windows"]], [20, 30])
        self.assertEqual(card["plan_type"], "ALLEGRETTO")
        self.assertIn("quota_note", card)

    def test_new_data_wins_without_duplicate_five_hour_window(self):
        card = quota.parse_kimi_usage({"usages": {"limit_5h": {"used_ratio": .23}},
            "limits": [{"window": {"duration": 300}, "detail": {"limit": 100, "used": 20}}]})
        self.assertEqual(len(card["windows"]), 1)
        self.assertEqual(card["windows"][0]["used_percent"], 23)

    def test_zero_omission_and_invalid_ratios(self):
        card = quota.parse_kimi_usage({"usages": {
            "limit_5h": {}, "limit_7d": {"used_ratio": "NaN"},
            "limit_month_total": {"used_ratio": 2}, "limit_month_code": {"used_ratio": -.1}}})
        self.assertEqual([w["used_percent"] for w in card["windows"]], [0, 100, 0])

    def test_profile_failure_preserves_usage_and_cache_is_not_mutated_by_callers(self):
        with mock.patch.object(quota, "_access_token", return_value=("test", None)), \
             mock.patch.object(quota, "_http_json", side_effect=[
                 {"usages": {"limit_month_total": {"used_ratio": .5}}}, OSError("offline")]) as http:
            card = quota.query_kimi()
            self.assertNotIn("error", card)
            self.assertIn("plan_note", card)
            card["windows"][0]["used_percent"] = 99
            self.assertEqual(quota.query_kimi()["windows"][0]["used_percent"], 50)
            self.assertEqual(http.call_count, 2)

    def test_unauthorized_is_an_error_not_zero_quota(self):
        with mock.patch.object(quota, "_access_token", return_value=("test", None)), \
             mock.patch.object(quota, "_http_json", side_effect=urllib.error.HTTPError(
                 quota.USAGES_URL, 401, "unauthorized", {}, None)):
            self.assertIn("error", quota.query_kimi())

    def test_monthly_windows_never_enter_token_estimation(self):
        monthly = quota.parse_kimi_usage({"usages": {"limit_month_total": {"used_ratio": .5}}})
        with mock.patch.object(server.quota, "query_kimi", return_value=monthly), \
             mock.patch.object(server, "load_settings", return_value=dict(server.DEFAULT_SETTINGS)), \
             mock.patch.object(server, "active_peers", return_value=[]), \
             mock.patch.object(server.identity, "local_accounts", return_value={}), \
             mock.patch.object(server.identity, "display_accounts", return_value=[]), \
             mock.patch.object(server.volc, "query_ark", return_value={"error": "unconfigured"}), \
             mock.patch.object(server.collector, "latest_rate_limits", return_value=None), \
             mock.patch.object(server, "merged_records", return_value=([], {})), \
             mock.patch.object(server, "load_plans", return_value=[]), \
             mock.patch.object(server, "_estimate_window") as estimate:
            result = server.compute_plans()
            estimate.assert_not_called()
            self.assertEqual(result["auto"][0]["windows"][0]["used_percent"], 50)


if __name__ == "__main__":
    unittest.main()
