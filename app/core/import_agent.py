#!/usr/bin/env python3
"""表格导入的「看一眼 → 拿主意 → 拿不准就问 → 自己修 → 再问」层。

分工仍然遵守项目的一贯原则：**能确定性算的绝不用模型，模型只用来理解格式**。
这里新增的不是「让 AI 再猜一次」，而是三件事：

1. 先自己看（profile）：表头在第几行、哪些行是数据、哪些是合计脚注、
   每列是什么类型（文本/数字/日期/金额）、同名候选列有哪些。全部规则算，零 token。
2. 先自己拿主意（plan）：按表头词典 + 列类型打分做列映射，判 per_row / group、
   判整表业务类型。有模型时再让模型补它认得出而规则认不出的列，
   两边冲突**不擅自选**，变成问题抛给用户。
3. 拿不准就问（questions）：缺关键列、一列能当两个字段、表头行有两行都像、
   表里没有往来单位、失败行占比过高……都变成结构化问题（选择题/填空题），
   用户答完按答复重算，最多 2 轮，剩下的按默认值走并留 warning。
4. 自己修（autorepair）：日期整列解析失败时，自己试几种日期格式，
   哪种能救回最多行就用哪种，试的过程写进 trace——这是「自主」而不是「等指示」。

所有决定都落 trace（谁做的：rule / llm / auto / user，置信度多少，依据是什么），
前端可以把这条链完整展示出来，也可以回放给评审看。
"""
import re

from . import sheet as sheetmod
from .sheet import FIELD_ENUM

# ---------------------------------------------------------------------------
# 表头词典（列名 → 字段）。中文 ERP 导出/手工表的常见叫法，命中越多越可信。
# ---------------------------------------------------------------------------
HEADER_LEX = {
    "item_name": ["商品", "品名", "货品", "货物", "产品", "材料", "物料", "存货",
                  "商品名称", "货品名称", "名称", "品名规格", "规格型号", "型号", "规格"],
    "qty": ["数量", "件数", "个数", "数目", "入库数量", "出库数量", "销售数量",
            "采购数量", "收货数量", "发货数量", "qty", "quantity"],
    "unit": ["单位", "计量单位", "规格单位", "基本单位"],
    "price": ["单价", "进价", "售价", "采购价", "销售价", "含税单价", "不含税单价",
              "成交价", "price", "unitprice"],
    "amount": ["金额", "总价", "合计金额", "价税合计", "小计", "总额", "应收金额",
               "应付金额", "amount", "total"],
    "party": ["供应商", "客户", "往来单位", "供货商", "购货单位", "销售单位",
              "对方单位", "单位名称", "往来客户", "supplier", "customer", "vendor"],
    "depot": ["仓库", "仓位", "库房", "存放地点", "入库仓库", "出库仓库"],
    "oper_time": ["日期", "单据日期", "业务日期", "时间", "开单日期", "入库日期",
                  "出库日期", "制单日期", "date"],
    "pay_amount": ["实付", "实付金额", "付款金额", "已付", "已付金额", "收款金额",
                   "实收", "实收金额"],
    "tax_rate": ["税率", "税点", "税%", "税率%"],
    "remark": ["备注", "说明", "摘要", "note", "remark", "memo"],
    "bill_no": ["单号", "单据号", "订单号", "原单号", "编号", "单据编号",
                "billno", "orderno", "单号编号"],
}

FIELD_ZH = {
    "item_name": "商品名", "qty": "数量", "unit": "单位", "price": "单价",
    "amount": "金额", "party": "往来单位", "depot": "仓库", "oper_time": "日期",
    "pay_amount": "实付金额", "tax_rate": "税率", "remark": "备注", "bill_no": "原单号",
}

BIZ_LEX = [
    (r"销售|销售出库|出库单|销货|卖给", "sale_out"),
    (r"采购|采购入库|入库单|进货|收货", "purchase_in"),
    (r"采购退货|退给供应商|退货给", "purchase_return"),
    (r"销售退货|客户退|退回", "sale_return"),
]
BIZ_VALUES = {
    "入库": "purchase_in", "采购入库": "purchase_in", "进货": "purchase_in",
    "出库": "sale_out", "销售出库": "sale_out", "销售": "sale_out",
    "退货": "purchase_return", "采购退货": "purchase_return",
}

