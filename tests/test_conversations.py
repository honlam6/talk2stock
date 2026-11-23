#!/usr/bin/env python3
"""会话持久化的离线单测：隔离目录、用户隔离、瘦身与限额。"""
import os
import shutil
import sys
import tempfile
import unittest

APP_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

from core import conversations as conv  # noqa: E402


class TestConversations(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="t2s_conv_")
        self._old = conv.CONV_DIR
        conv.CONV_DIR = os.path.join(self.tmp, "conversations")

    def tearDown(self):
        conv.CONV_DIR = self._old
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_upsert_list_get_delete(self):
        turns = [
            {"kind": "user", "text": "今天卖了多少"},
            {"kind": "answer", "answer": "今天销售 0 笔。", "steps": [{"tool": "today_stats", "ok": True, "latency_ms": 12}]},
        ]
        item = conv.upsert("alice", "", "", turns)
        self.assertTrue(item["id"].startswith("c"))
        self.assertEqual(item["title"], "今天卖了多少")
        listing = conv.list_items("alice")
        self.assertEqual(listing["active"], item["id"])
        self.assertEqual(listing["items"][0]["count"], 2)

        full = conv.get_item("alice", item["id"])
        self.assertEqual(full["turns"][0]["text"], "今天卖了多少")
        self.assertEqual(full["turns"][1]["steps"][0]["tool"], "today_stats")

        # 覆盖写入（前端每轮结束都 upsert 一次）
        conv.upsert("alice", item["id"], "", turns + [{"kind": "user", "text": "再说一次"}])
        self.assertEqual(conv.get_item("alice", item["id"])["turns"][-1]["text"], "再说一次")

        conv.delete("alice", item["id"])
        self.assertEqual(conv.list_items("alice")["items"], [])
        self.assertEqual(conv.list_items("alice")["active"], "")

    def test_user_isolation(self):
        a = conv.upsert("alice", "", "", [{"kind": "user", "text": "A 的问题"}])
        b = conv.upsert("bob", "", "", [{"kind": "user", "text": "B 的问题"}])
        self.assertNotEqual(a["id"], b["id"])
        self.assertIsNone(conv.get_item("bob", a["id"]))
        self.assertEqual([i["id"] for i in conv.list_items("bob")["items"]], [b["id"]])

    def test_confirm_token_and_images_stripped(self):
        turns = [{"kind": "answer", "answer": "草稿好了", "images": ["data:image/png;base64,AAAA"],
                  "drafts": [{"batch_id": "b1", "raw_text": "x", "bills": [
                      {"draft": {"biz": "purchase"}, "preview": {"total": 10.5, "confirm_token": "secret"}}]}]}]
        item = conv.upsert("carol", "", "", turns)
        stored = conv.get_item("carol", item["id"])["turns"][0]
        self.assertNotIn("images", stored)
        self.assertNotIn("confirm_token", stored["drafts"][0]["bills"][0]["preview"])
        self.assertEqual(stored["drafts"][0]["bills"][0]["preview"]["total"], 10.5)

    def test_turn_limit_keeps_recent(self):
        turns = [{"kind": "user", "text": "第%d句" % i} for i in range(conv.MAX_TURNS + 25)]
        item = conv.upsert("dave", "", "", turns)
        stored = conv.get_item("dave", item["id"])["turns"]
        self.assertEqual(len(stored), conv.MAX_TURNS)
        self.assertEqual(stored[-1]["text"], "第%d句" % (conv.MAX_TURNS + 24))

    def test_rename_and_item_limit(self):
        first = conv.upsert("erin", "", "", [{"kind": "user", "text": "原始标题"}])
        renamed = conv.rename("erin", first["id"], "新标题")
        self.assertEqual(renamed["title"], "新标题")
        for i in range(conv.MAX_ITEMS + 5):
            conv.upsert("erin", "", "会话%d" % i, [{"kind": "user", "text": "t"}])
        self.assertLessEqual(len(conv.list_items("erin")["items"]), conv.MAX_ITEMS)


if __name__ == "__main__":
    unittest.main()
