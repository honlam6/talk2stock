#!/usr/bin/env python3
"""表格导入单测：xlsx(zip+xml) 解析、CSV 多编码、确定性变换（per_row/group）、
缺失兜底分级、脚注行跳过、日期序列值与多格式、脏值清洗。"""
import csv
import io
import os
import sys
import tempfile
import unittest
import zipfile

APP_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

from core import sheet as sm   # noqa: E402

XMLNS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"


def build_xlsx(sst_items, rows_xml, sheet_name="Sheet1"):
    buf = io.BytesIO()
    z = zipfile.ZipFile(buf, "w")
    z.writestr("xl/workbook.xml",
               '<?xml version="1.0"?><workbook xmlns="%s" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
               '<sheets><sheet name="%s" sheetId="1" r:id="rId1"/></sheets></workbook>' % (XMLNS, sheet_name))
    z.writestr("xl/_rels/workbook.xml.rels",
               '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
               '<Relationship Id="rId1" Type="t" Target="worksheets/sheet1.xml"/></Relationships>')
    z.writestr("xl/sharedStrings.xml",
               '<?xml version="1.0"?><sst xmlns="%s">%s</sst>' %
               (XMLNS, "".join("<si><t>%s</t></si>" % s for s in sst_items)))
    z.writestr("xl/styles.xml", '<?xml version="1.0"?><styleSheet xmlns="%s"></styleSheet>' % XMLNS)
    z.writestr("xl/worksheets/sheet1.xml",
               '<?xml version="1.0"?><worksheet xmlns="%s"><sheetData>%s</sheetData></worksheet>' % (XMLNS, rows_xml))
    z.close()
    return buf.getvalue()


SST = ["供应商", "品名", "数量", "单价", "美宜佳", "可乐", "张记", "纸箱"]


class TestXlsx(unittest.TestCase):
    def test_parse_shared_strings_and_numbers(self):
        xml = ('<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c></row>'
               '<row r="2"><c r="A2" t="s"><v>4</v></c><c r="B2"><v>30</v></c></row>')
        wb = sm.read_workbook("t.xlsx", build_xlsx(SST, xml))
        rows = wb["sheets"][0]["rows"]
        self.assertEqual(rows[0], ["供应商", "品名"])
        self.assertEqual(rows[1], ["美宜佳", "30"])

    def test_inline_and_bare_t(self):
        xml = ('<row r="1"><c r="A1" t="inlineStr"><is><t>备注行</t></is></c></row>'
               '<row r="2"><c r="A2"><t xml:space="preserve">无类型标记</t></c></row>')
        rows = sm.read_workbook("t.xlsx", build_xlsx([], xml))["sheets"][0]["rows"]
        self.assertEqual(rows[0], ["备注行"])
        self.assertEqual(rows[1], ["无类型标记"])

    def test_xls_rejected_with_guidance(self):
        with self.assertRaises(sm.SheetError) as cm:
            sm.read_workbook("t.xls", b"binary")
        self.assertIn(".xlsx", str(cm.exception))


class TestCsv(unittest.TestCase):
    def _csv(self, text, name="t.csv"):
        return sm.read_workbook(name, text.encode("utf-8"))["sheets"][0]["rows"]

    def test_utf8_bom_and_gbk(self):
        rows = self._csv("﻿客户,品名\n美宜佳,可乐")
        self.assertEqual(rows[0], ["客户", "品名"])
        wb = sm.read_workbook("t.csv", "客户,品名\n美宜佳,可乐".encode("gb18030"))
        self.assertEqual(wb["sheets"][0]["rows"][1], ["美宜佳", "可乐"])

    def test_quoted_and_semicolon(self):
        rows = self._csv('客户;品名\n"张记,分店";纸箱')
        self.assertEqual(rows[1][0], "张记,分店")

    def test_empty_rows_dropped(self):
        rows = self._csv("a,b\n\n1,2\n\n")
        self.assertEqual(len(rows), 2)


