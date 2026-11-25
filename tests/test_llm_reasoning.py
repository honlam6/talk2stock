#!/usr/bin/env python3
"""LLM 请求体构造（推理强度 / 厂商参数透传）单测。

背景（2026-11-09 F12）：不同厂商对「推理强度」的叫法与形态不一——OpenAI 系
reasoning_effort(minimal/low/medium/high) 已成事实标准；Qwen 等只有开/关思考
（enable_thinking）；Claude 系用 thinking 配置对象。方案：档案级
reasoning_effort + extra_body 透传两个旋钮；agent 决策步默认 low（档案显式
设置优先），配合 agent_max_tokens 输出封顶（F10）。端点不认的参数 400 一次
后记入 _quirks 自动摘除。
"""
import os
import sys
import unittest

APP_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

from core import llm as llmmod      # noqa: E402

MSG = [{"role": "user", "content": "hi"}]


class TestBasePayload(unittest.TestCase):
    def setUp(self):
        self._saved = set(llmmod._quirks)

    def tearDown(self):
        llmmod._quirks.clear()
        llmmod._quirks.update(self._saved)

    def _payload(self, prof=None, **kw):
        return llmmod._base_payload(prof or {"model": "m", "temperature": 0}, MSG, **kw)

    def test_plain_profile_has_no_optional_params(self):
        p = self._payload()
        self.assertNotIn("reasoning_effort", p)
        self.assertNotIn("max_tokens", p)

    def test_profile_effort_explicit(self):
        p = self._payload({"model": "m", "temperature": 0, "reasoning_effort": "high"})
        self.assertEqual(p["reasoning_effort"], "high")

    def test_extra_effort_used_when_profile_unset(self):
        """agent 决策步的建议值：档案没设置时才生效。"""
        p = self._payload(extra_effort="low")
        self.assertEqual(p["reasoning_effort"], "low")

    def test_profile_effort_wins_over_extra(self):
        p = self._payload({"model": "m", "temperature": 0, "reasoning_effort": "medium"},
                          extra_effort="low")
        self.assertEqual(p["reasoning_effort"], "medium")

    def test_quirk_no_reasoning_drops_param(self):
        llmmod._quirks.add("no_reasoning_effort")
        p = self._payload({"model": "m", "temperature": 0, "reasoning_effort": "high"},
                          extra_effort="low")
        self.assertNotIn("reasoning_effort", p)

    def test_extra_body_merged_and_reserved_filtered(self):
        prof = {"model": "m", "temperature": 0,
                "extra_body": {"enable_thinking": False, "model": "hijack", "x": None}}
        p = self._payload(prof)
        self.assertFalse(p["enable_thinking"])
        self.assertEqual(p["model"], "m")          # 保留键不许被覆盖
        self.assertNotIn("x", p)                   # None 值不透传

    def test_quirk_no_extra_body_drops(self):
        llmmod._quirks.add("no_extra_body")
        p = self._payload({"model": "m", "temperature": 0,
                           "extra_body": {"enable_thinking": False}})
        self.assertNotIn("enable_thinking", p)

    def test_agent_purpose_caps_output(self):
        p = self._payload(purpose="agent")
        self.assertEqual(p["max_tokens"], 2000)    # 默认封顶（hy4 高推理需思考余量）
        p2 = self._payload({"model": "m", "temperature": 0, "agent_max_tokens": 300},
                           purpose="agent")
        self.assertEqual(p2["max_tokens"], 300)

    def test_force_json_adds_response_format(self):
        p = self._payload(force_json=True)
        self.assertEqual(p["response_format"], {"type": "json_object"})
        p2 = self._payload(force_json=False)
        self.assertNotIn("response_format", p2)

    def test_quirks_scoped_per_profile(self):
        """某端点不认 reasoning_effort 不应污染另一个端点/模型。"""
        prof_a = {"base_url": "https://a.example/v1", "model": "m1", "temperature": 0, "reasoning_effort": "low"}
        prof_b = {"base_url": "https://b.example/v1", "model": "m2", "temperature": 0, "reasoning_effort": "low"}
        llmmod._add_quirk(prof_a, "no_reasoning_effort")
        pa = llmmod._base_payload(prof_a, MSG)
        pb = llmmod._base_payload(prof_b, MSG)
        self.assertNotIn("reasoning_effort", pa)
        self.assertEqual(pb.get("reasoning_effort"), "low")

    def test_extract_json_with_braces_in_string(self):
        """JSON 字符串字面量里含花括号 { } 时，括号扫描不能提前截断。"""
        raw = '前置说明 {"thought": "备注含 {测试} 括号", "final": "答案是 {A}"} 尾部'
        obj = llmmod._extract_json(raw)
        self.assertEqual(obj["thought"], "备注含 {测试} 括号")
        self.assertEqual(obj["final"], "答案是 {A}")

    def test_fallback3_preserves_decision_max_tokens(self):
        """_chat 降级分支 #3（撤掉 response_format）仍必须透传 decision_max_tokens。"""
        from unittest import mock
        import urllib.error
        seen = []

        def fake_post(url, payload, api_key, **kw):
            seen.append(dict(payload))
            if payload.get("response_format"):
                raise urllib.error.HTTPError(url, 400, "bad", {}, None)
            return '{"ok":1}', {"prompt_tokens": 10, "completion_tokens": 5}

        prof = {"base_url": "https://x.example/v1", "model": "mx", "api_key": "k", "temperature": 0}
        with mock.patch.object(llmmod, "_post_retry", side_effect=fake_post):
            llmmod._chat(prof, MSG, purpose="agent", decision_max_tokens=2600)
        self.assertGreaterEqual(len(seen), 2)
        self.assertEqual(seen[-1]["max_tokens"], 2600)


if __name__ == "__main__":
    unittest.main()
