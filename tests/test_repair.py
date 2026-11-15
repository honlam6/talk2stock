#!/usr/bin/env python3
"""导入修复 agent（P-C）离线单测：fix 白名单/阈值/存在性校验；submit_verdict 通道；
run_repair 触发边界（无问题行跳过、行数超限跳过）；repair_transform_result 集成。

跑法：python3 -m unittest tests.test_repair -v
"""
import json
import os
import sys
import unittest
from unittest import mock

APP_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

from core import config as cfgmod    # noqa: E402
from core import llm as llmmod       # noqa: E402
from core import repair as rp        # noqa: E402


def _resp(payload):
    return json.dumps(payload, ensure_ascii=False)


def _patch_profile():
    return mock.patch.object(cfgmod, "active_profile", lambda: {"model": "mock"})


class _StubErp(object):
    def material_by_id(self, mid):
        return {"id": mid, "name": "6号电池", "model": ""} if int(mid) == 33 else None

    def find_material(self, name):
        return None, "none"

    def material_candidates(self, name):
        return [{"id": 33, "name": "6号电池", "model": ""}]

    def find_party(self, name, ptype=None):
        return None, "none"

    def party_candidates(self, name, ptype=None):
        return [{"id": 7, "supplier": "永辉超市"}]


class TestApplyFixes(unittest.TestCase):
    def setUp(self):
        self.rows = [
            {"row_no": 2, "problems": ["商品「6号电沲」没有匹配的档案"],
             "draft": {"biz": "sale_out", "party": "", "depot": "",
                       "items": [{"name": "6号电沲", "qty": 10, "unit": "节", "price": 2.5}]}},
            {"row_no": 3, "problems": [],
             "draft": {"biz": "sale_out", "party": "已填", "depot": "",
                       "items": [{"name": "可乐", "qty": 1, "unit": "瓶", "price": 3}]}},
        ]

    def test_name_fix_with_material_id(self):
        fixes = [{"row_index": 0, "item_index": 0, "field": "item_name", "value": "6号电池",
                  "material_id": 33, "confidence": 0.95, "reason": "候选唯一"}]
        applied, sugg, rej = rp.apply_fixes(self.rows, fixes, _StubErp())
        self.assertEqual(len(applied), 1)
        self.assertEqual(self.rows[0]["draft"]["items"][0]["name"], "6号电池")
        self.assertEqual(self.rows[0]["draft"]["items"][0]["material_id"], 33)
        self.assertEqual(self.rows[0]["draft"]["items"][0]["price"], 2.5)   # 数字不动

    def test_party_fix_empty_only(self):
        fixes = [{"row_index": 0, "field": "party", "value": "永辉超市", "confidence": 0.9},
                 {"row_index": 1, "field": "party", "value": "永辉超市", "confidence": 0.9}]
        applied, sugg, rej = rp.apply_fixes(self.rows, fixes, _StubErp())
        self.assertEqual(len(applied), 1)
        self.assertEqual(self.rows[0]["draft"]["party"], "永辉超市")
        self.assertIn("不许覆盖", rej[0]["why"])

    def test_whitelist_and_threshold_and_bounds(self):
        fixes = [
            {"row_index": 0, "item_index": 0, "field": "qty", "value": 999, "confidence": 0.99},
            {"row_index": 0, "item_index": 0, "field": "price", "value": 0.01, "confidence": 0.99},
            {"row_index": 0, "item_index": 0, "field": "item_name", "value": "6号电池", "confidence": 0.5},
            {"row_index": 99, "field": "party", "value": "x", "confidence": 0.99},
            {"row_index": 0, "item_index": 0, "field": "item_name", "value": "6号电池",
             "material_id": 999, "confidence": 0.99},
        ]
        applied, sugg, rej = rp.apply_fixes(self.rows, fixes, _StubErp())
        self.assertEqual(applied, [])
        self.assertEqual(len(sugg), 1)                                   # 低置信进建议
        whys = " ".join(r["why"] for r in rej)
        self.assertIn("白名单", whys)
        self.assertIn("越界", whys)
        self.assertIn("material_id 不存在", whys)
        # 高置信改名成立（material_id 无效只丢绑定，名字依据来自观察仍然合法）
        self.assertEqual(self.rows[0]["draft"]["items"][0]["name"], "6号电池")
        self.assertNotIn("material_id", self.rows[0]["draft"]["items"][0])

    def test_material_id_missing_notes_rename_only(self):
        fixes = [{"row_index": 0, "item_index": 0, "field": "item_name", "value": "6号电池",
                  "material_id": 33, "confidence": 0.99}]
        rows2 = [self.rows[0]]
        applied, sugg, rej = rp.apply_fixes(rows2, fixes, _StubErp())
        self.assertEqual(len(applied), 1)


class TestRunRepair(unittest.TestCase):
    def test_skips_clean_rows(self):
        rows = [{"row_no": 2, "problems": [], "draft": {"biz": "sale_out", "items": []}}]
        with _patch_profile():
            rep = rp.run_repair(rows, erp=None)
        self.assertTrue(rep["skipped"])
        self.assertEqual(rep.get("reason"), "no_problems")

    def test_skips_too_many_rows(self):
        rows = [{"row_no": i, "problems": ["x"], "draft": {}} for i in range(rp.MAX_REPAIR_ROWS + 1)]
        with _patch_profile():
            rep = rp.run_repair(rows, erp=None)
        self.assertTrue(rep["skipped"])
        self.assertIn("too_many_rows", rep.get("reason", ""))

    def test_submit_verdict_channel(self):
        rows = [{"row_no": 2, "problems": ["商品「6号电沲」没有匹配的档案"],
                 "draft": {"biz": "sale_out", "party": "", "depot": "",
                           "items": [{"name": "6号电沲", "qty": 10, "unit": "节", "price": 2.5}]}}]
        fixes = [{"row_index": 0, "item_index": 0, "field": "item_name", "value": "6号电池",
                  "material_id": 33, "confidence": 0.93, "reason": "find_material 唯一候选"}]
        replies = [
            _resp({"thought": "批量查档案", "tool": "find_material", "args": {"names": ["6号电沲"]}}),
            _resp({"thought": "唯一候选", "tool": "submit_verdict", "args": {"fixes": fixes}}),
            _resp({"thought": "收工", "final": "修复完成"}),
        ]
        with _patch_profile(), mock.patch.object(llmmod, "_chat", side_effect=replies):
            # repair_transform_result 内部再跑一次 run_repair——整个链路必须在同一
            # mock 上下文里（_chat side_effect 按调用次数供给）
            tr2, summary = rp.repair_transform_result({"drafts": rows}, _StubErp(), operator="测试员")
        self.assertTrue(summary["ok"])
        self.assertEqual(len(summary["fixes"]), 1)
        self.assertEqual(rows[0]["draft"]["items"][0]["name"], "6号电池")
        self.assertEqual(len(summary["applied"]), 1)


if __name__ == "__main__":
    unittest.main()
