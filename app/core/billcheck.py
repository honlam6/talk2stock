#!/usr/bin/env python3
"""录单自主校验 agent（P-B / ADR-11）：草稿在出预览前先过一个只读取证 loop。

流程（parse_and_check，三入口共用：对话录单 / 图片识别 / 问一问起草）：
    llm.parse 抽取草稿
      → run_check：手写 agent loop（复用 core/agent.py 引擎，只换提示词与工具箱）
        工具：stock_check（库存够不够）/ last_price（该客户历史成交价）/
             habit_defaults（操作员习惯仓库与价格）/ party_history（往来单位最近单据）
      → apply_patches：确定性代码按白名单字段 + 置信度阈值把高置信补丁写回草稿
      → 澄清决策：agent 生成的自然追问优先，退回规则追问（单张才追问，多单在
        确认卡逐单标注——沿用 multi-entry 设计）
      → prepare_bills：补丁落定后的草稿才签令牌（令牌与补丁后内容哈希绑定）

不变量：校验全程只读，绝不写库；patch 应用是确定性代码（模型只能建议）；
patch 的 value 必须来自工具观察（数字接地校验兜底），宁缺勿错。
"""
import ast
import json

from . import agent as agentmod
from . import ask as askmod
from . import config as cfgmod
from . import habits as habitsmod

BILL_CHECK_PROMPT_VERSION = "bcheck-v1-20261116"

BILL_CHECK_PROMPT = """你是录单校验 agent。本次任务：校验用户话里抽出的单据草稿（完整草稿 JSON 见文末「待校验目标」）。

# 工具目录（每轮只能调用一个）
{tool_catalog}

# 已完成的步骤（观察是工具返回的真实数据）
{history}

# 输出格式（严格 JSON，每轮恰好一个动作）
继续取证：{{"thought":"简短说明为什么查这个","tool":"工具名","args":{{...}}}}
校验完成：{{"thought":"一句话总结校验结论","tool":"submit_verdict","args":{{"patches":[...],"clarify":null,"risks":""}}}}

patch 元素：{{"bill_index":0,"item_index":0,"field":"depot|price","value":值,"reason":"依据（引用观察里的数字）","confidence":0.0~1.0}}

# 规则
1. 最多 {max_steps} 轮，需要什么查什么：缺单价查 last_price 或 habit_defaults；
   没说仓库查 habit_defaults；卖单查 stock_check 看库存够不够；往来单位不熟查 party_history。
   观察已经足够就立即用 submit_verdict 收尾，不要为查而查。
2. submit_verdict 的 patches 只补缺失项：depot 只在草稿没写仓库时补，price 只在明细
   缺价或为 0 时补。草稿里已有的值一个都不许动。
3. patch 的 value 必须来自工具观察（历史成交价 / 习惯仓库），禁止编造、禁止心算新数字；
   没有可靠依据就不出 patch——宁缺勿错，空 patches 完全合法。
4. 关键信息缺失（商品 / 数量 / 往来单位）时在 submit_verdict 的 clarify 写一句自然的
   中文追问；多张单缺什么合并成一句，带上「第几张单缺什么」。没有缺失就 null。
5. risks 用一句话提示观察到的风险（库存不够 / 该单位有大额欠款 / 价格与历史差异大），
   没有就空串。
6. submit_verdict 必须调用恰好一次，调用后不要再输出其他动作。

# 待校验目标
{goal}"""


# --------------------------------------------------------------------------
# 只读工具（返回 (data, note)——note 是模板句，数字接地兜底用）
# --------------------------------------------------------------------------
def _tool_stock_check(args, erp):
    return askmod._stock_query(args, erp)


def _sql_escape(s):
    """LIKE 模式与字符串字面量的最小转义（只读查询，防注入仍由 dbread 白名单兜底）。"""
    return (str(s or "").strip().replace("\\", "\\\\").replace("'", "''")
            .replace("%", "\\%").replace("_", "\\_"))


