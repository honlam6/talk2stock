#!/usr/bin/env python3
"""业务防重单测——全 mock，不碰数据库、不碰 ERP。

覆盖：
- 同往来单位 + 同商品 + 同数量（或差 ≤10%）→ 命中，带出原单号；
- 单位不同 / 数量差太大 → 不命中（不误伤正常业务）；
- 其它业务类型（其它入库等）不参与防重；
- 查询失败 → skipped，不抛异常、不阻断出卡；
- invalidate 清缓存。

跑法：python3 -m unittest tests.test_dedupe -v
"""
import os
import sys
import unittest
from unittest import mock

APP_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

from core import dedupe as dmod   # noqa: E402


def _draft(party="美宜佳", name="可乐", qty=100, biz="purchase_in"):
    return {"biz": biz, "party": party,
            "items": [{"name": name, "qty": qty, "unit": "瓶", "price": 3.5}]}


def _rows(party="美宜佳", name="可乐", qty=100, number="CGRK001"):
    return [{"number": number, "create_time": "2026-11-25 10:20:00",
             "oper_time": "2026-11-25 10:00:00", "party": party,
             "material_name": name, "basic_number": qty, "oper_number": qty}]


class TestDedupe(unittest.TestCase):
    def setUp(self):
        dmod.invalidate()

    def _check(self, rows, draft=None, organ_id=100):
        with mock.patch.object(dmod, "_q", return_value=rows):
            return dmod.check(draft or _draft(), organ_id=organ_id)

    def test_same_bill_hits(self):
        """同单位、同商品、同数量 → 命中并带出原单号。"""
        r = self._check(_rows())
        self.assertTrue(r["hit"])
        self.assertFalse(r["skipped"])
        self.assertEqual(r["matches"][0]["number"], "CGRK001")
        self.assertEqual(r["matches"][0]["hits"][0]["name"], "可乐")

    def test_quantity_tolerance(self):
        """数量差 5% 仍算同一笔（拆行/改包装余量）；差 50% 不算。"""
        self.assertTrue(self._check(_rows(qty=105))["hit"])
        dmod.invalidate()
        self.assertFalse(self._check(_rows(qty=50))["hit"])

    def test_different_material(self):
        self.assertFalse(self._check(_rows(name="矿泉水"))["hit"])

    def test_sql_scopes_by_organ_id(self):
        """按 organ_id 精确过滤，不 join 客户表（jshERP 无 jsh_customer）。"""
        calls = []

        def fake_q(sql):
            calls.append(sql)
            return []

        with mock.patch.object(dmod, "_q", side_effect=fake_q):
            dmod.check(_draft(), organ_id=777)
        self.assertIn("h.organ_id = 777", calls[0])
        self.assertNotIn("jsh_customer", calls[0])

    def test_non_bill_biz_skipped(self):
        r = self._check(_rows(), _draft(biz="other_in"))
        self.assertFalse(r["hit"])
        self.assertTrue(r["skipped"])

    def test_missing_organ_id_skipped(self):
        """新单位还没 id → 不判重（第一次建档案，不可能重复）。"""
        r = self._check(_rows(), organ_id=None)
        self.assertFalse(r["hit"])
        self.assertTrue(r["skipped"])
        self.assertIn("往来单位ID", r["reason"])

    def test_query_failure_degrades(self):
        """数据库不可达 → skipped，绝不抛异常。"""
        with mock.patch.object(dmod, "_q", side_effect=RuntimeError("db down")):
            r = dmod.check(_draft(), organ_id=100)
        self.assertFalse(r["hit"])
        self.assertTrue(r["skipped"])
        self.assertIn("不可用", r["reason"])

    def test_cache_reuse_and_invalidate(self):
        """窗口查询 45 秒内复用；invalidate 后重新查。"""
        calls = []

        def fake_q(sql):
            calls.append(sql)
            return _rows()

        with mock.patch.object(dmod, "_q", side_effect=fake_q):
            dmod.check(_draft(), organ_id=100)
            dmod.check(_draft(), organ_id=100)
            self.assertEqual(len(calls), 1)
            dmod.invalidate()
            dmod.check(_draft(), organ_id=100)
            self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()
