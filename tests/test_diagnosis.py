#!/usr/bin/env python3
"""AI 经营诊断离线单测——全 mock，不碰网络与数据库。

覆盖：
- submit_diagnosis 收口：overview/issues 走工具 args 通道；
- 调查链校验：evidence_steps 必须指向真实步骤，证据数字必须能在该步骤观察里找到；
- 编造数字的 issue 被丢弃；全部丢弃 → 降级到规则洞察（degraded=True）；
- evidence_steps 无效 → 退化全观察池校验并标 lax；
- 未配置模型 → ok False。

跑法：python3 -m unittest tests.test_diagnosis -v
"""
import json
import os
import sys
import unittest
from unittest import mock

APP_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

from core import ask as askmod            # noqa: E402
from core import config as cfgmod         # noqa: E402
from core import dbread as dbmod          # noqa: E402
from core import diagnosis as dmod        # noqa: E402
from core import insight as insmod        # noqa: E402
from core import llm as llmmod            # noqa: E402


def _resp(payload):
    return json.dumps(payload, ensure_ascii=False)


def _stats():
    return {
        "today": {"buy": 800.0, "sell": 1200.0, "buy_n": 2, "sell_n": 3, "profit": 400.0},
        "month": {"buy": 9000.0, "sell": 15000.0, "buy_n": 20, "sell_n": 30, "profit": 6000.0},
        "stock_money": {"total_stock": 100, "total_value": 5000.0},
    }


def _summary():
    return {"debt_recv": [{"name": "永辉超市", "amount": 500.0}], "debt_recv_total": 500.0,
            "debt_pay": [], "debt_pay_total": 0.0}


def _stock():
    return {"kinds": 10, "negative": 0, "low": 0, "threshold": 10, "warn_list": []}


def _patch_ask_stats():
    return mock.patch.multiple(
        askmod,
        official_stats=lambda erp=None: _stats(),
        summary=lambda erp=None, bills=None: _summary(),
        stock_overview=lambda erp=None, **kw: _stock(),
        _rb=lambda *a, **k: [])


def _patch_profile():
    return mock.patch.object(cfgmod, "agent_profile",
                             lambda: {"model": "mock", "base_url": "http://mock"})


def _patch_sql(rows=None):
    rows = rows if rows is not None else [{"organ_name": "永辉超市", "debt": 500.0}]
    return mock.patch.object(
        dbmod, "sql_query",
        lambda a: ({"sql": "SELECT ...", "columns": list(rows[0].keys()) if rows else [],
                    "rows": rows, "row_count": len(rows), "truncated": False},
                   "只读查询返回 %d 行。" % len(rows)))


def _replies(issues, overview="本月销售 15,000.00 元，应收账款集中在永辉超市。"):
    """标准四轮：today_stats → sql_query（下钻）→ submit_diagnosis → final。"""
    return [
        _resp({"thought": "先看今日", "tool": "today_stats", "args": {}}),
        _resp({"thought": "追一下欠款来源", "tool": "sql_query",
               "args": {"sql": "SELECT organ_name, debt FROM jsh_depot_head"}}),
        _resp({"thought": "结论", "tool": "submit_diagnosis",
               "args": {"overview": overview, "issues": issues}}),
        _resp({"thought": "收工", "final": "诊断完成。"}),
    ]


_GOOD_ISSUE = {"title": "应收集中", "evidence": "永辉超市欠款 500.00 元",
               "evidence_steps": [2], "cause": "单一客户回款依赖度高",
               "action": "本周安排一次回款跟进", "severity": "warn"}


class TestDiagnose(unittest.TestCase):
    def test_submit_channel_and_step_grounding(self):
        """正常路径：issue 保留，evidence_steps 指向第 2 步（sql_query 下钻），
        overview 数字来自观察池。"""
        replies = _replies([_GOOD_ISSUE])
        with _patch_profile(), _patch_ask_stats(), _patch_sql(), \
             mock.patch.object(llmmod, "_chat", side_effect=replies) as chat:
            out = dmod.diagnose(erp=object(), operator="测试员")
        self.assertTrue(out["ok"])
        self.assertFalse(out["degraded"])
        self.assertEqual(out["prompt_version"], dmod.DIAG_PROMPT_VERSION)
        self.assertEqual(len(out["issues"]), 1)
        issue = out["issues"][0]
        self.assertEqual(issue["evidence_steps"], [2])
        self.assertEqual(issue["severity"], "warn")
        self.assertIn("应收集中", out["report_md"])
        # 3 轮工具 + 1 轮 final
        self.assertEqual(len([s for s in out["steps"] if s.get("tool")]), 3)
        self.assertEqual(chat.call_count, 4)

    def test_fabricated_numbers_dropped_and_degraded(self):
        """证据含观察外数字 → issue 被丢；全丢 → 降级规则洞察（degraded）。"""
        bad = dict(_GOOD_ISSUE, evidence="今天卖了 99999.00 元")
        with _patch_profile(), _patch_ask_stats(), _patch_sql(), \
             mock.patch.object(insmod, "insights", lambda erp=None: {
                 "ok": True, "insights": [{"title": "本月采销", "detail": "本月采购 9,000.00 元",
                                          "level": "info"}]}), \
             mock.patch.object(llmmod, "_chat", side_effect=_replies([bad])):
            out = dmod.diagnose(erp=object())
        self.assertTrue(out["ok"])
        self.assertTrue(out["degraded"])
        self.assertEqual(out["source"], "rule_fallback")
        self.assertEqual(len(out["issues"]), 1)
        self.assertEqual(out["issues"][0]["title"], "本月采销")

    def test_invalid_steps_fall_back_to_full_pool_with_lax(self):
        """evidence_steps 指向不存在的步骤 → 不丢结论，退化全观察池校验并标 lax。"""
        issue = {"title": "今日规模", "evidence": "今天销售 1,200.00 元",
                 "evidence_steps": [99], "cause": "客流集中在午后",
                 "action": "评估晚间促销时段", "severity": "info"}
        with _patch_profile(), _patch_ask_stats(), _patch_sql(), \
             mock.patch.object(llmmod, "_chat", side_effect=_replies([issue], overview="")):
            out = dmod.diagnose(erp=object())
        self.assertTrue(out["ok"])
        self.assertFalse(out["degraded"])
        self.assertEqual(len(out["issues"]), 1)
        self.assertIn("今日规模", out["lax"])
        # overview 为空时用确定性计数句兜底
        self.assertIn("1 个", out["overview"])

    def test_no_profile(self):
        """未配置模型 → ok False，不碰 LLM。"""
        with mock.patch.object(cfgmod, "agent_profile", lambda: None), \
             mock.patch.object(llmmod, "_chat") as chat:
            out = dmod.diagnose(erp=object())
        self.assertFalse(out["ok"])
        self.assertIn("模型", out["message"])
        chat.assert_not_called()

    def test_llm_failure_degrades(self):
        """LLM 全炸 → 规则洞察兜底，绝不空手。"""
        with _patch_profile(), _patch_ask_stats(), \
             mock.patch.object(insmod, "insights", lambda erp=None: {
                 "ok": True, "insights": [{"title": "库存风险", "detail": "1 种负库存",
                                          "level": "warn"}]}), \
             mock.patch.object(llmmod, "_chat", side_effect=Exception("boom")):
            out = dmod.diagnose(erp=object())
        self.assertTrue(out["ok"])
        self.assertTrue(out["degraded"])
        self.assertEqual(out["issues"][0]["severity"], "warn")


if __name__ == "__main__":
    unittest.main()
