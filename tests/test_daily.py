#!/usr/bin/env python3
"""老板日报 agent（P-D）离线单测——全 mock，不碰网络。

覆盖：
- run_agent 路径：submit_verdict args 通道出 points/suggest_drafts（final 只写总结）；
- 当日缓存：第二次调用 cached=True 且 LLM 调用次数不变；invalidate() 后真重跑；
- 未配置模型 → ok False；
- LLM 炸 → 规则模板兜底（degraded=True，句子复用问一问工具模板句，数字全来自 mock 的
  official_stats / summary / stock_overview——patch 打在 askmod 命名空间）；
- 结论合法化：points 数字接地（编造数字的点被丢）、suggest_drafts 结构合法
  （biz 限 purchase_in / items 非空 / price 被剥掉 / party 必须在观察里）。

跑法：python3 -m unittest tests.test_daily -v
"""
import json
import os
import sys
import unittest
from unittest import mock

APP_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

from core import ask as askmod           # noqa: E402
from core import config as cfgmod        # noqa: E402
from core import daily as dmod           # noqa: E402
from core import llm as llmmod           # noqa: E402

DAILY_PROMPT_VERSION = dmod.DAILY_PROMPT_VERSION


def _resp(payload):
    return json.dumps(payload, ensure_ascii=False)


def _stats():
    """official_stats 的确定替身：today 采销 + stock_money。"""
    return {
        "today": {"buy": 800.0, "sell": 1200.0, "buy_n": 2, "sell_n": 3, "profit": 400.0},
        "month": {"buy": 9000.0, "sell": 15000.0, "buy_n": 20, "sell_n": 30, "profit": 6000.0},
        "stock_money": {"total_stock": 100, "total_value": 5000.0},
    }


def _summary():
    """summary 的确定替身：应收/应付 top。"""
    return {"debt_recv": [{"name": "永辉超市", "amount": 500.0}], "debt_recv_total": 500.0,
            "debt_pay": [{"name": "美宜佳", "amount": 300.0}], "debt_pay_total": 300.0}


def _stock():
    """stock_overview 的确定替身：负库存 1 + 低库存 2，最紧张是可乐。"""
    return {"kinds": 10, "negative": 1, "low": 2, "threshold": 10,
            "warn_list": [{"name": "可乐", "unit": "瓶", "stock": 3, "threshold": 10}]}


def _bills():
    """recent_bills 的确定替身：带供应商名（suggest_drafts 的 party 溯源用）。"""
    return [{"number": "N1", "time": "2026-11-15", "type": "采购入库·采购",
             "party": "美宜佳", "goods": "可乐", "count": 30, "total": 105.0, "debt": 0.0}]


def _patch_ask_stats():
    """把问一问工具的数据依赖全部钉死在 askmod 命名空间（daily.py 经 askmod 调用）。"""
    return mock.patch.multiple(
        askmod,
        official_stats=lambda erp=None: _stats(),
        summary=lambda erp=None, bills=None: _summary(),
        stock_overview=lambda erp=None, **kw: _stock(),
        _rb=lambda *a, **k: _bills())


def _patch_profile():
    return mock.patch.object(cfgmod, "agent_profile",
                             lambda: {"model": "mock", "base_url": "http://mock"})


# 提交 verdict 的标准观察序列：recent_bills（供 party 溯源）→ today_stats → submit → final
def _replies(points, drafts):
    return [
        _resp({"thought": "翻最近单据找供应商", "tool": "recent_bills", "args": {}}),
        _resp({"thought": "查今日经营", "tool": "today_stats", "args": {}}),
        _resp({"thought": "结论", "tool": "submit_verdict",
               "args": {"points": points, "suggest_drafts": drafts}}),
        _resp({"thought": "收工", "final": "早报已生成。"}),
    ]


_GOOD_POINTS = [
    {"title": "今日销售", "detail": "今天销售 1,200.00 元（3 笔）", "severity": "good"},
    # 数字只用观察里有的（today_stats 的 sell_n=3）——接地点校验会丢弃观察外数字
    {"title": "库存预警", "detail": "今日 3 笔销售后库存吃紧", "severity": "warn"},
]
_GOOD_DRAFTS = [{"biz": "purchase_in", "party": "美宜佳",
                 "items": [{"name": "可乐", "qty": 50, "unit": "瓶"}]}]

# 算法侧补货建议（daily 现在只采信 procurement 的确定性数量，模型给的 qty 不作数）
_ALGO = {"可乐": {"name": "可乐", "suggest_qty": 50, "unit": "瓶",
                  "supplier": "美宜佳", "level": "reorder"}}


