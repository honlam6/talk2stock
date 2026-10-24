#!/usr/bin/env python3
"""gate 无状态 HMAC 令牌单测：合法/篡改/重放/跨「重启」（重载模块仍可验）。"""
import os
import sys
import tempfile
import unittest

APP_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

import importlib   # noqa: E402
from core import config as cfgmod   # noqa: E402


class TestGateToken(unittest.TestCase):
    def setUp(self):
        # 独立 data 目录，避免污染真实 .gatekey / used_tokens
        self.td = tempfile.TemporaryDirectory()
        self._orig = cfgmod.data_dir
        cfgmod.data_dir = lambda: self.td.name
        import core.gate as gate
        importlib.reload(gate)
        self.gate = gate

    def tearDown(self):
        cfgmod.data_dir = self._orig
        import core.gate as gate
        importlib.reload(gate)
        self.td.cleanup()

    DRAFT = {"biz": "purchase_in", "party": "美宜佳",
             "items": [{"name": "可乐", "qty": 3, "price": 3.5}]}

    def test_issue_and_check_ok(self):
        t = self.gate.issue(self.DRAFT, {"raw_text": "买可乐"})
        ok, meta, reason = self.gate.check(t, self.DRAFT)
        self.assertTrue(ok, reason)
        self.assertEqual(meta.get("raw_text"), "买可乐")

    def test_replay_rejected(self):
        t = self.gate.issue(self.DRAFT)
        ok, _, _ = self.gate.check(t, self.DRAFT)
        self.assertTrue(ok)
        ok2, _, r2 = self.gate.check(t, self.DRAFT)   # 重放
        self.assertFalse(ok2)
        self.assertIn("已被使用", r2)

    def test_tampered_draft_rejected(self):
        t = self.gate.issue(self.DRAFT)
        bad = dict(self.DRAFT, party="别人")
        ok, _, r = self.gate.check(t, bad)
        self.assertFalse(ok)
        self.assertIn("改动", r)

    def test_forged_signature_rejected(self):
        t = self.gate.issue(self.DRAFT)
        body, _ = t.split(".")
        forged = body + "." + "0" * 32
        ok, _, r = self.gate.check(forged, self.DRAFT)
        self.assertFalse(ok)
        self.assertIn("签名无效", r)

    def test_survives_reload(self):
        """模拟服务重启（模块重载、内存态清空）：令牌仍可验、黑名单仍在。"""
        t = self.gate.issue(self.DRAFT)
        importlib.reload(self.gate)
        ok, _, reason = self.gate.check(t, self.DRAFT)
        self.assertTrue(ok, reason)   # 无状态：重启后令牌依然有效
        importlib.reload(self.gate)
        ok2, _, _ = self.gate.check(t, self.DRAFT)
        self.assertFalse(ok2)         # 但已用过 → 黑名单（持久化）拦住重放

    def test_garbage_token_rejected(self):
        for junk in ["", "abc", "a.b", "....", "x" * 200]:
            ok, _, _ = self.gate.check(junk, self.DRAFT)
            self.assertFalse(ok, junk)


if __name__ == "__main__":
    unittest.main()
