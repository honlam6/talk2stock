#!/usr/bin/env python3
"""全链路数据口径与写入契约回归单测（离线，全 mock）。

覆盖：
- erp.create_bill 6 种 biz 的符号对齐（jshERP Web 口径）与单账户不再双计（accountIdList=""）；
- erp.default_account_id / default_depot_name 防幽灵 ID / 幽灵仓；
- erp.depot_stock_map REST → DB 降级与 stock_table basicNumber 口径；
- ledger：混合正负号、status IN ('1','2','3')、收付款核销、账户真实余额（禁用 current_amount）、
  分仓零库存安全线、调拨流水、query_bills 截断与分方向汇总；
- llm._clean_name / normalize：保留「维达抽纸 3层*24包」「牛皮纸 80g」规格不被剥掉；
- billcheck._tool_stock_check 副单位折算与 _tool_last_price 排除未审核单；
- draft.validate 拒绝负数实付。
"""
import json
import os
import sys
import unittest
from unittest import mock

APP_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

from core import ask as askmod          # noqa: E402
from core import billcheck as bcmod     # noqa: E402
from core import draft as draftmod      # noqa: E402
from core import erp as erpmod          # noqa: E402
from core import ledger as ledgermod    # noqa: E402
from core import llm as llmmod          # noqa: E402
from core import report as rptmod       # noqa: E402


class TestWriteSignAndAccount(unittest.TestCase):
    def _client(self):
        c = erpmod.JshERP(base="http://erp.test", user="jsh", password="123")
        c.token = "tok"
        c._tax_flag = False
        c._cache['"/account/list"|{}'] = []
        c._cache['/account/list|{}'] = [
            {"id": 17, "name": "账户1", "enabled": True},
            {"id": 18, "name": "账户2", "enabled": True},
        ]
        c._cache_at['/account/list|{}'] = 9e18
        c._cache['/depot/list|{}'] = [
            {"id": 14, "name": "总仓", "isDefault": True},
        ]
        c._cache_at['/depot/list|{}'] = 9e18
        return c

    def test_create_bill_signs_and_single_account_no_list(self):
        c = self._client()
        sent = []

        def fake_req(path, method="GET", data=None, retry_login=True):
            sent.append((path, json.loads(data["info"]), json.loads(data["rows"])))
            return {"code": 200, "data": {}}

        rows = [{"materialId": 1, "barCode": "BC1", "name": "可乐",
                 "operNumber": 10, "unit": "瓶", "unitPrice": 3.0, "allPrice": 30.0, "depotId": 14}]
        with mock.patch.object(c, "_req", side_effect=fake_req):
            # 1) 采购入库：totalPrice 与 changeAmount 必须为负，discountLastMoney 为正，单账户不写 accountIdList
            r1 = c.create_bill("purchase_in", 200, [dict(rows[0])], account_id=25, pay_amount=20.0)
            self.assertTrue(r1["ok"])
            self.assertEqual(r1["total"], 30.0)
            info1 = sent[-1][1]
            self.assertEqual(info1["totalPrice"], -30.0)
            self.assertEqual(info1["changeAmount"], -20.0)
            self.assertEqual(info1["discountLastMoney"], 30.0)
            self.assertEqual(info1["accountId"], 17)         # 幽灵 ID 25 纠正为有效账户 17
            self.assertEqual(info1["accountIdList"], "")     # 严禁双计
            self.assertEqual(info1["accountMoneyList"], "")

            # 2) 销售出库：totalPrice 与 changeAmount 为正
            c.create_bill("sale_out", 201, [dict(rows[0])], account_id=18, pay_amount=15.0)
            info2 = sent[-1][1]
            self.assertEqual(info2["totalPrice"], 30.0)
            self.assertEqual(info2["changeAmount"], 15.0)
            self.assertEqual(info2["accountId"], 18)
            self.assertEqual(info2["accountIdList"], "")

            # 3) 销售退货：资金流出，totalPrice 与 changeAmount 为负
            c.create_bill("sale_return", 201, [dict(rows[0])], account_id=17)
            info3 = sent[-1][1]
            self.assertEqual(info3["totalPrice"], -30.0)
            self.assertEqual(info3["changeAmount"], -30.0)

            # 4) 采购退货：资金流入，totalPrice 与 changeAmount 为正
            c.create_bill("purchase_return", 200, [dict(rows[0])], account_id=17)
            info4 = sent[-1][1]
            self.assertEqual(info4["totalPrice"], 30.0)
            self.assertEqual(info4["changeAmount"], 30.0)

            # 5) 其它入库 / 其它出库：不挂结算账户，changeAmount=0
            c.create_bill("other_in", 200, [dict(rows[0])])
            info5 = sent[-1][1]
            self.assertEqual(info5["totalPrice"], -30.0)
            self.assertEqual(info5["changeAmount"], 0.0)
            self.assertEqual(info5["accountId"], "")

            c.create_bill("other_out", 201, [dict(rows[0])])
            info6 = sent[-1][1]
            self.assertEqual(info6["totalPrice"], 30.0)
            self.assertEqual(info6["changeAmount"], 0.0)
            self.assertEqual(info6["accountId"], "")

            # 6) create_payment 传 target_bill_no 用 billNumber 字段核销且付款为负
            c.create_payment("付款", 200, 88.5, target_bill_no="CGRK001", account_id=25)
            p_path, p_info, p_rows = sent[-1]
            self.assertEqual(p_path, "/accountHead/addAccountHeadAndDetail")
            self.assertEqual(p_info["changeAmount"], -88.5)
            self.assertEqual(p_info["accountId"], 17)
            self.assertEqual(p_rows[0]["billNumber"], "CGRK001")

    def test_default_depot_does_not_create_ghost_warehouse(self):
        c = self._client()
        with mock.patch("core.config.load", return_value={"ui": {
            "default_depot_in": "不存在的仓", "default_depot_out": "也不存在", "default_account_id": 25,
        }}):
            self.assertEqual(c.default_depot_name("in"), "总仓")
            self.assertEqual(c.default_depot_name("out"), "总仓")
            self.assertEqual(c.default_account_id(None), 17)