class TestTransform(unittest.TestCase):
    M_PER = {"header_row": 1, "mode": "per_row",
             "cols": {"A": "party", "B": "item_name", "C": "qty", "D": "price"},
             "file_biz": "purchase_in"}

    def test_per_row_and_footer_skip(self):
        rows = [["供应商", "品名", "数量", "单价"],
                ["美宜佳", "可乐", "30", "3.5"],
                ["合计", "", "42", "245"]]
        tr = sm.transform(rows, self.M_PER)
        self.assertEqual(tr["summary"]["total"], 1)
        d = tr["drafts"][0]["draft"]
        self.assertEqual((d["party"], d["items"][0]["qty"], d["items"][0]["price"]),
                         ("美宜佳", 30.0, 3.5))

    def test_group_mode(self):
        rows = [["单号", "客户", "品名", "数量", "金额"],
                ["D001", "美宜佳", "可乐", "30瓶", "¥105"],
                ["D001", "美宜佳", "纸箱", "10", "50"],
                ["D002", "张记", "方便面", "2", "90"],
                ["合计", "", "", "42", "245"]]
        m = {"header_row": 1, "mode": "group", "group_col": "A",
             "cols": {"A": "bill_no", "B": "party", "C": "item_name", "D": "qty", "E": "amount"},
             "file_biz": "sale_out"}
        tr = sm.transform(rows, m)
        self.assertEqual(tr["summary"]["total"], 2)
        d1 = tr["drafts"][0]["draft"]
        self.assertEqual(d1["party"], "美宜佳")
        self.assertEqual(len(d1["items"]), 2)
        self.assertEqual(d1["items"][0]["qty"], 30.0)          # "30瓶"→30
        self.assertAlmostEqual(d1["items"][0]["price"], 3.5)   # 105/30
        self.assertIn("D001", d1["remark"])                     # 原单号进备注
        self.assertEqual(tr["drafts"][1]["draft"]["items"][0]["price"], 45.0)

    def test_missing_tiers(self):
        """缺失分级：缺单价→0 可提交；缺数量/商品名→该行标问题不阻塞其他行。"""
        rows = [["客户", "品名", "数量", "单价"],
                ["美宜佳", "可乐", "", ""],
                ["张记", "纸箱", "5", "4"]]
        tr = sm.transform(rows, dict(self.M_PER, file_biz="sale_out"))
        self.assertEqual(tr["summary"]["total"], 2)
        self.assertTrue(any("数量" in p for p in tr["drafts"][0]["problems"]))
        self.assertEqual(tr["drafts"][1]["draft"]["items"][0]["price"], 4.0)
        d = tr["drafts"][1]["draft"]
        self.assertEqual(d["items"][0]["price"], 4.0)   # 就绪行照常出草稿

    def test_amount_only_derives_price(self):
        rows = [["客户", "品名", "数量", "金额"], ["张记", "纸箱", "4", "20"]]
        tr = sm.transform(rows, dict(self.M_PER, cols={"A": "party", "B": "item_name",
                                                       "C": "qty", "D": "amount"}))
        self.assertAlmostEqual(tr["drafts"][0]["draft"]["items"][0]["price"], 5.0)

    def test_date_formats_and_serial(self):
        self.assertEqual(sm.cell_date("2024-01-05"), "2024-01-05 00:00:00")
        self.assertEqual(sm.cell_date("2024.1.5"), "2024-01-05 00:00:00")
        self.assertEqual(sm.cell_date("2024年1月5日"), "2024-01-05 00:00:00")
        self.assertEqual(sm.cell_date("20240105"), "2024-01-05 00:00:00")
        self.assertEqual(sm.cell_date("45296"), "2024-01-05 00:00:00")   # Excel 序列值
        self.assertEqual(sm.cell_date("2024/1/5 13:30"), "2024-01-05 13:30:00")
        self.assertEqual(sm.cell_date("乱码"), "")

    def test_cell_num_dirty_values(self):
        self.assertEqual(sm.cell_num("¥1,234.5元"), 1234.5)
        self.assertEqual(sm.cell_num("30瓶"), 30.0)
        self.assertEqual(sm.cell_num("１２３"), 123.0)   # 全角
        self.assertIsNone(sm.cell_num("abc"))

    def test_biz_col_mapping(self):
        rows = [["类型", "客户", "品名", "数量"], ["出库", "张记", "纸箱", "5"],
                ["入库", "美宜佳", "可乐", "3"]]
        m = dict(self.M_PER, biz_col="A", biz_values={"入库": "purchase_in", "出库": "sale_out"})
        tr = sm.transform(rows, m)
        self.assertEqual(tr["drafts"][0]["draft"]["biz"], "sale_out")
        self.assertEqual(tr["drafts"][1]["draft"]["biz"], "purchase_in")

    def test_header_row_not_first(self):
        rows = [["XX贸易公司送货单", "", "", ""],
                ["供应商", "品名", "数量", "单价"],
                ["美宜佳", "可乐", "30", "3.5"]]
        tr = sm.transform(rows, dict(self.M_PER, header_row=2))
        self.assertEqual(tr["summary"]["total"], 1)
        self.assertEqual(tr["drafts"][0]["draft"]["party"], "美宜佳")

    def test_clean_mapping_whitelist(self):
        m = sm._clean_mapping({"header_row": "2", "mode": "group", "group_col": "B",
                               "cols": {"A": "item_name", "Z": "hack", "C": "qty"}})
        self.assertEqual(m["cols"], {0: "item_name", 2: "qty"})   # 非法字段被剥
        self.assertEqual(m["header_row"], 2)


class TestRepairSamples(unittest.TestCase):
    def test_failure_samples_names_failing_columns(self):
        rows = [["客户", "品名", "数量"], ["美宜佳", "可乐", "三十"]]
        tr = sm.transform(rows, {"header_row": 1, "mode": "per_row",
                                 "cols": {"A": "party", "B": "item_name", "C": "qty"},
                                 "file_biz": "purchase_in"})
        fs = sm.failure_samples(rows, tr["drafts"],
                                {"header_row": 1, "mode": "per_row",
                                 "cols": {"A": "party", "B": "item_name", "C": "qty"}})
        self.assertIn("qty", fs)
        self.assertIn("三十", fs)


if __name__ == "__main__":
    unittest.main()
