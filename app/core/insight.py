#!/usr/bin/env python3
"""经营洞察与主动预警：规则算事实，LLM 只组织语言，数字接地兜底。

分层：
- alerts()   纯规则预警（零 LLM、零延迟）：负库存 / 低库存 / 应收集中 / 大额单
- insights() 规则层先从看板数据提取可解释事实（facts），LLM 一跳把事实组织成
             洞察卡文案；每条洞察的数字必须能在 facts 里找到（numeric_grounded），
             不通过则退回规则模板文案——与 /ask 同一条「数字可追溯」不变量
- daily_report() 图文日报数据包（今日/本月采销、库存、欠款、top 往来、洞察）
"""
import json
import time

from . import config as cfgmod
from . import llm as llmmod
from .report import summary, official_stats, stock_overview, recent_bills
from .erp import get_client

PROMPT_VERSION = "insight-v1-20261024"

BIG_BILL_YUAN = 2000      # 大额单阈值（元）
RECV_FOCUS_RATIO = 0.5    # 应收集中度：top1 占比超过该值提示


def collect_facts(erp=None):
    """规则层：从看板数据模块提取结构化事实（全部只读、全部可解释）。"""
    erp = erp or get_client()
    facts = {"generated_at": time.strftime("%Y-%m-%d %H:%M")}

    ov = stock_overview(erp)
    facts["stock"] = {"kinds": ov.get("kinds"), "negative_kinds": ov.get("negative"),
                      "low_kinds": ov.get("low"), "threshold": ov.get("threshold"),
                      "tightest": [{"name": w["name"], "stock": w["stock"], "unit": w["unit"]}
                                   for w in (ov.get("warn_list") or [])[:3]]}

    st = official_stats(erp)
    facts["today"] = st.get("today") or {}
    facts["month"] = st.get("month") or {}
    facts["stock_money"] = st.get("stock_money") or {}

    s = summary(erp)
    recv = s.get("debt_recv") or []
    recv_total = s.get("debt_recv_total") or 0
    facts["debts"] = {
        "receivable_total": recv_total,
        "payable_total": s.get("debt_pay_total") or 0,
        "receivable_top": [{"name": x["name"], "amount": x["amount"]} for x in recv[:3]],
        "recv_top1_ratio": round(recv[0]["amount"] / recv_total, 3) if recv and recv_total else None,
    }

    bills = recent_bills(erp, limit=20)
    big = [b for b in bills if (b.get("total") or 0) >= BIG_BILL_YUAN]
    facts["big_bills"] = [{"time": b["time"], "type": b["type"], "party": b["party"],
                           "total": b["total"]} for b in big[:3]]
    facts["bill_count_recent"] = len(bills)
    return facts


def alerts(erp=None):
    """纯规则主动预警（零 LLM）。返回 [{level, kind, text}]，level: warn|info。"""
    try:
        f = collect_facts(erp)
    except Exception as e:
        return {"ok": False, "message": "数据收集失败：%s" % str(e)[:120]}
    out = []
    st = f["stock"]
    if st.get("negative_kinds"):
        out.append({"level": "warn", "kind": "negative_stock",
                    "text": "有 %d 种商品负库存，账实可能不符，建议盘点" % st["negative_kinds"]})
    if st.get("low_kinds"):
        tight = "、".join("%s(%s%s)" % (w["name"], w["stock"], w["unit"]) for w in st.get("tightest") or [])
        out.append({"level": "warn", "kind": "low_stock",
                    "text": "%d 种商品低于安全库存%s" % (st["low_kinds"], ("，最紧张：" + tight) if tight else "")})
    d = f["debts"]
    if d.get("recv_top1_ratio") is not None and d["recv_top1_ratio"] >= RECV_FOCUS_RATIO:
        top = (d["receivable_top"] or [{}])[0]
        out.append({"level": "info", "kind": "recv_focus",
                    "text": "应收 %.2f 元，其中 %s 占 %.0f%%，回款集中度较高" % (
                        d["receivable_total"], top.get("name", "?"), d["recv_top1_ratio"] * 100)})
    if f["big_bills"]:
        b = f["big_bills"][0]
        out.append({"level": "info", "kind": "big_bill",
                    "text": "近期大额单：%s %s %.2f 元（%s）" % (b["type"], b["party"], b["total"], b["time"])})
    return {"ok": True, "alerts": out, "facts_ts": f["generated_at"]}


INSIGHT_PROMPT = """你是进销存系统的经营分析师。下面是从 ERP 提取的结构化事实（全部真实），
请把它们组织成 2-4 条经营洞察，帮小店老板看懂今天的生意。

规则：
1. 每条洞察 {"title":"8字以内标题","detail":"一到两句中文说明","level":"info 或 warn"}。
2. detail 里的每个数字必须原样来自事实 JSON，禁止编造、禁止计算新数字（比率已给的就用）。
3. 数字带单位（元/瓶/种/笔），金额两位小数。
4. 没有明显异常就给经营现状类洞察（采销规模、库存价值、欠款结构），不要硬编问题。
5. 只输出 JSON：{"insights":[...]}，不要多余键。

事实 JSON：
{facts}"""