REQUIRED = ("item_name", "qty")


def norm_header(s):
    """表头归一：去空格/标点、全角转半角、小写、去「(元)」「（数量）」这类括注。"""
    s = str(s or "").strip().lower()
    s = s.translate(str.maketrans("０１２３４５６７８９（）％．－", "0123456789()%.-"))
    s = re.sub(r"\([^)]*\)", "", s)
    s = re.sub(r"[\s_\-—/\\·:：,，。.、\"'“”‘’\[\]【】]", "", s)
    return s


def col_letter(idx):
    s, i = "", idx + 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def cell_kind(v):
    """单格粗分类：empty / num / date / text。只用于打分，不做解析。"""
    s = str(v or "").strip()
    if not s:
        return "empty"
    if sheetmod.cell_date(s):
        return "date"
    n = sheetmod.cell_num(s)
    if n is not None and re.search(r"\d", s):
        # 带货币符号/千分位的纯金额也算 num
        if not re.search(r"[\u4e00-\u9fa5]", re.sub(r"[元人民币¥￥$,，\s]", "", s)):
            return "num"
        return "num" if sheetmod.cell_unit(s) else "text"
    return "text"


def _padded(row, n):
    r = list(row or [])
    return r + [""] * (n - len(r))


# ---------------------------------------------------------------------------
# 1) 自己看：结构画像
# ---------------------------------------------------------------------------
def profile(rows, max_header_scan=6):
    """返回表格结构画像。全部规则计算，零 LLM、零外部依赖。"""
    width = min(max((len(r) for r in rows[:80]), default=0), sheetmod.MAX_COLS)
    data = [_padded(r, width) for r in rows]

    # 表头候选打分：命中表头词典 + 文本占比 + 非空数 + 下一行数字占比（表头下一行通常是数据）
    cands = []
    for i in range(min(max_header_scan, len(data))):
        row = data[i]
        cells = [str(x).strip() for x in row]
        nonempty = [c for c in cells if c]
        if len(nonempty) < 2:
            continue
        lex_hits = 0
        for c in nonempty:
            nh = norm_header(c)
            if any(norm_header(k) == nh or norm_header(k) in nh for kws in HEADER_LEX.values() for k in kws):
                lex_hits += 1
        textish = sum(1 for c in nonempty if re.search(r"[\u4e00-\u9fa5A-Za-z]", c) and len(c) <= 14)
        nxt = data[i + 1] if i + 1 < len(data) else []
        nxt_kinds = [cell_kind(x) for x in nxt if str(x).strip()]
        num_ratio = (nxt_kinds.count("num") / len(nxt_kinds)) if nxt_kinds else 0.0
        score = lex_hits * 2.0 + min(len(nonempty), 8) * 0.3 + textish * 0.5 + num_ratio * 2.0
        cands.append({"row": i + 1, "score": round(score, 2), "lex_hits": lex_hits,
                      "nonempty": len(nonempty),
                      "preview": " | ".join(cells)[:80]})
    cands.sort(key=lambda c: (-c["score"], c["row"]))
    header_row = cands[0]["row"] if cands else 1
    ambiguous = bool(len(cands) > 1 and cands[0]["score"] - cands[1]["score"] < 1.2)

    # 数据区：表头后到第一个合计行为止（合计行由 sheet._is_footer 判）
    body_start = header_row
    footer_row = None
    for ri in range(header_row, len(data)):
        if sheetmod._is_footer(data[ri]):
            footer_row = ri + 1
            break
    body_end = (footer_row - 1) if footer_row else len(data)
    body = [r for r in data[body_start:body_end] if any(str(x).strip() for x in r)]

    cols = []
    for ci in range(width):
        vals = [str(r[ci]).strip() for r in body if ci < len(r)]
        nonempty = [v for v in vals if v]
        kinds = [cell_kind(v) for v in nonempty]
        cols.append({
            "letter": col_letter(ci), "idx": ci,
            "header": str(data[header_row - 1][ci]).strip() if header_row - 1 < len(data) else "",
            "samples": nonempty[:3],
            "nonempty": len(nonempty),
            "num_ratio": round(kinds.count("num") / len(kinds), 2) if kinds else 0.0,
            "date_ratio": round(kinds.count("date") / len(kinds), 2) if kinds else 0.0,
            "text_ratio": round(kinds.count("text") / len(kinds), 2) if kinds else 0.0,
            "distinct": len(set(nonempty)),
        })
    return {
        "header_row": header_row,
        "header_candidates": cands[:3],
        "header_ambiguous": ambiguous,
        "footer_row": footer_row,
        "body_rows": len(body),
        "width": width,
        "cols": cols,
    }


