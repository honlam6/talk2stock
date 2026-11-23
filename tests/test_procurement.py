#!/usr/bin/env python3
"""智能采购建议离线单测——全 mock，不碰 ERP、不碰数据库。

覆盖：
- 确定性公式：日均销量 ×（采购周期 + 覆盖天数）+ 安全库存 − 当前库存，向上取整；
- 采购周期：历史采购间隔中位数优先，无历史用默认值并写进 assumptions；
- 分级：负库存/低于安全线/撑不到采购周期 → urgent；建议量>0 → reorder；无销量不催货；
- 草稿：按供应商分组、无供应商进 skipped、数量服务端重算、不写库（prepare_bills 被 mock）；
- 安全库存映射：material_id → 跨仓合计（供库存预警口径修正）。

跑法：python3 -m unittest tests.test_procurement -v
"""
import os
import sys
import unittest
from unittest import mock

APP_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

from core import procurement as pmod   # noqa: E402
from core import draft as draftmod     # noqa: E402


class FakeErp:
    """最小 ERP 替身：只提供商品主数据（compute 的唯一 REST 依赖）。"""

    def __init__(self, materials):
        self._mats = materials

    def materials(self):
        return self._mats


def _mats():
    return [
        {"id": 1, "name": "可乐", "model": "500ml", "unit": "瓶"},
        {"id": 2, "name": "矿泉水", "model": "", "unit": "瓶"},
        {"id": 3, "name": "纸巾", "model": "", "unit": "包"},
        {"id": 4, "name": "过期面包", "model": "", "unit": "个"},
        {"id": 5, "name": "方便面", "model": "", "unit": "包"},
    ]


def _q(sql, **kw):
    """按 SQL 内容分派固定结果（只匹配 procurement 的 5 条查询）。"""
    if "jsh_material_current_stock" in sql:
        return [
            {"material_id": 1, "depot_id": 1, "current_number": 20},
            {"material_id": 2, "depot_id": 1, "current_number": 200},
            {"material_id": 3, "depot_id": 1, "current_number": 5},
            {"material_id": 4, "depot_id": 1, "current_number": -3},
            {"material_id": 5, "depot_id": 1, "current_number": 3},
        ]
    if "AS sold" in sql:
        return [
            {"material_id": 1, "sold": 90},    # 日均 3
            {"material_id": 3, "sold": 60},    # 日均 2
            {"material_id": 5, "sold": 30},    # 日均 1
        ]
    if "sub_type = '采购'" in sql:
        return [
            {"material_id": 1, "organ_id": 100, "oper_time": "2026-11-01 10:00:00", "unit_price": 3.2},
            {"material_id": 1, "organ_id": 100, "oper_time": "2026-11-06 10:00:00", "unit_price": 3.5},
        ]
    if "jsh_material_initial_stock" in sql:
        return [
            {"material_id": 1, "safe_stock": 10},
            {"material_id": 3, "safe_stock": 20},
        ]
    if "jsh_supplier" in sql:
        return [{"id": 100, "supplier": "美宜佳"}]
    return []


def _erp():
    return FakeErp(_mats())


class TestCompute(unittest.TestCase):
    def setUp(self):
        pmod._safe_cache.update({"at": 0.0, "data": {}})

    def _run(self, **kw):
        with mock.patch.object(pmod, "_q", side_effect=_q):
            return pmod.compute(_erp(), **kw)

    def test_formula_and_levels(self):
        """可乐：日均 3、历史间隔 5 天、安全 10、库存 20
        → 目标 3×(5+30)+10 = 115，建议 95，reorder。"""
        res = self._run()
        self.assertTrue(res["ok"])
        by = {r["name"]: r for r in res["rows"]}
        cola = by["可乐"]
        self.assertEqual(cola["daily"], 3.0)
        self.assertEqual(cola["lead_time_days"], 5)
        self.assertEqual(cola["lead_time_source"], "history")
        self.assertEqual(cola["target"], 115.0)
        self.assertEqual(cola["suggest_qty"], 95)
        self.assertEqual(cola["level"], pmod.LEVEL_REORDER)
        self.assertEqual(cola["supplier"], "美宜佳")
        self.assertEqual(cola["last_price"], 3.5)

    def test_urgent_rules(self):
        """纸巾低于安全线 → urgent；负库存无销量 → urgent 但建议量 0；
        撑不到采购周期（库存 3 < 日均 1×7）→ urgent。"""
        res = self._run()
        by = {r["name"]: r for r in res["rows"]}
        self.assertEqual(by["纸巾"]["level"], pmod.LEVEL_URGENT)
        self.assertEqual(by["纸巾"]["suggest_qty"], 89)       # 2×37+20-5=89
        self.assertEqual(by["过期面包"]["level"], pmod.LEVEL_URGENT)
        self.assertEqual(by["过期面包"]["suggest_qty"], 0)     # 无销量不催货
        self.assertEqual(by["方便面"]["level"], pmod.LEVEL_URGENT)
        self.assertEqual(by["方便面"]["suggest_qty"], 34)      # 1×37+0-3=34

    def test_no_history_default_lead_time(self):
        """无历史采购 → 默认 7 天，assumptions 里显式声明。"""
        res = self._run()
        by = {r["name"]: r for r in res["rows"]}
        self.assertEqual(by["纸巾"]["lead_time_source"], "default")
        self.assertEqual(by["纸巾"]["lead_time_days"], 7)
        joined = "\n".join(res["assumptions"])
        self.assertIn("纸巾", joined)
        self.assertIn("默认", joined)

    def test_no_demand_no_suggestion(self):
        """零销量商品：不进待采购清单，理由是「无销售记录」。"""
        res = self._run()
        by = {r["name"]: r for r in res["rows"]}
        self.assertEqual(by["矿泉水"]["suggest_qty"], 0)
        self.assertEqual(by["矿泉水"]["level"], pmod.LEVEL_OK)
        self.assertIn("无销售记录", by["矿泉水"]["reason"])
        self.assertEqual(res["totals"]["to_buy"], 3)   # 可乐/纸巾/方便面

    def test_sql_failure_degrades(self):
        """数据库不可达 → ok False + 中文原因，绝不抛异常。"""
        with mock.patch.object(pmod, "_q", side_effect=Exception("db down")):
            res = pmod.compute(_erp())
        self.assertFalse(res["ok"])
        self.assertIn("只读数据库通道", res["message"])

    def test_safe_stock_map(self):
        """安全库存映射：material_id → 跨仓合计。"""
        with mock.patch.object(pmod, "_q", side_effect=_q):
            m = pmod.safe_stock_map(_erp())
        self.assertEqual(m[1], 10)
        self.assertEqual(m[3], 20)