class TestMorningReport(unittest.TestCase):
    def setUp(self):
        dmod.invalidate()   # 模块级缓存按天共享，逐用例清场
        # _valid_drafts 会调 _suggest_map（SQL 通道）；离线单测钉死为算法替身
        p = mock.patch.object(dmod, "_suggest_map", lambda erp=None: dict(_ALGO))
        p.start()
        self.addCleanup(p.stop)

    def test_submit_verdict_channel(self):
        """结论走 submit_verdict 的 args：points/suggest_drafts 从工具通道落地，
        final 只写总结（接地校验对 final 仍生效，与 billcheck 同模式）。"""
        replies = _replies(_GOOD_POINTS, _GOOD_DRAFTS)
        with _patch_profile(), _patch_ask_stats(), \
             mock.patch.object(llmmod, "_chat", side_effect=replies) as chat:
            out = dmod.morning_report(erp=object(), operator="测试员")
        self.assertTrue(out["ok"])
        self.assertFalse(out["cached"])
        self.assertEqual(out["prompt_version"], DAILY_PROMPT_VERSION)
        self.assertEqual(len(out["points"]), 2)
        self.assertEqual(out["points"][0]["severity"], "good")
        self.assertEqual(out["points"][1]["title"], "库存预警")
        self.assertEqual(out["suggest_drafts"][0]["party"], "美宜佳")
        self.assertIn("今日销售", out["report_md"])
        # 3 轮工具调用 + 1 轮 final
        self.assertEqual(len([s for s in out["steps"] if s.get("tool")]), 3)
        self.assertEqual(chat.call_count, 4)

    def test_cache_and_invalidate(self):
        """当日缓存：第二次调用 cached=True 且不再打 LLM；invalidate() 后真重跑。"""
        replies = _replies(_GOOD_POINTS, _GOOD_DRAFTS) * 2   # 够两次完整运行
        with _patch_profile(), _patch_ask_stats(), \
             mock.patch.object(llmmod, "_chat", side_effect=replies) as chat:
            first = dmod.morning_report(erp=object())
            self.assertFalse(first["cached"])
            self.assertEqual(chat.call_count, 4)
            second = dmod.morning_report(erp=object())
            self.assertTrue(second["cached"])
            self.assertEqual(chat.call_count, 4)             # 缓存命中：LLM 零调用
            self.assertEqual(second["points"], first["points"])
            dmod.invalidate()
            third = dmod.morning_report(erp=object())
            self.assertFalse(third["cached"])
            self.assertEqual(chat.call_count, 8)             # 失效后重新取证

    def test_no_profile_returns_error(self):
        """未配置模型 → ok False + 提示语，绝不碰 LLM。"""
        with mock.patch.object(cfgmod, "agent_profile", lambda: None), \
             mock.patch.object(llmmod, "_chat") as chat:
            out = dmod.morning_report(erp=object())
        self.assertFalse(out["ok"])
        self.assertIn("模型", out["message"])
        chat.assert_not_called()

    def test_llm_failure_degrades_to_template(self):
        """LLM 全炸 → 规则模板兜底：ok True + degraded True，数字全部来自
        official_stats/summary/stock_overview 的替身（零编造），无补货建议。"""
        with _patch_profile(), _patch_ask_stats(), \
             mock.patch.object(llmmod, "_chat", side_effect=Exception("boom")):
            out = dmod.morning_report(erp=object())
        self.assertTrue(out["ok"])
        self.assertTrue(out["degraded"])
        self.assertFalse(out["cached"])
        self.assertGreaterEqual(len(out["points"]), 3)       # 今日/本月/欠款/库存
        self.assertEqual(out["suggest_drafts"], [])
        for p in out["points"]:
            self.assertIn(p["severity"], ("info", "warn", "good"))
        # 库存点按负库存/低库存存在而给 warn；detail 数字来自替身
        stock_pt = [p for p in out["points"] if p["title"] == "库存预警"]
        self.assertEqual(stock_pt[0]["severity"], "warn")
        self.assertIn("800.00", out["report_md"])            # 今日采购 800.00
        self.assertIn("1,200.00", out["report_md"])          # 今日销售 1,200.00

    def test_draft_and_point_sanitizing(self):
        """结论合法化：编造数字的点被接地校验丢弃；补货建议只留算法覆盖的
        purchase_in（数量来自算法、price 剥掉、算法无此商品则整条丢弃）。"""
        messy_drafts = [
            {"biz": "purchase_in", "party": "美宜佳",
             "items": [{"name": "可乐", "qty": 50, "unit": "瓶", "price": 999}]},   # 合法，price 被剥
            {"biz": "sale_out", "party": "美宜佳",
             "items": [{"name": "可乐", "qty": 5, "unit": "瓶"}]},                  # biz 不合法
            {"biz": "purchase_in", "party": "美宜佳", "items": []},                 # 空明细
            {"biz": "purchase_in", "party": "美宜佳",
             "items": [{"name": "雪碧", "qty": 5, "unit": "瓶"}]},                  # 算法无此商品
            {"biz": "purchase_in", "party": "美宜佳",
             "items": [{"name": "可乐", "qty": "很多", "unit": "瓶"}]},              # 模型数量不作数
        ]
        points = [dict(_GOOD_POINTS[0]),
                  {"title": "编的", "detail": "今天卖了 99999.00 元", "severity": "warn"}]
        with _patch_profile(), _patch_ask_stats(), \
             mock.patch.object(llmmod, "_chat",
                               side_effect=_replies(points, messy_drafts)):
            out = dmod.morning_report(erp=object())
        self.assertTrue(out["ok"])
        self.assertFalse(out.get("degraded"))
        # 编造数字的点被丢，接地点保留
        self.assertEqual(len(out["points"]), 1)
        self.assertEqual(out["points"][0]["title"], "今日销售")
        # 补货建议只剩结构合法的一条
        self.assertEqual(len(out["suggest_drafts"]), 1)
        d = out["suggest_drafts"][0]
        self.assertEqual(d["biz"], "purchase_in")
        self.assertEqual(d["party"], "美宜佳")
        self.assertEqual(len(d["items"]), 1)
        self.assertEqual(set(d.keys()), {"biz", "party", "items"})
        self.assertEqual(set(d["items"][0].keys()), {"name", "qty", "unit"})   # price 已剥
        self.assertNotIn("price", d["items"][0])


if __name__ == "__main__":
    unittest.main()
