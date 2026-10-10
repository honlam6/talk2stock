#!/usr/bin/env python3
"""只读问答智能体（/api/ask）。

编排方式是三段式"选工具 → 执行 → 依数据作答"，不依赖 OpenAI function-calling
（自建网关/推理端点兼容性差），与录单链路同一套 _chat 降级机制：

  1. LLM 第一跳：从工具目录里选一个（或 none），输出严格 JSON {tool, args}
  2. 执行：全部是只读函数（库存/统计/欠款/流水），绝不写库
  3. LLM 第二跳：只准引用返回数据里的数字组织中文回答；失败则退回模板句

数据引用规则（提案承诺"答案能追溯到 ERP 真实值"）：回答必须基于第 2 步的
结构化数据，模板句和 LLM 润色都不引入数据里没有的数字。
"""
import json

from . import config as cfgmod
from . import llm as llmmod
from . import report as reportmod
from .erp import get_client

PROMPT_VERSION = "ask-v1-20261010"

SELECT_PROMPT = """你是进销存系统的只读问答助手。判断用户问题该用哪个数据工具，只输出 JSON。

# 工具目录
- stock_query   查某种商品的库存（哪个仓、还剩多少）。args: {"material":"商品名","depot":"仓库名，没说就空字符串"}
- low_stock     哪些货快没了/缺货/该补货了。args: {}
- today_stats   今天买了多少/卖了多少。args: {}
- month_stats   本月采购、销售、差额。args: {}
- debts         谁欠我钱、我欠谁钱（应收应付）。args: {}
- recent_bills  最近录了哪些单。args: {}
- stock_value   库存里的货总共值多少钱。args: {}
- none          以上都不是（闲聊、或超出经营数据范围）。args: {}

# 输出格式（严格 JSON，不要多余键）
{"tool":"stock_query","args":{"material":"可乐","depot":""},"answer":""}
tool 选 none 时才写 answer（一句话说明你只能答库存、采销、欠款这类经营问题）。

# 可选规则
用户说的商品/仓库名尽量对照下面列表里的写法：
{names}
"""

ANSWER_PROMPT = """根据工具返回的数据回答用户问题，用中文口语。

规则：
1. 只能引用数据 JSON 里出现的数字和名称，绝对不能编造或心算新数字。
2. 先直接回答问题，再补充关键数字；最多三句话。
3. 数字带单位（元/瓶/箱），金额保留两位小数。
4. 数据为空就直说"系统里没有查到相关数据"。

用户问题：{q}
使用工具：{tool}
数据：{data}

只输出回答文本，不要 JSON，不要解释。"""


def _names_block(erp):
    try:
        mats = [m.get("name") for m in erp.materials()][:80]
        deps = [d.get("name") for d in erp.depots()][:20]
        return "商品：" + "、".join(filter(None, mats)) + "\n仓库：" + "、".join(filter(None, deps))
    except Exception:
        return ""


