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
from .report import (official_stats, recent_bills as _rb, stock_overview, summary,
                     material_flow, _safe_stock_map)
from .erp import get_client, parallel

PROMPT_VERSION = "unified-v1-20261119"

# --------------------------------------------------------------------------
# 复合问题拆解提示：只拆分真正并列的多个子问题（避免把「可乐还有多少库存」里的「还有」误拆）
# --------------------------------------------------------------------------
def _compound_hints(text):
    """复合问题：把子问题显式列成清单注入 goal——实测仅靠规则约束模型仍会
    只查一半就作答（"库存总值未取到"式偷懒），显式清单是最可靠的干预。"""
    raw = text or ""
    if re.search(r"(生成草稿|起草|开一?张?单|录单|入库单|出库单)", raw) and not re.search(
            r"(今天卖了|本月|欠款|余额|排行|体检)", raw):
        return ""
    parts = re.split(r"另外|以及|顺便|然后|同时|还有(?!(?:多少|几|吗|没有|剩))", raw)
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
    flow = material_flow(mrow["id"], (deps[0].get("id") if depot_filter and deps else None)) or {}
    safe_val = None
    try:
        safe_val = _safe_stock_map(erp).get(int(mrow["id"]))
    except Exception:
        safe_val = None
    data = {"material": mrow.get("name"), "unit": unit, "by_depot": rows,
            "total": round(total, 2), "matched": kind or "exact",
            "initial": flow.get("initial"), "in_sum": flow.get("in_sum"),
            "out_sum": flow.get("out_sum")}
    if safe_val:
        data["safe_stock"] = safe_val
    if depot_filter:
        data["depot_matched"] = dkind
    detail = "；".join(f"{r['depot']} {r['stock']:g}" for r in rows) if rows else "各仓均无库存记录"
    trail = ""
    if flow.get("initial"):
        trail = ("期初 {initial:g}{u}、累计入库 {i:g}{u}、累计出库 {o:g}{u}。"
                 .format(u=unit, initial=flow["initial"], i=flow.get("in_sum") or 0,
                         o=flow.get("out_sum") or 0))
    safe_note = ""
    if safe_val and total < safe_val and not depot_filter:
        safe_note = f"低于安全库存 {safe_val:g}{unit}。"
    return data, (f"「{mrow.get('name')}」当前库存 {total:g}{unit}。{detail}。{trail}{safe_note}"
                  if rows else f"「{mrow.get('name')}」在系统里没有库存记录。")


def _low_stock(_args, erp):
    ov = stock_overview(erp, safe_map=_safe_stock_map(erp))
    warn = ov.get("warn_list") or []
    data = {"kinds": ov.get("kinds"), "negative": ov.get("negative"),
            "low": ov.get("low"), "threshold": ov.get("threshold"),
            "warn_list": warn[:12]}
    head = f"共 {ov.get('kinds')} 种商品，其中 {ov.get('negative')} 种负库存、{ov.get('low')} 种低于安全库存。"
    def _wlabel(w):
        if w.get("tight"):
            t = w["tight"]
            return ("%s 合计 %g%s（%s %g，安全线 %g）"
                    % (w["name"], w["stock"], w["unit"], t["depot"], t["stock"], t["low"]))
        return "%s %g%s（安全线 %g）" % (w["name"], w["stock"], w["unit"], w.get("threshold") or ov.get("threshold") or 0)

    body = "最紧张：" + "；".join(_wlabel(w) for w in warn[:5]) if warn else "暂无紧张库存。"
    return data, head + body


