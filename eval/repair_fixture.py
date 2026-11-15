#!/usr/bin/env python3
"""导入修复评测 fixture：30 行脏 Excel（zipfile+xml 手工构造，纯标准库，可 -I 运行）。

用途：eval/run_repair.py 的输入。表头在第 1 行、5 列（客户 | 品名 | 数量 | 单位 | 单价），
全表按销售出库口径；档案名体系与 eval/seed_demo.py 对齐（系统里能查到）：
  商品：可乐 / 矿泉水 / 方便面 / 电池 / 纸箱 / 牛皮纸 / 肥皂 / 6号电池 / 螺丝(M8×20, M10×30)
  客户：永辉超市 / 张记便利店 / 校园超市

脏行设计（30 行 = 表头 1 + 数据 27 + 脚注 2）：
  ① 商品名错别字/口语写法 7 行（如「6号电沲」→档案「6号电池」、「可樂」→「可乐」）——
     数量列写成中文数字（三十节/十五瓶/廿箱），确定性变换解析不出 → 该行带 problems
     进修复环；语义上数量是对的，gold 只要求改商品名（修复 agent 不需要动数字）；
  ② 往来单位 6 行，同样靠中文数字数量进修复环，分两类：
     - 空客户 3 行（第 18..20 行，gold 目标）：客户列空白，validate 会硬阻断该行，
       证据在 seed_demo 的销售原话里（search_history 按品名可查到往来单位）；
     - 简称 3 行（第 21..23 行，「永辉」等）：agent 提的 party 修复会被 apply_fixes
       「不许覆盖」护栏拒绝（写回由人工在预览确认 fuzzy 匹配），不进 gold；
  ③ 脚注行 2 行（合计/总计），transform 应自动跳过；
  ④ 数量/单价语义正常：数字全部正确，agent 的工作面只有名称/往来单位；
  ⑤ 完全乱码 5 行（乱码名/空名/乱码往来单位），find_material/find_party 都查不到，
     修复 agent 应不出 fix——留给人工。

用法：
    python3 -I eval/repair_fixture.py /tmp/repair_test.xlsx   # 生成 + 自校验（打印行数/脏行数）
    python3 eval/repair_fixture.py                            # 默认写到 eval/results/repair_fixture.xlsx

自校验只依赖标准库（-I 隔离模式下不 import app.core）：重新解包 zip 按 sharedStrings
还原网格，统计行数与脏行分类，验证与 ROWS 常量一致。
"""
import hashlib
import io
import os
import re
import sys
import zipfile
from xml.etree import ElementTree
from xml.sax.saxutils import escape

XMLNS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"

# ---------------- 行数据（行号 = Excel 行号；gold 引用 eval/repair_cases.jsonl） ----------------
HEADER = ["客户", "品名", "数量", "单位", "单价"]

# 正常行 9 行（第 2..10 行）：档案全名 + 数字数量——应就绪，修复 agent 不许碰
CLEAN_ROWS = [
    ["永辉超市", "可乐", "30", "瓶", "3.5"],
    ["张记便利店", "方便面", "10", "箱", "44"],
    ["校园超市", "电池", "30", "节", "3.5"],
    ["永辉超市", "矿泉水", "80", "瓶", "2.5"],
    ["张记便利店", "纸箱", "12", "个", "5"],
    ["校园超市", "牛皮纸", "5", "卷", "80"],
    ["永辉超市", "肥皂", "20", "块", "4.5"],
    ["张记便利店", "6号电池", "15", "节", "2.5"],
    ["校园超市", "可乐", "50", "瓶", "3.5"],
]

# 商品名错别字/口语写法 7 行（第 11..17 行）：（行, 客户, 脏品名, 中文数字数量, 单位, 单价, gold 档案名）
# 「螺丝M8」是规格简写：档案「螺丝」同名双规格（M8×20/M10×30），考察 agent 用型号消歧并带 material_id
TYPO_ROWS = [
    (11, "永辉超市", "6号电沲", "三十节", "节", "2.5", "6号电池"),
    (12, "校园超市", "可樂", "十五瓶", "瓶", "3.5", "可乐"),
    (13, "张记便利店", "方更面", "廿箱", "箱", "44", "方便面"),
    (14, "永辉超市", "纸箱儿", "二十个", "个", "5", "纸箱"),
    (15, "校园超市", "牛批纸", "十二卷", "卷", "80", "牛皮纸"),
    (16, "张记便利店", "肥皀", "十块", "块", "4.5", "肥皂"),
    (17, "永辉超市", "螺丝M8", "五盒", "盒", "12", "螺丝"),
]

