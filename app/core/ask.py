#!/usr/bin/env python3
"""Talk2Stock 统一业务 Agent：查账、录单与系统操作共用一次推理循环。

模型理解意图并决定工具；工具负责结构化数据与业务约束；错误和未通过接地的
回答会回喂给模型修正。draft_order 只创建可编辑草稿，写入仍须人工确认。
历史规则路由已移除，确定性统计函数继续作为可审计工具服务。
"""
import re
import time
import uuid

from . import config as cfgmod
from . import dbread as dbmod
from . import draft as draftmod
from .report import official_stats, recent_bills as _rb, stock_overview, summary, _safe_stock_map
from .erp import get_client, parallel

PROMPT_VERSION = "unified-v1-20261119"

# --------------------------------------------------------------------------
# 复合问题拆解提示：只拆分真正并列的多个子问题（避免把「可乐还有多少库存」里的「还有」误拆）
# --------------------------------------------------------------------------
def _compound_hints(text):
    """复合问题：把子问题显式列成清单注入 goal——实测仅靠规则约束模型仍会
    只查一半就作答（"库存总值未取到"式偷懒），显式清单是最可靠的干预。"""
    parts = re.split(r"另外|以及|顺便|然后|同时|还有(?!(?:多少|几|吗|没有|剩))", text or "")
    parts = [p.strip(" ？?，。,") for p in parts if p.strip(" ？?，。,")]
    if len(parts) > 1:
        return ("\n\n# 该问题包含 %d 个子问题，每个都必须先用工具取证、全部查完才能作答：\n"
                % len(parts)) + "\n".join("- %s" % p for p in parts)
    return ""


def _context(erp):
    """主数据名单作为 agent 的 context block，帮模型对齐系统里的真实名称。"""
    try:
        mats = []
        for m in erp.materials()[:80]:
            nm = str(m.get("name") or "").strip()
            mo = str(m.get("model") or "").strip()
            mats.append(f"{nm} {mo}" if nm and mo else nm)
        deps = [d.get("name") for d in erp.depots()][:20]
        out = ("# 系统主数据参考（工具参数尽量用这里的写法，商品带型号的整串照抄）\n商品："
               + "、".join(filter(None, mats)) + "\n仓库：" + "、".join(filter(None, deps)))
    except Exception:
        out = ""
    # 全库表结构动态注入：数据库不可达时为空串，旧工具照常可用
    try:
        brief = dbmod.schema_brief()
        if brief:
            out = (out + "\n\n" + brief).strip()
    except Exception:
        pass
    return out


# --------------------------------------------------------------------------
# 工具实现（全部只读，返回 (data, 模板句)——模板句是接地兜底，只用 data 里的数字）
# --------------------------------------------------------------------------
def _cand_label(c):
    """商品候选的可读写法：名字+型号（同名不同规格靠型号区分）。"""
    if isinstance(c, dict):
        return c.get("name", "") + (f"（{c['model']}）" if c.get("model") else "")
    return str(c)


def _stock_query(args, erp):
    name = str(args.get("material") or "").strip()
    mrow, kind = erp.find_material(name)
    if not mrow:
        cands = erp.material_candidates(name)
        labels = [_cand_label(c) for c in cands]
        return {"material": name, "found": False, "candidates": cands}, \
            f"系统里没有查到「{name}」的唯一档案" + (
                f"，最接近的档案：{'、'.join(labels)}，请说明要哪个规格" if labels else "")
    depot_filter = str(args.get("depot") or "").strip()
    if depot_filter:
        # 仓库过滤走 find_depot（exact/模糊/双向包含），与商品匹配同口径。
        # 查无此仓必须明说——旧实现精确匹配查空后误报「没有库存记录」（失败案例 F10）。
        drow, dkind = erp.find_depot(depot_filter)
        if not drow:
            cands = erp._ranked_candidates(erp.depots(), "name", depot_filter, n=3)
            data = {"material": mrow.get("name"), "depot": depot_filter,
                    "found_depot": False, "candidates": cands}
            note = (f"系统里没有叫「{depot_filter}」的仓库"
                    + (f"，最接近的仓库：{'、'.join(cands)}" if cands else "。")
                    + f"。要查「{mrow.get('name')}」库存请用正确的仓库名，或不指定仓库查各仓合计。")
            return data, note
        deps = [drow]
    else:
        deps = erp.depots()
    rows, total = [], 0.0
    for d in deps:
        st = erp.stock(mrow["id"], d["id"])
        if st is None:
            continue
        rows.append({"depot": d.get("name"), "stock": float(st)})
        total += float(st)
    unit = mrow.get("unit") or ""
    data = {"material": mrow.get("name"), "unit": unit, "by_depot": rows,
            "total": round(total, 2), "matched": kind or "exact"}
    if depot_filter:
        data["depot_matched"] = dkind
    detail = "；".join(f"{r['depot']} {r['stock']:g}" for r in rows) if rows else "各仓均无库存记录"
    return data, (f"「{mrow.get('name')}」当前库存 {total:g}{unit}。{detail}。"
                  if rows else f"「{mrow.get('name')}」在系统里没有库存记录。")


