#!/usr/bin/env python3
"""auth.erp_login 对 jshERP 登录响应形态的回归单测。

实测（2026-11-09，服务器真机）：jshERP 密码错误时返回
HTTP 200 + {"code":200, "data":{"msgTip":"user password error"}}——
错误信号只在 msgTip 里。旧代码只看 code，把一切错密码说成
「ERP 返回异常，请稍后再试」（误导，评审视角下同 F11 性质）。
"""
import io
import json
import os
import sys
import unittest
from unittest import mock

APP_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

from core import auth as authmod      # noqa: E402


class FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _erp_reply(payload):
    return mock.patch("core.auth.urllib.request.urlopen",
                      return_value=FakeResp(json.dumps(payload).encode()))


class TestErpLogin(unittest.TestCase):
    def test_success_returns_user(self):
        payload = {"code": 200, "data": {"user": {"loginName": "jsh", "id": 1, "tenantId": 63},
                                         "token": "t", "msgTip": "user login success"}}
        with _erp_reply(payload):
            u, err = authmod.erp_login("jsh", "123456")
        self.assertEqual(err, "")
        self.assertEqual(u["loginName"], "jsh")
        self.assertNotIn("password", u)

    def test_wrong_password_is_not_erp_error(self):
        """服务器真机形态：code=200 + msgTip=user password error → 账号或密码不对。"""
        payload = {"code": 200, "data": {"msgTip": "user password error"}}
        with _erp_reply(payload):
            u, err = authmod.erp_login("jsh", "wrong")
        self.assertIsNone(u)
        self.assertEqual(err, "账号或密码不对")

    def test_user_not_found_same_message(self):
        """与密码错误同一文案（不帮猜号）。"""
        payload = {"code": 200, "data": {"msgTip": "user is not exist"}}
        with _erp_reply(payload):
            u, err = authmod.erp_login("nosuch", "x")
        self.assertIsNone(u)
        self.assertEqual(err, "账号或密码不对")

    def test_code_non200_is_wrong_credentials(self):
        payload = {"code": 1005, "data": None}
        with _erp_reply(payload):
            u, err = authmod.erp_login("jsh", "x")
        self.assertIsNone(u)
        self.assertEqual(err, "账号或密码不对")

    def test_empty_user_without_msgtip_still_erp_error(self):
        """无 msgTip 的异常形态才允许说「ERP 返回异常」（真正的基础设施问题）。"""
        payload = {"code": 200, "data": {}}
        with _erp_reply(payload):
            u, err = authmod.erp_login("jsh", "x")
        self.assertIsNone(u)
        self.assertEqual(err, "ERP 返回异常，请稍后再试")


if __name__ == "__main__":
    unittest.main()