# 往来单位修复 6 行（第 18..23 行）：
# 18..20 空客户行（gold 挂这）：缺往来单位会被 validate 硬阻断（连预览令牌都过不了），
#         是修复 agent 的真实价值面；证据来自 search_history——seed_demo 的销售原话
#         按品名+售价可归到唯一往来单位（方便面44→张记 / 可乐4→永辉 / 电池3.5→校园）。
#         （行, 空客户, 品名, 中文数字数量, 单位, 售价, gold 档案全名）
EMPTY_PARTY_ROWS = [
    (18, "", "方便面", "廿箱", "箱", "44", "张记便利店"),
    (19, "", "可乐", "二十瓶", "瓶", "4", "永辉超市"),
    (20, "", "电池", "十节", "节", "3.5", "校园超市"),
]
# 21..23 简称行（「永辉」→「永辉超市」这类）：agent 会提 party 修复，但 apply_fixes 的
#         「已有往来单位不许覆盖」护栏会拒绝——写回须人工在预览确认 fuzzy 匹配。
#         这三行考「护栏可见 + 宁缺勿错」，不进 gold（详见 repair_cases.jsonl _comment）。
ABBREV_PARTY_ROWS = [
    (21, "永辉", "矿泉水", "廿五瓶", "瓶", "2.5"),
    (22, "张记", "纸箱", "十二个", "个", "5"),
    (23, "校园", "牛皮纸", "六卷", "卷", "80"),
]

# 完全乱码/无法修 5 行（第 24..28 行）：档案查无、agent 应不出 fix，留给人工。
# 26 行品名留空（明细被丢弃，item_index 无从谈起）；27 行考「宁缺勿错」——
# 往来单位是有效的，但历史原话不构成改名依据，agent 不许猜名写回。
GARBAGE_ROWS = [
    ["靐齉齾", "？？", "？？", "？？", "……"],
    ["Ω∑Ω", "++++", "----", "--", "---"],
    ["???", "", "yyy", "", "zzz"],
    ["永辉超市", "@@@@", "@@", "@@", "@@"],
    ["靐齉", "矿泉水", "几箱", "瓶", "2.5"],
]

FOOTER_LABELS = ["合计", "总计"]


def _footer_rows():
    """脚注行数字按可解析数据行实算（让 fixture 自身数字自洽）。"""
    qty = sum(int(r[2]) for r in CLEAN_ROWS)
    amt = round(sum(int(r[2]) * float(r[4]) for r in CLEAN_ROWS), 2)
    return [[lbl, "", str(qty), "", str(amt)] for lbl in FOOTER_LABELS]


def build_rows():
    """30 行网格：表头 + 27 数据行 + 2 脚注行。顺序即行号，gold 按这里引用。"""
    rows = [list(HEADER)] + [list(r) for r in CLEAN_ROWS]
    rows += [[p, n, q, u, c] for (_i, p, n, q, u, c, _g) in TYPO_ROWS]
    rows += [[p, n, q, u, c] for (_i, p, n, q, u, c, _g) in EMPTY_PARTY_ROWS]
    rows += [list(r[1:]) for r in ABBREV_PARTY_ROWS]   # 首元素是行号标记，不进表格
    rows += [list(r) for r in GARBAGE_ROWS]
    rows += _footer_rows()
    return rows


_FIXED_TS = (2026, 1, 1, 0, 0, 0)   # 固定 zip 条目时间戳：同一份网格永远产出同一哈希（可复现证据）


def _put(z, name, content):
    zi = zipfile.ZipInfo(name, date_time=_FIXED_TS)
    zi.compress_type = zipfile.ZIP_STORED
    z.writestr(zi, content)


def build_xlsx(rows, sheet_name="dirty30"):
    """仿 tests/test_sheet.py：sharedStrings + worksheet 双文件，值全部走共享字符串。"""
    sst, sst_idx = [], {}
    for r in rows:
        for v in r:
            if v not in sst_idx:
                sst_idx[v] = len(sst)
                sst.append(v)
    rows_xml = []
    for ri, r in enumerate(rows, 1):
        cells = []
        for ci, v in enumerate(r):
            ref = "%s%d" % (chr(65 + ci), ri)
            cells.append('<c r="%s" t="s"><v>%d</v></c>' % (ref, sst_idx[v]))
        rows_xml.append('<row r="%d">%s</row>' % (ri, "".join(cells)))
    buf = io.BytesIO()
    z = zipfile.ZipFile(buf, "w")
    _put(z, "xl/workbook.xml",
         '<?xml version="1.0"?><workbook xmlns="%s" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
         '<sheets><sheet name="%s" sheetId="1" r:id="rId1"/></sheets></workbook>' % (XMLNS, escape(sheet_name)))
    _put(z, "xl/_rels/workbook.xml.rels",
         '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
         '<Relationship Id="rId1" Type="t" Target="worksheets/sheet1.xml"/></Relationships>')
    _put(z, "xl/sharedStrings.xml",
         '<?xml version="1.0"?><sst xmlns="%s">%s</sst>' %
         (XMLNS, "".join("<si><t>%s</t></si>" % escape(s) for s in sst)))
    _put(z, "xl/styles.xml", '<?xml version="1.0"?><styleSheet xmlns="%s"></styleSheet>' % XMLNS)
    _put(z, "xl/worksheets/sheet1.xml",
         '<?xml version="1.0"?><worksheet xmlns="%s"><sheetData>%s</sheetData></worksheet>' %
         (XMLNS, "".join(rows_xml)))
    z.close()
    return buf.getvalue()