def _low_stock(_args, erp):
    ov = stock_overview(erp, safe_map=_safe_stock_map(erp))
    warn = ov.get("warn_list") or []
    data = {"kinds": ov.get("kinds"), "negative": ov.get("negative"),
            "low": ov.get("low"), "threshold": ov.get("threshold"),
            "warn_list": warn[:12]}
    head = f"共 {ov.get('kinds')} 种商品，其中 {ov.get('negative')} 种负库存、{ov.get('low')} 种低于 {ov.get('threshold')}。"
    body = "最紧张：" + "；".join(f"{w['name']} {w['stock']:g}{w['unit']}" for w in warn[:5]) if warn else "暂无紧张库存。"
    return data, head + body


def _today_stats(_args, erp):
    st = official_stats(erp)
    scope = st.get("today") or {}
    data = {"scope": "今天", "buy": scope.get("buy"), "buy_n": scope.get("buy_n"),
            "sell": scope.get("sell"), "sell_n": scope.get("sell_n"),
            "profit_cash": scope.get("profit")}
    return data, (f"今天采购 {scope.get('buy'):,.2f} 元（{scope.get('buy_n')} 笔），"
                  f"销售 {scope.get('sell'):,.2f} 元（{scope.get('sell_n')} 笔），"
                  f"现金流差额 {scope.get('profit'):,.2f} 元（销售-采购，非毛利）。")


def _month_stats(_args, erp):
    st = official_stats(erp)
    scope = st.get("month") or {}
    data = {"scope": "本月", "buy": scope.get("buy"), "buy_n": scope.get("buy_n"),
            "sell": scope.get("sell"), "sell_n": scope.get("sell_n"),
            "profit_cash": scope.get("profit")}
    return data, (f"本月采购 {scope.get('buy'):,.2f} 元（{scope.get('buy_n')} 笔），"
                  f"销售 {scope.get('sell'):,.2f} 元（{scope.get('sell_n')} 笔），"
                  f"现金流差额 {scope.get('profit'):,.2f} 元（销售-采购，非毛利）。")


def _debts(_args, erp):
    s = summary(erp)
    data = {"receivable_top": s.get("debt_recv"), "receivable_total": s.get("debt_recv_total"),
            "payable_top": s.get("debt_pay"), "payable_total": s.get("debt_pay_total")}
    recv = "；".join(f"{x['name']} {x['amount']:,.2f}" for x in (s.get("debt_recv") or [])[:4])
    pay = "；".join(f"{x['name']} {x['amount']:,.2f}" for x in (s.get("debt_pay") or [])[:4])
    tpl = (f"客户共欠我 {s.get('debt_recv_total'):,.2f} 元（{recv or '无'}）；"
           f"我欠供应商 {s.get('debt_pay_total'):,.2f} 元（{pay or '无'}）。")
    return data, tpl


def _recent_bills(_args, erp):
    bills = _rb(erp, limit=6)
    data = {"bills": bills, "count": len(bills)}
    if not bills:
        return data, "系统里还没有单据。"
    lines = "；".join(f"{b['time']} {b['type']} {b['party']} {b['total']:,.2f}元" for b in bills[:5])
    return data, f"最近 {len(bills)} 张单：{lines}。"


