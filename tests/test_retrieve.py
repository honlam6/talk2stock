#!/usr/bin/env python3
"""retrieve.py（BM25 轻检索）单测：纯离线，不依赖 ERP / LLM。"""
import os
import sys
import tempfile
import unittest

APP_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

from core import retrieve as rmod   # noqa: E402


class TestTokenize(unittest.TestCase):
    def test_chinese_bigram(self):
        self.assertEqual(rmod.tokenize("库存"), ["库存"])

    def test_long_chinese(self):
        toks = rmod.tokenize("库存查询")
        self.assertIn("库存", toks)
        self.assertIn("查询", toks)
        self.assertIn("存查", toks)   # 相邻 bigram 也在（召回更高）

    def test_ascii_lowercase(self):
        self.assertEqual(rmod.tokenize("ERP-Agent v2"), ["erp-agent", "v2"])

    def test_mixed(self):
        toks = rmod.tokenize("jshERP 怎么建仓库")
        self.assertIn("jsherp", toks)
        self.assertIn("仓库", toks)


class TestBM25(unittest.TestCase):
    DOCS = [
        {"id": 0, "title": "入库", "text": "采购入库 从供应商买进货品 入原料仓"},
        {"id": 1, "title": "出库", "text": "销售出库 卖给客户 从成品仓发货"},
        {"id": 2, "title": "退货", "text": "采购退货 把货退回给供应商"},
    ]

    def test_relevance_ordering(self):
        idx = rmod.BM25([dict(d) for d in self.DOCS])
        hits = idx.search("供应商 进货")
        self.assertTrue(hits)
        # 入库文档同时命中 供应/应商/进货 三个 bigram，应排第一
        self.assertEqual(hits[0]["title"], "入库")

    def test_no_hit_returns_empty(self):
        idx = rmod.BM25([dict(d) for d in self.DOCS])
        self.assertEqual(idx.search("毫不相关词组"), [])

    def test_empty_corpus(self):
        idx = rmod.BM25([])
        self.assertEqual(idx.search("任何"), [])


class TestHistorySearch(unittest.TestCase):
    def test_search_over_tmp_audit(self):
        rows = [
            {"event": "commit_ok", "ts": "2026-10-24 10:00:00",
             "raw_text": "从美宜佳买了10瓶可乐",
             "draft": {"party": "美宜佳", "items": [{"name": "可乐", "qty": 10, "unit": "瓶"}],
                        "remark": ""},
             "result": {"biz_label": "采购入库", "total": 35.0, "number": "001"}},
            {"event": "commit_ok", "ts": "2026-10-24 11:00:00",
             "raw_text": "卖给永辉超市200个纸箱",
             "draft": {"party": "永辉超市", "items": [{"name": "纸箱", "qty": 200, "unit": "个"}],
                        "remark": ""},
             "result": {"biz_label": "销售出库", "total": 1000.0, "number": "002"}},
            {"event": "commit_rejected", "ts": "2026-10-24 12:00:00", "reason": "x"},  # 应被忽略
        ]
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "audit.jsonl")
            with open(path, "w", encoding="utf8") as f:
                for r in rows:
                    f.write(__import__("json").dumps(r, ensure_ascii=False) + "\n")
            # 指向 tmp 索引
            orig = rmod._audit_path
            rmod._audit_path = lambda: path
            try:
                data, tpl = rmod.search_history("可乐 美宜佳")
                self.assertEqual(data["total_bills"], 2)          # rejected 不计
                self.assertTrue(data["hits"])
                self.assertIn("美宜佳", data["hits"][0]["snippet"] + data["hits"][0]["title"] + tpl)
            finally:
                rmod._audit_path = orig
                rmod._cache.pop("history", None)


class TestDocsIndex(unittest.TestCase):
    def test_docs_index_loads_repo_files(self):
        idx = rmod.docs_index()
        # 仓库文档存在则索引非空（CI 环境文件在则必过）
        if rmod.DOC_FILES:
            self.assertGreater(len(idx.docs), 0)
            hits = idx.search("单位 换算")
            # 不强断言内容（文档演进），只验证搜索不炸且结构正确
            for h in hits:
                self.assertIn("score", h)
                self.assertIn("source", h)


if __name__ == "__main__":
    unittest.main()