def _today_stats(_args, erp):
    st = official_stats(erp)
    scope = st.get("today") or {}
    data = {"scope": "今天", "buy": scope.get("buy"), "buy_n": scope.get("buy_n"),
            "sell": scope.get("sell"), "sell_n": scope.get("sell_n"),
            "return_n": scope.get("return_n", 0), "return_amt": scope.get("return_amt", 0.0),
            "sell_return_n": scope.get("sell_return_n", 0), "sell_return_amt": scope.get("sell_return_amt", 0.0),
            "buy_return_n": scope.get("buy_return_n", 0), "buy_return_amt": scope.get("buy_return_amt", 0.0),
            "profit_cash": scope.get("profit")}
    ret = (f"（含退货 {data['return_n']} 笔 {data['return_amt']:,.2f} 元）"
           if data["return_n"] else "")
    return data, (f"今天采购 {scope.get('buy'):,.2f} 元（{scope.get('buy_n')} 笔），"
                  f"销售 {scope.get('sell'):,.2f} 元（{scope.get('sell_n')} 笔）{ret}，"
                  f"现金流差额 {scope.get('profit'):,.2f} 元（销售-采购，非毛利）。")


def _month_stats(_args, erp):
    st = official_stats(erp)
    scope = st.get("month") or {}
    data = {"scope": "本月", "buy": scope.get("buy"), "buy_n": scope.get("buy_n"),
            "sell": scope.get("sell"), "sell_n": scope.get("sell_n"),
            "return_n": scope.get("return_n", 0), "return_amt": scope.get("return_amt", 0.0),
            "sell_return_n": scope.get("sell_return_n", 0), "sell_return_amt": scope.get("sell_return_amt", 0.0),
            "buy_return_n": scope.get("buy_return_n", 0), "buy_return_amt": scope.get("buy_return_amt", 0.0),
            "profit_cash": scope.get("profit")}
    ret = (f"（含退货 {data['return_n']} 笔 {data['return_amt']:,.2f} 元）"
           if data["return_n"] else "")
    return data, (f"本月采购 {scope.get('buy'):,.2f} 元（{scope.get('buy_n')} 笔），"
                  f"销售 {scope.get('sell'):,.2f} 元（{scope.get('sell_n')} 笔）{ret}，"
                  f"现金流差额 {scope.get('profit'):,.2f} 元（销售-采购，非毛利）。")


def _debts(_args, erp):
    s = summary(erp)
    data = {"receivable_top": s.get("debt_recv"), "receivable_total": s.get("debt_recv_total"),
            "payable_top": s.get("debt_pay"), "payable_total": s.get("debt_pay_total")}
    try:
        from . import ledger as ledgermod
        ds = ledgermod.debt_summary()
        ops = ledgermod.ops_counts()
        if ds:
            data["need_recv_count"] = int(ds.get("need_recv") or 0)
            data["need_pay_count"] = int(ds.get("need_pay") or 0)
        if ops:
            data["wait_bills"] = int(ops.get("wait_bills") or 0)
    except Exception:
        pass
    recv = "；".join(f"{x['name']} {x['amount']:,.2f}" for x in (s.get("debt_recv") or [])[:4])
    pay = "；".join(f"{x['name']} {x['amount']:,.2f}" for x in (s.get("debt_pay") or [])[:4])
    tpl = (f"客户共欠我 {s.get('debt_recv_total'):,.2f} 元（{recv or '无'}）；"
           f"我欠供应商 {s.get('debt_pay_total'):,.2f} 元（{pay or '无'}）。")
    return data, tpl


def _account_balance(_args, erp):
    """资金账户余额与本月净变动（统一走 ledger 会计流水口径，不读静态 current_amount）。"""
    acc = None
    try:
        from . import ledger as ledgermod
        acc = ledgermod.account_summary()
    except Exception:
        acc = None
    if acc:
        rows = [
            {"name": a.get("name") or "", "balance": float(a.get("amount") or 0),
             "initial": float(a.get("initial") or 0), "month_change": float(a.get("month") or 0)}
            for a in (acc.get("accounts") or [])
        ]
        data = {
            "total_balance": round(float(acc.get("current") or 0), 2),
            "month_change": round(float(acc.get("month") or 0), 2),
            "accounts": rows,
        }
    else:
        st = official_stats(erp)
        ac = st.get("account") or {}
        rows = [
            {"name": a.get("name") or "",
             "balance": round(float(a.get("currentAmount") or 0), 2)}
            for a in (erp.accounts() or [])
        ]
        data = {
            "total_balance": round(float(ac.get("current") or 0), 2),
            "month_change": round(float(ac.get("month") or 0), 2),
            "accounts": rows,
        }
    detail = "、".join(f"{a['name']} {a['balance']:,.2f} 元" for a in data["accounts"]) or "无活跃账户"
    tpl = (f"当前资金账户总余额 {data['total_balance']:,.2f} 元"
           f"（本月净变动 {data['month_change']:+,.2f} 元）：{detail}。")
    return data, tpl


