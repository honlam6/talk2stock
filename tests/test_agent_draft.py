#!/usr/bin/env python3
"""supervisor 写路由（draft_order）离线单测：agent 只起草到预览为止，写入仍归闸门。

核心不变量：
- draft_order 产出「预览 + 逐单确认令牌」，绝不触发 draft.commit / gate.check；
- 改库存/删单据类文本在工具层直接拒绝（不信任 prompt 单层约束）；
- 3 元组返回值走 draft 事件给前端，大载荷不进模型观察。

跑法：python3 -m unittest tests.test_agent_draft -v
"""
import json
import os
import sys
import unittest
from unittest import mock

APP_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

from core import agent                       # noqa: E402
from core import ask as askmod               # noqa: E402
from core import config as cfgmod            # noqa: E402
from core import dbread as dbmod             # noqa: E402
from core import draft as draftmod           # noqa: E402
from core import gate as gatemod             # noqa: E402
from core import llm as llmmod               # noqa: E402


def _resp(payload):
    return json.dumps(payload, ensure_ascii=False)


class _StubErp(object):
    """只带路由/上下文需要的最小表面；工具内部依赖全部 mock，不触碰真 ERP。"""

    def _norm(self, s):
        return str(s or "").strip().lower()

    def materials(self):
        return []

    def suppliers(self, ptype):
        return []

    def depots(self):
        return []


def _patch_profile():
    return mock.patch.object(cfgmod, "active_profile", lambda: {"model": "mock"})


DRAFT = {"biz": "sale_out", "party": "永辉超市", "depot": "",
         "items": [{"name": "可乐", "qty": 50, "unit": "瓶", "price": 3.5}],
         "oper_time": "2026-11-16 10:00:00", "pay_type": "现付",
         "account_id": None, "remark": ""}
PREVIEW = {"ok": True,
           "items": [{"name": "可乐", "matched_name": "可乐", "qty": 50,
                      "price": 3.5, "price_missing": False}],
           "warnings": [], "errors": [], "total": 175.0}
META = {"model": "mock", "bills": [{"confidence": "high", "note": "", "problems": []}]}


class TestRunAgentDraftEvent(unittest.TestCase):
    """3 元组协议：extra 只走 draft 事件，不进模型观察。"""

    def test_three_tuple_emits_draft_event(self):
        events = []

        def run3(args):
            return ({"drafts": 1, "total": 175.0}, "已起草 1 张",
                    {"batch_id": "x", "bills": [{"draft": 1}]})

        tools = {"draft_order": {"desc": "起草", "args_hint": "{}", "run": run3}}
        with _patch_profile(), \
             mock.patch.object(llmmod, "_chat", side_effect=[
                 _resp({"thought": "起草", "tool": "draft_order", "args": {"text": "卖可乐"}}),
                 _resp({"thought": "好", "final": "已起草 1 张单据草稿，合计 175 元"}),
             ]):
            out = agent.run_agent("给我开一单", tools, max_steps=3, on_event=events.append)
        self.assertTrue(out["ok"])
        self.assertTrue(out["grounded"])          # 175 来自观察
        drafts = [e for e in events if e.get("type") == "draft"]
        self.assertEqual(len(drafts), 1)
        self.assertEqual(drafts[0]["payload"]["batch_id"], "x")
        self.assertEqual(drafts[0]["tool"], "draft_order")
        # 大载荷不进观察：模型看到的只有摘要 data
        self.assertNotIn("bills", json.dumps(out["steps"][0]["observation"], ensure_ascii=False))

    def test_two_tuple_tools_unaffected(self):
        tools = {"echo": {"desc": "回显", "args_hint": "{}",
                          "run": lambda a: ({"v": 7}, "7 个")}}
        with _patch_profile(), \
             mock.patch.object(llmmod, "_chat", side_effect=[
                 _resp({"thought": "查", "tool": "echo", "args": {}}),
                 _resp({"thought": "答", "final": "有 7 个"}),
             ]):
            out = agent.run_agent("几个", tools, max_steps=2,
                                  on_event=lambda e: (_ for _ in ()).throw(AssertionError("不该有 draft 事件")))
        self.assertTrue(out["ok"])
        self.assertEqual(out["answer"], "有 7 个")


