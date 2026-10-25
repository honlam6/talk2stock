#!/usr/bin/env python3
"""档案级自定义请求头（extra_headers）单测：X-Prompt-Mode 分流的 ERP 侧配套。

- _post 把档案头原样带上（值大小写由 urllib 标题化，键值不变）
- upsert_profile 从旧档案继承 extra_headers（不在设置表单里，不能被保存抹掉）
"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

APP_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

import core.llm as llmmod       # noqa: E402
from core import config as cfgmod   # noqa: E402


class _FakeResp:
    def __init__(self, payload):
        self._p = payload

    def read(self):
        return json.dumps(self._p).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class TestPostExtraHeaders(unittest.TestCase):
    def test_post_injects_extra_headers(self):
        captured = {}

        def fake_urlopen(req, **kw):
            captured["headers"] = dict(req.headers)
            return _FakeResp({"choices": [{"message": {"content": "ok"}}]})

        with mock.patch.object(llmmod.urllib.request, "urlopen", fake_urlopen):
            text, _ = llmmod._post("http://x/v1/chat/completions",
                                   {"model": "m", "messages": []}, "k",
                                   extra_headers={"X-Prompt-Mode": "passthrough"})
        self.assertEqual(text, "ok")
        lower = {k.lower(): v for k, v in captured["headers"].items()}
        self.assertEqual(lower.get("x-prompt-mode"), "passthrough")
        self.assertEqual(lower.get("authorization"), "Bearer k")

    def test_post_no_extra_headers_untouched(self):
        captured = {}

        def fake_urlopen(req, **kw):
            captured["headers"] = dict(req.headers)
            return _FakeResp({"choices": [{"message": {"content": "ok"}}]})

        with mock.patch.object(llmmod.urllib.request, "urlopen", fake_urlopen):
            llmmod._post("http://x/v1/chat/completions", {"model": "m", "messages": []}, "k")
        lower = {k.lower() for k in captured["headers"]}
        self.assertNotIn("x-prompt-mode", lower)


class TestUpsertPreservesExtraHeaders(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self._orig = cfgmod.CONFIG_PATH
        cfgmod.CONFIG_PATH = os.path.join(self.td.name, "config.json")
        cfgmod.save({"llm": {"active": "wb", "profiles": {"wb": {
            "name": "wb", "base_url": "http://g/v1", "api_key": "", "model": "m",
            "merge_system": True, "insecure": True,
            "extra_headers": {"X-Prompt-Mode": "passthrough"},
        }}}})

    def tearDown(self):
        cfgmod.CONFIG_PATH = self._orig
        self.td.cleanup()

    def test_upsert_inherits_extra_headers(self):
        # 设置页保存只带表单字段（不带 extra_headers）——档案头必须原样保留
        cfgmod.upsert_profile("wb", {
            "name": "wb", "base_url": "http://g2/v1", "api_key": "",
            "model": "m2", "temperature": 0,
        })
        p = cfgmod.load()["llm"]["profiles"]["wb"]
        self.assertEqual(p.get("extra_headers"), {"X-Prompt-Mode": "passthrough"})
        self.assertTrue(p.get("merge_system"))
        self.assertEqual(p["base_url"], "http://g2/v1")


if __name__ == "__main__":
    unittest.main()