def _ops_summary(_args, erp):
    """待审核、待收款、待付款单据张数与应收应付合计（权威账本口径）。"""
    ops = ds = acc = None
    try:
        from . import ledger as ledgermod
        ops = ledgermod.ops_counts()
        ds = ledgermod.debt_summary()
        acc = ledgermod.account_summary()
    except Exception:
        ops = ds = acc = None
    if ops is not None and ds is not None:
        wait = int(ops.get("wait_bills") or 0)
        recv = int(ds.get("need_recv") or 0)
        pay = int(ds.get("need_pay") or 0)
        recv_tot = round(float(ds.get("debt_recv_total") or 0), 2)
        pay_tot = round(float(ds.get("debt_pay_total") or 0), 2)
        acc_bal = round(float((acc or {}).get("current") or 0), 2)
    else:
        st = official_stats(erp)
        s = summary(erp)
        wait = int(st.get("wait_bills") or 0)
        recv = int(st.get("need_recv") or 0)
        pay = int(st.get("need_pay") or 0)
        recv_tot = round(float(s.get("debt_recv_total") or 0), 2)
        pay_tot = round(float(s.get("debt_pay_total") or 0), 2)
        acc_bal = round(float((st.get("account") or {}).get("current") or 0), 2)
    data = {
        "wait_bills": wait,
        "need_recv_count": recv,
        "need_pay_count": pay,
        "receivable_total": recv_tot,
        "payable_total": pay_tot,
        "account_balance": acc_bal,
    }
    tpl = (f"当前待审核单据 {wait} 张；"
           f"待收款（未收清销售单）{recv} 张、应收合计 {recv_tot:,.2f} 元；"
           f"待付款（未付清采购单）{pay} 张、应付合计 {pay_tot:,.2f} 元。")
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
    """按往来单位/日期/商品查单据明细（优先 DB 全量计数后截断，绕开 REST 翻页上限）。"""
    party = str(args.get("party") or "").strip()
    date_s = str(args.get("date") or "").strip()   # YYYY-MM-DD / 今天 / 昨天
    material = str(args.get("material") or "").strip()
    today = time.strftime("%Y-%m-%d")
    alias = {"今天": today, "昨天": time.strftime("%Y-%m-%d", time.localtime(time.time() - 86400))}
    day = alias.get(date_s, date_s)
    if day and not re.match(r"^\d{4}-\d{2}-\d{2}$", day):
        return {"error": "date 要用 YYYY-MM-DD（或「今天」「昨天」）", "got": date_s}, \
            "date 参数没认出来（%s），请用 YYYY-MM-DD" % date_s

    data = None
    try:
        from . import ledger as ledgermod
        data = ledgermod.query_bills(party=party, date=day, material=material, limit=20)
    except Exception:
        data = None

    if data is None:
        bills = erp.bills(page_size=500)
        hits, matched_ids = [], []
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
            hits.append({"number": b.get("number"), "time": t,
                         "type": (b.get("type") or "") + "·" + (b.get("subType") or ""),
                         "sub_type": b.get("subType") or "",
                         "party": b.get("organName") or "—",
                         "goods": goods.strip()[:60], "total": float(b.get("totalPrice") or 0),
                         "debt": float(b.get("debt") or 0)})
            matched_ids.append(b.get("id"))
        total_count = len(hits)
        total_amount = round(sum(h["total"] for h in hits), 2)
        rows = hits[:20]
        matched_ids = matched_ids[:20]
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
        sale_amt = round(sum(h["total"] for h in hits if h["sub_type"] in ("销售", "零售")), 2)
        sale_ret = round(sum(h["total"] for h in hits if h["sub_type"] in ("销售退货", "零售退货")), 2)
        buy_amt = round(sum(h["total"] for h in hits if h["sub_type"] == "采购"), 2)
        buy_ret = round(sum(h["total"] for h in hits if h["sub_type"] == "采购退货"), 2)
        for r in rows:
            r.pop("sub_type", None)
        return_amount = round(sale_ret + buy_ret, 2)
        net_amount = round((sale_amt - sale_ret) + (buy_amt - buy_ret), 2)
        data = {"filters": {"party": party, "date": day, "material": material},
                "count": len(rows), "total_count": total_count,
                "total_amount": total_amount,
                "sale_amount": sale_amt, "sale_return_amount": sale_ret,
                "sale_net_amount": round(sale_amt - sale_ret, 2),
                "buy_amount": buy_amt, "buy_return_amount": buy_ret,
                "buy_net_amount": round(buy_amt - buy_ret, 2),
                "return_amount": return_amount, "net_amount": net_amount,
                "truncated": total_count > len(rows), "bills": rows}

    rows = data.get("bills") or []
    scope = "、".join(x for x in ((party or ""), (day or ""), ("含" + material) if material else "") if x)
    if not rows:
        return data, "没有查到符合条件（%s）的单据。" % (scope or "给定条件")
    lines = "；".join("%s %s %s %s %g元" % (r["time"], r["party"], r["goods"][:20], r["type"], r["total"])
                      for r in rows[:5])
    total_count = data.get("total_count", len(rows))
    head = "查到 %d 张" % total_count
    if total_count > len(rows):
        head += "（明细只列前 %d 张）" % len(rows)
    ret_amt = data.get("return_amount") or 0
    if ret_amt:
        money_note = ("正向 %g 元、退货 %g 元、净额 %g 元"
                      % (data.get("non_return_amount") or data.get("sale_amount") or data.get("buy_amount") or data.get("total_amount"),
                         ret_amt, data.get("net_amount")))
    else:
        money_note = "合计 %g 元" % (data.get("total_amount") or 0)
    return data, "%s，%s：%s。" % (head, money_note, lines)