class TestDraftOrder(unittest.TestCase):
    def _ask(self, text, replies):
        """统一起草：mock 模型决策与校验，任何直接写库都立即失败。"""
        def fake_resolve(d, erp):
            return dict(PREVIEW, items=[dict(x) for x in PREVIEW["items"]])

        def fake_parse(*args, **kwargs):
            return {"drafts": [dict(DRAFT, items=[dict(DRAFT["items"][0])])],
                    "meta": dict(META), "check": {"ok": True, "skipped": False},
                    "clarify": None, "clarify_all": None}

        with _patch_profile(), \
             mock.patch.object(llmmod, "_chat", side_effect=replies), \
             mock.patch("core.billcheck.parse_and_check", side_effect=fake_parse) as mp, \
             mock.patch.object(draftmod, "resolve", side_effect=fake_resolve), \
             mock.patch.object(gatemod, "issue", return_value="TOK") as mi, \
             mock.patch.object(gatemod, "prompt_fingerprint", return_value={}), \
             mock.patch.object(askmod.dbmod, "schema_brief", return_value=""), \
             mock.patch.object(draftmod, "commit",
                               side_effect=AssertionError("闸门被绕过：draft.commit 被调用")):
            out = askmod.ask(text, erp=_StubErp())
        return out, mp, mi

    def test_unified_entry_always_uses_agent_for_business_stats(self):
        replies = [
            _resp({"thought": "需要今日经营数据", "tool": "today_stats", "args": {}}),
            _resp({"thought": "按观察回答", "final": "今天销售 200 元，共 2 笔"}),
        ]
        stats = {"today": {"buy": 100.0, "buy_n": 1, "sell": 200.0, "sell_n": 2, "profit": 100.0}}
        with _patch_profile(), \
             mock.patch.object(llmmod, "_chat", side_effect=replies) as chat, \
             mock.patch.object(askmod, "official_stats", return_value=stats), \
             mock.patch.object(askmod.dbmod, "schema_brief", return_value=""):
            out = askmod.ask("今天卖了多少？", erp=_StubErp())
        self.assertEqual(out["route"], "agent")
        self.assertEqual(out["drafts"], [])
        self.assertEqual(chat.call_count, 2)
        self.assertIn("200", out["answer"])

    def test_draft_flow_issues_tokens_never_commits(self):
        replies = [
            _resp({"thought": "用户要录单，起草", "tool": "draft_order",
                   "args": {"text": "给永辉超市卖50瓶可乐单价3块5"}}),
            _resp({"thought": "草稿已出", "final": "已起草 1 张销售草稿，共 1 行，请确认"}),
        ]
        out, mp, mi = self._ask("给我开一单，卖给永辉超市50瓶可乐，单价3块5", replies)
        self.assertTrue(out["ok"])
        self.assertEqual(out["route"], "agent")
        self.assertEqual(mp.call_count, 1)                       # parse 真被走到
        self.assertEqual(mi.call_count, 1)                       # 令牌按单签发
        self.assertEqual(len(out["drafts"]), 1)
        payload = out["drafts"][0]
        self.assertEqual(payload["bills"][0]["confirm_token"], "TOK")
        self.assertEqual(payload["raw_text"], "给永辉超市卖50瓶可乐单价3块5")
        step = next(s for s in out["steps"] if s.get("tool") == "draft_order")
        self.assertTrue(step["ok"])
        self.assertEqual(step["observation"]["drafts"], 1)
        self.assertFalse(step["observation"]["written"])         # 观察里写明未写入
        self.assertIn("确认", out["answer"])

    def test_modification_refused_at_tool_layer(self):
        """「改库存」即使被模型路由进 draft_order，也在工具层拒绝（防误解析成出货单）。"""
        def parse_boom(*a, **k):
            raise AssertionError("修改类请求不应走到 llm.parse")
        replies = [
            _resp({"thought": "起草", "tool": "draft_order",
                   "args": {"text": "把可乐的库存改成999"}}),
            _resp({"thought": "拒绝", "final": "问一问只能查询和起草新单，不能修改已有数据"}),
        ]
        with _patch_profile(), \
             mock.patch.object(llmmod, "_chat", side_effect=replies), \
             mock.patch.object(llmmod, "parse", side_effect=parse_boom), \
             mock.patch.object(askmod.dbmod, "schema_brief", return_value=""):
            out = askmod.ask("把可乐的库存改成999", erp=_StubErp())
        step = next(s for s in out["steps"] if s.get("tool") == "draft_order")
        self.assertEqual(step["observation"]["refused"], "modification_not_supported")
        self.assertEqual(out["drafts"], [])

    def test_empty_text_and_oversize(self):
        data, note = askmod._draft_order({}, _StubErp())
        self.assertIn("error", data)
        data, note = askmod._draft_order({"text": "x" * 1001}, _StubErp())
        self.assertIn("过长", data["error"])


if __name__ == "__main__":
    unittest.main()