def _stock_value(_args, erp):
    st = official_stats(erp)
    sm = st.get("stock_money") or {}
    data = {"total_stock": sm.get("total_stock"), "total_value": sm.get("total_value")}
    return data, (f"库存货值约 {sm.get('total_value'):,.2f} 元"
                  f"（{sm.get('total_stock'):g} 件，按当前成本价估算）。")


def _procurement_suggest(args, erp):
    """agent 工具：智能采购建议（确定性算法，数字可复算）。"""
    from . import procurement as pmod
    return pmod.tool_suggest(erp, args)


def _master_count(args, erp):
    """主数据计数（走 ERP REST 业务口径：自动排除软删除/停用档案）。"""
    raw = str(args.get("entity") or args.get("kind") or "").strip()
    alias = {"supplier": "供应商", "customer": "客户", "material": "商品", "product": "商品",
             "depot": "仓库", "warehouse": "仓库", "account": "账户", "unit": "单位", "category": "分类"}
    ent = alias.get(raw.lower(), raw) or "商品"
    fetch = {
        "供应商": lambda: erp.suppliers("供应商"),
        "客户": lambda: erp.suppliers("客户"),
        "商品": lambda: erp.materials(), "产品": lambda: erp.materials(),
        "仓库": lambda: erp.depots(), "账户": lambda: erp.accounts(),
        "单位": lambda: erp.units(), "分类": lambda: erp.categories(),
        "收支项目": lambda: erp.in_out_items(),
    }
    rows = (fetch.get(ent) or fetch["商品"])() or []
    unit = {"供应商": "家", "客户": "家", "商品": "种", "产品": "种",
            "分类": "类"}.get(ent, "个")
    def label(r):
        for k in ("supplier", "name", "userName"):
            if r.get(k):
                return str(r[k])
        return ""
    names = [x for x in (label(r) for r in rows) if x][:6]
    data = {"entity": ent, "count": len(rows), "sample": names}
    tpl = f"系统里共有 {len(rows)} {unit}{ent}。"
    if names:
        tpl += "例如：" + "、".join(names) + ("等。" if len(rows) > len(names) else "。")
    return data, tpl


def _query_bills(args, erp):
    """按往来单位/日期/商品查单据明细（v3 评测补的工具面：「谁买了什么/某天卖了
    什么/某商品最近进出」这类多跳问题此前只能靠模型拼多表 SQL，实测反复失败）。"""
    party = str(args.get("party") or "").strip()
    date_s = str(args.get("date") or "").strip()   # YYYY-MM-DD / 今天 / 昨天
    material = str(args.get("material") or "").strip()
    today = time.strftime("%Y-%m-%d")
    alias = {"今天": today, "昨天": time.strftime("%Y-%m-%d", time.localtime(time.time() - 86400))}
    day = alias.get(date_s, date_s)
    if day and not re.match(r"^\d{4}-\d{2}-\d{2}$", day):
        return {"error": "date 要用 YYYY-MM-DD（或「今天」「昨天」）", "got": date_s}, \
            "date 参数没认出来（%s），请用 YYYY-MM-DD" % date_s
    bills = erp.bills(page_size=500)
    rows, matched_ids = [], []
    for b in bills:
        if str(b.get("deleteFlag")) == "1":
            continue
        t = str(b.get("operTime") or "")[:10]
        if day and t != day:
            continue
        if party and erp._norm(party) not in erp._norm(b.get("organName") or ""):
            continue
        goods = str(b.get("materialsList") or "")
        if material and erp._norm(material) not in erp._norm(goods):
            continue
        rows.append({"number": b.get("number"), "time": t,
                     "type": (b.get("type") or "") + "·" + (b.get("subType") or ""),
                     "party": b.get("organName") or "—",
                     "goods": goods.strip()[:60], "total": float(b.get("totalPrice") or 0),
                     "debt": float(b.get("debt") or 0)})
        matched_ids.append(b.get("id"))
        if len(rows) >= 20:
            break
    # 补明细行（materialsList 只有商品名没有数量；「电池今天卖了多少」要的是数量）
    if rows:
        bc2name = {}
        for m in erp.materials():
            bc = str(m.get("mBarCode") or m.get("barCode") or "")
            if bc:
                bc2name[bc] = (str(m.get("name") or ""), str(m.get("unit") or ""))
        details = parallel([lambda i=i: erp._bill_details(i) for i in matched_ids], workers=6)
        for row, drows in zip(rows, details):
            items = []
            for dr in (drows or [])[:10]:
                name, unit = bc2name.get(str(dr.get("barCode") or ""), ("?", ""))
                try:
                    qty = float(dr.get("operNumber") or 0)
                except (TypeError, ValueError):
                    qty = 0.0
                items.append({"name": name, "qty": qty, "unit": unit})
            row["items"] = items
    data = {"filters": {"party": party, "date": day, "material": material},
            "count": len(rows), "bills": rows}
    scope = "、".join(x for x in ((party or ""), (day or ""), ("含" + material) if material else "") if x)
    if not rows:
        return data, "没有查到符合条件（%s）的单据（最近 %d 张里）。" % (scope or "给定条件", len(bills))
    lines = "；".join("%s %s %s %s %g元" % (r["time"], r["party"], r["goods"][:20], r["type"], r["total"])
                      for r in rows[:5])
    return data, "查到 %d 张：%s。" % (len(rows), lines)