# ---------------------------------------------------------------------------
# 2) 自己拿主意：规则映射
# ---------------------------------------------------------------------------
def _lex_score(header, field):
    """表头与字段词典的匹配度：完全相等 3 分，包含 2 分，被包含 1.4 分。"""
    nh = norm_header(header)
    if not nh:
        return 0.0
    best = 0.0
    for kw in HEADER_LEX.get(field, []):
        nk = norm_header(kw)
        if not nk:
            continue
        if nh == nk:
            best = max(best, 3.0)
        elif nk in nh:
            best = max(best, 2.0 + (0.2 if len(nk) >= 2 else 0))
        elif nh in nk and len(nh) >= 2:
            best = max(best, 1.4)
    return best


def rule_mapping(prof, rows):
    """按表头词典 + 列类型打分，产出 (cols{letter:field}, scores, trace)。"""
    scored = []   # (score, field, col)
    for c in prof["cols"]:
        if not c["nonempty"]:
            continue
        for f in FIELD_ENUM - {"ignore"}:
            s = _lex_score(c["header"], f)
            if s <= 0:
                continue
            # 类型一致性加分：数量/单价/金额偏数字，日期偏日期，往来单位偏文本
            if f in ("qty", "price", "amount", "pay_amount", "tax_rate") and c["num_ratio"] >= 0.6:
                s += 0.4
            if f == "oper_time" and c["date_ratio"] >= 0.6:
                s += 0.6
            if f == "party" and c["text_ratio"] >= 0.6:
                s += 0.3
            scored.append((round(s, 2), f, c))
    scored.sort(key=lambda x: (-x[0], x[2]["idx"]))

    cols, picked, used_cols, notes = {}, set(), set(), []
    for s, f, c in scored:
        if f in picked or c["letter"] in used_cols:
            continue
        if s < 1.3:
            continue
        cols[c["letter"]] = f
        picked.add(f)
        used_cols.add(c["letter"])
        notes.append({"step": "rule_map", "action": "列映射",
                      "detail": "%s列「%s」→ %s（匹配度 %.1f）" % (c["letter"], c["header"], FIELD_ZH[f], s),
                      "source": "rule", "confidence": "high" if s >= 2.5 else "medium"})
    return cols, scored, notes


def rule_biz(prof, cols):
    """整表业务类型：先看表头字眼，再看有没有「入库/出库」这种取值列。"""
    heads = " ".join(c["header"] for c in prof["cols"])
    for pat, biz in BIZ_LEX:
        m = re.search(pat, heads)
        if m:
            return biz, "", {}, "表头出现「%s」" % m.group(0)
    # 取值列：某列样本值高度集中在上面的业务词表里
    for c in prof["cols"]:
        vals = [v for v in c["samples"] if v]
        if vals and all(v in BIZ_VALUES for v in vals):
            return "purchase_in", c["letter"], dict(BIZ_VALUES), "检测到类型列 %s（取值：%s）" % (
                c["letter"], "、".join(vals))
    return "purchase_in", "", {}, "无业务类型线索，默认按采购入库（可改）"


