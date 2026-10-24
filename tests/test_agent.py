#!/usr/bin/env python3
"""agent loop 离线单测：mock 掉 LLM 调用，验证控制流不变量。

跑法：python3 -m unittest tests.test_agent -v   （或 pytest tests/test_agent.py）
"""
import os
import sys
import unittest
from unittest import mock

APP_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

from core import agent                       # noqa: E402
from core import llm as llmmod               # noqa: E402


def _resp(payload):
    """构造 mock _chat 的返回（_chat 返回纯文本，agent 里再 _extract_json）。"""
    import json
    return json.dumps(payload, ensure_ascii=False)


TOOLS = {
    "echo": {
        "desc": "回显数据",
        "args_hint": '{"x": 1}',
        "run": lambda args: ({"value": 102, "unit": "瓶"}, "库存 102 瓶"),
    },
    "boom": {
        "desc": "必定抛错",
        "args_hint": "{}",
        "run": lambda args: (_ for _ in ()).throw(RuntimeError("挂了")),
    },
}


class TestGrounding(unittest.TestCase):
    def test_numeric_grounded_ok(self):
        self.assertTrue(agent.numeric_grounded("还有 102 瓶", [{"value": 102}]))

    def test_numeric_grounded_reject_invented(self):
        # 99 不在任何观察里 → 不可信
        self.assertFalse(agent.numeric_grounded("还有 99 瓶", [{"value": 102}]))

    def test_numeric_grounded_empty_obs(self):
        # 没有观察时任何带数字的回答都不可信；无数字回答可信
        self.assertFalse(agent.numeric_grounded("还有 5 瓶", []))
        self.assertTrue(agent.numeric_grounded("系统里没有记录", []))

    def test_thousand_separator_normalized(self):
        self.assertTrue(agent.numeric_grounded("欠款 1,234.00 元", [{"amount": 1234}]))


class TestLoop(unittest.TestCase):
    def _run(self, replies):
        with mock.patch.object(cfgmod_holder(), "active_profile", lambda: {"model": "mock"}), \
             mock.patch.object(llmmod, "_chat", side_effect=replies):
            return agent.run_agent("库存还有多少", TOOLS, max_steps=4)


def cfgmod_holder():
    from core import config as cfgmod_
    return cfgmod_


class TestFlow(TestLoop):
    def test_two_step_then_final(self):
        replies = [
            _resp({"thought": "先查库存", "tool": "echo", "args": {"x": 1}}),
            _resp({"thought": "够了", "final": "还有 102 瓶"}),
        ]
        out = self._run(replies)
        self.assertTrue(out["ok"])
        self.assertEqual(out["answer"], "还有 102 瓶")
        self.assertTrue(out["grounded"])
        self.assertEqual(len(out["steps"]), 2)
        self.assertEqual(out["steps"][0]["tool"], "echo")
        self.assertEqual(out["steps"][0]["observation"]["value"], 102)

    def test_invented_number_falls_back_to_template(self):
        replies = [
            _resp({"thought": "查", "tool": "echo", "args": {}}),
            _resp({"thought": "作答", "final": "还有 55 瓶"}),   # 55 是编的
        ]
        out = self._run(replies)
        self.assertFalse(out["grounded"])
        self.assertEqual(out["answer"], "库存 102 瓶")            # 退回模板句
        self.assertEqual(out["final_raw"], "还有 55 瓶")          # 原文留审计

    def test_unknown_tool_becomes_observation(self):
        replies = [
            _resp({"thought": "调错", "tool": "nope", "args": {}}),
            _resp({"thought": "改用 echo", "tool": "echo", "args": {}}),
            _resp({"thought": "答", "final": "102 瓶"}),
        ]
        out = self._run(replies)
        self.assertTrue(out["ok"])
        self.assertEqual(out["steps"][0]["ok"], False)
        self.assertIn("没有这个工具", out["steps"][0]["observation"]["error"])

    def test_tool_exception_recorded_not_raised(self):
        replies = [
            _resp({"thought": "调用会炸的工具", "tool": "boom", "args": {}}),
            _resp({"thought": "换一个", "tool": "echo", "args": {}}),
            _resp({"thought": "答", "final": "102 瓶"}),
        ]
        out = self._run(replies)
        self.assertTrue(out["ok"])
        self.assertFalse(out["steps"][0]["ok"])
        self.assertIn("挂了", out["steps"][0]["observation"]["error"])

    def test_budget_exhaust_forces_final(self):
        # 每轮都选工具 → 步数预算耗尽 → 第 5 次调用是"只准作答"的收尾
        replies = [
            _resp({"thought": "查", "tool": "echo", "args": {}}),
            _resp({"thought": "再查", "tool": "echo", "args": {}}),
            _resp({"thought": "还查", "tool": "echo", "args": {}}),
            _resp({"thought": "继续", "tool": "echo", "args": {}}),
            _resp({"thought": "被迫作答", "final": "102 瓶"}),
        ]
        out = self._run(replies)
        self.assertTrue(out["ok"])
        self.assertTrue(any(s.get("forced_final") for s in out["steps"]))
        self.assertEqual(out["meta"]["forced"], False)

    def test_total_failure_returns_template(self):
        # 4 步全选工具 + 收尾也抛错 → 模板句兜底
        def exploding(prompt_side=None):
            return None
        replies = [
            _resp({"thought": "查", "tool": "echo", "args": {}}),
            _resp({"thought": "再查", "tool": "echo", "args": {}}),
            _resp({"thought": "还查", "tool": "echo", "args": {}}),
            _resp({"thought": "继续", "tool": "echo", "args": {}}),
            RuntimeError("收尾也炸"),
        ]
        out = self._run(replies)
        self.assertTrue(out["ok"])
        self.assertEqual(out["answer"], "库存 102 瓶")
        self.assertTrue(out["meta"]["forced"])


if __name__ == "__main__":
    unittest.main()