# --------------------------------------------------------------------------
# supervisor 写路由（ADR-10）：录单/下单需求 → 起草单据草稿。
# 写权限边界在这里划死：本函数绝不写库，只产出「预览 + 逐单确认令牌」；
# 令牌与对话录单共用同一闸门（gate.issue，与草稿内容哈希绑定），真正写入
# 必须由用户在确认卡上确认（/api/commit 校验令牌）——agent 自主性到草稿为止。
# --------------------------------------------------------------------------
DRAFT_TEXT_MAX = 1000   # 用户消息上限（与多单解析口径一致）
DRAFT_ATTACH_TEXT_MAX = 6000   # 带文本附件（PDF/DOCX/TXT）时的合并上限
# 草稿工具只管「新建」：改/删/调整已有数据在这里拦下；修订尚未写入的草稿例外。
_DRAFT_DENY = re.compile(r"(已有|现存|库存|单据|记录|档案).{0,6}(改|删|修改|更新|作废|冲销|调整|撤销|红冲)|(改|删|修改|更新|作废|冲销|调整|撤销|红冲).{0,6}(已有|现存|库存|单据|记录|档案)")


def _draft_order(args, erp, on_event=None, images=None, previous_draft=None, attachments=None):
    raw_user_text = str(args.get("text") or "").strip()
    text = raw_user_text
    images = [str(x) for x in (args.get("images") or images or [])[:4]]
    attachments = [x for x in (attachments or []) if isinstance(x, dict)]
    selected = None
    has_text_attach = False
    if attachments:
        from . import attachments as attachmod
        selector = args.get("attachment") or args.get("attachment_id") or ""
        selected = attachmod.pick(attachments, selector)
        if selected is None and len(attachments) == 1:
            selected = attachments[0]
        if selected is None:
            names = "、".join("%d.%s" % (i + 1, a.get("name")) for i, a in enumerate(attachments))
            return ({"error": "有多个附件但未指定 attachment",
                     "attachments": [a.get("name") for a in attachments]},
                    "请先说明要处理哪个附件（%s），在 args.attachment 传编号或文件名。" % names)
        if selected.get("error") and selected.get("kind") == "unsupported":
            return ({"error": "附件无法读取：%s" % selected.get("error"), "attachment": selected.get("name")},
                    "附件「%s」无法读取：%s。" % (selected.get("name"), selected.get("error")))
        if selected.get("kind") == "image" and selected.get("data_url"):
            images = [selected["data_url"]]
        if selected.get("kind") == "text" and selected.get("text"):
            has_text_attach = True
            text = (text + "\n\n【附件「%s」的内容】\n%s" % (
                selected.get("name"), selected.get("text")))[:DRAFT_ATTACH_TEXT_MAX]
    if not text and images:
        text = "请把图片中的业务内容整理成单据草稿。"
    if not text:
        return ({"error": "缺少 text 参数（用户的录单原话）"},
                "draft_order 需要 text 参数：把用户的录单需求原话传进来。")
    if _DRAFT_DENY.search(raw_user_text or text):
        return ({"refused": "modification_not_supported", "text": raw_user_text or text},
                "起草工具只能新建单据，不能修改或删除已有数据——请向用户说明。")
    if not has_text_attach and len(text) > DRAFT_TEXT_MAX:
        return ({"error": "内容过长（%d 字，上限 %d）" % (len(text), DRAFT_TEXT_MAX)},
                "录单内容太长（上限 %d 字），请让用户分次录入。" % DRAFT_TEXT_MAX)
    from . import billcheck as bcmod
    from . import llm as llmmod
    try:
        check = {"ok": True, "skipped": True}
        if selected and selected.get("kind") == "table" and selected.get("table_rows"):
            from . import sheet as sheetmod
            rows = selected["table_rows"]
            mapping = llmmod.map_sheet(selected.get("table_name") or selected.get("name"), sheetmod.grid_desc(rows))
            tr = sheetmod.transform(rows, mapping)
            drafts = [d["draft"] for d in tr.get("drafts") or []]
            meta = {"model": "", "profile": "", "confidence": "medium",
                    "note": "从附件表格识别，请核对行级提示", "problems": [],
                    "bills": [{"confidence": "medium", "note": "", "problems": d.get("problems") or []}
                              for d in tr.get("drafts") or []],
                    "clarify": None, "count": len(drafts)}
            check = bcmod.check_and_patch(drafts, erp, operator=cfgmod.current_username(),
                                          on_event=on_event, meta=meta)
        elif images:
            drafts, meta = llmmod.parse_images(images, text, erp)
            check = bcmod.check_and_patch(drafts, erp, operator=cfgmod.current_username(),
                                          on_event=on_event, meta=meta)
        else:
            parsed = bcmod.parse_and_check(
                text, erp, operator=cfgmod.current_username(), prev_draft=previous_draft,
                on_event=on_event)
            drafts, meta, check = parsed["drafts"], parsed["meta"], parsed["check"]
            if parsed.get("clarify"):
                meta["clarify"] = parsed["clarify"]
            elif parsed.get("clarify_all"):
                meta["clarify"] = {"question": parsed["clarify_all"]}
    except Exception as e:
        return ({"error": "解析失败：%s" % str(e)[:120]}, "单据解析失败：%s" % str(e)[:120])
    if not drafts:
        return ({"parsed": False, "text": text},
                "这段话里没有识别出可起草的单据——请向用户确认要录什么。")
    # 附件随草稿进入同一份哈希与确认卡：入库路径来自 ERP 上传代理
    attach_meta = []
    if selected and selected.get("path"):
        attach_meta = [{"path": selected["path"], "name": selected.get("name") or "附件"}]
        for d in drafts:
            d["file_name"] = selected["path"]
    batch_id = uuid.uuid4().hex[:12]
    bills = draftmod.prepare_bills(drafts, meta, erp, "ai", text, batch_id)
    for b in bills:
        if attach_meta:
            b["attach"] = [dict(a) for a in attach_meta]
    rows = []
    for b in bills:
        d, pv = b["draft"], b["preview"]
        items = []
        for it, pvit in zip(d.get("items") or [], (pv.get("items") or [])):
            items.append({"name": it.get("name"), "qty": it.get("qty"),
                          "unit": it.get("unit"), "price": it.get("price"),
                          "matched": (pvit.get("matched_name") or it.get("name") or "")})
        rows.append({
            "biz": draftmod.BIZ_LABEL.get(d.get("biz"), d.get("biz") or ""),
            "party": d.get("party") or "", "depot": d.get("depot") or "",
            "items": items,
            "attachments": [a.get("name") for a in attach_meta],
            "total": pv.get("total"),
            "missing_price_lines": sum(1 for x in (pv.get("items") or []) if x.get("price_missing")),
            "warnings": pv.get("warnings") or [],
            "errors": pv.get("errors") or [],
        })
    cl = meta.get("clarify") if isinstance(meta.get("clarify"), dict) else None
    data = {"drafts": len(bills), "bills": rows, "written": False,
            "needs_user_confirm": True}
    if cl:
        data["clarify"] = cl.get("question", "")
    note = ("已起草 %d 张单据草稿（仅预览，未写入系统）：%s。请告诉用户在确认卡上核对"
            "内容并确认写入；有缺失项的草稿请提醒用户补全。" % (
                len(bills),
                "；".join("%s %s %d行" % (r["biz"], r["party"] or "？", len(r["items"])) for r in rows)))
    if cl:
        note = note + "待补信息：" + str(cl.get("question") or "")
    extra = {"batch_id": batch_id, "bills": bills,
             "meta": draftmod.meta_public(meta), "raw_text": text,
             "check": check, "clarify": cl, "attachments": attach_meta}
    return data, note, extra