def rule_mode(prof, cols, rows):
    """per_row 还是 group：有单号列且大量重复 → 按单号分组更接近原始单据。"""
    bill_col = next((c for c in prof["cols"] if cols.get(c["letter"]) == "bill_no"), None)
    if not bill_col:
        return "per_row", "", 0.0
    body = rows[prof["header_row"]:]
    vals = [str(r[bill_col["idx"]]).strip() for r in body if bill_col["idx"] < len(r)]
    vals = [v for v in vals if v]
    if len(vals) < 3:
        return "per_row", bill_col["letter"], 0.0
    dup = 1 - len(set(vals)) / len(vals)
    return ("group" if dup >= 0.5 else "per_row"), bill_col["letter"], round(dup, 2)


# ---------------------------------------------------------------------------
# 3) 拿不准就问：问题生成
# ---------------------------------------------------------------------------
def _col_options(prof, field_hint=None, include_empty=True):
    """给选择题准备「列」选项：列号 + 表头 + 样本，用户一眼能认。"""
    opts = []
    for c in prof["cols"]:
        if not c["nonempty"]:
            continue
        label = "%s列 %s" % (c["letter"], c["header"] or "（无表头）")
        if c["samples"]:
            label += "｜样本：" + " / ".join(s[:12] for s in c["samples"][:2])
        opts.append({"value": c["letter"], "label": label})
    if include_empty:
        opts.append({"value": "", "label": "（没有这一列 / 不使用）"})
    return opts