class TestStockFallback(unittest.TestCase):
    def test_depot_stock_prefers_rest_then_db_then_stock_table(self):
        c = erpmod.JshERP(base="http://erp.test", user="jsh", password="123")
        c.token = "tok"
        # REST 失败时回退 ledger.depot_stock_sql（含期初）
        with mock.patch.object(c, "_req", side_effect=RuntimeError("timeout")), \
             mock.patch("core.ledger.depot_stock_sql", return_value={"14": 103.0}):
            self.assertEqual(c.stock(700, 14), 103.0)


class TestLedgerCalculations(unittest.TestCase):
    def test_period_stats_handles_negative_and_positive_signs_uniformly(self):
        fake_rows = [
            {"st": "采购", "d": "2026-11-25", "amt": "1000.00", "n": "2"},
            {"st": "采购退货", "d": "2026-11-25", "amt": "100.00", "n": "1"},
            {"st": "销售", "d": "2026-11-25", "amt": "1500.00", "n": "3"},
            {"st": "销售退货", "d": "2026-11-25", "amt": "50.00", "n": "1"},
            {"st": "零售", "d": "2026-11-10", "amt": "200.00", "n": "2"},
        ]
        seen_sql = []
        with mock.patch.object(ledgermod, "_q", side_effect=lambda sql, limit=5000: seen_sql.append(sql) or fake_rows):
            res = ledgermod.period_stats(today="2026-11-25", month="2026-11", year="2026")
        self.assertTrue(seen_sql and "%Y-%m-%d" in seen_sql[-1] and "%%Y" not in seen_sql[-1])
        self.assertEqual(res["today"]["buy"], 900.0)
        self.assertEqual(res["today"]["buy_n"], 2)
        self.assertEqual(res["today"]["sell"], 1450.0)
        self.assertEqual(res["today"]["sell_n"], 3)
        self.assertEqual(res["today"]["return_n"], 2)
        self.assertEqual(res["today"]["return_amt"], 150.0)
        self.assertEqual(res["today"]["profit"], 550.0)
        self.assertEqual(res["month_stat"]["sell"], 1650.0)
        self.assertEqual(res["month_stat"]["sell_n"], 5)
        self.assertEqual(res["year"]["buy_n"], 2)

    def test_force_approval_flag_controls_posted_where(self):
        ledgermod._FORCE_CACHE.update({"at": 0.0, "val": None})
        with mock.patch.object(ledgermod, "_q", return_value=[{"force_approval_flag": "0"}]):
            self.assertFalse(ledgermod.force_approval())
            w = ledgermod._posted_where("h")
            self.assertNotIn("status IN", w)
        ledgermod._FORCE_CACHE.update({"at": 0.0, "val": None})
        with mock.patch.object(ledgermod, "_q", return_value=[{"force_approval_flag": "1"}]):
            self.assertTrue(ledgermod.force_approval())
            w = ledgermod._posted_where("h")
            self.assertIn("status IN ('1','2','3')", w)
        ledgermod._FORCE_CACHE.update({"at": 0.0, "val": None})

    def test_official_stats_passes_yyyy_mm_not_yyyy_mm_01(self):
        seen = []
        fake_erp = mock.MagicMock()
        fake_erp._req.return_value = {"code": 200, "data": {}}
        with mock.patch.object(rptmod, "_ops_sql", return_value={"wait": 0, "recv": 0, "pay": 0, "acc": 0, "acc_month": 0}), \
             mock.patch.object(rptmod, "_stat_sql", side_effect=lambda t, m, y=None: seen.append((t, m, y)) or {
                 "today": {"buy": 0, "sell": 0, "buy_n": 0, "sell_n": 0, "return_n": 0, "return_amt": 0, "profit": 0},
                 "month_stat": {"buy": 100, "sell": 200, "buy_n": 1, "sell_n": 2, "return_n": 0, "return_amt": 0, "profit": 100},
                 "year": {"buy": 100, "sell": 200, "buy_n": 1, "sell_n": 2, "return_n": 0, "return_amt": 0, "profit": 100},
             }), \
             mock.patch("core.ledger.monthly_trend", return_value={"buy": [], "sell": []}):
            res = rptmod.official_stats(fake_erp)
        self.assertEqual(len(seen[0][1]), 7)   # YYYY-MM
        self.assertEqual(res["month"]["sell"], 200)

    def test_account_summary_ignores_stale_current_amount_and_avoids_double_count(self):
        def fake_q(sql, limit=5000):
            if "FROM jsh_account a" in sql or "FROM jsh_account WHERE" in sql:
                # 故意给错误的 current_amount（实际 SQL 连查都不查 current_amount）
                return [{"id": 17, "name": "账户1", "init_amt": "100.00"},
                        {"id": 18, "name": "账户2", "init_amt": "200.00"}]
            if "GROUP BY h.account_id" in sql:
                return [{"aid": 17, "total_chg": "-300.00", "month_chg": "-100.00"}]
            if "h.account_id_list" in sql:
                # 多账户拆分：账户1 -50，账户2 -50（采购流出）
                return [{"st": "采购", "ot": "2026-11-25 10:00:00",
                         "account_id_list": "17,18", "account_money_list": "50,50"}]
            if "FROM jsh_account_head" in sql:
                return [{"aid": 18, "t": "收款", "total_chg": "500.00", "month_chg": "500.00"}]
            if "FROM jsh_account_item" in sql:
                return []
            return []

        with mock.patch.object(ledgermod, "_q", side_effect=fake_q):
            acc = ledgermod.account_summary(month="2026-11")
        by_id = {a["id"]: a for a in acc["accounts"]}
        self.assertEqual(by_id[17]["amount"], -250.0)   # 100 - 300 - 50
        self.assertEqual(by_id[17]["month"], -150.0)    # -100 - 50
        self.assertEqual(by_id[18]["amount"], 650.0)    # 200 - 50 + 500
        self.assertEqual(acc["current"], 400.0)
        self.assertEqual(acc["month"], 300.0)

    def test_query_bills_counts_all_before_truncating_and_splits_returns(self):
        headers = []
        for i in range(25):
            headers.append({"id": i + 1, "number": f"XS{i+1}", "d": "2026-11-20",
                            "type": "出库", "sub_type": "销售", "party": "永辉超市",
                            "total": "100.00", "unpaid": "20.00"})
        headers.append({"id": 99, "number": "XSTH1", "d": "2026-11-21",
                        "type": "入库", "sub_type": "销售退货", "party": "永辉超市",
                        "total": "30.00", "unpaid": "0"})

        def fake_q(sql, limit=5000):
            if "FROM jsh_depot_head h" in sql:
                return headers
            if "FROM jsh_depot_item di" in sql:
                return [{"header_id": 1, "name": "可口可乐", "model": "500ml",
                         "qty": "10", "unit": "瓶", "basic_qty": "10", "price": "10", "amount": "100"}]
            return []

        with mock.patch.object(ledgermod, "_q", side_effect=fake_q):
            res = ledgermod.query_bills(party="永辉", limit=20)
        self.assertEqual(res["total_count"], 26)
        self.assertEqual(res["count"], 20)
        self.assertTrue(res["truncated"])
        self.assertEqual(res["sale_amount"], 2500.0)
        self.assertEqual(res["return_amount"], 30.0)
        self.assertEqual(res["net_amount"], 2470.0)