class TestDrafts(unittest.TestCase):
    def setUp(self):
        pmod._safe_cache.update({"at": 0.0, "data": {}})

    def test_group_by_supplier_and_skip(self):
        """按供应商分组：可乐有历史供应商 → 美宜佳一张草稿；纸巾/方便面
        无供应商 → skipped（不猜供应商）。数量来自算法，草稿不写库。"""
        with mock.patch.object(pmod, "_q", side_effect=_q):
            res = pmod.compute(_erp())
        captured = {}

        def fake_prepare(drafts, meta, erp, source, raw, batch_id=""):
            captured["drafts"] = drafts
            captured["source"] = source
            return [{"draft": d, "preview": {}, "confirm_token": "tok-%d" % i}
                    for i, d in enumerate(drafts)]

        with mock.patch.object(draftmod, "prepare_bills", side_effect=fake_prepare):
            out = pmod.to_drafts(res, _erp())
        self.assertTrue(out["ok"])
        self.assertEqual(len(out["bills"]), 1)
        self.assertEqual(captured["source"], "procurement")
        d = captured["drafts"][0]
        self.assertEqual(d["biz"], "purchase_in")
        self.assertEqual(d["party"], "美宜佳")
        self.assertEqual(d["items"][0]["name"], "可乐")
        self.assertEqual(d["items"][0]["qty"], 95)      # 服务端算法值
        self.assertEqual(d["items"][0]["price"], 3.5)   # 最近采购价
        skipped = {x["name"] for x in out["skipped"]}
        self.assertEqual(skipped, {"纸巾", "方便面"})

    def test_only_filter(self):
        """only=[material_id] 只对勾选商品出草稿。"""
        with mock.patch.object(pmod, "_q", side_effect=_q):
            res = pmod.compute(_erp())
        with mock.patch.object(draftmod, "prepare_bills",
                               side_effect=lambda drafts, meta, erp, source, raw, batch_id="":
                               [{"draft": d, "preview": {}, "confirm_token": "t"} for d in drafts]):
            out = pmod.to_drafts(res, _erp(), only=[1])
        self.assertTrue(out["ok"])
        self.assertEqual(len(out["bills"]), 1)
        self.assertEqual(out["bills"][0]["draft"]["items"][0]["qty"], 95)

    def test_nothing_to_buy(self):
        """没有需要采购的商品 → ok False + 明确提示。"""
        with mock.patch.object(pmod, "_q", side_effect=_q):
            res = pmod.compute(_erp())
        with mock.patch.object(pmod, "_q", side_effect=_q):
            out = pmod.to_drafts(res, _erp(), only=[])
        self.assertFalse(out["ok"])
        self.assertIn("没有需要采购", out["message"])


class TestToolSuggest(unittest.TestCase):
    def test_tool_observation_shape(self):
        """对话工具观察：前 10 行 + totals + 模板句（数字全部来自计算）。"""
        with mock.patch.object(pmod, "_q", side_effect=_q):
            data, note = pmod.tool_suggest(_erp())
        self.assertEqual(data["totals"]["to_buy"], 3)
        self.assertLessEqual(len(data["rows"]), 10)
        self.assertIn("3 种商品需要采购", note)
        self.assertIn("可乐", note)


if __name__ == "__main__":
    unittest.main()