def build_questions(prof, mapping, mode, biz, dup_ratio, scores, result=None, round_no=1,
                    answered=()):
    """按「缺什么 / 冲突什么 / 失败什么」生成结构化问题。最多 4 个，按重要性排序。"""
    qs = []
    cols = mapping.get("cols") or {}
    mapped = {f: c for c, f in cols.items()}

    if prof["header_ambiguous"] and round_no == 1:
        opts = [{"value": str(c["row"]), "label": "第 %d 行：%s" % (c["row"], c["preview"])}
                for c in prof["header_candidates"][:3]]
        qs.append({
            "id": "header_row", "kind": "choose", "ask": "表头在第几行？",
            "why": "前几行都像表头，规则拿不准（候选得分 %s）" % " / ".join(
                str(c["score"]) for c in prof["header_candidates"][:3]),
            "options": opts, "default": str(prof["header_row"]), "blocking": False,
        })

    for f in REQUIRED:
        if f not in mapped:
            qs.append({
                "id": "field:" + f, "kind": "choose",
                "ask": "哪一列是%s？" % FIELD_ZH[f],
                "why": "没认出%s列，缺它没法生成单据" % FIELD_ZH[f],
                "options": _col_options(prof), "default": "", "blocking": True,
            })

    # 同一字段有多列候选（「数量」和「件数」）：不擅自挑，让用户指一下
    for f in ("qty", "price", "amount", "party", "item_name", "oper_time"):
        cand = []
        for c in prof["cols"]:
            if not c["nonempty"]:
                continue
            s = _lex_score(c["header"], f)
            if s >= 1.3:
                cand.append((s, c))
        if len(cand) < 2:
            continue
        cand.sort(key=lambda x: -x[0])
        cur = mapped.get(f)
        if f not in mapped:
            continue      # 缺这一列的问题上面已经问过（必答）
        # 已有主选且领先明显：不打扰（只有像到分不出来时才问）
        if cur == cand[0][1]["letter"] and cand[0][0] - cand[1][0] >= 1.0:
            continue
        qs.append({
            "id": "field:" + f, "kind": "choose",
            "ask": "有 %d 列都像%s，用哪一列？" % (len(cand), FIELD_ZH[f]),
            "why": "候选：" + "、".join("%s列「%s」(%.1f)" % (c["letter"], c["header"], s)
                                       for s, c in cand[:3]),
            "options": [{"value": c["letter"],
                         "label": "%s列 %s｜样本 %s" % (c["letter"], c["header"],
                                                      " / ".join(s2[:10] for s2 in c["samples"][:2]))}
                        for s, c in cand[:4]],
            "default": cur or cand[0][1]["letter"], "blocking": False,
        })

    if "party" not in mapped:
        qs.append({
            "id": "field:party", "kind": "choose",
            "ask": "哪一列是往来单位（供应商/客户）？",
            "why": "表里没认出往来单位列；如果整张表都是同一个单位，也可以直接在下面填名称",
            "options": _col_options(prof), "default": "",
            "allow_text": True, "text_hint": "整表同一个单位？直接填名称，例如「美宜佳」",
            "blocking": False,
        })

    # 一列能当两个字段：得分接近（词典本身有歧义，如「金额」既可能 amount 也可能 price）
    by_col = {}
    for s, f, c in scores:
        by_col.setdefault(c["letter"], []).append((s, f))
    for letter, lst in by_col.items():
        lst = [x for x in lst if x[0] >= 1.3][:2]
        if len(lst) == 2 and lst[0][0] - lst[1][0] < 0.5:
            f0, f1 = lst[0][1], lst[1][1]
            if f0 in mapped and f1 in mapped:
                continue      # 两个字段都另有归属，不用问
            qs.append({
                "id": "col:%s:field" % letter, "kind": "choose",
                "ask": "%s列「%s」到底算%s还是%s？" % (
                    letter, next(c["header"] for c in prof["cols"] if c["letter"] == letter),
                    FIELD_ZH[f0], FIELD_ZH[f1]),
                "why": "两种解释得分几乎一样（%.1f vs %.1f）" % (lst[0][0], lst[1][0]),
                "options": [{"value": f0, "label": FIELD_ZH[f0]},
                            {"value": f1, "label": FIELD_ZH[f1]},
                            {"value": "ignore", "label": "都不用（忽略这列）"}],
                "default": f0, "blocking": False,
            })

    if "bill_no" in mapped and round_no == 1 and dup_ratio >= 0.5:
        qs.append({
            "id": "mode", "kind": "choose", "ask": "多行共用一个单号，怎么处理？",
            "why": "单号列有 %.0f%% 的取值重复，像是同一张单的多行明细" % (dup_ratio * 100),
            "options": [{"value": "group", "label": "按单号合并成一张单（推荐）"},
                        {"value": "per_row", "label": "每行都单独建一张单"}],
            "default": "group", "blocking": False,
        })

    if not mapping.get("biz_col") and round_no == 1:
        heads = " ".join(c["header"] for c in prof["cols"])
        if re.search(r"入库|出库|收|发|退", heads):
            opts = [{"value": k, "label": v} for k, v in (
                ("purchase_in", "采购入库"), ("sale_out", "销售出库"),
                ("purchase_return", "采购退货"), ("sale_return", "销售退货"))]
            qs.append({
                "id": "biz", "kind": "choose", "ask": "整张表按哪种业务导入？",
                "why": "表头有出入库字样但看不出方向（当前先按%s）" % biz,
                "options": opts, "default": biz, "blocking": False,
            })

    # 失败行占比过高：不是逐行报错就完事，而是问「要不要我按某种方式修/跳过」
    if result and result.get("drafts"):
        drafts = result["drafts"]
        bad = [d for d in drafts if d.get("problems")]
        ratio = len(bad) / len(drafts) if drafts else 0
        if ratio >= 0.4:
            reasons = {}
            for d in bad:
                for p in d["problems"][:1]:
                    reasons[p] = reasons.get(p, 0) + 1
            top = max(reasons.items(), key=lambda kv: kv[1])[0] if reasons else ""
            qs.append({
                "id": "skip_failed", "kind": "boolean",
                "ask": "%d/%d 行有问题（主要是「%s」），先跳过这些行导入其余的吗？" % (
                    len(bad), len(drafts), top),
                "why": "失败行占比 %.0f%%，逐行改太慢；跳过可以先落库其余部分" % (ratio * 100),
                "options": [{"value": True, "label": "跳过问题行，先导入其余"},
                            {"value": False, "label": "不跳，我回去改映射"}],
                "default": True, "blocking": False,
            })

    # 用户这一轮已经答过的不再问（回答本身已进 trace，重复问很烦）
    qs = [q for q in qs if q["id"] not in set(answered or ())]
    qs.sort(key=lambda q: (0 if q.get("blocking") else 1))
    return qs[:4]


