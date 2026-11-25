#!/usr/bin/env python3
"""只读 SQL 的错误增强单测：表名不存在时返回相近表名提示。

背景：线上诊断实测里模型连续两次猜错表名（jsh_bill 之类），错误信息
没告诉它正确表名，3 步预算就烧掉了。这里钉住「1146 → 候选表名」的行为。

跑法：python3 -m unittest tests.test_dbread_hint -v
"""
import os
import sys
import unittest
from unittest import mock

APP_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

from core import dbread as dbmod   # noqa: E402


class TestTableHint(unittest.TestCase):
    def test_1146_returns_close_table_names(self):
        err = ("ERROR 1146 (42S02) at line 1: Table 'jsh_erp.jsh_depot_hade' doesn't exist")
        with mock.patch.object(dbmod, "table_names",
                               lambda: ["jsh_depot_head", "jsh_depot_item", "jsh_supplier"]), \
             mock.patch.object(dbmod, "_run", side_effect=dbmod.DbError(err)):
            data, note = dbmod.sql_query({"sql": "SELECT * FROM jsh_depot_hade"})
        self.assertIsNone(note)
        self.assertIn("error", data)
        self.assertIn("相近的业务表", data["hint"])
        self.assertIn("jsh_depot_head", data["hint"])

    def test_other_errors_keep_generic_hint(self):
        with mock.patch.object(dbmod, "_run", side_effect=dbmod.DbError("Unknown column 'foo'")):
            data, _ = dbmod.sql_query({"sql": "SELECT foo FROM jsh_depot_head"})
        self.assertIn("error", data)
        self.assertNotIn("相近的业务表", data["hint"])

    def test_1146_without_close_match_stays_generic(self):
        err = "ERROR 1146 (42S02) at line 1: Table 'jsh_erp.zzz_qqq' doesn't exist"
        with mock.patch.object(dbmod, "table_names", lambda: ["jsh_depot_head"]), \
             mock.patch.object(dbmod, "_run", side_effect=dbmod.DbError(err)):
            data, _ = dbmod.sql_query({"sql": "SELECT * FROM zzz_qqq"})
        self.assertNotIn("相近的业务表", data["hint"])

    def test_1054_unknown_column_returns_real_table_columns(self):
        err = "ERROR 1054 (42S22) at line 1: Unknown column 'h.supplier_id' in 'field list'"
        schema_rows = [
            {"table_name": "jsh_depot_head", "column_name": "id"},
            {"table_name": "jsh_depot_head", "column_name": "organ_id"},
            {"table_name": "jsh_depot_head", "column_name": "discount_last_money"},
            {"table_name": "jsh_supplier", "column_name": "id"},
            {"table_name": "jsh_supplier", "column_name": "supplier"},
        ]
        with mock.patch.object(dbmod, "_schema_rows", return_value=schema_rows), \
             mock.patch.object(dbmod, "_run", side_effect=dbmod.DbError(err)):
            data, _ = dbmod.sql_query({"sql": "SELECT h.supplier_id FROM jsh_depot_head h JOIN jsh_supplier s ON s.id=h.supplier_id"})
        self.assertIn("真实字段", data["hint"])
        self.assertIn("organ_id", data["hint"])
        self.assertIn("discount_last_money", data["hint"])


if __name__ == "__main__":
    unittest.main()