# --------------------------------------------------------------------------
# 工具实现（全部只读）
# --------------------------------------------------------------------------
def _run_tool(tool, args, erp):
    """执行只读工具。返回 (data_dict_or_list, 模板回答)。"""
    args = args if isinstance(args, dict) else {}

    if tool == "stock_query":
        name = str(args.get("material") or "").strip()
        mrow, kind = erp.find_material(name)
        if not mrow:
            cands = erp.material_candidates(name)
            return {"material": name, "found": False, "candidates": cands}, \
                f"系统里没有查到「{name}」的库存记录" + (f"，最接近的档案：{'、'.join(cands)}" if cands else "")
        deps = erp.depots()
        depot_filter = str(args.get("depot") or "").strip()
        rows, total = [], 0.0
        for d in deps:
            if depot_filter and erp._norm(d.get("name")) != erp._norm(depot_filter):
                continue
            st = erp.stock(mrow["id"], d["id"])
            if st is None:
                continue
            rows.append({"depot": d.get("name"), "stock": float(st)})
            total += float(st)
        unit = mrow.get("unit") or ""
        data = {"material": mrow.get("name"), "unit": unit, "by_depot": rows,
                "total": round(total, 2), "matched": kind or "exact"}
        detail = "；".join(f"{r['depot']} {r['stock']:g}" for r in rows) if rows else "各仓均无库存记录"
        return data, (f"「{mrow.get('name')}」当前库存 {total:g}{unit}。{detail}。"
                      if rows else f"「{mrow.get('name')}」在系统里没有库存记录。")

    if tool == "low_stock":
        ov = reportmod.stock_overview(erp)
        warn = ov.get("warn_list") or []
        data = {"kinds": ov.get("kinds"), "negative": ov.get("negative"),
                "low": ov.get("low"), "threshold": ov.get("threshold"),
                "warn_list": warn[:12]}
        head = f"共 {ov.get('kinds')} 种商品，其中 {ov.get('negative')} 种负库存、{ov.get('low')} 种低于 {ov.get('threshold')}。"
        body = "最紧张：" + "；".join(f"{w['name']} {w['stock']:g}{w['unit']}" for w in warn[:5]) if warn else "暂无紧张库存。"
        return data, head + body

    if tool in ("today_stats", "month_stats"):
        st = reportmod.official_stats(erp)
        scope = st.get("today" if tool == "today_stats" else "month") or {}
        label = "今天" if tool == "today_stats" else "本月"
        data = {"scope": label, "buy": scope.get("buy"), "buy_n": scope.get("buy_n"),
                "sell": scope.get("sell"), "sell_n": scope.get("sell_n"),
                "profit_cash": scope.get("profit")}
        return data, (f"{label}采购 {scope.get('buy'):,.2f} 元（{scope.get('buy_n')} 笔），"
                      f"销售 {scope.get('sell'):,.2f} 元（{scope.get('sell_n')} 笔），"
                      f"现金流差额 {scope.get('profit'):,.2f} 元（销售-采购，非毛利）。")

    if tool == "debts":
        s = reportmod.summary(erp)
        data = {"receivable_top": s.get("debt_recv"), "receivable_total": s.get("debt_recv_total"),
                "payable_top": s.get("debt_pay"), "payable_total": s.get("debt_pay_total")}
        recv = "；".join(f"{x['name']} {x['amount']:,.2f}" for x in (s.get("debt_recv") or [])[:4])
        pay = "；".join(f"{x['name']} {x['amount']:,.2f}" for x in (s.get("debt_pay") or [])[:4])
        tpl = (f"客户共欠我 {s.get('debt_recv_total'):,.2f} 元（{recv or '无'}）；"
               f"我欠供应商 {s.get('debt_pay_total'):,.2f} 元（{pay or '无'}）。")
        return data, tpl

    if tool == "recent_bills":
        bills = reportmod.recent_bills(erp, limit=6)
        data = {"bills": bills}
        if not bills:
            return data, "系统里还没有单据。"
        lines = "；".join(f"{b['time']} {b['type']} {b['party']} {b['total']:,.2f}元" for b in bills[:5])
        return data, f"最近 {len(bills)} 张单：{lines}。"

    if tool == "stock_value":
        st = reportmod.official_stats(erp)
        sm = st.get("stock_money") or {}
        data = {"total_stock": sm.get("total_stock"), "total_value": sm.get("total_value")}
        return data, (f"库存货值约 {sm.get('total_value'):,.2f} 元"
                      f"（{sm.get('total_stock'):g} 件，按当前成本价估算）。")

    return {}, ""


def ask(text, erp=None):
    """只读问答入口。返回 {ok, answer, tool, data, meta}。"""
    erp = erp or get_client()
    prof = cfgmod.active_profile()
    if not prof:
        return {"ok": False, "message": "尚未配置 AI 模型，请先在「设置」里添加。"}

    # 1) 选工具
    try:
        raw = llmmod._chat(prof, [
            {"role": "system", "content": SELECT_PROMPT.replace("{names}", _names_block(erp))},
            {"role": "user", "content": text},
        ])
        choice = llmmod._extract_json(raw)
    except Exception as e:
        return {"ok": False, "message": "AI 解析失败：%s" % str(e)[:120]}
    tool = str(choice.get("tool") or "none").strip()
    args = choice.get("args") if isinstance(choice.get("args"), dict) else {}

    meta = {"model": prof.get("model", ""), "prompt_version": PROMPT_VERSION}
    if tool == "none":
        return {"ok": True, "tool": "none",
                "answer": str(choice.get("answer") or "我只能回答库存、采购销售、欠款这类经营数据问题。"),
                "data": None, "meta": meta}

    # 2) 执行只读工具
    try:
        data, tpl = _run_tool(tool, args, erp)
    except Exception as e:
        return {"ok": False, "message": "数据查询失败：%s" % str(e)[:120]}

    # 3) 依数据作答（失败退模板，模板也绝不引入数据外的数字）
    answer = tpl or "已查到数据，但暂时无法组织回答。"
    try:
        raw2 = llmmod._chat(prof, [
            {"role": "system", "content": ANSWER_PROMPT.format(q=text, tool=tool,
                                                               data=json.dumps(data, ensure_ascii=False))},
            {"role": "user", "content": "请回答"},
        ])
        txt = (raw2 or "").strip()
        if txt:
            txt = llmmod._extract_json(txt)["answer"] if txt.lstrip().startswith("{") else txt
            if txt:
                answer = txt.strip()
    except Exception:
        pass
    return {"ok": True, "tool": tool, "answer": answer, "data": data, "meta": meta}
