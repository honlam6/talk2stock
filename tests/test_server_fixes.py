#!/usr/bin/env python3
"""服务端与边界回归单测（2026-11-25 审计修复项）。

覆盖：
- /api/llm/save 与 /api/my/model 保留 vision 布尔标志；
- /api/sheet/map 正确初始化 erp 再传给 repair_transform_result（不再静默 NameError）；
- /api/commit 与 /api/commit/batch 成功后同步失效 report/procurement/dedupe/daily 缓存并记录习惯；
- /api/history 超过 64KB 逆序分块边界时不丢记录、不截断行；
- /api/health 无需登录直接返回 200。
"""
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

APP_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

import server as srvmod                    # noqa: E402
from core import auth as authmod           # noqa: E402
from core import config as cfgmod          # noqa: E402
from core import daily as dailymod         # noqa: E402
from core import dedupe as dedupemod       # noqa: E402
from core import draft as draftmod         # noqa: E402
from core import gate as gatemod           # noqa: E402
from core import habits as habitsmod       # noqa: E402
from core import llm as llmmod             # noqa: E402
from core import procurement as pmod       # noqa: E402
from core import repair as repairmod       # noqa: E402
from core import report as rptmod          # noqa: E402
from core import sheet as sheetmod         # noqa: E402


class _FakeHandler(srvmod.Handler):
    """用于测试 Handler.do_GET（/api/health 免鉴权直返）。"""

    def __init__(self, path="/"):
        self.command = "GET"
        self.path = path
        self.headers = {}
        self.rfile = io.BytesIO(b"")
        self.wfile = io.BytesIO()
        self.status_code = None
        self.resp_headers = {}

    def send_response(self, code, message=None):
        self.status_code = code

    def send_header(self, k, v):
        self.resp_headers[k] = v

    def end_headers(self):
        pass

    def log_message(self, *a):
        pass

    def json_body(self):
        return json.loads(self.wfile.getvalue().decode("utf-8"))


