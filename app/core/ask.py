#!/usr/bin/env python3
"""只读问答智能体（/api/ask）—— 三层递进架构。

第 1 层 规则直答（0 token）：高频统计问句（今天买了多少 / 谁欠我钱 / 什么货快没了 /
        多少个供应商）由确定性路由直连只读工具，模板句作答——零成本零幻觉零延迟。
第 2 层 agent loop（多步取证）：自然语言 / 复合问题交给手写 function-calling
        循环（core/agent.py：决策 {thought,tool,args} → 执行 → observation 回喂），
        支持跨工具链式取证（「上月谁买得最多？他欠我多少？」）。
        工具箱含 sql_query / db_schema（core/dbread.py）：对 ERP 全库 32 张表有只读
        权限，表结构从 information_schema 动态读取并注入上下文，任何清单/计数/明细/
        排名/配置类问题都能查，不再局限于预置的几个统计工具。
第 3 层 确定性兜底：LLM 作答必须通过数字接地校验——答案里每个数字都要来自
        某步观察 JSON，不通过退回模板句拼接，回答永远可追溯到 ERP 真实值。

所有工具只读、绝不写库。手写 JSON 动作循环是主动的架构选择：每步可审计
（trace）、可预算（步数 + token 双约束）、可回放，且对任意 OpenAI 兼容端点
行为一致。规则直答保守设计：只匹配无参数的整句统计意图，宁缺勿滥，长句
一律交给 agent loop（参数抽取交给模型更可靠）。
"""
import re
import time

from . import config as cfgmod
from . import dbread as dbmod
from .report import official_stats, recent_bills as _rb, stock_overview, summary
from .erp import get_client, parallel

PROMPT_VERSION = "ask-v7-20261109"   # v7: 步数预算 6→4（专用工具 1~2 步即答）+ 歧义候选不许 SQL 合并 + recent_bills 非全量语义（F11）

# --------------------------------------------------------------------------
# 第 1 层：规则直答（0 token）。只收录无参数的统计意图，带参数的查询
# （如商品库存，需要抽商品名）一律走 agent loop。
# --------------------------------------------------------------------------
_RULES = [
    (r"今天.*(买|卖|采购|销售|进货|出货|营收|流水).*(多少|几)", "today_stats"),
    (r"(本月|这个月|当月).*(买|卖|采购|销售|营收|流水).*(多少|几)", "month_stats"),
    (r"谁欠我|我欠谁|应收应付|欠款情况|欠了多少", "debts"),
    (r"(最近|最新).{0,4}(的单|单据|录了哪些|账单|流水)", "recent_bills"),
    (r"(什么|哪些)(货|商品|东西).{0,6}(快没|缺|不够|要补)", "low_stock"),
    (r"(库存|货).{0,8}(值多少钱|总值|货值|价值多少)", "stock_value"),
]


def _master_names(erp):
    """主数据名集合（归一化，len>=2），供规则路由的实体护栏。读缓存列表，成本可忽略。"""
    names = set()
    try:
        for row in (erp.materials() or []):
            n = str(row.get("name") or "").strip()
            if len(n) >= 2:
                names.add(erp._norm(n))
        for ptype in ("供应商", "客户"):
            for row in (erp.suppliers(ptype) or []):
                n = str(row.get("supplier") or "").strip()
                if len(n) >= 2:
                    names.add(erp._norm(n))
    except Exception:
        pass
    return names


def _rule_route(text, erp=None):
    """命中返回 (tool, {})，未命中返回 None。

    三道护栏：长句（>24 字）、复合意图（含「另外/还有/顺便」等连接词）、
    实体参数（文本含主数据名——「电池今天卖了多少」today_stats 答不了「电池」，
    v1 评测实测截胡：参数化问题一律交 agent loop 抽参）。
    规则直答只服务无参数的整句统计问句，宁缺勿滥。
    """
    t = (text or "").strip()
    if not t or len(t) > 24:
        return None
    if re.search(r"另外|还有|以及|顺便|然后|同时|再问|还想知道", t):
        return None
    if erp is not None:
        tn = erp._norm(t)
        for name in _master_names(erp):
            if name and name in tn:
                return None
    for pat, tool in _RULES:
        if re.search(pat, t):
            return tool, {}
    ent = _master_entity(t)
    if ent:
        return "master_count", {"entity": ent}
    return None


# 主数据计数问句（「有多少个供应商」）——必须保守：带买卖/金额/欠/库存等
# 动词的一律不截胡，交给 agent loop（「这个月卖了多少商品」问的是销量不是商品总数）。
_MASTER_ENTITIES = "供应商|客户|商品|产品|仓库|账户|单位|收支项目|分类"
_MASTER_EXCLUDE = re.compile(r"买|卖|采购|销售|进货|出货|营收|流水|钱|元|金额|欠|库存|值|录|单")


