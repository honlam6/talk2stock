#!/usr/bin/env python3
"""反向单据草稿单测——纯函数，不碰 ERP、不写库。

覆盖：
- 四种业务类型的对调方向；
- 数量/单价/往来单位/仓库原样保留，备注带原单号；
- 其它类型与空明细拒绝；
- 无效行（缺名称/数量≤0）被跳过，不生成脏明细。

跑法：python3 -m unittest tests.test_reverse -v
"""
import os
import sys
import unittest

APP_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

from core import reverse as rmod   # noqa: E402


def _draft(biz="sale_out"):
    return {"biz": biz, "party": "张记", "depot": "成品仓", "pay_type": "现收",
            "account_id": 25, "items": [
                {"name": "方便面", "qty": 2, "unit": "箱", "price": 45},
                {"name": "", "qty": 3, "unit": "瓶", "price": 1},
                {"name": "可乐", "qty": 0, "unit": "瓶", "price": 3.5},
            ]}


class TestReverse(unittest.TestCase):
    def test_all_directions(self):
        pairs = {"purchase_in": "purchase_return", "purchase_return": "purchase_in",
                 "sale_out": "sale_return", "sale_return": "sale_out"}
        for src, dst in pairs.items():
            out = rmod.build(_draft(src), bill_no="XS001")
            self.assertTrue(out["ok"], src)
            self.assertEqual(out["draft"]["biz"], dst)

    def test_fields_preserved(self):
        out = rmod.build(_draft(), bill_no="XS20261125-001", reason="客户退货")
        d = out["draft"]
        self.assertEqual(d["party"], "张记")
        self.assertEqual(d["depot"], "成品仓")
        self.assertEqual(d["items"], [{"name": "方便面", "qty": 2.0, "unit": "箱", "price": 45.0}])
        self.assertIn("XS20261125-001", d["remark"])
        self.assertIn("客户退货", d["remark"])
        self.assertEqual(out["message"][:3], "已生成")

    def test_unsupported_biz_rejected(self):
        out = rmod.build(_draft("other_in"), bill_no="QT001")
        self.assertFalse(out["ok"])
        self.assertIn("没有对应的反向单据", out["message"])

    def test_empty_items_rejected(self):
        d = _draft()
        d["items"] = [{"name": "", "qty": 0}]
        out = rmod.build(d, bill_no="X")
        self.assertFalse(out["ok"])
        self.assertIn("可冲销", out["message"])

    def test_remark_truncated(self):
        out = rmod.build(_draft(), bill_no="N" * 2000)
        self.assertLessEqual(len(out["draft"]["remark"]), rmod.REMARK_MAX)


if __name__ == "__main__":
    unittest.main()
