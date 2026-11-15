#!/usr/bin/env python3
"""表格导入层：任意来源的 Excel/CSV → 统一草稿。纯标准库（zipfile + xml + csv）。

分工（成本与可靠性的第一性原理）：
- 文件解析（xlsx = zip + XML）、单元格清洗、行 → 草稿变换：全部确定性代码，零 LLM；
  500 行和 5 行成本相同，结果可复现可单测。
- LLM 只做一件事：看前几行猜「表头在哪、哪列是啥」（llm.map_sheet）。
  任意来源（金蝶/用友/手工表/其他 ERP 导出）不预设格式，靠映射而不是靠模板。
- 映射经人工确认后，变换永远用同一份确定性代码，AI 只在解析失败时参与修复环（服务端编排，≤2 轮）。

xlsx 解析范围（够用的最小集）：
- sharedStrings / inlineStr / number / formula string / boolean
- 日期：styles.xml 的 numFmt 判定 + 1899-12-30 序列值换算（老 ERP 导出大多是这个口径）
- 不支持：合并单元格拆分（值落在左上格，其余为空——对「一单多行」表正好是对的）、
  公式重算（取缓存值）、.xls 老二进制格式（提示另存为 .xlsx/.csv）。
"""
import csv
import io
import re
import zipfile
from datetime import datetime, timedelta
from xml.etree import ElementTree

from .llm import UNITS, normalize

MAX_ROWS = 500          # 单次导入行数上限（与 /api/sheet/* 服务端上限一致）
MAX_COLS = 40           # 解析列数上限
SAMPLE_ROWS = 6         # 给 LLM 映射看的原始行数
SAMPLE_VALUES = 4       # 每列给 LLM 看的非空样本数


class SheetError(Exception):
    pass


# ---------------------------------------------------------------------------
# xlsx / csv 解析
# ---------------------------------------------------------------------------
def _ln(tag):
    """去掉 XML 命名空间前缀（spreadsheetml 的默认命名空间各版本不同，按 localname 匹配）。"""
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else tag


def _col_index(ref):
    """'A'→0, 'Z'→25, 'AA'→26。ref 是单元格坐标如 'AB12'。"""
    letters = re.match(r"([A-Z]+)", (ref or "").upper())
    if not letters:
        return None
    n = 0
    for ch in letters.group(1):
        n = n * 26 + (ord(ch) - 64)
    return n - 1


_DATE_FMT_IDS = set(range(14, 23)) | set(range(27, 37)) | {45, 46, 47} | set(range(50, 59))
_EPOCH = datetime(1899, 12, 30)   # Excel 序列值纪元（含 1900 闰年 bug 的兼容口径）


def _is_date_fmt(fid, code):
    if not fid:
        return False
    if int(fid) in _DATE_FMT_IDS:
        return True
    # 自定义格式：含 y/m/d/h/s 字母（避开被反斜杠转义与引号包裹的字面量）按日期算
    c = re.sub(r'"[^"]*"|\\.', "", str(code or "")).lower()
    return bool(re.search(r"[ymdhs]", c)) and "0.00" not in c


