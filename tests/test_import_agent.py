#!/usr/bin/env python3
"""导入智能体单测：结构画像、规则映射、问题生成、答复重算、日期自愈、trace 完整性。

这些用例全部不依赖模型：规则层必须自己就能跑到「可以直接预览」，
模型只是补位与解释——这条边界一旦破了，导入就会变成「没有模型不能用」。
"""
import os
import sys
import tempfile
import unittest

APP_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

from core import import_agent as ia   # noqa: E402

DELIVERY = [
    ["某某商贸送货单", "", "", "", "", "", ""],
    ["单号", "日期", "客户", "商品名称", "数量", "单价", "金额"],
    ["D001", "2024.1.5", "美宜佳", "可乐 500ml", "3", "3.5", "10.5"],
    ["D001", "2024.1.5", "美宜佳", "雪碧", "2", "3", "6"],
    ["D002", "2024.1.6", "永辉超市", "纸箱", "200", "5", "1000"],
    ["合计", "", "", "", "", "", 1016.5],
]

NO_PARTY = [
    ["商品", "数量", "单价", "金额"],
    ["可乐", "3", "3.5", "10.5"],
    ["雪碧", "2", "3", "6"],
]

TWO_QTY = [
    ["单号", "品名", "数量", "件数", "单价", "客户"],
    ["D1", "可乐", "3", "3", "3.5", "美宜佳"],
    ["D1", "雪碧", "2", "2", "3", "美宜佳"],
    ["D1", "纸箱", "10", "10", "5", "美宜佳"],
]

DDMMYYYY = [
    ["品名", "数量", "单价", "客户", "日期"],
    ["可乐", "3", "3.5", "美宜佳", "05/01/2024"],
    ["雪碧", "2", "3", "永辉", "06/01/2024"],
]


class TestProfile(unittest.TestCase):
    def test_header_row_skips_title_and_footer(self):
        p = ia.profile(DELIVERY)
        self.assertEqual(p["header_row"], 2)          # 第 1 行是标题
        self.assertEqual(p["footer_row"], 6)          # 合计行被认出
        self.assertEqual(p["body_rows"], 3)           # 3 行数据

    def test_column_kinds(self):
        p = ia.profile(DELIVERY)
        by_letter = {c["letter"]: c for c in p["cols"]}
        self.assertGreaterEqual(by_letter["E"]["num_ratio"], 0.9)   # 数量
        self.assertGreaterEqual(by_letter["B"]["date_ratio"], 0.9)  # 日期

    def test_ambiguous_header_detected(self):
        rows = [["送货单", "", ""],
                ["供应商", "商品", "数量"],
                ["美宜佳", "可乐", "3"]]
        p = ia.profile(rows)
        # 两行都像表头；至少得给出候选行，交给问题层决定
        self.assertGreaterEqual(len(p["header_candidates"]), 2)


class TestRuleMapping(unittest.TestCase):
    def test_maps_all_required_fields(self):
        out = ia.build(DELIVERY)
        cols = out["mapping"]["cols"]
        mapped = set(cols.values())
        self.assertIn("item_name", mapped)
        self.assertIn("qty", mapped)
        self.assertIn("party", mapped)
        self.assertIn("oper_time", mapped)

    def test_one_column_one_field(self):
        out = ia.build(DELIVERY)
        self.assertEqual(len(out["mapping"]["cols"]), len(set(out["mapping"]["cols"].values())))

    def test_group_mode_from_duplicate_bill_no(self):
        out = ia.build(TWO_QTY)
        self.assertEqual(out["mapping"]["mode"], "group")
        self.assertEqual(out["mapping"]["group_col"], "A")

    def test_deterministic_without_llm(self):
        out = ia.build(DELIVERY)
        self.assertTrue(out["ok"])
        self.assertGreaterEqual(out["result"]["summary"]["ok"], 3)
        self.assertFalse(any(t["source"] == "llm" for t in out["trace"]))


class TestQuestions(unittest.TestCase):
    def test_missing_party_asks_with_text_option(self):
        out = ia.build(NO_PARTY)
        q = next(q for q in out["questions"] if q["id"] == "field:party")
        self.assertTrue(q["allow_text"])
        self.assertFalse(q["blocking"])       # 可只填一个单位名

    def test_duplicate_qty_columns_ask(self):
        out = ia.build(TWO_QTY)
        q = next(q for q in out["questions"] if q["id"] == "field:qty")
        values = [o["value"] for o in q["options"]]
        self.assertIn("C", values)
        self.assertIn("D", values)

    def test_answer_column_changes_mapping(self):
        first = ia.build(TWO_QTY)
        self.assertIn("field:qty", [q["id"] for q in first["questions"]])
        second = ia.build(TWO_QTY, answers={"field:qty": "D"}, round_no=2)
        self.assertEqual(second["mapping"]["cols"].get("D"), "qty")
        self.assertNotEqual(second["mapping"]["cols"].get("C"), "qty")

    def test_answered_question_is_not_asked_again(self):
        second = ia.build(TWO_QTY, answers={"field:qty": "B", "mode": "group"}, round_no=2)
        ids = [q["id"] for q in second["questions"]]
        self.assertNotIn("field:qty", ids)
        self.assertNotIn("mode", ids)

    def test_answer_party_text_fills_drafts(self):
        out = ia.build(NO_PARTY, answers={"field:party": "美宜佳"}, round_no=2)
        self.assertTrue(all(d["draft"]["party"] == "美宜佳" for d in out["result"]["drafts"]))

    def test_no_more_than_four_questions(self):
        out = ia.build(DELIVERY)
        self.assertLessEqual(len(out["questions"]), 4)


class TestAutoRepair(unittest.TestCase):
    def test_day_first_date_repaired_automatically(self):
        out = ia.build(DDMMYYYY)
        dates = [d["draft"]["oper_time"] for d in out["result"]["drafts"]]
        self.assertTrue(all(d.startswith("2024-01-") for d in dates), dates)
        self.assertTrue(any(t["step"] == "auto_repair" for t in out["trace"]))

    def test_repair_is_recorded_in_trace(self):
        out = ia.build(DDMMYYYY)
        rep = next(t for t in out["trace"] if t["step"] == "auto_repair")
        self.assertEqual(rep["source"], "auto")
        self.assertIn("→", rep["detail"])


class TestTraceAndSafety(unittest.TestCase):
    def test_trace_has_sources_and_order(self):
        out = ia.build(DELIVERY)
        self.assertTrue(out["trace"])
        for t in out["trace"]:
            self.assertIn(t["source"], ("rule", "llm", "auto", "user"))
            self.assertTrue(t["detail"])
        self.assertEqual(out["trace"][-1]["step"], "ready")

    def test_failed_mapping_returns_questions_not_crash(self):
        rows = [["随便", "什么"], ["a", "b"]]
        out = ia.build(rows)
        self.assertTrue(out["ok"])
        ids = [q["id"] for q in out["questions"]]
        self.assertIn("field:item_name", ids)

    def test_no_write_path(self):
        """导入计划层不碰 ERP、不签令牌——只做结构与草稿。"""
        src_path = os.path.join(APP_DIR, "core", "import_agent.py")
        with open(src_path, encoding="utf-8") as f:
            src = f.read()
        for bad in ("create_bill", "gate.issue", "erp."):
            self.assertNotIn(bad, src)


if __name__ == "__main__":
    unittest.main()