def _tools(erp, on_event=None, images=None, previous_draft=None, attachments=None):
    """构造 Agent 工具箱；draft_order 是起草能力，不是写入能力。"""
    def bind(fn):
        return lambda args: fn(args, erp)
    from . import retrieve as rmod
    return {
        "stock_query": {"desc": "查某种商品的库存（哪个仓、还剩多少）",
                        "args_hint": '{"material":"商品名","depot":"仓库名，没说就空字符串"}',
                        "run": bind(_stock_query)},
        "low_stock": {"desc": "哪些货快没了/缺货/该补货了", "args_hint": "{}",
                      "run": bind(_low_stock)},
        "today_stats": {"desc": "今天买了多少/卖了多少", "args_hint": "{}",
                        "run": bind(_today_stats)},
        "month_stats": {"desc": "本月采购、销售、差额", "args_hint": "{}",
                        "run": bind(_month_stats)},
        "debts": {"desc": "谁欠我钱、我欠谁钱（应收应付）", "args_hint": "{}",
                  "run": bind(_debts)},
        "recent_bills": {"desc": "最近录了哪些单（最多 6 张，不是全量；问单据总数用 sql_query COUNT）", "args_hint": "{}",
                         "run": bind(_recent_bills)},
        "stock_value": {"desc": "库存里的货总共值多少钱", "args_hint": "{}",
                        "run": bind(_stock_value)},
        "master_count": {"desc": "查系统里有多少个供应商/客户/商品/仓库/账户/单位/分类（一步出准确数，排除停用和软删除）",
                         "args_hint": '{"entity":"供应商|客户|商品|仓库|账户|单位|分类|收支项目"}',
                         "run": bind(_master_count)},
        "search_docs": {"desc": "查操作文档（怎么录单/什么字段必填/术语解释等概念问题）",
                        "args_hint": '{"query":"检索词"}',
                        "run": lambda a: rmod.search_docs(str(a.get("query") or ""))},
        "search_history": {"desc": "搜历史单据（上次/之前跟谁买过什么、某商品进过几次货、原话找单）",
                           "args_hint": '{"query":"检索词，如商品名或往来单位"}',
                           "run": lambda a: rmod.search_history(str(a.get("query") or ""))},
        "query_bills": {"desc": "按条件查单据明细（谁买了什么/某天卖了什么/某商品最近进出/某单位欠款单）",
                        "args_hint": '{"party":"往来单位，可空","date":"YYYY-MM-DD或今天/昨天，可空","material":"商品名，可空"}',
                        "run": lambda a: _query_bills(a, erp)},
        "procurement_suggest": {"desc": "哪些商品该补货、建议采购多少（按近30天销量/当前库存/安全库存/采购周期确定性计算，数字可复算）",
                                "args_hint": '{"days":"销量统计天数，可空默认30"}',
                                "run": lambda a: _procurement_suggest(a, erp)},
        "sql_query": {"desc": "对 ERP 全库只读 SQL 查询：任意表的清单/数量/明细/汇总/排名/时间区间，专用工具没覆盖的问题一律用它",
                      "args_hint": '{"sql":"单条 SELECT；表结构见上下文；软删除表加 delete_flag 为 0 的条件；大表带 LIMIT"}',
                      "run": lambda a: dbmod.sql_query(a)},
        "db_schema": {"desc": "看某张表的全部字段与取值样本（写 SQL 前不确定字段/枚举值先查它）",
                      "args_hint": '{"table":"表名，如 jsh_supplier"}',
                      "run": lambda a: dbmod.db_schema(a)},
        "draft_order": {"desc": "用户要录单/新建单据时起草草稿；有附件时按编号/文件名选择对应文件（只出预览绝不写库）",
                        "args_hint": '{"text":"用户的业务原话（结合会话补全代词）","attachment":"有附件时传编号或文件名；无附件留空"}',
                        "run": lambda a: _draft_order(a, erp, on_event, images,
                                                       previous_draft, attachments)},
    }