def _master_entity(t):
    if not re.search(r"(多少|几)", t):
        return None
    if _MASTER_EXCLUDE.search(t):
        return None
    hits = re.findall(_MASTER_ENTITIES, t)
    # 复合问题（「供应商和客户各有几家」）一律交给 agent loop 用 SQL 一次查全——
    # 规则直答只服务单一实体，宁缺勿滥（历史教训：复合问句被截胡只答一半）。
    if len(set(hits)) != 1:
        return None
    return hits[0]


def _compound_hints(text):
    """复合问题：把子问题显式列成清单注入 goal——实测仅靠规则约束模型仍会
    只查一半就作答（"库存总值未取到"式偷懒），显式清单是最可靠的干预。"""
    parts = re.split(r"另外|还有|以及|顺便|然后|同时", text)
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
    ov = stock_overview(erp)
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


def _master_count(args, erp):
    """主数据计数（走 ERP REST 业务口径：自动排除软删除/停用档案）。"""
    ent = str(args.get("entity") or "").strip()
    fetch = {
        "供应商": lambda: erp.suppliers("供应商"),
        "客户": lambda: erp.suppliers("客户"),
        "商品": erp.materials, "产品": erp.materials,
        "仓库": erp.depots, "账户": erp.accounts,
        "单位": erp.units, "分类": erp.categories,
        "收支项目": erp.in_out_items,
    }
    rows = (fetch.get(ent) or erp.materials)() or []
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


# 规则直答层允许的工具
_RULE_TOOLS = {
    "today_stats": _today_stats, "month_stats": _month_stats, "debts": _debts,
    "recent_bills": _recent_bills, "low_stock": _low_stock, "stock_value": _stock_value,
    "master_count": _master_count,
}


def _tools(erp):
    """构造 agent 工具注册表（全部只读）。"""
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
        "search_docs": {"desc": "查操作文档（怎么录单/什么字段必填/术语解释等概念问题）",
                        "args_hint": '{"query":"检索词"}',
                        "run": lambda a: rmod.search_docs(str(a.get("query") or ""))},
        "search_history": {"desc": "搜历史单据（上次/之前跟谁买过什么、某商品进过几次货、原话找单）",
                           "args_hint": '{"query":"检索词，如商品名或往来单位"}',
                           "run": lambda a: rmod.search_history(str(a.get("query") or ""))},
        "query_bills": {"desc": "按条件查单据明细（谁买了什么/某天卖了什么/某商品最近进出/某单位欠款单）",
                        "args_hint": '{"party":"往来单位，可空","date":"YYYY-MM-DD或今天/昨天，可空","material":"商品名，可空"}',
                        "run": lambda a: _query_bills(a, erp)},
        "sql_query": {"desc": "对 ERP 全库只读 SQL 查询：任意表的清单/数量/明细/汇总/排名/时间区间，专用工具没覆盖的问题一律用它",
                      "args_hint": '{"sql":"单条 SELECT；表结构见上下文；软删除表加 delete_flag 为 0 的条件；大表带 LIMIT"}',
                      "run": lambda a: dbmod.sql_query(a)},
        "db_schema": {"desc": "看某张表的全部字段与取值样本（写 SQL 前不确定字段/枚举值先查它）",
                      "args_hint": '{"table":"表名，如 jsh_supplier"}',
                      "run": lambda a: dbmod.db_schema(a)},
    }


# --------------------------------------------------------------------------
# 入口：三层递进
# --------------------------------------------------------------------------
def ask(text, erp=None, on_event=None):
    """只读问答。返回 {ok, answer, tool, route, steps, tokens, meta}。
    on_event：SSE 流式回调——规则直答发一条 step，agent loop 发 action/step 序列。"""
    erp = erp or get_client()
    prof = cfgmod.agent_profile()
    if not prof:
        return {"ok": False, "message": "尚未配置 AI 模型，请先在「设置」里添加。"}
    meta = {"model": prof.get("model", ""), "prompt_version": PROMPT_VERSION}

    def _emit(evt):
        if on_event:
            try:
                on_event(evt)
            except Exception:
                pass

    # ---- 第 1 层：规则直答（0 token）----
    hit = _rule_route(text, erp)
    if hit:
        tool, args = hit
        try:
            data, tpl = _RULE_TOOLS[tool](args, erp)
        except Exception as e:
            return {"ok": False, "message": "数据查询失败：%s" % str(e)[:120], "meta": meta}
        rec = {"step": 1, "thought": "规则直答（零 LLM 调用）", "tool": tool,
               "args": args, "ok": True, "observation": data}
        _emit({"type": "step", "step": rec})
        return {"ok": True, "tool": tool, "answer": tpl, "data": data,
                "route": "rule", "grounded": True, "meta": meta,
                "steps": [rec],
                "tokens": {"prompt": 0, "completion": 0, "estimated": False}}

    # ---- 第 2 层：agent loop（多步取证 + 第 3 层接地兜底在 loop 内）----
    from . import agent as agentmod
    out = agentmod.run_agent(text + _compound_hints(text), _tools(erp),
                             max_steps=6, context_block=_context(erp),
                             on_event=on_event)
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
    }