def _tool_last_price(args, erp):
    """某往来单位某商品最近成交单价。

    实现走 dbread 只读 SQL（jsh_depot_item.unit_price 是真实行价；REST 的
    getDetailList 不吐行价，只吐商品档案价快照 purchaseDecimal，不能用）。"""
    party = str(args.get("party") or "").strip()
    material = str(args.get("material") or "").strip()
    direction = str(args.get("direction") or "").strip() or "out"
    if not party or not material:
        return ({"error": "party 与 material 必填", "got": args},
                "last_price 需要 party 和 material 两个参数。")
    from . import dbread as dbmod
    type_zh = "出库" if direction == "out" else "入库"
    organ_type = "客户" if direction == "out" else "供应商"
    sql = ("SELECT dh.oper_time AS time, di.unit_price AS price, di.oper_number AS qty, di.material_unit AS unit "
           "FROM jsh_depot_item di JOIN jsh_depot_head dh ON di.header_id = dh.id "
           "WHERE dh.delete_flag='0' AND di.delete_flag='0' AND dh.type='%s' AND di.unit_price > 0 "
           "AND dh.organ_id IN (SELECT id FROM jsh_supplier WHERE delete_flag='0' AND type='%s' AND supplier LIKE '%%%s%%') "
           "AND di.material_id IN (SELECT id FROM jsh_material WHERE delete_flag='0' AND name LIKE '%%%s%%') "
           "ORDER BY dh.oper_time DESC LIMIT 6") % (
        type_zh, organ_type, _sql_escape(party), _sql_escape(material))
    data, note = dbmod.sql_query({"sql": sql})
    rows = data.get("rows") or [] if "error" not in data else []
    records = []
    for r in rows:
        try:
            records.append({"time": str(r.get("time") or "")[:10],
                            "price": round(float(r.get("price")), 4),
                            "qty": float(r.get("qty") or 0),
                            "unit": r.get("unit") or ""})
        except (TypeError, ValueError):
            continue
    out = {"found": bool(records), "party": party, "material": material,
           "direction": direction, "records": records}
    if not records:
        if "error" in data:
            return out, "历史价查询失败（%s），请改用 habit_defaults 或向用户确认。" % str(data["error"])[:60]
        note = "最近单据里没有 %s 的 %s %s记录。" % (party, material, "销售" if direction == "out" else "采购")
        return out, note
    first = records[0]
    note = "%s 最近一笔 %s %s单价 %g 元（%s）。" % (
        party, material, "销售" if direction == "out" else "采购", first["price"], first["time"])
    return out, note


def _tool_habit_defaults(args, erp):
    operator = str(args.get("operator") or "").strip()
    party = str(args.get("party") or "").strip()
    biz = str(args.get("biz") or "").strip()
    s = habitsmod.summary(operator=operator, party=party, biz=biz)
    prices = {k: v["price"] for k, v in (s.get("prices") or {}).items()}
    data = {"operator": operator, "party": party, "biz": biz,
            "depot": s.get("depot", ""), "prices": prices, "records": s.get("records", 0)}
    note = ""
    if s.get("depot"):
        note += "习惯仓库：%s。" % s["depot"]
    if prices:
        note += "历史单价：" + "；".join("%s %g元" % (k, v) for k, v in list(prices.items())[:6]) + "。"
    if not note:
        note = "还没有该操作员（或全局）的可借鉴习惯记录。"
    return data, note


def _tool_party_history(args, erp):
    party = str(args.get("party") or "").strip()
    if not party:
        return ({"error": "party 必填"}, "party_history 需要 party 参数。")
    rows, debt_total = [], 0.0
    for b in erp.bills(page_size=200):
        if str(b.get("deleteFlag")) == "1":
            continue
        if erp._norm(party) not in erp._norm(b.get("organName") or ""):
            continue
        try:
            debt = float(b.get("debt") or 0)
        except (TypeError, ValueError):
            debt = 0.0
        debt_total += debt
        rows.append({"number": b.get("number"), "time": str(b.get("operTime") or "")[:10],
                     "type": (b.get("type") or "") + (b.get("subType") or ""),
                     "total": float(b.get("totalPrice") or 0), "debt": debt})
        if len(rows) >= 5:
            break
    data = {"party": party, "count": len(rows), "bills": rows, "debt_recent": round(debt_total, 2),
            "window": 200}
    if not rows:
        return data, "最近 %d 张单里没有 %s 的记录（新往来单位）。" % (200, party)
    note = "%s 最近 %d 张单，近期未付合计 %g 元。" % (party, len(rows), debt_total)
    return data, note


_TOOLS = {"stock_check": _tool_stock_check, "last_price": _tool_last_price,
          "habit_defaults": _tool_habit_defaults, "party_history": _tool_party_history}


