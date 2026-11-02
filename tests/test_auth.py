#!/usr/bin/env python3
"""登录鉴权与用户级模型档案的单元测试（不触网：ERP 登录链路由冒烟测试覆盖）。"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

from core import auth as authmod          # noqa: E402
from core import config as cfgmod         # noqa: E402


class SessionTokenTest(unittest.TestCase):
    def test_roundtrip(self):
        tok = authmod.issue_session("test1", "测试一号", "member", uid=146)
        p = authmod.verify_session(tok)
        self.assertIsNotNone(p)
        self.assertEqual(p["u"], "test1")
        self.assertEqual(p["n"], "测试一号")
        self.assertEqual(p["r"], "member")
        self.assertEqual(p["uid"], 146)

    def test_tampered_sig_rejected(self):
        tok = authmod.issue_session("test1", "x", "member")
        body, sig = tok.split(".", 1)
        self.assertIsNone(authmod.verify_session(body + "." + ("0" * len(sig))))

    def test_expired_rejected(self):
        tok = authmod.issue_session("test1", "x", "member", ttl_days=-1)
        self.assertIsNone(authmod.verify_session(tok))

    def test_bad_role_downgraded_to_member(self):
        tok = authmod.issue_session("x", "x", "root")   # 非法角色 → 落到 member
        self.assertEqual(authmod.verify_session(tok)["r"], "member")

    def test_static_pass_header_is_not_a_session(self):
        self.assertIsNone(authmod.verify_session("some-static-password"))


class RoleDecisionTest(unittest.TestCase):
    def test_erp_ismanager_wins(self):
        self.assertEqual(authmod.decide_role("admin", 1), "admin")
        self.assertEqual(authmod.decide_role("test1", 0), "member")

    def test_admin_allowlist(self):
        with mock.patch.dict(os.environ, {"T2S_ADMINS": "test1, test2"}):
            self.assertEqual(authmod.decide_role("test1", 0), "admin")
            self.assertEqual(authmod.decide_role("test3", 0), "member")

    def test_allowlist_beats_non_manager(self):
        with mock.patch.dict(os.environ, {"T2S_ADMINS": "test3"}):
            self.assertEqual(authmod.decide_role("test3", None), "admin")


class LoginThrottleTest(unittest.TestCase):
    def test_locks_after_five_fails(self):
        authmod._fails.pop("brute", None)
        for _ in range(authmod._FAIL_MAX - 1):
            authmod._login_fail("brute")
        self.assertEqual(authmod._login_throttled("brute"), 0)   # 差一次还不锁
        authmod._login_fail("brute")
        self.assertGreater(authmod._login_throttled("brute"), 0)
        authmod._login_ok("brute")
        self.assertEqual(authmod._login_throttled("brute"), 0)


class UserProfileStoreTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_dir = cfgmod.DATA_DIR
        cfgmod.DATA_DIR = self._tmp.name

    def tearDown(self):
        cfgmod.DATA_DIR = self._old_dir
        self._tmp.cleanup()

    def test_save_and_mask(self):
        ok, _ = authmod.save_user_profile("test1", "My DeepSeek", {
            "name": "我的DeepSeek", "base_url": "https://api.deepseek.com/v1/",
            "api_key": "sk-1234567890abcdef", "model": "deepseek-chat",
        })
        self.assertTrue(ok)
        rec = authmod.user_profiles("test1")
        self.assertEqual(rec["active"], "my-deepseek")       # 首个档案自动启用
        masked = authmod.masked_user_profiles("test1")
        k = masked["profiles"]["my-deepseek"]
        self.assertIn("****", k["api_key"])
        self.assertNotIn("sk-1234567890abcdef", k["api_key"])
        self.assertTrue(k["has_key"])
        # 明文只落服务端文件，不出现在脱敏视图
        self.assertEqual(authmod.user_profiles("test1")["profiles"]["my-deepseek"]["base_url"],
                         "https://api.deepseek.com/v1")

    def test_edit_keeps_key_when_masked(self):
        authmod.save_user_profile("test1", "m1", {
            "base_url": "https://x/v1", "api_key": "sk-aaaabbbbccccdddd", "model": "m"})
        ok, _ = authmod.save_user_profile("test1", "m1", {
            "base_url": "https://x/v1", "api_key": "sk-aa****dddd", "model": "m"})
        self.assertTrue(ok)
        self.assertEqual(authmod.user_profiles("test1")["profiles"]["m1"]["api_key"],
                         "sk-aaaabbbbccccdddd")

    def test_delete_and_fallback_active(self):
        authmod.save_user_profile("test1", "m1", {"base_url": "https://x/v1", "api_key": "sk-1", "model": "m1"})
        authmod.save_user_profile("test1", "m2", {"base_url": "https://y/v1", "api_key": "sk-2", "model": "m2"})
        self.assertTrue(authmod.set_user_active("test1", "m1"))
        ok, _ = authmod.delete_user_profile("test1", "m1")
        self.assertTrue(ok)
        self.assertEqual(authmod.user_profiles("test1")["active"], "m2")

    def test_delete_missing_profile(self):
        ok, msg = authmod.delete_user_profile("test1", "nope")
        self.assertFalse(ok)
        self.assertTrue(msg)

    def test_pf_key_normalization(self):
        self.assertEqual(authmod._pf_key("My Model!"), "my-model")
        self.assertEqual(authmod._pf_key("中文"), "")   # 非 ASCII → 空，调用方兜底为 "model"


class ActiveProfileFallbackTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_dir = cfgmod.DATA_DIR
        cfgmod.DATA_DIR = self._tmp.name

    def tearDown(self):
        cfgmod.DATA_DIR = self._old_dir
        self._tmp.cleanup()
        cfgmod.set_request_user(None)

    def test_user_profile_overrides_shared(self):
        cfgmod.set_request_user("test1", "member", "测试一号")
        authmod.save_user_profile("test1", "mine", {
            "base_url": "https://mine.example/v1", "api_key": "sk-mine", "model": "my-model"})
        prof = cfgmod.active_profile()
        self.assertIsNotNone(prof)
        self.assertEqual(prof["model"], "my-model")
        self.assertEqual(prof["key"], "mine")

    def test_no_own_profile_falls_back(self):
        cfgmod.set_request_user("test1", "member", "测试一号")
        # test1 没有自己的档案 → 走共享档案（本仓库 config.json 来自 example，可能有也可能没有）
        prof = cfgmod.active_profile()
        self.assertTrue(prof is None or prof.get("key") != "mine")

    def test_empty_context_ignores_user_store(self):
        authmod.save_user_profile("ghost", "mine", {
            "base_url": "https://mine.example/v1", "api_key": "sk-mine", "model": "ghost-model"})
        cfgmod.set_request_user(None)
        prof = cfgmod.active_profile()
        self.assertTrue(prof is None or prof["model"] != "ghost-model")


if __name__ == "__main__":
    unittest.main()
