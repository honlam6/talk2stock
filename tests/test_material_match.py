#!/usr/bin/env python3
"""多规格商品匹配（issue #1「一个配件多个规格对不上」）单测：

- 匹配纯函数：同名多档案 → ambiguous，绝不静默挑第一个；规格在型号字段时
  「螺丝M8」这类输入靠 名字+型号 合成串命中；×/x、全半角括号、大小写归一。
- 候选：同名档案全进候选且排最前，条目带 id/型号（点选直达 material_id）。
- draft.resolve/commit 预检：material_id 直达档案；未点选的同名明细写入前被拦。
- 提示词 v4 回归：规格保留规则在，旧的「名字绝不能含数字」不再出现。
"""
import os
import sys
import unittest

APP_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

from core import erp as erpmod          # noqa: E402
from core import draft as draftmod      # noqa: E402
from core import llm as llmmod          # noqa: E402
from core.erp import (_mat_composite, _norm_text, match_material_rows,  # noqa: E402
                      material_candidate_info, rank_material_rows)


def mat(i, name, model="", standard="", unit="个"):
    return {"id": i, "name": name, "model": model, "standard": standard, "unit": unit}


ROWS = [
    mat(1, "可乐", unit="瓶"),
    mat(2, "电池", unit="节"),
    mat(3, "6号电池", unit="节"),
    mat(4, "螺丝", model="M8×20", unit="盒"),
    mat(5, "螺丝", model="M10×30", unit="盒"),
    mat(6, "PPR管", model="4分", unit="根"),
    mat(7, "PPR管", model="6分", unit="根"),
]


class TestNorm(unittest.TestCase):
    def test_parens_and_times_normalized(self):
        self.assertEqual(_norm_text("螺丝(M8)"), "螺丝m8")
        self.assertEqual(_norm_text("螺丝（M8）"), "螺丝m8")
        self.assertEqual(_norm_text("M8×20"), "m8x20")
        self.assertEqual(_norm_text("M8x20"), "m8x20")
        self.assertEqual(_norm_text(" 螺丝 M8 "), "螺丝m8")

    def test_composite(self):
        self.assertEqual(_mat_composite(mat(4, "螺丝", model="M8×20")), "螺丝m8x20")
        self.assertEqual(_mat_composite(mat(1, "可乐")), "可乐")


class TestMatch(unittest.TestCase):
    def test_unique_exact(self):
        row, kind = match_material_rows(ROWS, "可乐")
        self.assertEqual((row["id"], kind), (1, "exact"))

    def test_duplicate_name_is_ambiguous_never_first_pick(self):
        row, kind = match_material_rows(ROWS, "螺丝")
        self.assertIsNone(row)
        self.assertEqual(kind, "ambiguous")

    def test_composite_exact_and_fuzzy(self):
        row, kind = match_material_rows(ROWS, "螺丝M8×20")
        self.assertEqual((row["id"], kind), (4, "exact"))
        # 合成串包含：M8 前缀只命中 M8×20 那一档
        row, kind = match_material_rows(ROWS, "螺丝M8")
        self.assertEqual((row["id"], kind), (4, "fuzzy"))
        row, kind = match_material_rows(ROWS, "PPR管4分")
        self.assertEqual((row["id"], kind), (6, "exact"))

    def test_spec_first_order_unmatched_not_wrong(self):
        # 规格前置（M8螺丝 / 4分PPR管）匹配不上时必须是 none，
        # 绝不能静默落到另一档规格
        row, kind = match_material_rows(ROWS, "M8螺丝")
        self.assertIsNone(row)
        self.assertEqual(kind, "none")
        row, kind = match_material_rows(ROWS, "4分PPR管")
        self.assertIsNone(row)
        self.assertEqual(kind, "none")

    def test_name_with_digit_distinct_from_plain(self):
        row, kind = match_material_rows(ROWS, "6号电池")
        self.assertEqual((row["id"], kind), (3, "exact"))
        row, kind = match_material_rows(ROWS, "电池")
        self.assertEqual((row["id"], kind), (2, "exact"))

    def test_empty(self):
        self.assertEqual(match_material_rows(ROWS, ""), (None, "none"))
        self.assertEqual(match_material_rows(ROWS, None), (None, "none"))


class TestCandidates(unittest.TestCase):
    def test_duplicate_names_all_first_with_model(self):
        cands = [material_candidate_info(r) for r in rank_material_rows(ROWS, "螺丝")]
        names = [c["name"] for c in cands]
        self.assertEqual(names, ["螺丝", "螺丝"])   # 两档同名全进候选、排最前
        self.assertTrue(all(c["id"] in (4, 5) for c in cands[:2]))
        self.assertTrue(cands[0]["model"] and cands[1]["model"])  # 型号可区分

    def test_spec_query_ranks_matching_model_first(self):
        cands = [material_candidate_info(r) for r in rank_material_rows(ROWS, "螺丝M10")]
        self.assertEqual(cands[0]["model"], "M10×30")

    def test_candidate_shape(self):
        c = material_candidate_info(mat(4, "螺丝", model="M8×20", unit="盒"))
        self.assertEqual(c, {"id": 4, "name": "螺丝", "model": "M8×20",
                             "standard": "", "unit": "盒"})