def _tools(erp, operator, drafts):
    """取证工具箱 + submit_verdict 收口。

    校验结论走 submit_verdict 的 args 通道而不是 final——结论里的 confidence 等
    自报数字不属于「ERP 观察」，走 final 会被数字接地校验整体退回（所有数字
    必须可溯源是问一问的不变量，这里不能为校验结论开洞）。"""
    def bind(fn):
        return lambda args: fn(args, erp)

    verdict_box = {}

    def _submit(args):
        verdict_box.update({
            "patches": (args or {}).get("patches") or [],
            "clarify": (args or {}).get("clarify"),
            "risks": str((args or {}).get("risks") or ""),
        })
        return ({"received": True, "patches": len(verdict_box["patches"])},
                "校验结论已收到。")

    return {
        "stock_check": {"desc": "查某商品库存够不够（卖单出货前必看）",
                        "args_hint": '{"material":"商品名","depot":"仓库名，没说就空字符串"}',
                        "run": bind(_TOOLS["stock_check"])},
        "last_price": {"desc": "查某往来单位某商品最近成交单价（补缺失价格用）",
                       "args_hint": '{"party":"往来单位","material":"商品名","direction":"out=卖价/in=进价"}',
                       "run": bind(_TOOLS["last_price"])},
        "habit_defaults": {"desc": "查操作员习惯：常用仓库、历史单价（补默认值用）",
                           "args_hint": '{"operator":"操作员名","party":"往来单位，可空","biz":"业务类型，可空"}',
                           "run": bind(_TOOLS["habit_defaults"])},
        "party_history": {"desc": "查某往来单位最近单据与欠款（新面孔/大额单谨慎确认）",
                          "args_hint": '{"party":"往来单位"}',
                          "run": bind(_TOOLS["party_history"])},
        "submit_verdict": {"desc": "提交校验结论（结束取证，恰好调用一次）",
                           "args_hint": '{"patches":[{"bill_index":0,"item_index":0,"field":"depot|price","value":..,"reason":"..","confidence":0.0-1.0}],"clarify":null,"risks":""}',
                           "run": _submit},
    }, verdict_box


# --------------------------------------------------------------------------
# 校验结论解析 + patch 确定性应用
# --------------------------------------------------------------------------
def _parse_verdict(answer):
    """模型 final → {patches, clarify, risks}。final 可能是 JSON 字符串或 dict 的
    repr（run_agent 统一 str()），依次尝试 json.loads / ast.literal_eval。"""
    if not answer:
        return None
    raw = str(answer).strip()
    for parser in (json.loads, ast.literal_eval):
        try:
            v = parser(raw)
            if isinstance(v, dict):
                return {
                    "patches": v.get("patches") or [],
                    "clarify": v.get("clarify"),
                    "risks": str(v.get("risks") or ""),
                }
        except Exception:
            continue
    # final 是嵌在别的文本里的 JSON——抠最后一个平衡大括号再试
    idx = raw.rfind("}")
    while idx > 0:
        try:
            v = json.loads(raw[:idx + 1])
            if isinstance(v, dict) and "patches" in v:
                return {"patches": v.get("patches") or [], "clarify": v.get("clarify"),
                        "risks": str(v.get("risks") or "")}
        except Exception:
            pass
        idx = raw.rfind("}", 0, idx)
    return None


PATCH_MIN_CONFIDENCE = 0.85   # 低于阈值不出自动补丁——宁缺勿错
_PATCH_FIELDS = {"depot", "price"}


def apply_patches(drafts, patches):
    """白名单字段 + 置信度阈值 + 只补缺失——模型只能建议，写回是确定性代码。
    返回 (applied, rejected)：rejected 带拒绝原因（trace 展示用）。"""
    applied, rejected = [], []
    for p in patches or []:
        if not isinstance(p, dict):
            continue
        try:
            i = int(p.get("bill_index") or 0)
        except (TypeError, ValueError):
            i = -1
        field = str(p.get("field") or "").strip()
        try:
            conf = float(p.get("confidence") or 0)
        except (TypeError, ValueError):
            conf = 0.0
        reason = str(p.get("reason") or "")[:120]
        base = {"bill_index": i, "field": field, "value": p.get("value"),
                "reason": reason, "confidence": conf}
        if i < 0 or i >= len(drafts):
            rejected.append({**base, "why": "bill_index 越界"})
            continue
        if field not in _PATCH_FIELDS:
            rejected.append({**base, "why": "字段不在白名单（只许 depot/price）"})
            continue
        if conf < PATCH_MIN_CONFIDENCE:
            rejected.append({**base, "why": "置信度 %.2f 低于阈值 %.2f" % (conf, PATCH_MIN_CONFIDENCE)})
            continue
        d = drafts[i]
        if field == "depot":
            if (d.get("depot") or "").strip():
                rejected.append({**base, "why": "草稿已填仓库，不许覆盖"})
                continue
            val = str(p.get("value") or "").strip()
            if not val:
                rejected.append({**base, "why": "空值"})
                continue
            d["depot"] = val
        else:   # price
            try:
                item_i = int(p.get("item_index") or 0)
            except (TypeError, ValueError):
                item_i = -1
            items = d.get("items") or []
            if item_i < 0 or item_i >= len(items):
                rejected.append({**base, "why": "item_index 越界"})
                continue
            try:
                val = round(float(p.get("value")), 4)
            except (TypeError, ValueError):
                rejected.append({**base, "why": "value 不是数字"})
                continue
            if val <= 0:
                rejected.append({**base, "why": "价格必须为正数"})
                continue
            cur = items[item_i].get("price")
            if cur not in (None, "", 0, 0.0):
                rejected.append({**base, "why": "明细已填单价，不许覆盖"})
                continue
            items[item_i]["price"] = val
        applied.append(base)
    return applied, rejected