class TestServerFixes(unittest.TestCase):
    def test_health_endpoint_no_auth(self):
        h = _FakeHandler("/api/health")
        with mock.patch.object(cfgmod, "require_auth", return_value=True):
            h.do_GET()
        self.assertEqual(h.status_code, 200)
        res = h.json_body()
        self.assertTrue(res["ok"])

    def test_llm_save_preserves_vision_flag(self):
        captured = {}

        def fake_upsert(key, prof):
            captured[key] = dict(prof)

        with mock.patch.object(cfgmod, "upsert_profile", side_effect=fake_upsert), \
             mock.patch.object(srvmod, "api_llm_get", return_value={"profiles": {}}):
            res = srvmod.api_llm_save("vlm-test", {
                "name": "vlm-test", "base_url": "https://x.test/v1", "api_key": "sk-1",
                "model": "gpt-4o", "vision": True, "activate": False,
            })
        self.assertTrue(res["ok"])
        self.assertTrue(captured["vlm-test"]["vision"])

    def test_my_model_preserves_vision_flag(self):
        captured = {}

        def fake_save(login, key, prof):
            captured[key] = dict(prof)
            return True, ""

        with mock.patch.object(cfgmod, "current_request_user", return_value="alice"), \
             mock.patch.object(authmod, "user_profiles", return_value={"profiles": {}, "active": ""}), \
             mock.patch.object(authmod, "save_user_profile", side_effect=fake_save), \
             mock.patch.object(srvmod, "_my_model_view", return_value={"mine": {}, "shared": None, "role": "member"}):
            res = srvmod.api_my_model_post({}, {
                "action": "save", "key": "myvlm",
                "profile": {"name": "我的视觉", "base_url": "https://x.test/v1",
                            "api_key": "sk-1", "model": "vl-1", "vision": True},
            })
        self.assertTrue(res["ok"])
        self.assertTrue(captured["myvlm"]["vision"])

    def test_sheet_map_passes_real_erp_to_repair(self):
        """/api/sheet/map 触发修复 Agent 时必须先初始化 erp 再传入 repair_transform_result。"""
        seen_erp = []
        fake_erp = object()
        fake_tr = {"ok": True, "drafts": [{"row_no": 2, "problems": ["缺商品名"], "draft": {}}],
                   "skipped": [], "total_rows": 2}

        def fake_repair(tr, erp, operator=""):
            seen_erp.append(erp)
            return tr, {"ok": True, "applied": [], "suggestions": [], "steps": []}

        with mock.patch.object(llmmod, "is_configured", return_value=True), \
             mock.patch.object(cfgmod, "agent_profile", return_value={"model": "m"}), \
             mock.patch.object(srvmod, "_load_tmp_sheet",
                               return_value=({"name": "Sheet1", "rows": [["商品", "数量"], ["", "5"]]}, None)), \
             mock.patch.object(sheetmod, "grid_desc", return_value="grid"), \
             mock.patch.object(llmmod, "map_sheet", return_value={"cols": {"0": "name"}}), \
             mock.patch.object(sheetmod, "transform", return_value=fake_tr), \
             mock.patch.object(sheetmod, "failure_samples", return_value=[]), \
             mock.patch.object(srvmod, "_erp", return_value=fake_erp), \
             mock.patch.object(repairmod, "repair_transform_result", side_effect=fake_repair):
            res = srvmod.api_sheet_map({}, {"ref": "tmp1", "sheet": "Sheet1"})
        self.assertTrue(res["ok"])
        self.assertEqual(seen_erp, [fake_erp])
        self.assertNotIn("error", res["repair"])

    def test_commit_and_batch_invalidate_all_caches_and_record_habits(self):
        draft = {"biz": "purchase_in", "party": "美宜佳", "depot": "原料仓",
                 "items": [{"name": "可乐", "qty": 10, "unit": "瓶", "price": 3.5}]}
        commit_res = {"ok": True, "number": "CG001", "total": 35.0, "biz_label": "采购入库"}
        fake_erp = mock.MagicMock()
        with mock.patch.object(gatemod, "check", return_value=(True, {"source": "ai", "raw_text": "买可乐"}, "")), \
             mock.patch.object(gatemod, "log_write"), \
             mock.patch.object(draftmod, "commit", return_value=commit_res), \
             mock.patch.object(srvmod, "_erp", return_value=fake_erp), \
             mock.patch.object(srvmod, "_erp_operator_log"), \
             mock.patch.object(habitsmod, "record") as m_hb, \
             mock.patch.object(rptmod, "invalidate") as m_rpt, \
             mock.patch.object(dailymod, "invalidate") as m_day, \
             mock.patch.object(pmod, "invalidate") as m_proc, \
             mock.patch.object(dedupemod, "invalidate") as m_dup:
            r1 = srvmod.api_commit({}, {"draft": draft, "confirm_token": "tok", "operator": "admin"})
            self.assertTrue(r1["ok"])
            self.assertEqual(m_hb.call_count, 1)
            self.assertEqual(m_rpt.call_count, 1)
            self.assertEqual(m_day.call_count, 1)
            self.assertEqual(m_proc.call_count, 1)
            self.assertEqual(m_dup.call_count, 1)

            r2 = srvmod.api_commit_batch({}, {
                "items": [{"draft": draft, "confirm_token": "tok2"}], "operator": "admin"
            })
            self.assertTrue(r2["ok"])
            self.assertEqual(m_hb.call_count, 2)
            self.assertEqual(m_rpt.call_count, 2)
            self.assertEqual(m_day.call_count, 2)
            self.assertEqual(m_proc.call_count, 2)
            self.assertEqual(m_dup.call_count, 2)

    def test_history_chunk_boundary_over_64kb(self):
        """audit.jsonl 超过 64KB 时，逆序读块跨边界不得丢行或断行。"""
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "audit.jsonl")
            # 写 150 条 ~600B 的记录（总计 ~90KB > 64KB chunk），验证跨块边界连续性
            with open(p, "w", encoding="utf-8") as f:
                for i in range(150):
                    rec = {
                        "event": "commit_ok", "ts": f"2026-11-25 10:00:{i:02d}",
                        "source": "ai", "raw_text": "x" * 520,
                        "draft": {"party": "美宜佳", "depot": "原料仓", "items": []},
                        "result": {"number": f"CG{i:04d}", "total": i, "biz_label": "采购入库"},
                    }
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            with mock.patch.object(cfgmod, "DATA_DIR", td):
                res = srvmod.api_history({"limit": ["100"]}, {})
            self.assertTrue(res["ok"])
            self.assertEqual(len(res["items"]), 100)
            nums = [x["number"] for x in res["items"]]
            self.assertEqual(nums, [f"CG{i:04d}" for i in range(149, 49, -1)])


if __name__ == "__main__":
    unittest.main()