# ---------------------------------------------------------------------------
# 4) 自己修：日期格式自愈（确定性试错，不花 token）
# ---------------------------------------------------------------------------
DATE_FORMATS = ["%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%Y%m%d",
                "%d/%m/%Y", "%m/%d/%Y", "%Y年%m月%d日", "%d-%m-%Y"]


def auto_repair(rows, mapping, result, trace):
    """日期列整列解析失败时自动试格式；救回最多行的那一种直接采纳。"""
    bad_dates = sum(1 for d in result.get("drafts", [])
                    for p in d.get("problems", []) if "日期" in p)
    if not bad_dates:
        return mapping, result
    best = (bad_dates, mapping, result, None)
    for fmt in DATE_FORMATS:
        if fmt == mapping.get("date_format"):
            continue
        m2 = dict(mapping, date_format=fmt)
        try:
            tr2 = sheetmod.transform(rows, m2)
        except Exception:
            continue
        bad2 = sum(1 for d in tr2.get("drafts", [])
                   for p in d.get("problems", []) if "日期" in p)
        if bad2 < best[0]:
            best = (bad2, m2, tr2, fmt)
    if best[3]:
        trace.append({"step": "auto_repair", "action": "自动修复日期格式",
                      "detail": "试了 %d 种格式，采用 %s（日期错误 %d → %d）" % (
                          len(DATE_FORMATS), best[3], bad_dates, best[0]),
                      "source": "auto", "confidence": "high"})
        return best[1], best[2]
    return mapping, result


