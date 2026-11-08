#!/usr/bin/env python3
"""问一问分仓库存的仓库过滤单测（失败案例 F10 回归）：

- 匹配纯函数：exact；双向包含（「原料仓库」多一个字也命中「原料仓」）；
  多仓都含关键词 → none（不静默挑一个）。
- _stock_query：仓库名打多字/少字走 find_depot 模糊命中；查无此仓必须
  明说「没有叫…的仓库」并给相近候选——绝不误报「没有库存记录」。
- 不指定仓库 → 各仓合计。
"""
import os
import sys
import unittest

APP_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

from core import ask as askmod                      # noqa: E402
from core.erp import JshERP, match_depot_rows       # noqa: E402

DEPOTS = [{"id": 11, "name": "原料仓"}, {"id": 12, "name": "成品仓"}, {"id": 13, "name": "废品暂存仓"}]
MATS = [{"id": 1, "name": "可乐", "unit": "瓶", "mBarCode": "BC1"}]


def depot(i, name):
    return {"id": i, "name": name}


class TestMatchDepotRows(unittest.TestCase):
    def test_exact(self):
        row, kind = match_depot_rows(DEPOTS, "原料仓")
        self.assertEqual(kind, "exact")
        self.assertEqual(row["id"], 11)

    def test_reverse_containment(self):
        """用户多说一个字（原料仓库）也能命中——F10 的直接根因。"""
        row, kind = match_depot_rows(DEPOTS, "原料仓库")
        self.assertEqual(kind, "fuzzy")
        self.assertEqual(row["id"], 11)

    def test_forward_containment(self):
        row, kind = match_depot_rows(DEPOTS, "原料")
        self.assertEqual(kind, "fuzzy")
        self.assertEqual(row["id"], 11)

    def test_multi_hit_is_none(self):
        """「仓」命中所有仓 → 交上层处理，绝不静默挑第一个。"""
        row, kind = match_depot_rows(DEPOTS, "仓")
        self.assertIsNone(row)
        self.assertEqual(kind, "none")

    def test_empty_name(self):
        self.assertEqual(match_depot_rows(DEPOTS, ""), (None, "none"))


class StubERP(JshERP):
    """真 JshERP：匹配/候选/排序全走真实代码，只把网络查询（depots/materials/stock）钉死。"""

    def depots(self):
        return DEPOTS

    def materials(self):
        return MATS

    def stock(self, material_id, depot_id=None):
        return {11: 50.0, 12: 52.0}.get(int(depot_id))


class TestStockQueryDepotFilter(unittest.TestCase):
    def setUp(self):
        self.erp = StubERP()

    def test_no_filter_sums_all(self):
        data, note = askmod._stock_query({"material": "可乐"}, self.erp)
        self.assertEqual(data["total"], 102.0)
        self.assertIn("原料仓 50", note)

    def test_multi_char_depot_name_hits_fuzzy(self):
        """「原料仓库」→ 命中「原料仓」50 瓶，不再误报无库存。"""
        data, note = askmod._stock_query({"material": "可乐", "depot": "原料仓库"}, self.erp)
        self.assertEqual(data["total"], 50.0)
        self.assertEqual(data.get("depot_matched"), "fuzzy")
        self.assertIn("50", note)

    def test_bogus_depot_says_no_such_depot(self):
        data, note = askmod._stock_query({"material": "可乐", "depot": "二号仓库"}, self.erp)
        self.assertFalse(data.get("found_depot", True))
        self.assertIn("没有叫「二号仓库」的仓库", note)
        self.assertNotIn("没有库存记录", note)   # 误导句不许再出现

    def test_near_miss_depot_lists_candidates(self):
        """相似度达阈值的错名给相近候选（成品库房→成品仓 0.57）。"""
        data, note = askmod._stock_query({"material": "可乐", "depot": "成品库房"}, self.erp)
        self.assertFalse(data.get("found_depot", True))
        self.assertIn("最接近的仓库：成品仓", note)
        self.assertNotIn("没有库存记录", note)

    def test_unknown_material_still_reports_candidates(self):
        data, note = askmod._stock_query({"material": "雪碧"}, self.erp)
        self.assertFalse(data["found"])
        self.assertIn("唯一档案", note)


if __name__ == "__main__":
    unittest.main()