def insights(erp=None, on_event=None):
    """facts → LLM 一跳 → 接地校验 → 洞察卡。LLM 不可用/不接地时退回规则模板。"""
    try:
        f = collect_facts(erp)
    except Exception as e:
        return {"ok": False, "message": "数据收集失败：%s" % str(e)[:120]}

    # 规则模板兜底（LLM 失败/不接地时用，天然只用事实里的数字）
    fallback = _template_insights(f)

    prof = cfgmod.active_profile()
    if not prof:
        return {"ok": True, "insights": fallback, "source": "rule",
                "meta": {"prompt_version": PROMPT_VERSION}}

    def emit(evt):
        if on_event:
            try:
                on_event(evt)
            except Exception:
                pass

    emit({"type": "action", "tool": "insight_gen", "args": {}})
    try:
        raw = llmmod._chat(prof, [
            {"role": "system", "content": INSIGHT_PROMPT.replace("{facts}", json.dumps(f, ensure_ascii=False))},
            {"role": "user", "content": "请生成洞察"},
        ], purpose="insight")
        obj = llmmod._extract_json(raw)
        cand = obj.get("insights") if isinstance(obj, dict) else None
    except Exception as e:
        return {"ok": True, "insights": fallback, "source": "rule_fallback",
                "meta": {"prompt_version": PROMPT_VERSION, "error": str(e)[:120]}}

    emit({"type": "step", "step": {"tool": "insight_gen", "ok": True,
                                   "thought": "LLM 洞察生成完成，进入数字接地校验"}})

    from .agent import numeric_grounded
    ok_cards = []
    for c in (cand or []):
        if not isinstance(c, dict):
            continue
        text = "%s %s" % (c.get("title") or "", c.get("detail") or "")
        if not (c.get("title") and c.get("detail")):
            continue
        if not numeric_grounded(text, [f]):
            continue   # 数字不能追溯到事实的条目直接丢弃
        ok_cards.append({"title": str(c["title"])[:24], "detail": str(c["detail"])[:160],
                         "level": "warn" if c.get("level") == "warn" else "info"})
    if not ok_cards:
        ok_cards = fallback
    return {"ok": True, "insights": ok_cards,
            "source": "llm" if ok_cards is not fallback else "rule_fallback",
            "meta": {"prompt_version": PROMPT_VERSION}}


def _template_insights(f):
    """规则模板洞察（零 LLM 兜底）：从事实直接拼可读文案。"""
    out = []
    t, m = f.get("today") or {}, f.get("month") or {}
    if m.get("buy") is not None:
        out.append({"title": "本月采销",
                    "detail": "本月采购 %s 元（%s 笔），销售 %s 元（%s 笔）。"
                              % (f"{m.get('buy'):,.2f}", m.get("buy_n"), f"{m.get('sell'):,.2f}", m.get("sell_n")),
                    "level": "info"})
    sm = f.get("stock_money") or {}
    if sm.get("total_value") is not None:
        out.append({"title": "库存货值",
                    "detail": "库存 %s 件，按成本价约值 %s 元。"
                              % (f"{sm.get('total_stock'):g}", f"{sm.get('total_value'):,.2f}"),
                    "level": "info"})
    d = f.get("debts") or {}
    if d.get("receivable_total"):
        out.append({"title": "应收欠款",
                    "detail": "客户共欠 %s 元，我欠供应商 %s 元。"
                              % (f"{d['receivable_total']:,.2f}", f"{d.get('payable_total') or 0:,.2f}"),
                    "level": "info"})
    st = f.get("stock") or {}
    if st.get("negative_kinds") or st.get("low_kinds"):
        out.append({"title": "库存风险",
                    "detail": "%d 种负库存、%d 种低于安全库存 %s。" % (
                        st.get("negative_kinds") or 0, st.get("low_kinds") or 0, st.get("threshold")),
                    "level": "warn"})
    return out[:4]


def daily_report(erp=None):
    """图文日报数据包：今日/本月采销 + 库存 + 欠款 + top 往来 + 洞察。"""
    from .report import top_parties, daily_trend
    erp = erp or get_client()
    ins = insights(erp)
    al = alerts(erp)
    bills = erp.all_bills()
    return {
        "ok": True,
        "date": time.strftime("%Y-%m-%d"),
        "insights": ins.get("insights") if ins.get("ok") else [],
        "alerts": al.get("alerts") if al.get("ok") else [],
        "top_parties": top_parties(bills, top=5),
        "trend": daily_trend(bills),
    }