class FakeErp:
    """只实现 draft.resolve/commit 预检用到的只读方法。"""

    def __init__(self, rows=ROWS):
        self._rows = rows

    def materials(self):
        return self._rows

    def find_material(self, name):
        return match_material_rows(self._rows, name)

    def material_candidates(self, name, n=3):
        return [material_candidate_info(r) for r in rank_material_rows(self._rows, name, n)]

    def material_by_id(self, mid):
        return next((r for r in self._rows if r.get("id") == int(mid)), None)

    def find_party(self, name, ptype):
        return ({"id": 11, "supplier": "美宜佳"}, "exact") if name == "美宜佳" else (None, "none")

    def party_candidates(self, name, ptype, n=3):
        return []

    def find_depot(self, name):
        return ({"id": 21, "name": "原料仓"}, "exact") if name == "原料仓" else (None, "none")

    def price_tax_flag(self):
        return False

    def unit_group_by_id(self, _uid):
        return None

    def unit_allowed(self, group, unit):
        return False

    def unit_ratio(self, group, unit):
        return 1.0

    def stock(self, mid, did):
        return None

    _norm = staticmethod(erpmod.JshERP._norm)


def _draft(**over):
    d = {"biz": "purchase_in", "party": "美宜佳", "depot": "原料仓",
         "items": [{"name": "螺丝", "qty": 10, "unit": "盒", "price": 5}]}
    d.update(over)
    return d


class TestResolve(unittest.TestCase):
    def test_ambiguous_flags_and_offers_candidates(self):
        p = draftmod.resolve(_draft(), erp=FakeErp())
        self.assertFalse(p["errors"])
        row = p["items"][0]
        self.assertEqual(row["match_kind"], "ambiguous")
        self.assertFalse(row["matched"])
        self.assertFalse(row["will_create"])       # 歧义≠新建
        self.assertEqual(len(row["candidates"]), 2)
        self.assertTrue(any("多个同名档案" in w for w in p["warnings"]))

    def test_material_id_pick_through(self):
        d = _draft(items=[{"name": "螺丝", "qty": 10, "unit": "盒",
                           "price": 5, "material_id": 4}])
        p = draftmod.resolve(d, erp=FakeErp())
        row = p["items"][0]
        self.assertEqual(row["match_kind"], "picked")
        self.assertTrue(row["matched"])
        self.assertEqual(row["material_id"], 4)
        self.assertEqual(row["matched_model"], "M8×20")
        self.assertFalse(any("多个同名档案" in w for w in p["warnings"]))

    def test_stale_material_id_falls_back_to_name(self):
        d = _draft(items=[{"name": "可乐", "qty": 3, "unit": "瓶",
                           "price": 3.5, "material_id": 99999}])
        p = draftmod.resolve(d, erp=FakeErp())
        row = p["items"][0]
        self.assertEqual(row["match_kind"], "exact")
        self.assertEqual(row["material_id"], 1)
        self.assertTrue(any("点选的商品档案已不存在" in w for w in p["warnings"]))

    def test_commit_precheck_blocks_unpicked_ambiguous(self):
        erp = FakeErp()
        res = draftmod.commit(_draft(), erp=erp)
        self.assertFalse(res["ok"])
        self.assertIn("多个同名档案", res["message"])

    def test_commit_precheck_passes_with_material_id(self):
        captured = {}

        class CommitErp(FakeErp):
            def ensure_material(self, name, unit="个", **kw):
                captured.setdefault("ensure", []).append(name)
                return mat(4, "螺丝", model="M8×20"), False

            def ensure_party(self, name, ptype):
                return {"id": 11, "supplier": name}, False

            def ensure_depot(self, name):
                return {"id": 21, "name": name}, False

            def material_barcode(self, mid, unit="个"):
                return "BC%d" % mid

            def create_bill(self, *a, **kw):
                return {"ok": True, "number": "CGRK00000001", "total": 50}

        d = _draft(items=[{"name": "螺丝", "qty": 10, "unit": "盒",
                           "price": 5, "material_id": 4}])
        # config 默认仓/账户在 resolve 里已固定，这里给足字段
        d["oper_time"] = "2026-10-25 10:00:00"
        res = draftmod.commit(d, erp=CommitErp())
        self.assertTrue(res.get("ok"), res)
        self.assertNotIn("螺丝", captured.get("ensure", []))   # 点选档案不走 ensure


class TestEnsureMaterialAmbiguous(unittest.TestCase):
    def test_ensure_raises_on_ambiguous(self):
        import importlib
        from core.erp import ErpError, JshERP
        fake = JshERP.__new__(JshERP)
        fake.materials = lambda: ROWS
        with self.assertRaises(ErpError) as cm:
            fake.ensure_material("螺丝", "盒")
        self.assertIn("多个同名档案", str(cm.exception))


class TestPromptV4(unittest.TestCase):
    """提示词回归：规格规则改动后再动 SYSTEM_PROMPT，这里会先红。"""

    def test_version_bumped(self):
        self.assertEqual(llmmod.PROMPT_VERSION, "v4-20261025")

    def test_spec_rule_present_old_rule_gone(self):
        p = llmmod.SYSTEM_PROMPT
        self.assertIn("型号、规格必须完整保留", p)
        self.assertIn("M8螺丝", p)
        self.assertNotIn("绝不能包含数字", p)          # 旧规则会把规格数字剥掉
        self.assertIn("各20根", p)                      # 多规格拆行示例
        self.assertIn("多笔业务", p)                    # 多笔业务护栏
        # 示例输出本身也要守规格规则
        self.assertIn('"name":"4分PPR管"', p)
        self.assertIn('"name":"6号电池"', p)

    def test_mat_label(self):
        self.assertEqual(llmmod._mat_label(mat(4, "螺丝", model="M8×20")), "螺丝 M8×20")
        self.assertEqual(llmmod._mat_label(mat(1, "可乐")), "可乐")


if __name__ == "__main__":
    unittest.main()
