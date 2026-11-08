#!/usr/bin/env python3
"""多单契约单测：normalize_bills 三种形状兼容 + meta 组装 + v5 提示词关键规则在场。"""
import os
import sys
import unittest

APP_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

from core import llm   # noqa: E402


class TestNormalizeBills(unittest.TestCase):
    def test_v5_shape_multi(self):
        raw = {"bills": [
            {"biz": "purchase_in", "party": "美宜佳",
             "items": [{"name": "可乐", "qty": 3, "unit": "瓶", "price": 3.5}],
             "confidence": "high", "note": ""},
            {"biz": "sale_out", "party": "张记",
             "items": [{"name": "方便面", "qty": 2, "unit": "箱", "price": 45}],
             "confidence": "low", "note": "第二笔没说仓库"},
        ]}
        bills = llm.normalize_bills(raw)
        self.assertEqual(len(bills), 2)
        self.assertEqual(bills[0]["draft"]["party"], "美宜佳")
        self.assertEqual(bills[0]["draft"]["items"][0]["price"], 3.5)
        self.assertEqual(bills[1]["confidence"], "low")
        self.assertIn("仓库", bills[1]["note"])

    def test_single_object_compat(self):
        """老模型回退成单对象时自动包成单元素（单笔业务不炸）。"""
        raw = {"biz": "purchase_in", "party": "美宜佳",
               "items": [{"name": "可乐", "qty": 3, "unit": "瓶", "price": 3.5}]}
        bills = llm.normalize_bills(raw)
        self.assertEqual(len(bills), 1)
        self.assertEqual(bills[0]["draft"]["biz"], "purchase_in")

    def test_bare_list_compat(self):
        raw = [{"biz": "other_in", "party": "仓A", "items": [{"name": "x", "qty": 1}]},
               {"garbage": True}]
        bills = llm.normalize_bills(raw)
        # 无法识别的片段保留为「带问题的失败单」让用户看见，而不是静默吞掉
        self.assertEqual(len(bills), 2)
        self.assertEqual(bills[0]["draft"]["biz"], "other_in")
        self.assertTrue(bills[1]["problems"])

    def test_garbage_never_crashes(self):
        for bad in ("garbage", None, 42, {}, {"bills": []}, {"bills": ["x", 1]}):
            bills = llm.normalize_bills(bad)
            self.assertEqual(len(bills), 1)
            self.assertTrue(bills[0]["problems"])

    def test_clean_name_preserved_in_spec(self):
        """v4 规格保留规则在多单契约下不回退：6号电池不能被剥成电池。"""
        bills = llm.normalize_bills({"bills": [
            {"biz": "purchase_in", "party": "日化批发部",
             "items": [{"name": "6号电池", "qty": 100, "unit": "节", "price": 2.5}]}]})
        self.assertEqual(bills[0]["draft"]["items"][0]["name"], "6号电池")


class TestMetaAssembly(unittest.TestCase):
    def test_clarify_only_single(self):
        one = [{"draft": llm.normalize({"biz": "purchase_in", "items": []})[0],
                "confidence": "", "note": "", "problems": []}]
        meta = llm._bills_meta_from(one, {"model": "m"}, {}, None)
        self.assertIsNotNone(meta["clarify"])       # 单张缺明细 → 追问
        two = one + [dict(one[0])]
        meta2 = llm._bills_meta_from(two, {"model": "m"}, {}, None)
        self.assertIsNone(meta2["clarify"])          # 多单不追问，卡上逐单标
        self.assertEqual(meta2["count"], 2)

    def test_prompt_v5_rules_present(self):
        self.assertEqual(llm.PROMPT_VERSION, "v5-20261109")
        for kw in ('{"bills"', "多张单", "绝不搬给前一笔", "6号电池"):
            self.assertIn(kw, llm.SYSTEM_PROMPT)
        # 旧版「只处理第一笔」的规则必须已移除
        self.assertNotIn("只处理语序上的第一笔", llm.SYSTEM_PROMPT)


if __name__ == "__main__":
    unittest.main()