# --------------------------------------------------------------------------
# 入口：统一业务 Agent 循环
# --------------------------------------------------------------------------
def _conversation_context(history):
    """有界会话记忆：保留意图与草稿状态，不回放确认令牌或大载荷。"""
    rows = []
    for m in (history or [])[-10:]:
        if not isinstance(m, dict):
            continue
        role = "用户" if m.get("role") == "user" else "助手"
        content = str(m.get("content") or "").strip()[:1200]
        if content:
            rows.append("%s：%s" % (role, content))
    return "# 当前会话（供代词、追问和草稿修正使用）\n" + "\n".join(rows) if rows else ""


def ask(text, erp=None, on_event=None, history=None, images=None, previous_draft=None,
        attachments=None):
    """统一 Agent 入口：模型识别意图、选工具、根据观察继续或修正。

    所有业务请求都经过同一个循环，不再由正则表达式抢占主入口。
    确定性统计仍作为工具复用；草稿与真实写入之间保留人工确认闸门。
    """
    erp = erp or get_client()
    prof = cfgmod.agent_profile()
    if not prof:
        return {"ok": False, "message": "尚未配置 AI 模型，请先在「设置」里添加。"}
    meta = {"model": prof.get("model", ""), "prompt_version": PROMPT_VERSION}
    draft_events = []

    def _emit(evt):
        if evt.get("type") == "draft":
            draft_events.append(evt.get("payload") or {})
        if on_event:
            try:
                on_event(evt)
            except Exception:
                pass

    from . import agent as agentmod
    images = [x for x in (images or []) if isinstance(x, str) and re.match(r"^data:image/(?:png|jpeg|jpg|webp|gif);base64,[A-Za-z0-9+/=]+$", x)][:4]
    previous_draft = previous_draft if isinstance(previous_draft, dict) and previous_draft else None
    attachments = [x for x in (attachments or []) if isinstance(x, dict)][:6]
    attach_note = ""
    if attachments:
        from . import attachments as attachmod
        attach_note = attachmod.context_digest(attachments)
    image_note = "# 本次输入附有单据图片\ndraft_order 工具已能读取本次图片；若用户要录入图片中的业务，直接调用该工具。" if images and not attachments else ""
    context = "\n\n".join(x for x in (_context(erp), _conversation_context(history), attach_note, image_note) if x)
    goal = text + _compound_hints(text)
    out = agentmod.run_agent(goal, _tools(erp, on_event=_emit, images=images,
                                         previous_draft=previous_draft,
                                         attachments=attachments), max_steps=8,
                             context_block=context, on_event=_emit)
    if not out.get("ok"):
        out["meta"] = meta
        return out
    tools_used = [s["tool"] for s in out.get("steps", []) if s.get("tool") and s.get("ok")]
    obs = out.get("observations") or []
    return {
        "ok": True,
        "tool": tools_used[0] if len(tools_used) == 1 else "agent",
        "answer": out["answer"],
        "data": obs[0] if len(obs) == 1 else obs,
        "route": "agent",
        "grounded": out.get("grounded"),
        "steps": out.get("steps", []),
        "tokens": out.get("tokens", {}),
        "meta": dict(out.get("meta") or {}, **meta),
        "drafts": draft_events,
    }