# --------------------------------------------------------------------------
# supervisor 写路由（ADR-10）：录单/下单需求 → 起草单据草稿。
# 写权限边界在这里划死：本函数绝不写库，只产出「预览 + 逐单确认令牌」；
# 令牌与对话录单共用同一闸门（gate.issue，与草稿内容哈希绑定），真正写入
# 必须由用户在确认卡上确认（/api/commit 校验令牌）——agent 自主性到草稿为止。
# --------------------------------------------------------------------------
DRAFT_TEXT_MAX = 1000   # 用户消息上限（与多单解析口径一致）
DRAFT_ATTACH_TEXT_MAX = 6000   # 带文本附件（PDF/DOCX/TXT）时的合并上限
# 草稿工具只管「新建」：改/删/调整/审核已有数据，以及纯收付款单、调拨单在这里拦下；修订尚未写入的草稿例外。
_DRAFT_DENY = re.compile(
    r"(已有|现存|库存|单据|记录|档案|待审核).{0,6}(改|删|修改|更新|作废|冲销|调整|撤销|红冲|审核|反审核)"
    r"|(改|删|修改|更新|作废|冲销|调整|撤销|红冲|反审核).{0,6}(已有|现存|库存|单据|记录|档案)"
    r"|(一键|批量|全部|所有).{0,4}审核.{0,6}(单|账)?"
)
_PAY_ONLY = re.compile(r"(收款单|付款单|回款|(?:^|[，,。\s])(收|付).{0,10}(货款|欠款|账款|尾款)|结清欠款)")
_PAY_GOODS = re.compile(r"(买|卖|采购|进货|销售|出库|入库|退货|\d+\s*(?:瓶|箱|盒|袋|个|件|包|卷|台|支|根|块|节|斤|克|kg|KG|米|套|桶|罐|条))")
_TRANSFER_PAT = re.compile(r"(调拨|从.{1,10}仓.{0,8}(?:调|转|移).{0,10}仓)")


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
    check_text = raw_user_text or text
    if _DRAFT_DENY.search(check_text):
        return ({"refused": "modification_not_supported", "text": check_text},
                "起草工具只能新建单据，不能修改、删除或审核已有数据——请向用户说明。")
    if not previous_draft:
        if _TRANSFER_PAT.search(check_text):
            return ({"unsupported": "transfer", "text": check_text},
                    "对话起草目前仅支持 6 类出入库单（采购/销售/退货/其他出入库），暂不支持仓库调拨单；调拨请在 jshERP 后台「仓库管理 → 调拨出库」操作。")
        if _PAY_ONLY.search(check_text) and not _PAY_GOODS.search(check_text):
            return ({"unsupported": "payment", "text": check_text},
                    "对话起草目前仅支持 6 类出入库单；收款/付款单请前往左侧「录单 → 收付款」页面录入（支持关联欠款单核销）。")
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
            "party": d.get("party") or "",
            "depot": d.get("depot") or (pv.get("depot") or {}).get("name") or "",
            "oper_time": d.get("oper_time") or pv.get("oper_time") or "",
            "pay_amount": d.get("pay_amount"),
            "remark": d.get("remark") or "",
            "to_create": pv.get("to_create") or [],
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
    note = ("已起草 %d 张单据草稿（仅预览，未写入系统）：%s。未指定日期的已默认当前时间，"
            "请在确认卡上核对日期与内容后确认写入；有缺失项的草稿请提醒用户补全。" % (
                len(bills),
                "；".join("%s %s %s %d行" % (
                    r["biz"], r["party"] or "？", (r["oper_time"] or "")[:10], len(r["items"]))
                    for r in rows)))
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
        "low_stock": {"desc": "哪些商品低于安全库存/安全下限/负库存/库存预警（按仓对照安全线，问安全库存预警必用它，不要手写 SQL）", "args_hint": "{}",
                      "run": bind(_low_stock)},
        "today_stats": {"desc": "今天买了多少/卖了多少", "args_hint": "{}",
                        "run": bind(_today_stats)},
        "month_stats": {"desc": "本月采购、销售、差额", "args_hint": "{}",
                        "run": bind(_month_stats)},
        "debts": {"desc": "谁欠我钱、我欠谁钱（应收应付）", "args_hint": "{}",
                  "run": bind(_debts)},
        "account_balance": {"desc": "查资金账户总余额、每个结算账户的实时余额与本月变动（权威账本口径，含期初与全部进销存/收付款流水；问资金余额/账户多少钱必须用它，不要手写 SQL）",
                            "args_hint": "{}",
                            "run": bind(_account_balance)},
        "ops_summary": {"desc": "查待审核单据数、待收款/待付款单据张数及应收应付合计（权威账本口径；问待审核/待收款/待付款张数必须用它，不要用 pay_type='月结' 猜）",
                        "args_hint": "{}",
                        "run": bind(_ops_summary)},
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
        "query_bills": {"desc": "按往来单位/日期/商品查全部单据总数、正向金额、退货金额、退货净额及明细（DB全量统计不截断；问某客户/供应商一共多少张单、买了多少钱、退货净额必用它，不要自己写多条 SQL）",
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
                      "internal": True,
                      "run": lambda a: dbmod.db_schema(a)},
        "draft_order": {"desc": "用户要录单/新建单据/生成草稿时直接调用（已内置多单拆分、历史进价核价与库存校验，无需先查历史或库存）；有附件时按编号/文件名选择对应文件（只出预览绝不写库）",
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
                             context_block=context, on_event=_emit,
                             sticky_context=dbmod.CORE_SQL_CHEATSHEET)
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