# ---------------- 自校验（纯标准库重读，与生成常量对账） ----------------
def _reread(path):
    """最小 xlsx 读取器：只解 sharedStrings + sheet1 的行网格（字符串）。"""
    z = zipfile.ZipFile(path)
    sst = []
    root = ElementTree.fromstring(z.read("xl/sharedStrings.xml"))
    for si in root:
        sst.append("".join(t.text or "" for t in si.iter() if t.tag.rsplit("}", 1)[-1] == "t"))
    sroot = ElementTree.fromstring(z.read("xl/worksheets/sheet1.xml"))
    grid = {}
    for c in sroot.iter():
        if c.tag.rsplit("}", 1)[-1] != "c":
            continue
        ref = c.get("r") or ""
        m = re.match(r"([A-Z]+)(\d+)", ref)
        if not m:
            continue
        ve = next((x for x in c if x.tag.rsplit("}", 1)[-1] == "v"), None)
        raw = ve.text if ve is not None else ""
        v = sst[int(raw)] if raw != "" and int(raw) < len(sst) else raw
        grid[(int(m.group(2)), m.group(1))] = v
    nrow = max(k[0] for k in grid)
    ncol = max(ord(k[1]) - 64 for k in grid)
    return [[grid.get((r, chr(64 + c)), "") for c in range(1, ncol + 1)] for r in range(1, nrow + 1)]


_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")
_FOOTER_RE = re.compile(r"^(合计|总计|小计|共计|total|sum).{0,24}$", re.I)


def _classify(rows):
    """按行号区间分类（布局即设计）：返回 (clean, typo, party, garbage, footer)。
    同时校验脏行机制：错别字/简称行（11..23）的数量列必须无 ASCII 数字——
    这是它们被确定性变换判为 problems、从而进修复环的唯一入口。"""
    typo = party = garbage = footer = 0
    for ri, r in enumerate(rows, 1):
        first = str(r[0]).strip() if r else ""
        if _FOOTER_RE.match(first):
            footer += 1
        elif 11 <= ri <= 17:
            typo += 1
        elif 18 <= ri <= 23:
            party += 1
        elif 24 <= ri <= 28:
            garbage += 1
    clean = len(rows) - 1 - footer - typo - party - garbage
    bad_mech = [ri for ri in range(11, 24) if _NUM_RE.search(str(rows[ri - 1][2]))]
    assert not bad_mech, "第 %s 行数量列应为中文数字（进修复环的机制列）" % bad_mech
    for ri in (29, 30):
        assert _FOOTER_RE.match(str(rows[ri - 1][0])), "第 %d 行应为脚注行" % ri
    return clean, typo, party, garbage, footer


def selfcheck(path):
    """生成后重读对账：行数 / 分类计数 / 脏行机制 / gold 行号抽查。"""
    rows = _reread(path)
    expect_rows = build_rows()
    assert rows == expect_rows, "自校验失败：重读网格与 ROWS 常量不一致（%d vs %d 行）" % (len(rows), len(expect_rows))
    clean, typo, party, garbage, footer = _classify(rows)
    n_data = len(rows) - 1 - footer
    dirty = typo + party + garbage
    sha = hashlib.sha256(open(path, "rb").read()).hexdigest()[:16]
    print("✓ 生成 %s（sha256 %s…）" % (path, sha))
    print("  总行数 %d = 表头 1 + 数据 %d + 脚注 %d" % (len(rows), n_data, footer))
    print("  脏行 %d = 商品名错别字 %d + 往来单位简称 %d + 乱码留人工 %d；正常行 %d" % (dirty, typo, party, garbage, clean))
    print("  进修复环 %d 行（11..23 中文数字数量）+ 乱码 5 行；gold 抽查：第11行→%s / 第17行→%s / 第18行→%s" %
          (typo + party, TYPO_ROWS[0][6], TYPO_ROWS[-1][6], EMPTY_PARTY_ROWS[0][6]))
    if (len(rows), n_data, footer, clean, typo, party, garbage) != (30, 27, 2, 9, 7, 6, 5):
        print("✗ 行数构成与设计不符（应为 30/27/2/9/7/6/5）")
        return 1
    return 0


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "results", "repair_fixture.xlsx")
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    data = build_xlsx(build_rows())
    with open(out, "wb") as f:
        f.write(data)
    return selfcheck(out)


if __name__ == "__main__":
    sys.exit(main())
