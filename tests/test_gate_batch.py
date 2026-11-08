#!/usr/bin/env python3
"""批量闸门与端点单测：
- 令牌按单独立：篡改一张拒一张，其他照写（批量下 C1-C4 不变量成立）
- /api/commit/batch：顺序提交、summary 统计、审计逐单落盘（batch_id 串联）
- 视觉：parse_images 消息构造（一次调用带全部图）、能力位路由、mime 嗅探
- config：vision 能力位保存/继承
"""
import base64
import importlib
import json
import os
import sys
import tempfile
import unittest

APP_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

from core import config as cfgmod   # noqa: E402


def _draft(party="美宜佳", name="可乐", qty=3, price=3.5, biz="purchase_in"):
    return {"biz": biz, "party": party, "depot": "", "items": [
        {"name": name, "qty": qty, "unit": "瓶", "price": price}],
        "oper_time": "", "remark": "", "pay_amount": None}


class _TempData:
    """gate/审计隔离到临时目录，避免污染真实 used_tokens / audit。"""
    def __enter__(self):
        self.td = tempfile.TemporaryDirectory()
        self._orig = cfgmod.data_dir
        cfgmod.data_dir = lambda: self.td.name
        return self.td.name

    def __exit__(self, *a):
        cfgmod.data_dir = self._orig


class TestGatePerBill(unittest.TestCase):
    def setUp(self):
        self._td = _TempData()
        self.datadir = self._td.__enter__()
        import core.gate as gate
        importlib.reload(gate)
        self.gate = gate

    def tearDown(self):
        self._td.__exit__()
        import core.gate as gate
        importlib.reload(gate)

    def test_each_bill_own_token(self):
        a, b = _draft(), _draft(party="张记", biz="sale_out")
        ta = self.gate.issue(a, {"source": "ai"})
        tb = self.gate.issue(b, {"source": "ai"})
        self.assertNotEqual(ta, tb)
        ok1, _, _ = self.gate.check(ta, a)
        ok2, _, reason = self.gate.check(ta, b)     # A 的令牌配 B 的内容 → 拒
        self.assertTrue(ok1)
        self.assertFalse(ok2)
        self.assertIn("改动", reason)

    def test_replay_blocked_per_bill(self):
        a = _draft()
        t = self.gate.issue(a)
        ok1, _, _ = self.gate.check(t, a)
        ok2, _, reason = self.gate.check(t, a)      # 一次性：重放被拒
        self.assertTrue(ok1)
        self.assertFalse(ok2)
        self.assertIn("已被使用", reason)


class TestCommitBatch(unittest.TestCase):
    def setUp(self):
        self._td = _TempData()
        self.datadir = self._td.__enter__()
        import core.gate as gate
        importlib.reload(gate)
        self.gate = gate
        import server as srv
        importlib.reload(srv)
        self.srv = srv
        # 打桩：不连 ERP、不真写；按调用顺序回单号
        self.committed = []
        self.srv.draftmod = type("DM", (), {})()
        self.srv.draftmod.commit = lambda d, erp=None: (
            self.committed.append(d["party"]) or
            {"ok": True, "number": "N%d" % len(self.committed), "total": 10,
             "biz_label": "测试", "created": ["商品「%s」" % d["items"][0]["name"]]})
        self.srv._erp = lambda: type("E", (), {"invalidate": lambda self, x="": None})()

    def tearDown(self):
        self._td.__exit__()
        import core.gate as gate
        importlib.reload(gate)

    def test_mixed_batch_gate_and_summary(self):
        a, b = _draft(), _draft(party="张记", name="纸箱", biz="sale_out")
        ta = self.gate.issue(a)
        tb = self.gate.issue(b)
        tampered = dict(b, party="篡改方")
        r = self.srv.api_commit_batch(None, {"items": [
            {"draft": a, "confirm_token": ta},
            {"draft": tampered, "confirm_token": tb},      # 篡改 → 拒
            {"draft": b},                                   # 缺令牌 → 拒
        ], "source": "ai", "raw_text": "批量", "_operator": "tester"})
        self.assertEqual(r["summary"], {"total": 3, "ok": 1, "fail": 2})
        self.assertTrue(r["results"][0]["ok"])
        self.assertFalse(r["results"][1]["ok"])
        self.assertTrue(r["results"][1].get("need_confirm"))
        self.assertFalse(r["results"][2]["ok"])
        self.assertEqual(self.committed, ["美宜佳"])       # 顺序执行；只有第一张真写

        # 审计逐单落盘：commit_ok 1 条 + commit_rejected 2 条，batch_id 串联
        with open(os.path.join(self.datadir, "audit.jsonl"), encoding="utf-8") as f:
            recs = [json.loads(l) for l in f if l.strip()]
        evs = [rec["event"] for rec in recs]
        self.assertEqual(evs.count("commit_ok"), 1)
        self.assertEqual(evs.count("commit_rejected"), 2)
        ok_rec = next(rec for rec in recs if rec["event"] == "commit_ok")
        self.assertEqual(ok_rec["operator"], "tester")
        self.assertEqual(ok_rec["result"]["number"], "N1")

    def test_empty_and_oversize(self):
        r1 = self.srv.api_commit_batch(None, {"items": []})
        self.assertFalse(r1["ok"])
        r2 = self.srv.api_commit_batch(None, {"items": [{"draft": _draft()} for _ in range(501)]})
        self.assertFalse(r2["ok"])
        self.assertIn("500", r2["message"])