# ---------------------------------------------------------------------------
# 5) 主流程：看一眼 → 拿主意 →（可选）模型补位 → 自修 → 出问题
# ---------------------------------------------------------------------------
def build(rows, sheet_name="", answers=None, round_no=1, llm_fn=None):
    """返回 {mapping, questions, result, trace, prof, notes}。

    llm_fn(grid_desc) -> mapping dict 或 None：仅在规则缺关键列时调用一次；
    模型的判断与规则冲突时不覆盖规则，而是变成问题让用户裁决。
    """
    answers = answers or {}
    trace = []

    prof = profile(rows)
    header_row = prof["header_row"]
    if answers.get("header_row"):
        try:
            header_row = max(1, int(answers["header_row"]))
            prof = profile(rows, max_header_scan=1)
            prof["header_row"] = header_row
        except (TypeError, ValueError):
            pass

    cols, scores, map_notes = rule_mapping(prof, rows)
    trace.extend(map_notes)
    biz, biz_col, biz_values, biz_why = rule_biz(prof, cols)
    mode, group_col, dup_ratio = rule_mode(prof, cols, rows)
    trace.append({"step": "rule_mode", "action": "拆单方式",
                  "detail": ("按单号列 %s 分组（重复率 %.0f%%）" % (group_col, dup_ratio * 100))
                  if mode == "group" else "每行一张单",
                  "source": "rule", "confidence": "high" if dup_ratio >= 0.5 else "medium"})
    trace.append({"step": "rule_biz", "action": "业务类型", "detail": biz_why,
                  "source": "rule", "confidence": "medium"})

    mapping = {
        "header_row": header_row, "mode": mode, "group_col": group_col,
        "file_biz": biz, "biz_col": biz_col, "biz_values": biz_values,
        "cols": dict(cols), "date_format": "auto",
    }

    # 用户答复优先（一轮问答后进入这里）
    for qid, val in answers.items():
        if qid.startswith("field:"):
            f = qid.split(":", 1)[1]
            v = str(val or "").strip()
            is_col = bool(re.fullmatch(r"[A-Za-z]{1,2}", v)) and any(
                c["letter"] == v.upper() for c in prof["cols"])
            if is_col:
                for c, ff in list(mapping["cols"].items()):
                    if ff == f:
                        del mapping["cols"][c]
                mapping["cols"][v.upper()] = f
                detail = "用户选择列 %s" % v.upper()
            elif v and f == "party":
                mapping["party_default"] = v      # 整表同一个单位，直接填名字
                detail = "整表统一往来单位：%s" % v
            else:
                detail = "用户选择：%s" % (v or "（无）")
            trace.append({"step": "user_answer", "action": "指定%s" % FIELD_ZH.get(f, f),
                          "detail": detail, "source": "user", "confidence": "high"})
        elif qid.startswith("col:") and qid.endswith(":field"):
            letter = qid.split(":")[1]
            if val and val != "ignore":
                for c, ff in list(mapping["cols"].items()):
                    if ff == val:
                        del mapping["cols"][c]
                mapping["cols"][letter] = val
            else:
                mapping["cols"].pop(letter, None)
            trace.append({"step": "user_answer", "action": "裁决歧义列 %s" % letter,
                          "detail": "用户选择：%s" % (val or "忽略"), "source": "user",
                          "confidence": "high"})
        elif qid == "mode" and val in ("per_row", "group"):
            mapping["mode"] = val
            trace.append({"step": "user_answer", "action": "拆单方式",
                          "detail": "用户选择：%s" % val, "source": "user", "confidence": "high"})
        elif qid == "biz" and val in ("purchase_in", "sale_out", "purchase_return",
                                      "sale_return", "other_in", "other_out"):
            mapping["file_biz"] = val
            trace.append({"step": "user_answer", "action": "业务类型",
                          "detail": "用户选择：%s" % val, "source": "user", "confidence": "high"})

    # 模型补位：只在规则缺必需列时才叫，且不让它推翻规则
    if llm_fn and round_no == 1 and any(f not in mapping["cols"].values() for f in REQUIRED):
        try:
            gd = sheetmod.grid_desc(rows)
            ai = llm_fn(gd) or {}
            ai_cols = {str(k).upper(): v for k, v in (ai.get("cols") or {}).items()}
            added = 0
            for letter, f in ai_cols.items():
                if f in FIELD_ENUM - {"ignore"} and f not in mapping["cols"].values() \
                        and letter not in mapping["cols"]:
                    mapping["cols"][letter] = f
                    added += 1
            if ai.get("header_row") and prof["header_ambiguous"]:
                mapping["header_row"] = int(ai["header_row"])
            trace.append({"step": "llm_fill", "action": "模型补认列",
                          "detail": "模型补了 %d 列（%s）" % (added, ai.get("notes") or "无说明"),
                          "source": "llm", "confidence": ai.get("confidence") or "medium"})
        except Exception as e:
            trace.append({"step": "llm_fill", "action": "模型补认列失败",
                          "detail": str(e)[:120], "source": "llm", "confidence": "low"})

    try:
        result = sheetmod.transform(rows, mapping)
    except Exception as e:
        return {"ok": False, "message": "按当前映射变换失败：%s" % e,
                "mapping": mapping, "questions": [], "trace": trace, "prof": prof}

    mapping, result = auto_repair(rows, mapping, result, trace)

    # 统一把「整表同一个往来单位」补进每一行草稿（用户答复的文本）
    if mapping.get("party_default"):
        for d in result.get("drafts", []):
            if not (d["draft"].get("party") or "").strip():
                d["draft"]["party"] = mapping["party_default"]

    questions = build_questions(prof, mapping, mapping["mode"], mapping["file_biz"],
                                dup_ratio, scores, result, round_no, answered=set(answers))

    # 无问题时补一条「完成」的 trace，前端可展示决策链收尾
    if not questions:
        trace.append({"step": "ready", "action": "可以直接预览",
                      "detail": "%d 行可导入，%d 行有问题" % (
                          result["summary"]["ok"], result["summary"]["failed"]),
                      "source": "auto", "confidence": "high"})
    return {"ok": True, "mapping": mapping, "questions": questions, "result": result,
            "trace": trace, "prof": {"header_row": prof["header_row"],
                                     "body_rows": prof["body_rows"],
                                     "footer_row": prof["footer_row"],
                                     "width": prof["width"],
                                     "header_ambiguous": prof["header_ambiguous"]},
            "round": round_no}
