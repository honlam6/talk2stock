#!/usr/bin/env python3
"""录单校验 agent（P-B）离线单测。

覆盖：
- apply_patches：白名单字段 / 置信度阈值 / 只补缺失不覆盖 / 越界；
- _parse_verdict：JSON 字符串 / dict repr / 文本嵌 JSON / 乱文本；
- run_check：submit_verdict 通道收结论（confidence 不被数字接地误杀）、降级路径；
- parse_and_check 编排：澄清优先级（agent > 规则）/ skip_clarify / 多单 clarify_all；
- habits：record→summary 往返（depot 习惯、最近价优先、方向过滤）。

跑法：python3 -m unittest tests.test_billcheck -v
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

from core import agent as agentmod          # noqa: E402
from core import billcheck as bc            # noqa: E402
from core import config as cfgmod           # noqa: E402
from core import habits as hb               # noqa: E402
from core import llm as llmmod              # noqa: E402


def _resp(payload):
    return json.dumps(payload, ensure_ascii=False)


def _patch_profile():
    return mock.patch.object(cfgmod, "active_profile", lambda: {"model": "mock"})


class TestApplyPatches(unittest.TestCase):
    def setUp(self):
        self.drafts = [
            {"biz": "sale_out", "party": "永辉超市", "depot": "",
             "items": [{"name": "可乐", "qty": 50, "unit": "瓶", "price": None}]},
            {"biz": "sale_out", "party": "张记", "depot": "成品仓",
             "items": [{"name": "方便面", "qty": 10, "unit": "箱", "price": 44}]},
        ]

    def test_fill_empty_depot_and_missing_price(self):
        patches = [
            {"bill_index": 0, "field": "depot", "value": "成品仓", "reason": "该单位历史单据全在成品仓", "confidence": 0.95},
            {"bill_index": 0, "item_index": 0, "field": "price", "value": 4.0, "reason": "永辉最近一笔可乐 4 元", "confidence": 0.92},
        ]
        applied, rejected = bc.apply_patches(self.drafts, patches)
        self.assertEqual(len(applied), 2)
        self.assertEqual(len(rejected), 0)
        self.assertEqual(self.drafts[0]["depot"], "成品仓")
        self.assertEqual(self.drafts[0]["items"][0]["price"], 4.0)
        # 第二张已有值不动
        self.assertEqual(self.drafts[1]["depot"], "成品仓")
        self.assertEqual(self.drafts[1]["items"][0]["price"], 44)

    def test_refuse_overwrite_existing_value(self):
        patches = [
            {"bill_index": 1, "field": "depot", "value": "原料仓", "confidence": 0.99},
            {"bill_index": 1, "item_index": 0, "field": "price", "value": 99, "confidence": 0.99},
        ]
        applied, rejected = bc.apply_patches(self.drafts, patches)
        self.assertEqual(applied, [])
        self.assertEqual({r["why"] for r in rejected}, {"草稿已填仓库，不许覆盖", "明细已填单价，不许覆盖"})

    def test_whitelist_and_threshold(self):
        patches = [
            {"bill_index": 0, "field": "qty", "value": 999, "confidence": 0.99},
            {"bill_index": 0, "field": "price", "value": 3.0, "confidence": 0.5},
            {"bill_index": 9, "field": "depot", "value": "成品仓", "confidence": 0.99},
        ]
        applied, rejected = bc.apply_patches(self.drafts, patches)
        self.assertEqual(applied, [])
        whys = [r["why"] for r in rejected]
        self.assertTrue(any("白名单" in w for w in whys))
        self.assertTrue(any("置信度" in w for w in whys))
        self.assertTrue(any("越界" in w for w in whys))

    def test_reject_non_positive_price(self):
        patches = [{"bill_index": 0, "item_index": 0, "field": "price", "value": -2, "confidence": 0.99}]
        applied, rejected = bc.apply_patches(self.drafts, patches)
        self.assertEqual(applied, [])
        self.assertIn("正数", rejected[0]["why"])


class TestParseVerdict(unittest.TestCase):
    def test_json_string(self):
        v = bc._parse_verdict('{"patches": [{"field": "depot"}], "clarify": null, "risks": ""}')
        self.assertEqual(v["patches"], [{"field": "depot"}])

    def test_dict_repr(self):
        v = bc._parse_verdict(str({"patches": [], "clarify": "缺什么？", "risks": ""}))
        self.assertEqual(v["clarify"], "缺什么？")

    def test_garbage_returns_none(self):
        self.assertIsNone(bc._parse_verdict("我觉得没问题"))
        self.assertIsNone(bc._parse_verdict(""))


class TestRunCheck(unittest.TestCase):
    def test_submit_verdict_channel(self):
        """结论走 submit_verdict 的 args——confidence 数字不进 final，不被接地误杀。"""
        draft = {"biz": "sale_out", "party": "永辉超市", "depot": "",
                 "items": [{"name": "可乐", "qty": 50, "unit": "瓶", "price": None}]}
        verdict = {"patches": [{"bill_index": 0, "item_index": 0, "field": "price",
                                "value": 4.0, "reason": "永辉最近 4 元", "confidence": 0.93}],
                   "clarify": None, "risks": ""}
        replies = [
            _resp({"thought": "查习惯", "tool": "habit_defaults",
                   "args": {"operator": "测试员", "party": "永辉超市", "biz": "sale_out"}}),
            _resp({"thought": "结论", "tool": "submit_verdict", "args": verdict}),
            _resp({"thought": "收工", "final": "校验完成，补了历史价"}),
        ]
        with _patch_profile(), \
             mock.patch.object(llmmod, "_chat", side_effect=replies), \
             mock.patch.object(hb, "summary",
                               return_value={"depot": "成品仓", "records": 3,
                                             "prices": {"可乐": {"price": 4.0, "scope": "该往来单位"}}}):
            chk = bc.run_check([draft], erp=None, operator="测试员")
        self.assertTrue(chk["ok"])
        self.assertEqual(chk["patches"][0]["field"], "price")
        self.assertEqual(chk["patches"][0]["value"], 4.0)
        self.assertEqual(len([s for s in chk["steps"] if s.get("tool")]), 2)   # habit + submit 两轮工具调用

    def test_no_profile_returns_empty(self):
        with mock.patch.object(cfgmod, "active_profile", lambda: None):
            chk = bc.run_check([{"biz": "sale_out"}], erp=None)
        self.assertFalse(chk["ok"])
        self.assertEqual(chk["patches"], [])


class TestParseAndCheck(unittest.TestCase):
    def _run(self, text, drafts, meta, verdict=None, skip_clarify=False):
        with _patch_profile(), \
             mock.patch.object(llmmod, "parse", side_effect=[(drafts, meta)]), \
             mock.patch.object(bc, "run_check",
                               side_effect=[dict({"ok": True, "steps": [], "grounded": True,
                                                  "tokens": {}, "prompt_version": "t"},
                                                 **(verdict or {"patches": [], "clarify": None,
                                                                "risks": ""}))]):
            return bc.parse_and_check(text, erp=None, operator="测试员", skip_clarify=skip_clarify)

    def test_patches_applied_and_clarify_from_agent(self):
        d = {"biz": "sale_out", "party": "永辉超市", "depot": "",
             "items": [{"name": "可乐", "qty": 50, "unit": "瓶", "price": None}]}
        meta = {"clarify": {"question": "规则追问", "missing": "price"}}
        verdict = {"patches": [{"bill_index": 0, "item_index": 0, "field": "price",
                                "value": 4.0, "confidence": 0.95}],
                   "clarify": "请问这批可乐是按上次的 4 元结算吗？", "risks": ""}
        out = self._run("卖50瓶可乐", [d], meta, verdict)
        self.assertEqual(out["drafts"][0]["items"][0]["price"], 4.0)     # 补丁写回草稿
        self.assertEqual(len(out["check"]["patches_applied"]), 1)
        # agent 追问优先于规则追问
        self.assertEqual(out["clarify"]["question"], "请问这批可乐是按上次的 4 元结算吗？")
        self.assertEqual(out["check"]["clarify_source"], "agent")

    def test_rule_clarify_fallback_and_skip(self):
        d = {"biz": "sale_out", "party": "永辉超市", "depot": "",
             "items": [{"name": "可乐", "qty": 50, "unit": "瓶", "price": None}]}
        meta = {"clarify": {"question": "单价多少？", "missing": "price"}}
        out = self._run("卖50瓶可乐", [d], meta, verdict={"patches": [], "clarify": None, "risks": ""})
        self.assertEqual(out["clarify"]["question"], "单价多少？")       # agent 没给 → 规则兜底
        out2 = self._run("卖50瓶可乐", [dict(d)], meta,
                         verdict={"patches": [], "clarify": None, "risks": ""}, skip_clarify=True)
        self.assertIsNone(out2["clarify"])                              # skip_clarify 压制追问

    def test_multi_bill_clarify_becomes_note(self):
        d1 = {"biz": "sale_out", "party": "A", "depot": "", "items": [{"name": "可乐", "qty": 1, "unit": "瓶", "price": None}]}
        d2 = {"biz": "sale_out", "party": "B", "depot": "", "items": [{"name": "矿泉水", "qty": 1, "unit": "瓶", "price": None}]}
        out = self._run("两单", [d1, d2], {},
                        verdict={"patches": [], "clarify": "第1、2张单都缺单价", "risks": ""})
        self.assertIsNone(out["clarify"])                               # 多单不追问（multi-entry 设计）
        self.assertEqual(out["clarify_all"], "第1、2张单都缺单价")


class TestHabits(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False, encoding="utf8")
        self.tmp.close()
        self._pather = mock.patch.object(hb, "_PATH", self.tmp.name)
        self._pather.start()
        self.addCleanup(self._pather.stop)
        self.addCleanup(lambda: os.unlink(self.tmp.name))

    def _commit(self, operator, biz, party, depot, items, day_offset=0):
        hb.record(operator, {"biz": biz, "party": party, "depot": depot, "items": items},
                  {"number": "N%d" % day_offset})

    def test_record_and_summary(self):
        self._commit("张三", "sale_out", "永辉超市", "成品仓",
                     [{"name": "可乐", "unit": "瓶", "price": 4.0}])
        self._commit("张三", "sale_out", "永辉超市", "成品仓",
                     [{"name": "可乐", "unit": "瓶", "price": 4.5}])
        self._commit("张三", "sale_out", "张记", "原料仓",
                     [{"name": "可乐", "unit": "瓶", "price": 3.0}])
        s = hb.summary(operator="张三", party="永辉超市", biz="sale_out")
        self.assertEqual(s["depot"], "成品仓")                           # 该客户历史全在成品仓
        self.assertEqual(s["prices"]["可乐"]["price"], 4.5)              # 该客户口径 = 永辉最近一笔
        self.assertEqual(s["prices"]["可乐"]["scope"], "该往来单位")
        pair_only = hb.summary(operator="张三", party="不存在", biz="sale_out")
        self.assertEqual(pair_only["prices"]["可乐"]["price"], 3.0)      # 全口径 = 最近一笔（张记 3.0）

    def test_operator_fallback_to_global(self):
        self._commit("李四", "sale_out", "客户1", "原料仓",
                     [{"name": "肥皂", "unit": "块", "price": 4.5}])
        s = hb.summary(operator="新人", party="", biz="sale_out")        # 新人无记录 → 全局口径
        self.assertEqual(s["depot"], "原料仓")
        self.assertEqual(s["prices"]["肥皂"]["price"], 4.5)


if __name__ == "__main__":
    unittest.main()