class TestVision(unittest.TestCase):
    def test_sniff_mime(self):
        import server as srv
        self.assertEqual(srv._sniff_mime(b"\x89PNG\r\n"), "image/png")
        self.assertEqual(srv._sniff_mime(b"\xff\xd8\xff"), "image/jpeg")
        self.assertEqual(srv._sniff_mime(b"RIFF1234WEBP"), "image/webp")
        self.assertEqual(srv._sniff_mime(b"??"), "image/jpeg")

    def test_parse_images_one_call_all_pictures(self):
        """多张图一次请求带全（不逐图跑模型）；用户补充说明在最前。"""
        from core import llm
        captured = {}

        def fake_chat(prof, messages, force_json=True, purpose="", collect=None, reasoning_effort=None):
            captured["messages"] = messages
            captured["purpose"] = purpose
            if collect is not None:
                collect.clear()
                collect.update({"prompt_tokens": 1, "completion_tokens": 1})
            return json.dumps({"bills": [
                {"biz": "purchase_in", "party": "美宜佳",
                 "items": [{"name": "可乐", "qty": 3, "unit": "瓶", "price": 3.5}],
                 "confidence": "high", "note": ""}]})

        orig_chat, orig_prof = llm._chat, llm.cfgmod.active_profile
        llm._chat = fake_chat
        llm.cfgmod.active_profile = lambda: {
            "model": "vision-model", "api_key": "k", "base_url": "http://x/v1", "vision": True}
        try:
            # 视觉档案声明 vision=True 时直接用当前档案（不触发全档案扫描）
            drafts, meta = llm.parse_images(
                ["data:image/png;base64,aGVsbG8=", "data:image/jpeg;base64,aGVsbG8="],
                "都是今天的单", erp=None)
        finally:
            llm._chat, llm.cfgmod.active_profile = orig_chat, orig_prof
        self.assertEqual(len(drafts), 1)
        self.assertEqual(drafts[0]["party"], "美宜佳")
        self.assertEqual(captured["purpose"], "parse_image")
        user = captured["messages"][1]
        self.assertIsInstance(user["content"], list)
        texts = [p for p in user["content"] if p.get("type") == "text"]
        imgs = [p for p in user["content"] if p.get("type") == "image_url"]
        self.assertEqual(len(imgs), 2)                  # 两张图都进同一次请求
        self.assertIn("今天的单", texts[0]["text"])

    def test_parse_images_without_vision_gives_guidance(self):
        from core import llm
        orig = llm.vision_profile
        llm.vision_profile = lambda: None
        try:
            with self.assertRaises(RuntimeError) as cm:
                llm.parse_images(["data:image/png;base64,aGVsbG8="])
        finally:
            llm.vision_profile = orig
        self.assertIn("支持图片识别", str(cm.exception))


class TestVisionFlagPersist(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self._orig = (cfgmod.CONFIG_PATH, cfgmod.ENV_PATH, cfgmod.data_dir)
        cfgmod.CONFIG_PATH = os.path.join(self.td.name, "config.json")
        cfgmod.ENV_PATH = os.path.join(self.td.name, ".env")
        cfgmod.data_dir = lambda: self.td.name

    def tearDown(self):
        cfgmod.CONFIG_PATH, cfgmod.ENV_PATH, cfgmod.data_dir = self._orig
        self.td.cleanup()

    def test_vision_saved_and_inherited(self):
        cfgmod.upsert_profile("hy4", {"name": "hy4", "base_url": "http://x/v1",
                                      "api_key": "sk-test", "model": "hy4", "vision": True})
        self.assertTrue(cfgmod.load()["llm"]["profiles"]["hy4"].get("vision"))
        # 不携带 vision 键的保存（老客户端/CLI）→ 继承，不被抹掉
        cfgmod.upsert_profile("hy4", {"name": "hy4", "base_url": "http://x/v1",
                                      "api_key": "", "model": "hy4"})
        self.assertTrue(cfgmod.load()["llm"]["profiles"]["hy4"].get("vision"))
        # 显式 False 可关闭
        cfgmod.upsert_profile("hy4", {"name": "hy4", "base_url": "http://x/v1",
                                      "api_key": "", "model": "hy4", "vision": False})
        self.assertFalse(cfgmod.load()["llm"]["profiles"]["hy4"].get("vision"))


if __name__ == "__main__":
    unittest.main()
