#!/usr/bin/env python3
"""附件目录与选择逻辑的离线单测。"""
import os
import sys
import unittest

APP_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

from core import attachments  # noqa: E402


class TestAttachments(unittest.TestCase):
    def test_csv_is_table_with_rows_and_catalog(self):
        data = "供应商,商品,数量,单价\n美宜佳,可乐,2,3.5\n".encode("utf-8")
        item = attachments.inspect_file("purchase.csv", data)
        self.assertEqual(item["kind"], "table")
        self.assertEqual(item["table_rows"][1][:2], ["美宜佳", "可乐"])
        self.assertIn("供应商", item["text"])
        self.assertEqual(item["error"], "")

    def test_text_and_encoding(self):
        item = attachments.inspect_file("sale.txt", "销售给校园超市5瓶矿泉水".encode("gb18030"))
        self.assertEqual(item["kind"], "text")
        self.assertIn("校园超市", item["text"])
        self.assertTrue(item["text"])

    def test_image_data_url(self):
        png = (b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
        item = attachments.inspect_file("receipt.png", png)
        self.assertEqual(item["kind"], "image")
        self.assertTrue(item["data_url"].startswith("data:image/png;base64,"))

    def test_unknown_and_oversize(self):
        self.assertEqual(attachments.inspect_file("x.bin", b"x")["kind"], "unsupported")
        big = attachments.inspect_file("x.txt", b"x" * (attachments.MAX_ATTACHMENT_BYTES + 1))
        self.assertIn("8MB", big["error"])

    def test_pick_by_index_name_and_no_match(self):
        items = [{"name": "a.csv"}, {"name": "b.txt"}]
        self.assertEqual(attachments.pick(items, "1")["name"], "a.csv")
        self.assertEqual(attachments.pick(items, "b.txt")["name"], "b.txt")
        self.assertIsNone(attachments.pick(items, "z"))

    def test_digest_keeps_attachment_identity(self):
        items = [{"name": "a.csv", "kind": "table", "size": 12, "text": "a,b"},
                 {"name": "b.txt", "kind": "text", "size": 8, "text": "hello"}]
        digest = attachments.context_digest(items)
        self.assertIn("1. a.csv", digest)
        self.assertIn("2. b.txt", digest)
        self.assertIn("attachment", digest)


class TestDraftAttachmentGuard(unittest.TestCase):
    def test_multiple_attachments_require_selector(self):
        from core import ask
        data, note = ask._draft_order({"text": "生成草稿"}, object(), attachments=[
            {"name": "a.csv", "kind": "table", "text": ""},
            {"name": "b.txt", "kind": "text", "text": "x"},
        ])
        self.assertIn("error", data)
        self.assertIn("attachment", note)
        self.assertIn("a.csv", note)


if __name__ == "__main__":
    unittest.main()