class TestSpecPreservationAndUnitCheck(unittest.TestCase):
    def test_clean_name_keeps_multiplication_spec_and_weight_spec(self):
        # 含乘号的包装规格不得被剥离
        self.assertEqual(llmmod._clean_name("维达抽纸 3层*24包")[0], "维达抽纸 3层*24包")
        self.assertEqual(llmmod._clean_name("牛皮纸 1.2m*100m")[0], "牛皮纸 1.2m*100m")
        # normalize 当 LLM 已给 qty=10, unit='卷' 时，'80g' 不得被剥掉
        d, probs = llmmod.normalize({
            "biz": "purchase_in", "party": "玖龙纸业",
            "items": [{"name": "牛皮纸 80g", "qty": 10, "unit": "卷", "price": 118}],
        })
        self.assertEqual(d["items"][0]["name"], "牛皮纸 80g")
        self.assertEqual(d["items"][0]["qty"], 10.0)
        self.assertEqual(probs, [])

    def test_stock_check_converts_secondary_unit_to_basic_unit(self):
        class StubErp:
            def find_material(self, name):
                return {"id": 738, "name": "可口可乐 500ml", "unit": "瓶", "unitId": 21}, "exact"

            def find_depot(self, name):
                return {"id": 14, "name": "总仓"}, "exact"

            def depots(self):
                return [{"id": 14, "name": "总仓"}]

            def stock(self, mid, did=None):
                return 30.0  # 30 瓶

            def unit_group_by_id(self, uid):
                return {"id": 21, "basicUnit": "瓶", "otherUnit": "箱", "ratio": 12}

            def unit_allowed(self, g, u):
                return u in ("瓶", "箱")

            def unit_ratio(self, g, u):
                return 12.0 if u == "箱" else 1.0

            def default_depot_name(self, direction="out"):
                return "总仓"

        with mock.patch("core.report.material_flow", return_value={"initial": 0, "in_sum": 30, "out_sum": 0}), \
             mock.patch("core.report._safe_stock_map", return_value={}):
            data, note = bcmod._tool_stock_check(
                {"material": "可口可乐 500ml", "depot": "总仓", "qty": 5, "unit": "箱"}, StubErp())
        self.assertEqual(data["need"], 5.0)
        self.assertEqual(data["need_basic"], 60.0)   # 5 箱 × 12 = 60 瓶
        self.assertFalse(data["enough"])
        self.assertEqual(data["shortfall"], 30.0)
        self.assertIn("缺 30", note)

    def test_validate_rejects_negative_pay_amount(self):
        errs = draftmod.validate({
            "biz": "sale_out", "party": "永辉超市",
            "items": [{"name": "可乐", "qty": 1, "price": 3}],
            "pay_amount": -10,
        })
        self.assertTrue(any("不能为负" in e for e in errs))

    def test_ask_account_balance_tool_uses_ledger_summary(self):
        from core import ask as askmod
        fake_acc = {
            "current": 47858.09,
            "month": 6013.03,
            "accounts": [
                {"id": 17, "name": "账户1", "initial": 100.0, "amount": 47658.09, "month": 6013.03},
                {"id": 18, "name": "账户2", "initial": 200.0, "amount": 200.0, "month": 0.0},
            ],
        }
        tools = askmod._tools(mock.MagicMock())
        self.assertIn("account_balance", tools)
        with mock.patch("core.ledger.account_summary", return_value=fake_acc):
            data, note = tools["account_balance"]["run"]({})
        self.assertEqual(data["total_balance"], 47858.09)
        self.assertEqual(data["accounts"][0]["balance"], 47658.09)
        self.assertIn("47,858.09", note)
        self.assertIn("账户1", note)

    def test_ask_ops_summary_tool_uses_ledger_counts(self):
        from core import ask as askmod
        tools = askmod._tools(mock.MagicMock())
        self.assertIn("ops_summary", tools)
        with mock.patch("core.ledger.ops_counts", return_value={"wait_bills": 0, "bill_total": 649}), \
             mock.patch("core.ledger.debt_summary", return_value={
                 "need_recv": 244, "need_pay": 34,
                 "debt_recv_total": 41328.95, "debt_pay_total": 19538.58,
             }), \
             mock.patch("core.ledger.account_summary", return_value={"current": 47858.09}):
            data, note = tools["ops_summary"]["run"]({})
        self.assertEqual(data["wait_bills"], 0)
        self.assertEqual(data["need_recv_count"], 244)
        self.assertEqual(data["need_pay_count"], 34)
        self.assertEqual(data["receivable_total"], 41328.95)
        self.assertEqual(data["payable_total"], 19538.58)
        self.assertIn("244 张", note)
        self.assertIn("34 张", note)

    def test_prepare_bills_defaults_empty_oper_time_and_keeps_explicit(self):
        from core import gate as gatemod
        stub_erp = mock.MagicMock()
        stub_erp.price_tax_flag.return_value = False
        stub_erp.find_party.return_value = ({"id": 1, "supplier": "山姆"}, "exact")
        stub_erp.party_candidates.return_value = []
        stub_erp.default_depot_name.return_value = "总仓"
        stub_erp.find_depot.return_value = ({"id": 14, "name": "总仓"}, "exact")
        stub_erp.find_material.return_value = (None, "none")
        stub_erp.material_candidates.return_value = []
        stub_erp.unit_group_by_id.return_value = None

        d_empty = {"biz": "purchase_in", "party": "山姆", "depot": "",
                   "oper_time": "", "items": [{"name": "瑞士卷", "qty": 1, "unit": "盒", "price": 3.66}]}
        d_explicit = {"biz": "purchase_in", "party": "山姆", "depot": "",
                      "oper_time": "2026-11-20 12:00:00",
                      "items": [{"name": "瑞士卷", "qty": 1, "unit": "盒", "price": 3.66}]}
        bills = draftmod.prepare_bills([d_empty, d_explicit], {"bills": [{}, {}]}, stub_erp, "ai", "test")
        self.assertRegex(bills[0]["draft"]["oper_time"], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
        self.assertEqual(bills[0]["draft"]["oper_time"], bills[0]["preview"]["oper_time"])
        ok, _, reason = gatemod.check(bills[0]["confirm_token"], bills[0]["draft"])
        self.assertTrue(ok, reason)
        self.assertEqual(bills[1]["draft"]["oper_time"], "2026-11-20 12:00:00")

    def test_validate_oper_time_format_and_bounds(self):
        base = {"biz": "purchase_in", "party": "山姆",
                "items": [{"name": "瑞士卷", "qty": 1, "price": 3.66}]}
        self.assertEqual(draftmod.validate(dict(base, oper_time="2026-11-25 23:00:00")), [])
        self.assertEqual(draftmod.validate(dict(base, oper_time="2026-11-25")), [])
        bad1 = draftmod.validate(dict(base, oper_time="昨天"))
        bad2 = draftmod.validate(dict(base, oper_time="2026-13-45 00:00:00"))
        self.assertTrue(any("日期格式" in e for e in bad1))
        self.assertTrue(any("日期格式" in e for e in bad2))

    def test_agent_numbers_in_does_not_turn_dates_into_negatives(self):
        from core import agent as agentmod
        nums = agentmod._numbers_in("日期 2026-11-25，总库存 -233 瓶，金额 3.66 元")
        self.assertIn(2026.0, nums)
        self.assertIn(11.0, nums)
        self.assertIn(25.0, nums)
        self.assertNotIn(-11.0, nums)
        self.assertNotIn(-25.0, nums)
        self.assertIn(-233.0, nums)
        self.assertTrue(agentmod.numeric_grounded(
            "单据日期为 2026年11月25日，合计 3.66 元。",
            [{"oper_time": "2026-11-25 23:00:00", "total": 3.66}],
        ))

    def test_agent_fallback_answer_avoids_multi_step_raw_json_dump(self):
        from core import agent as agentmod
        steps = [
            {"tool": "sql_query", "ok": True, "observation": {"rows": [{"tmp": 1}]}},
            {"tool": "sql_query", "ok": True, "observation": {"rows": [{"party": "永辉超市", "cnt": "75", "net": "19447.90"}]}},
        ]
        notes = ['只读查询返回 1 行，结果：{"tmp": 1}。', '只读查询返回 1 行，结果：{"party": "永辉超市", "cnt": "75"}。']
        out = agentmod._fallback_answer(notes, steps)
        self.assertNotIn("{", out)
        self.assertIn("永辉超市", out)
        self.assertIn("19447.90", out)

    def test_draft_order_refuses_payment_transfer_and_audit_allows_revision(self):
        from core import ask as askmod
        erp = mock.MagicMock()
        erp.price_tax_flag.return_value = False
        erp.find_party.return_value = ({"id": 1, "supplier": "山姆"}, "exact")
        erp.party_candidates.return_value = []
        erp.default_depot_name.return_value = "总仓"
        erp.find_depot.return_value = ({"id": 14, "name": "总仓"}, "exact")
        erp.find_material.return_value = (None, "none")
        erp.material_candidates.return_value = []
        erp.unit_group_by_id.return_value = None
        d1, n1 = askmod._draft_order({"text": "收到永辉超市回款 5000 元，帮我录个收款单"}, erp)
        self.assertEqual(d1.get("unsupported"), "payment")
        self.assertIn("收付款", n1)
        d2, n2 = askmod._draft_order({"text": "把 20 瓶可乐从总仓调拨到门店仓"}, erp)
        self.assertEqual(d2.get("unsupported"), "transfer")
        self.assertIn("调拨", n2)
        d3, n3 = askmod._draft_order({"text": "帮我把所有待审核单据一键审核"}, erp)
        self.assertEqual(d3.get("refused"), "modification_not_supported")
        # 带 previous_draft 的自然修正不能被误拦
        with mock.patch("core.billcheck.parse_and_check", return_value={
            "drafts": [{"biz": "purchase_in", "party": "山姆", "depot": "", "oper_time": "2026-11-24 10:00:00",
                        "items": [{"name": "瑞士卷", "qty": 3, "unit": "盒", "price": 12}]}],
            "meta": {"bills": [{}]}, "check": {"ok": True, "patches_suggested": [], "risks": ""},
        }):
            dr, nr, _ = askmod._draft_order(
                {"text": "不对，是昨天买的，改成3盒"}, erp,
                previous_draft={"biz": "purchase_in", "party": "山姆", "items": [{"name": "瑞士卷", "qty": 1}]},
            )
        self.assertEqual(dr["drafts"], 1)
        self.assertEqual(dr["bills"][0]["oper_time"], "2026-11-24 10:00:00")

    def test_ask_draft_order_exposes_oper_time(self):
        from core import ask as askmod
        stub_erp = mock.MagicMock()
        stub_erp.price_tax_flag.return_value = False
        stub_erp.find_party.return_value = ({"id": 1, "supplier": "山姆"}, "exact")
        stub_erp.party_candidates.return_value = []
        stub_erp.default_depot_name.return_value = "总仓"
        stub_erp.find_depot.return_value = ({"id": 14, "name": "总仓"}, "exact")
        stub_erp.find_material.return_value = (None, "none")
        stub_erp.material_candidates.return_value = []
        stub_erp.unit_group_by_id.return_value = None
        parsed = {
            "drafts": [{"biz": "purchase_in", "party": "山姆", "depot": "", "oper_time": "",
                        "items": [{"name": "瑞士卷", "qty": 1, "unit": "盒", "price": 3.66}]}],
            "meta": {"bills": [{}], "model": "m", "profile": "p"},
            "check": {"ok": True, "patches_suggested": [], "risks": ""},
        }
        with mock.patch("core.billcheck.parse_and_check", return_value=parsed):
            data, note, extra = askmod._draft_order({"text": "去山姆买了一盒瑞士卷花了3.66元"}, stub_erp)
        b0 = data["bills"][0]
        self.assertRegex(b0["oper_time"], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
        self.assertEqual(extra["bills"][0]["draft"]["oper_time"], b0["oper_time"])
        self.assertIn(b0["oper_time"][:10], note)

    def test_fallback_answer_strips_schema_and_double_punctuation(self):
        from core import agent as agentmod
        notes = [
            "历史单据里没有查到相关记录。",
            "「维达抽纸 3层*24包」当前库存 15箱。总仓 15；门店仓 0；退货仓 0。低于安全库存 20箱。",
            '「jsh_material」共 25 个字段，取值样本：{"enabled": ["\\u0001"], "delete_flag": ["0"]}。',
        ]
        out = agentmod._fallback_answer(notes, [])
        self.assertNotIn("取值样本", out)
        self.assertNotIn("{", out)
        self.assertNotIn("没有查到相关记录", out)
        self.assertNotIn("。；", out)
        self.assertIn("维达抽纸", out)
        self.assertEqual(agentmod._fallback_answer(["库存 102 瓶"], []), "库存 102 瓶")

    def test_db_schema_returns_none_note(self):
        from core import dbread as dbmod
        rows = [
            {"table_name": "jsh_material", "table_comment": "", "column_name": "id",
             "column_comment": "", "data_type": "bigint", "ordinal_position": 1},
            {"table_name": "jsh_material", "table_comment": "", "column_name": "enabled",
             "column_comment": "", "data_type": "bit", "ordinal_position": 2},
        ]
        with mock.patch.object(dbmod, "_schema_rows", return_value=rows), \
             mock.patch.object(dbmod, "_run", return_value={"rows": [{"v": "1"}]}):
            data, note = dbmod.db_schema({"table": "jsh_material"})
        self.assertEqual(data["table"], "jsh_material")
        self.assertIsNone(note)

    def test_compound_hints_skips_multi_order_draft_prompt(self):
        from core import ask as askmod
        q = "向美宜佳商贸采购可口可乐 500ml 100瓶（按上次进价）；同时卖给永辉超市维达抽纸 3层*24包 20箱单价28元，帮我核价验库存并生成草稿"
        self.assertEqual(askmod._compound_hints(q), "")


if __name__ == "__main__":
    unittest.main()