def _serial_to_str(v):
    """Excel 日期序列值 → 显示串。纯时间（<1）只给时分秒。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f < 0 or f > 2958465:      # 序列值越界（9999-12-31）不当日期
        return None
    dt = _EPOCH + timedelta(days=f)
    if f < 1:
        return dt.strftime("%H:%M:%S")
    return dt.strftime("%Y-%m-%d %H:%M:%S") if f % 1 else dt.strftime("%Y-%m-%d")


def _parse_xlsx(data):
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise SheetError("文件不是有效的 .xlsx（可能是改了扩展名的其他格式），请另存为 .xlsx 或 .csv")
    names = set(zf.namelist())

    # 共享字符串表
    sst = []
    if "xl/sharedStrings.xml" in names:
        root = ElementTree.fromstring(zf.read("xl/sharedStrings.xml"))
        for si in root:
            if _ln(si.tag) != "si":
                continue
            text = "".join(t.text or "" for t in si.iter() if _ln(t.tag) == "t")
            sst.append(text)

    # 样式：numFmt 自定义表 + cellXfs 每格样式对应的 numFmtId
    numfmts, xfs = {}, []
    if "xl/styles.xml" in names:
        try:
            root = ElementTree.fromstring(zf.read("xl/styles.xml"))
            for el in root.iter():
                if _ln(el.tag) == "numFmt":
                    numfmts[el.get("numFmtId")] = el.get("formatCode")
                elif _ln(el.tag) == "cellXfs":
                    for xf in el:
                        if _ln(xf.tag) == "xf":
                            xfs.append(xf.get("numFmtId") or "0")
        except ElementTree.ParseError:
            pass

    # workbook.xml：sheet 名 → rId（保持工作簿里的顺序）
    wb = ElementTree.fromstring(zf.read("xl/workbook.xml"))
    rels = {}
    if "xl/_rels/workbook.xml.rels" in names:
        rroot = ElementTree.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
        for rel in rroot:
            if _ln(rel.tag) == "Relationship":
                tgt = rel.get("Target") or ""
                if tgt.startswith("/"):
                    tgt = tgt[1:]
                elif not tgt.startswith("xl/"):
                    tgt = "xl/" + tgt
                rels[rel.get("Id")] = tgt

    sheets = []
    for sh in wb.iter():
        if _ln(sh.tag) != "sheet":
            continue
        target = rels.get(sh.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"))
        if not target or target not in names:
            continue
        name = sh.get("name") or ("Sheet%d" % (len(sheets) + 1))
        sroot = ElementTree.fromstring(zf.read(target))
        grid = {}
        max_row = max_col = 0
        truncated = False
        for c in sroot.iter():
            if _ln(c.tag) != "c":
                continue
            ref = c.get("r") or ""
            ci = _col_index(ref)
            try:
                ri = int(re.sub(r"\D", "", ref) or 0) - 1
            except ValueError:
                ri = 0
            if ci is None or ri < 0:
                continue
            if ri >= MAX_ROWS or ci >= MAX_COLS:
                truncated = True
                continue
            t = c.get("t") or "n"
            v = None
            if t == "inlineStr":
                v = "".join(x.text or "" for x in c.iter() if _ln(x.tag) == "t")
            else:
                ve = next((x for x in c if _ln(x.tag) == "v"), None)
                raw = ve.text if ve is not None else None
                if raw is None:
                    # 宽容：部分生成器写 <c><t>文字</t></c> 不带类型标记
                    te = next((x for x in c if _ln(x.tag) == "t"), None)
                    if te is not None and (te.text or "").strip():
                        raw = te.text
                if raw is not None:
                    if t == "s":
                        try:
                            v = sst[int(raw)]
                        except (ValueError, IndexError):
                            v = raw
                    elif t == "b":
                        v = "1" if raw.strip() in ("1", "true") else "0"
                    else:
                        v = raw   # n / str（公式缓存串）原样取，清洗层再归类
            # 日期：样式判定 + 序列值换算（读到的原始串不是日期串时才走这条路）
            if v is not None and t in ("n", ""):
                xf = xfs[int(c.get("s") or 0)] if (c.get("s") or "").isdigit() and int(c.get("s") or 0) < len(xfs) else None
                if xf and _is_date_fmt(xf, numfmts.get(xf)):
                    d = _serial_to_str(v)
                    if d:
                        v = d
            if v is not None and str(v).strip() != "":
                grid.setdefault(ri, {})[ci] = str(v).strip()
                max_row, max_col = max(max_row, ri), max(max_col, ci)
        rows = [[grid.get(r, {}).get(c, "") for c in range(max_col + 1)]
                for r in range(max_row + 1)]
        sheets.append({"name": name, "rows": rows, "truncated": truncated})
    if not sheets:
        raise SheetError("xlsx 里没有可读的工作表")
    return sheets


def _sniff_delim(first_line):
    for d in ("\t", ",", ";", "|"):
        if d in first_line:
            return d
    return ","


def _parse_csv(data):
    text = None
    for enc in ("utf-8-sig", "gb18030", "big5", "latin-1"):
        try:
            text = data.decode(enc)
            break
        except (UnicodeDecodeError, LookupError):
            continue
    if text is None:
        raise SheetError("CSV 编码无法识别，请另存为 UTF-8")
    lines = text.splitlines()
    if not lines:
        raise SheetError("CSV 是空的")
    reader = csv.reader(io.StringIO(text), delimiter=_sniff_delim(lines[0]))
    rows = [row for row in reader]
    # 掐掉全空行（CSV 导出常见尾随空行），保留行号语义由调用方按原行号计
    out, holes = [], []
    for i, r in enumerate(rows):
        if any(str(x).strip() for x in r):
            out.append([str(x).strip() for x in r])
        else:
            holes.append(i)
    return [{"name": "CSV", "rows": out, "truncated": False, "_holes": holes}]


def read_workbook(filename, data):
    """文件名 + 字节 → {"sheets": [{name, rows, truncated}]}。rows[r][c] 都是字符串（日期已换算显示串）。"""
    low = (filename or "").lower()
    if low.endswith(".xlsx") or (low.endswith(".xlsm") and True):
        return {"sheets": _parse_xlsx(data), "kind": "xlsx"}
    if low.endswith(".csv") or low.endswith(".txt") or low.endswith(".tsv"):
        return {"sheets": _parse_csv(data), "kind": "csv"}
    if low.endswith(".xls"):
        raise SheetError("暂不支持老版 .xls 二进制格式，请在 Excel/WPS 里另存为 .xlsx 或 .csv")
    # 扩展名骗人时以内容为准：zip 魔数 → xlsx
    if data[:2] == b"PK":
        return {"sheets": _parse_xlsx(data), "kind": "xlsx"}
    raise SheetError("只支持 .xlsx / .csv 文件（.xls 请先另存）")


# ---------------------------------------------------------------------------
# 单元格清洗（确定性；数量带单位/金额带币符/多格式日期）
# ---------------------------------------------------------------------------
_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")


def cell_num(v, default=None):
    """'¥1,234.5元' / '30瓶' / '1２3'（全角）→ float；解析不出返回 default。"""
    if v is None:
        return default
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    if not s:
        return default
    s = s.translate(str.maketrans("０１２３４５６７８９．－", "0123456789.-"))
    s = s.replace(",", "").replace("，", "").replace(" ", "")
    s = re.sub(r"[¥￥$]|元|人民币", "", s)
    m = _NUM_RE.search(s)
    return float(m.group()) if m else default


_UNIT_ALT = "|".join(sorted(UNITS, key=len, reverse=True))


def cell_unit(v):
    """从数量串里提取单位（'30瓶'→'瓶'）；独立单位列用不上这个。"""
    s = str(v or "").strip()
    m = re.search(r"(%s)\s*$" % _UNIT_ALT, s)
    return m.group(1) if m else ""


def cell_date(v, fmt_hint="auto"):
    """多格式日期 → 'YYYY-MM-DD HH:MM:SS'；解析不出返回 ''。"""
    if v is None:
        return ""
    s = str(v).strip()
    if not s:
        return ""
    s = s.replace("年", "-").replace("月", "-").replace("日", "").replace("点", ":").replace("时", ":").replace("分", ":").replace("秒", "").strip()
    fmts = []
    hint = str(fmt_hint or "auto")
    if hint not in ("", "auto"):
        # 用户样本格式 → strftime 格式（只处理常见三种写法，其余交给 auto 序列）
        h = hint.replace("yyyy", "%Y").replace("YYYY", "%Y").replace("yy", "%y")
        h = h.replace("mm", "%m").replace("dd", "%d").replace("MM", "%m").replace("DD", "%d")
        h = h.replace("HH", "%H").replace("hh", "%H").replace("MM", "%M").replace("ss", "%S").replace("SS", "%S")
        fmts.append(h)
    fmts += ["%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
             "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y/%m/%d", "%Y.%m.%d", "%Y%m%d"]
    for f in fmts:
        try:
            dt = datetime.strptime(s, f)
            return dt.strftime("%Y-%m-%d %H:%M:%S") if any(x in f for x in ("%H", "%M")) else dt.strftime("%Y-%m-%d 00:00:00")
        except ValueError:
            continue
    # 纯数字且量级像 Excel 序列值（1990-01-01 ~ 2085 附近）→ 当序列日期
    if re.fullmatch(r"\d{5}(\.\d+)?", s):
        d = _serial_to_str(s)
        if d:
            return d if " " in d else d + " 00:00:00"
    # 最后兜底：datetime.fromisoformat（2024-01-05T10:00:00）
    try:
        return datetime.fromisoformat(s).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return ""


def cell_tax(v):
    n = cell_num(v)
    if n is None:
        return 0
    return n if n <= 100 else 0


# ---------------------------------------------------------------------------
# 行 → 草稿（确定性变换；映射来自 LLM 建议 + 人工确认）
# ---------------------------------------------------------------------------
FIELD_ENUM = {"party", "item_name", "qty", "unit", "price", "amount", "depot",
              "oper_time", "pay_amount", "tax_rate", "remark", "bill_no", "ignore"}


def _clean_mapping(mapping):
    """LLM/前端给的映射 → 严格形状（字段枚举白名单、列号合法化）。"""
    out = {
        "header_row": int(mapping.get("header_row") or 1),
        "mode": "group" if mapping.get("mode") == "group" else "per_row",
        "group_col": str(mapping.get("group_col") or "").strip().upper(),
        "file_biz": str(mapping.get("file_biz") or "purchase_in").strip(),
        "biz_col": str(mapping.get("biz_col") or "").strip().upper(),
        "biz_values": {str(k): str(v) for k, v in (mapping.get("biz_values") or {}).items()},
        "cols": {},
        "date_format": str(mapping.get("date_format") or "auto"),
    }
    hr = max(1, out["header_row"])
    out["header_row"] = hr
    for col, field in (mapping.get("cols") or {}).items():
        ci = _col_index(str(col))
        f = str(field or "").strip()
        if ci is None or f not in FIELD_ENUM or f == "ignore":
            continue
        out["cols"][ci] = f
    if out["mode"] == "group" and _col_index(out["group_col"]) is None:
        out["mode"] = "per_row"
        out["group_col"] = ""
    return out


def grid_desc(rows, max_rows=SAMPLE_ROWS):
    """给 LLM 映射看的文本：原始前几行 + 每列样本。列号用 Excel 字母（与映射输出对齐）。"""
    ncol = min(max((len(r) for r in rows[:max_rows]), default=0), MAX_COLS)
    letters = [_col_letter(i) for i in range(ncol)]
    head_i = out_header_index(rows)
    parts = ["前 %d 行原文（| 分隔，行号从 1 开始）：" % min(len(rows), max_rows)]
    for i, r in enumerate(rows[:max_rows], 1):
        cells = (list(r) + [""] * ncol)[:ncol]
        parts.append("行%d: %s" % (i, " | ".join(str(x) for x in cells)))
    parts.append("\n每列样本（列号: 第%d行表头 → 非空样本）：" % (head_i + 1))
    for c in range(ncol):
        col = [r[c] if c < len(r) else "" for r in rows]
        samples = [str(x) for x in col if str(x).strip()][:SAMPLE_VALUES]
        head = col[head_i] if 0 <= head_i < len(col) else ""
        parts.append("列%s: %s → %s" % (letters[c], head, " / ".join(samples) if samples else "(空)"))
    return "\n".join(parts)


def out_header_index(rows):
    """粗猜表头行（仅供 grid_desc 展示，真正判定在 LLM）：第一个含 ≥2 个非空且含中文/字母的行。"""
    for i, r in enumerate(rows[:10]):
        cells = [str(x).strip() for x in r]
        nn = [c for c in cells if c]
        if len(nn) >= 2 and sum(1 for c in nn if re.search(r"[\u4e00-\u9fa5A-Za-z]", c)) >= 2:
            return i
    return 0


def _col_letter(idx):
    s = ""
    idx += 1
    while idx:
        idx, r = divmod(idx - 1, 26)
        s = chr(65 + r) + s
    return s


def _row_val(row, ci):
    if ci is None or ci >= len(row):
        return ""
    return str(row[ci] or "").strip()


def _bill_from_row(row, m):
    """一行 → 一张单的 raw dict（未 normalize）。返回 (raw, errors)。"""
    errs = []
    get = lambda f: _row_val(row, next((c for c, fl in m["cols"].items() if fl == f), None))
    qty = cell_num(get("qty"))
    price = cell_num(get("price"))
    amount = cell_num(get("amount"))
    unit = get("unit") or (cell_unit(get("qty")) if qty is not None else "")
    name = get("item_name")
    if qty is None and amount is not None and price:
        qty = round(amount / price, 6) if price else None
    if price is None and amount is not None and qty:
        price = round(amount / qty, 4)
    if get("oper_time"):
        d = cell_date(get("oper_time"), m.get("date_format"))
        if not d:
            errs.append("日期「%s」无法解析" % get("oper_time"))
    else:
        d = ""
    if m["biz_col"]:
        raw_biz = _row_val(row, _col_index(m["biz_col"]))
        biz = m["biz_values"].get(raw_biz) or m["file_biz"]
    else:
        biz = m["file_biz"]
    item = {"name": name, "qty": qty if qty is not None else 0,
            "unit": unit or "个", "price": price if price is not None else 0}
    raw = {
        "biz": biz,
        "party": get("party"),
        "items": [item],
        "depot": get("depot"),
        "oper_time": d,
        "remark": get("remark"),
        "pay_amount": cell_num(get("pay_amount")),
    }
    if not name:
        errs.append("商品名为空")
    if qty is None:
        errs.append("数量无法解析「%s」" % get("qty"))
    if price is None and amount is None:
        errs.append("单价与金额都为空")
    return raw, errs


_FOOTER_RE = re.compile(r"^(合计|总计|小计|共计|total|sum).{0,24}$", re.I)


def _is_footer(row):
    """手工表常见的收尾行（「合计」起头，后面跟个总数）——不是业务行，跳过。
    以最左非空格判定：合计/总计标签几乎都在行首。"""
    for x in row:
        s = str(x).strip()
        if not s:
            continue
        return bool(_FOOTER_RE.match(s))
    return False


def transform(rows, mapping):
    """rows（read_workbook 的某一 sheet）+ 映射 → {drafts, row_errors, skipped}。

    drafts: [{row_no, draft, problems}]（draft 已过 llm.normalize，可直接 resolve）
    row_errors: [{row_no, errors}]（变换层就失败的行——给行级状态表与修复环）
    """
    m = _clean_mapping(mapping)
    hr = m["header_row"] - 1          # 0-based
    if hr >= len(rows):
        raise SheetError("表头行 %d 超出表格范围（共 %d 行）" % (m["header_row"], len(rows)))

    drafts, row_errors = [], []
    if m["mode"] == "per_row":
        for ri in range(hr + 1, len(rows)):
            row = rows[ri]
            if not any(str(x).strip() for x in row) or _is_footer(row):
                continue
            raw, errs = _bill_from_row(row, m)
            d, problems = normalize(raw)
            drafts.append({"row_no": ri + 1, "draft": d,
                           "problems": errs + problems})
    else:
        gci = _col_index(m["group_col"])
        groups, order = {}, []
        for ri in range(hr + 1, len(rows)):
            row = rows[ri]
            if not any(str(x).strip() for x in row) or _is_footer(row):
                continue
            key = _row_val(row, gci) or ("行%d" % (ri + 1))
            if key not in groups:
                groups[key] = []
                order.append(key)
            groups[key].append(ri)
        for key in order:
            ris = groups[key]
            head_raw, head_errs = _bill_from_row(rows[ris[0]], m)
            items = []
            group_errs = list(head_errs)
            for ri in ris:
                row = rows[ri]
                g = lambda f: _row_val(row, next((c for c, fl in m["cols"].items() if fl == f), None))
                name = g("item_name")
                if not name:
                    continue
                qty = cell_num(g("qty"))
                price = cell_num(g("price"))
                amount = cell_num(g("amount"))
                if price is None and amount is not None and qty:
                    price = round(amount / qty, 4)
                if qty is None:
                    group_errs.append("第%d行数量无法解析「%s」" % (ri + 1, g("qty")))
                items.append({"name": name, "qty": qty if qty is not None else 0,
                              "unit": g("unit") or cell_unit(g("qty")) or "个",
                              "price": price if price is not None else 0})
            if not items:
                row_errors.append({"row_no": ris[0] + 1, "errors": ["分组 %s 没有有效商品行" % key]})
                continue
            raw = dict(head_raw)
            raw["items"] = items
            raw["remark"] = ((head_raw.get("remark") or "") + (" 原单号:%s" % key if m["group_col"] else "")).strip()
            d, problems = normalize(raw)
            drafts.append({"row_no": ris[0] + 1, "draft": d,
                           "problems": group_errs + problems})

    drafts = drafts[:MAX_ROWS]
    ok = [d for d in drafts if not d["problems"]]
    return {"drafts": drafts, "row_errors": row_errors,
            "summary": {"total": len(drafts), "ok": len(ok),
                        "failed": len(drafts) - len(ok) + len(row_errors),
                        "mode": m["mode"], "biz": m["file_biz"]}}


def failure_samples(rows, drafts, mapping):
    """修复环素材：按列归集解析失败的原始样本（给 LLM 看「哪列、什么样的脏值」）。"""
    m = _clean_mapping(mapping)
    by_field = {}
    for d in drafts:
        if not d["problems"]:
            continue
        ri = d["row_no"] - 1
        if ri >= len(rows):
            continue
        for f in ("qty", "price", "amount", "oper_time", "party", "item_name"):
            ci = next((c for c, fl in m["cols"].items() if fl == f), None)
            if ci is None:
                continue
            v = _row_val(rows[ri], ci)
            if v:
                by_field.setdefault(f, []).append(v)
    if not by_field:
        return ""
    parts = ["以下列存在解析失败，样本如下（原值 → 出错行）："]
    for f, vals in by_field.items():
        parts.append("%s 列失败样本: %s" % (f, " | ".join(vals[:6])))
    parts.append("请重新输出完整映射 JSON：调整 header_row / cols / date_format / mode，让这些值能被正确解析。")
    return "\n".join(parts)
