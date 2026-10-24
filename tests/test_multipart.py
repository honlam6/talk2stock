#!/usr/bin/env python3
"""手写 multipart 解析器单测（替代 Python 3.13 移除的 cgi）。"""
import os
import sys
import unittest

APP_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

import server   # noqa: E402


def build_body(boundary, parts):
    out = b""
    for name, filename, payload in parts:
        out += ("--%s\r\n" % boundary).encode()
        if filename:
            out += ('Content-Disposition: form-data; name="%s"; filename="%s"\r\n'
                    'Content-Type: application/octet-stream\r\n\r\n' % (name, filename)).encode()
        else:
            out += ('Content-Disposition: form-data; name="%s"\r\n\r\n' % name).encode()
        out += payload + b"\r\n"
    out += ("--%s--\r\n" % boundary).encode()
    return out


class TestMultipart(unittest.TestCase):
    def test_binary_roundtrip(self):
        # 二进制内容含 \r\n、--、随机字节，验证精确截取
        blob = bytes(range(256)) + b"\r\n--fake\r\n" + os.urandom(64) + b"\r\n\r\n"
        body = build_body("XbOuNd", [("file", "发票.png", blob), ("biz", None, b"bill")])
        f = server._parse_multipart("multipart/form-data; boundary=XbOuNd", body)
        self.assertEqual(f["file"][0], "发票.png")
        self.assertEqual(f["file"][1], blob)          # 二进制逐字节还原
        self.assertEqual(f["biz"], "bill")

    def test_chinese_filename_and_field(self):
        body = build_body("BnD", [("file", "合同扫描件.pdf", b"%PDF-1.4 ..."), ("biz", None, "票据".encode())])
        f = server._parse_multipart("multipart/form-data; boundary=BnD", body)
        self.assertEqual(f["file"][0], "合同扫描件.pdf")
        self.assertEqual(f["biz"], "票据")

    def test_no_boundary_returns_empty(self):
        self.assertEqual(server._parse_multipart("text/plain", b"x"), {})


if __name__ == "__main__":
    unittest.main()