# --------------------------------------------------------------------------
# 编排：parse → check → patches → clarify 决策（server 三入口与评测跑分器共用）
# --------------------------------------------------------------------------
def run_check(drafts, erp, operator="", on_event=None):
    """只读校验 loop。返回 {ok, patches, clarify, risks, steps, grounded, tokens}。"""
    prof = cfgmod.agent_profile()
    empty = {"ok": False, "patches": [], "clarify": None, "risks": "",
             "steps": [], "grounded": None, "tokens": {}}
    if not prof:
        return empty
    goal = json.dumps([{"biz": d.get("biz"), "party": d.get("party"), "depot": d.get("depot"),
                        "items": d.get("items")} for d in drafts], ensure_ascii=False)
    # {n}（草稿数）是本提示词私有的占位符，先落地；四个标准占位符交给循环引擎 format
    template = BILL_CHECK_PROMPT.replace("{n}", "%d" % len(drafts))
    try:
        tools, verdict_box = _tools(erp, operator, drafts)
        out = agentmod.run_agent(
            goal, tools, max_steps=3,
            context_block="操作员：%s" % (operator or "（未登录）"),
            on_event=on_event, prompt_template=template)
    except Exception as e:
        return dict(empty, error=str(e)[:120])
    # 结论优先取 submit_verdict 工具的 args 通道；模型忘了收口时退回解析 final 文本
    verdict = dict(verdict_box) if verdict_box.get("patches") or verdict_box.get("clarify") \
        else _parse_verdict(out.get("answer"))
    if verdict is None:
        return dict(empty, error="verdict_unparsable", answer_raw=str(out.get("answer"))[:200],
                    steps=out.get("steps") or [], tokens=out.get("tokens") or {})
    return dict(verdict, ok=True, grounded=out.get("grounded"),
                steps=out.get("steps") or [], tokens=out.get("tokens") or {},
                meta=out.get("meta") or {}, prompt_version=BILL_CHECK_PROMPT_VERSION)


def check_and_patch(drafts, erp, operator="", on_event=None):
    """run_check + 确定性 patch 应用，返回可直接放进响应的 check 摘要。
    校验失败/未配置模型时降级为 skipped——解析结果照常交付（校验是增益不是闸门）。"""
    check = {"ok": False, "patches": [], "clarify": None, "risks": "", "steps": [],
             "grounded": None, "tokens": {}, "skipped": True,
             "patches_applied": [], "patches_rejected": [], "prompt_version": None}
    if not drafts or not cfgmod.agent_profile():
        return check
    try:
        chk = run_check(drafts, erp, operator=operator, on_event=on_event)
    except Exception as e:
        check["error"] = str(e)[:120]
        return check
    applied, rejected = apply_patches(drafts, chk.get("patches"))
    chk["patches_applied"] = applied
    chk["patches_rejected"] = rejected
    return chk


def parse_and_check(text, erp, operator="", prev_draft=None, skip_clarify=False, on_event=None):
    """llm.parse → run_check → apply_patches → 澄清决策。返回：
    {drafts, meta, check, clarify:{question}|None, clarify_all:str|None}

    clarify 只在单张且未跳过时给出（need_clarification 语义与旧版一致）；
    多单的追问以 clarify_all 返回，由前端以提醒条展示（跨单追问禁用是 multi-entry
    的设计决策，这里不推翻）。"""
    from . import llm as llmmod
    drafts, meta = llmmod.parse(text, erp, prev_draft=prev_draft)
    cl_rule = meta.get("clarify") if isinstance(meta.get("clarify"), dict) else None

    check = check_and_patch(drafts, erp, operator=operator, on_event=on_event)
    check["skipped"] = bool(check.get("skipped"))

    clarify_text = check.get("clarify")
    clarify_source = "agent" if clarify_text else None
    if not clarify_text and cl_rule:
        clarify_text = cl_rule.get("question") or ""
        clarify_source = "rule"
    need_clarify = bool(clarify_text) and not skip_clarify and len(drafts) == 1
    check_out = {k: check.get(k) for k in ("ok", "steps", "patches_applied", "patches_rejected",
                                           "risks", "grounded", "tokens", "skipped", "prompt_version",
                                           "error", "answer_raw")}
    check_out["clarify_source"] = clarify_source
    return {
        "drafts": drafts,
        "meta": meta,
        "check": check_out,
        "clarify": {"question": clarify_text} if need_clarify else None,
        "clarify_all": clarify_text if (clarify_text and len(drafts) > 1 and not skip_clarify) else None,
    }
