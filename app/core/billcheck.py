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

BILL_CHECK_PROMPT_VERSION = "bcheck-v2-20261125"

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
1. 最多 {max_steps} 轮，需要什么查什么：缺单价查 last_price（务必传 unit 防止不同单位串价）或 habit_defaults；
   没说仓库查 habit_defaults；卖单查 stock_check 看库存够不够；往来单位不熟查 party_history。
   观察已经足够就立即用 submit_verdict 收尾，不要为查而查。
2. submit_verdict 的 patches 只给「建议补全」：depot 只在草稿没写仓库时建议，price 只在明细
   缺价或为 0 时建议。草稿里已有的值一个都不许动。
3. patch 的 value 必须原样来自工具观察（历史成交价 / 习惯仓库），禁止编造、禁止心算新数字；
   来自同往来单位+同商品且单位一致的 last_price 或 habit_defaults 明确记录时，confidence 给 0.90~0.95；
   仅在跨客户估算或单位存疑时压低至 ≤0.75；若单位不一致或查无记录，就不出 patch——宁缺勿错，空 patches 完全合法。
4. 关键信息缺失（商品 / 数量 / 往来单位）时在 submit_verdict 的 clarify 写一句自然的
   中文追问；多张单缺什么合并成一句，带上「第几张单缺什么」。没有缺失就 null。
5. risks 用一句话提示观察到的风险（库存不够 / 该单位有大额欠款 / 价格与历史差异大），
   没有就空串。
6. submit_verdict 必须调用恰好一次，调用后循环立即结束。

# 待校验目标
{goal}"""


# --------------------------------------------------------------------------
# 只读工具（返回 (data, note)——note 是模板句，数字接地兜底用）
# --------------------------------------------------------------------------
def _tool_stock_check(args, erp):
    """出库前库存校验：args 没写仓库时补默认出库仓，按**该仓**库存判断。"""
    a = dict(args or {})
    if not a.get("depot"):
        fn_dep = getattr(erp, "default_depot_name", None)
        if callable(fn_dep):
            d = fn_dep("out")
        else:
            from . import config as cfgmod
            ui = cfgmod.load().get("ui") or {}
            d = ui.get("default_depot_out") or ui.get("default_depot_in") or ""
        if d:
            a["depot"] = d
            data, note = askmod._stock_query(a, erp)
            note = "（按默认出库仓「%s」校验）%s" % (d, note)
            return _judge(data, note, a, erp=erp)
    data, note = askmod._stock_query(a, erp)
    return _judge(data, note, a, erp=erp)


def _judge(data, note, args, erp=None):
    """把「要出多少（含副单位折算） / 仓里有多少 / 够不够」算出来，不让模型自己比。"""
    try:
        need = float(args.get("qty") or 0)
    except (TypeError, ValueError):
        need = 0.0
    if not need or not isinstance(data, dict):
        return data, note
    req_unit = str(args.get("unit") or "").strip()
    basic_unit = str(data.get("unit") or "").strip()
    need_basic = need
    if req_unit and erp is not None and data.get("found") is not False:
        try:
            mrow, _ = erp.find_material(args.get("material") or data.get("material") or "")
            if mrow:
                grp = erp.unit_group_by_id(mrow.get("unitId") or mrow.get("unit_id"))
                if grp and erp.unit_allowed(grp, req_unit):
                    ratio = float(erp.unit_ratio(grp, req_unit) or 1.0)
                    need_basic = round(need * ratio, 4)
                    basic_unit = str(mrow.get("unit") or basic_unit)
        except Exception:
            pass
    have = float(data.get("total") or 0)
    data["need"] = need
    if req_unit:
        data["need_unit"] = req_unit
    data["need_basic"] = need_basic
    data["enough"] = have >= need_basic
    data["shortfall"] = round(max(0.0, need_basic - have), 2)
    tail = "足够" if data["enough"] else "缺 %g%s" % (data["shortfall"], basic_unit)
    need_desc = ("%g%s（折合 %g%s）" % (need, req_unit, need_basic, basic_unit)
                 if need_basic != need and req_unit else "%g%s" % (need_basic, basic_unit))
    return data, "%s 需出 %s，仓内 %g%s，%s。" % (note.rstrip("。"), need_desc, have, basic_unit, tail)


def _sql_escape(s):
    """LIKE 模式与字符串字面量的最小转义（只读查询，防注入仍由 dbread 白名单兜底）。"""
    return (str(s or "").strip().replace("\\", "\\\\").replace("'", "''")
            .replace("%", "\\%").replace("_", "\\_"))


def _tool_last_price(args, erp):
    """某往来单位某商品最近成交单价（精确商品优先 + 单位一致性防护）。

    实现走 dbread 只读 SQL（jsh_depot_item.unit_price 是真实行价；REST 的
    getDetailList 不吐行价，只吐商品档案价快照 purchaseDecimal，不能用）。"""
    party = str(args.get("party") or "").strip()
    material = str(args.get("material") or "").strip()
    direction = str(args.get("direction") or "").strip() or "out"
    want_unit = str(args.get("unit") or "").strip()
    if not party or not material:
        return ({"error": "party 与 material 必填", "got": args},
                "last_price 需要 party 和 material 两个参数。")
    from . import dbread as dbmod
    erp_user = str((cfgmod.load().get("erp") or {}).get("user") or "jsh")
    tenant = ("(SELECT tenant_id FROM jsh_user WHERE login_name='%s' ORDER BY id LIMIT 1)"
              % erp_user.replace("'", "''"))   # 不加租户锚会跨租户捞价（正确性）
    type_zh = "出库" if direction == "out" else "入库"
    organ_type = "客户" if direction == "out" else "供应商"
    # 精确商品匹配优先（避免 LIKE '%PPR管%' 或同名多规格串价）
    mat_cond = ("di.material_id IN (SELECT id FROM jsh_material WHERE delete_flag='0' AND name LIKE '%%%s%%')"
                % _sql_escape(material))
    if erp is not None:
        try:
            mrow, mkind = erp.find_material(material)
            if mrow and mrow.get("id") and mkind in ("exact", "fuzzy"):
                mat_cond = "di.material_id=%d" % int(mrow["id"])
        except Exception:
            pass
    sql = ("SELECT dh.oper_time AS time, di.unit_price AS price, di.oper_number AS qty, di.material_unit AS unit "
           "FROM jsh_depot_item di JOIN jsh_depot_head dh ON di.header_id = dh.id "
           "WHERE dh.delete_flag='0' AND di.delete_flag='0' AND dh.status IN ('1','2','3') "
           "AND dh.type='%s' AND di.unit_price > 0 "
           "AND dh.tenant_id=%s "
           "AND dh.organ_id IN (SELECT id FROM jsh_supplier WHERE delete_flag='0' AND type='%s' AND supplier LIKE '%%%s%%') "
           "AND %s "
           "ORDER BY dh.oper_time DESC LIMIT 8") % (
        type_zh, tenant, organ_type, _sql_escape(party), mat_cond)
    data, note = dbmod.sql_query({"sql": sql})
    rows = data.get("rows") or [] if "error" not in data else []
    records = []
    for r in rows:
        try:
            records.append({"time": str(r.get("time") or "")[:10],
                            "price": round(float(r.get("price")), 4),
                            "qty": float(r.get("qty") or 0),
                            "unit": str(r.get("unit") or "").strip()})
        except (TypeError, ValueError):
            continue
    # 单位一致性检查：避免「箱」单套用「瓶」的历史价
    unit_mismatch = False
    other_unit_records = []
    if want_unit and records:
        same = [x for x in records if not x["unit"] or x["unit"] == want_unit]
        other = [x for x in records if x["unit"] and x["unit"] != want_unit]
        if same:
            records = same
        elif other:
            unit_mismatch = True
            other_unit_records = other[:3]
            records = []
    out = {"found": bool(records), "party": party, "material": material,
           "direction": direction, "unit": want_unit, "records": records}
    if unit_mismatch:
        out["unit_mismatch"] = True
        out["other_unit_records"] = other_unit_records
        o0 = other_unit_records[0]
        return out, ("%s 的 %s 历史成交单位为「%s」（%g元），与本单单位「%s」不一致，不可直接套用单价。"
                     % (party, material, o0["unit"], o0["price"], want_unit))
    if not records:
        if "error" in data:
            return out, "历史价查询失败（%s），请改用 habit_defaults 或向用户确认。" % str(data["error"])[:60]
        note = "最近单据里没有 %s 的 %s %s记录。" % (party, material, "销售" if direction == "out" else "采购")
        return out, note
    first = records[0]
    note = "%s 最近一笔 %s %s单价 %g 元/%s（%s）。" % (
        party, material, "销售" if direction == "out" else "采购",
        first["price"], first["unit"] or want_unit or "件", first["time"])
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
    try:
        from . import ledger as ledgermod
        sql_res = ledgermod.party_history_sql(party, limit=5)
        if sql_res is not None:
            if not sql_res["bills"]:
                return sql_res, "系统历史单据里没有 %s 的记录（新往来单位）。" % party
            note = "%s 共 %d 张单（最近 %d 张如下），未付/未收欠款合计 %g 元。" % (
                party, sql_res["total_count"], sql_res["count"], sql_res["debt_total"])
            return sql_res, note
    except Exception:
        pass
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
        "last_price": {"desc": "查某往来单位某商品最近成交单价（补缺失价格用，带单位一致性核对）",
                       "args_hint": '{"party":"往来单位","material":"商品名","direction":"out=卖价/in=进价","unit":"本单单位，可空"}',
                       "run": bind(_TOOLS["last_price"])},
        "habit_defaults": {"desc": "查操作员习惯：常用仓库、历史单价（补默认值用）",
                           "args_hint": '{"operator":"操作员名","party":"往来单位，可空","biz":"业务类型，可空"}',
                           "run": bind(_TOOLS["habit_defaults"])},
        "party_history": {"desc": "查某往来单位最近单据与欠款（新面孔/大额单谨慎确认）",
                          "args_hint": '{"party":"往来单位"}',
                          "run": bind(_TOOLS["party_history"])},
        "submit_verdict": {"desc": "提交校验结论（结束取证，恰好调用一次）",
                           "args_hint": '{"patches":[{"bill_index":0,"item_index":0,"field":"depot|price","value":..,"reason":"..","confidence":0.0-1.0}],"clarify":null,"risks":""}',
                           "run": _submit,
                           "terminal": True},
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


def apply_patches(drafts, patches, observations=None, apply_to_draft=True):
    """白名单字段 + 置信度阈值 + 观察接地 + 只补缺失——模型只能建议，写回是确定性代码。

    observations: 传入非空/空列表（非 None）时，强制校验补丁值必须来自工具观察（防模型编造价格/仓库）。
    apply_to_draft: True 时直接写回 drafts（供显式采纳或单测）；False 时只做校验归集为
                    patches_suggested，绝不静默篡改用户抽取出的草稿（事实与建议分离）。
    返回 (applied, rejected)：rejected 带拒绝原因（trace 展示用）。"""
    obs_nums, obs_text = None, None
    if observations is not None:
        obs_text = json.dumps(observations, ensure_ascii=False)
        obs_nums = agentmod._numbers_in(obs_text)
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
        if field == "price":
            try:
                base["item_index"] = int(p.get("item_index") or 0)
            except (TypeError, ValueError):
                base["item_index"] = -1
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
            if obs_text is not None and val not in obs_text:
                rejected.append({**base, "why": "建议仓库未出现在工具观察中（接地拦截）"})
                continue
            if apply_to_draft:
                d["depot"] = val
        else:   # price
            item_i = base.get("item_index", -1)
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
            if obs_nums is not None and round(val, 2) not in obs_nums:
                rejected.append({**base, "why": "建议价格未出现在工具观察中（接地拦截）"})
                continue
            base["value"] = val
            if apply_to_draft:
                items[item_i]["price"] = val
        applied.append(base)
    return applied, rejected


# --------------------------------------------------------------------------
# 确定性预检：完整草稿直接通过（0 LLM 额外延迟）；缺字段/库存异常/低置信走完整 Agent 循环
# --------------------------------------------------------------------------
def _needs_agent_check(drafts, erp, operator="", meta=None):
    """返回 (needs_llm, pre_steps, pre_risks, pre_patches)。
    完整单（要素齐全、非低置信、出库库存充足、无待查价格）直接由确定性取证闭环，
    省掉 2~3 轮无意义 LLM 调用，同时保留真实库存校验轨迹。"""
    pre_steps, pre_risks, pre_patches = [], [], []
    needs_llm = False
    bills_meta = (meta or {}).get("bills") or []
    for bi, d in enumerate(drafts):
        biz = str(d.get("biz") or "")
        party = str(d.get("party") or "").strip()
        depot = str(d.get("depot") or "").strip()
        items = d.get("items") or []
        bm = bills_meta[bi] if bi < len(bills_meta) and isinstance(bills_meta[bi], dict) else {}
        if bm.get("confidence") == "low" or bm.get("problems"):
            needs_llm = True
        if not items:
            needs_llm = True
        if biz in ("purchase_in", "sale_out", "purchase_return", "sale_return") and not party:
            needs_llm = True
        for it in items:
            try:
                q = float(it.get("qty") or 0)
            except (TypeError, ValueError):
                q = 0.0
            try:
                pr = float(it.get("price") or 0)
            except (TypeError, ValueError):
                pr = 0.0
            if q <= 0 or (pr <= 0 and biz in ("purchase_in", "sale_out", "purchase_return", "sale_return")):
                needs_llm = True
        # 未写仓库时，先用 1ms 本地习惯记忆查一下（无记录则无需跑 LLM 空转）
        if not depot and not needs_llm:
            try:
                hs = habitsmod.summary(operator=operator, party=party, biz=biz)
                if hs.get("depot"):
                    pre_steps.append({
                        "step": len(pre_steps) + 1, "tool": "habit_defaults",
                        "args": {"operator": operator, "party": party, "biz": biz},
                        "ok": True, "thought": "检查操作员常用仓库习惯",
                        "observation": {"depot": hs["depot"], "records": hs.get("records", 0)},
                    })
                    pre_patches.append({
                        "bill_index": bi, "field": "depot", "value": hs["depot"],
                        "reason": "操作员常用仓库（%d条历史记录）" % hs.get("records", 0),
                        "confidence": 0.9,
                    })
            except Exception:
                pass
        # 出库单：在确定性通道快速核对库存是否足够
        if biz in ("sale_out", "purchase_return", "other_out") and erp is not None and not needs_llm:
            for it in items:
                nm = str(it.get("name") or "").strip()
                if not nm:
                    continue
                try:
                    _qty = it.get("qty")
                    _unit = it.get("unit") or ""
                    sdata, snote = _tool_stock_check(
                        {"material": nm, "depot": depot, "qty": _qty, "unit": _unit}, erp)
                    pre_steps.append({
                        "step": len(pre_steps) + 1, "tool": "stock_check",
                        "args": {"material": nm, "depot": depot, "qty": _qty, "unit": _unit},
                        "ok": True, "thought": "出库前校验「%s」当前库存" % nm,
                        "observation": sdata,
                    })
                    tot = sdata.get("total")
                    need_cmp = sdata.get("need_basic") if sdata.get("need_basic") is not None else float(_qty or 0)
                    if sdata.get("found") is False:
                        pre_risks.append("「%s」在系统中暂无唯一档案" % nm)
                    elif sdata.get("enough") is False or (tot is not None and float(tot) < float(need_cmp)):
                        pre_risks.append("「%s」库存仅 %g，低于本次出库 %g" % (nm, float(tot), float(need_cmp)))
                except Exception:
                    pass
    return needs_llm, pre_steps, "；".join(pre_risks), pre_patches


# --------------------------------------------------------------------------
# 编排：parse → check → grounded suggestions → clarify 决策（三入口与评测共用）
# --------------------------------------------------------------------------
def run_check(drafts, erp, operator="", on_event=None, meta=None):
    """只读校验 loop。返回 {ok, patches, clarify, risks, steps, grounded, tokens, observations}。"""
    prof = cfgmod.agent_profile()
    empty = {"ok": False, "patches": [], "clarify": None, "risks": "",
             "steps": [], "grounded": None, "tokens": {}, "observations": []}
    if not prof:
        return empty
    needs_llm, pre_steps, pre_risks, pre_patches = _needs_agent_check(
        drafts, erp, operator=operator, meta=meta)
    if not needs_llm:
        for st in pre_steps:
            agentmod._emit(on_event, {"type": "step", "step": st})
        obs = [st["observation"] for st in pre_steps if st.get("observation") is not None]
        return {
            "ok": True, "patches": pre_patches, "clarify": None, "risks": pre_risks,
            "steps": pre_steps, "grounded": True, "tokens": {}, "observations": obs,
            "meta": {"steps_used": len(pre_steps), "fast_path": True},
            "prompt_version": BILL_CHECK_PROMPT_VERSION,
        }
    goal = json.dumps([{"biz": d.get("biz"), "party": d.get("party"), "depot": d.get("depot"),
                        "items": d.get("items")} for d in drafts], ensure_ascii=False)
    template = BILL_CHECK_PROMPT
    try:
        tools, verdict_box = _tools(erp, operator, drafts)
        out = agentmod.run_agent(
            goal, tools, max_steps=4,
            context_block="操作员：%s" % (operator or "（未登录）"),
            on_event=on_event, prompt_template=template)
    except Exception as e:
        return dict(empty, error=str(e)[:120])
    # 只把非 submit_verdict 工具的真实 ERP/习惯观察作为接地池（防自证其词）
    real_obs = [s["observation"] for s in (out.get("steps") or [])
                if s.get("tool") and s.get("tool") != "submit_verdict"
                and s.get("ok") and s.get("observation") is not None]
    verdict = dict(verdict_box) if "patches" in verdict_box else _parse_verdict(out.get("answer"))
    if verdict is None:
        return dict(empty, error="verdict_unparsable", answer_raw=str(out.get("answer"))[:200],
                    steps=out.get("steps") or [], tokens=out.get("tokens") or {},
                    observations=real_obs)
    return dict(verdict, ok=True, grounded=out.get("grounded"),
                steps=out.get("steps") or [], tokens=out.get("tokens") or {},
                observations=real_obs,
                meta=out.get("meta") or {}, prompt_version=BILL_CHECK_PROMPT_VERSION)


def check_and_patch(drafts, erp, operator="", on_event=None, meta=None, auto_apply=False):
    """run_check + 确定性校验（接地 + 白名单 + 阈值）。

    默认 auto_apply=False（事实与建议分离）：绝不静默篡改用户抽取出的草稿（价格/仓库），
    所有通过接地校验的高置信补丁存入 patches_suggested，供前端确认卡一键采纳；
    校验失败/未配置模型时降级为 skipped——解析结果照常交付（校验是增益不是闸门）。"""
    check = {"ok": False, "patches": [], "clarify": None, "risks": "", "steps": [],
             "grounded": None, "tokens": {}, "skipped": True,
             "patches_applied": [], "patches_suggested": [], "patches_rejected": [],
             "prompt_version": None}
    if not drafts or not cfgmod.agent_profile():
        return check
    try:
        chk = run_check(drafts, erp, operator=operator, on_event=on_event, meta=meta)
    except Exception as e:
        check["error"] = str(e)[:120]
        return check
    obs = chk.get("observations")
    valid, rejected = apply_patches(
        drafts, chk.get("patches"), observations=obs, apply_to_draft=bool(auto_apply))
    if auto_apply:
        chk["patches_applied"] = valid
        chk["patches_suggested"] = []
    else:
        chk["patches_applied"] = []
        chk["patches_suggested"] = valid
    chk["patches_rejected"] = rejected
    return chk


def parse_and_check(text, erp, operator="", prev_draft=None, skip_clarify=False, on_event=None):
    """llm.parse → run_check → 接地建议归集 → 澄清决策。返回：
    {drafts, meta, check, clarify:{question}|None, clarify_all:str|None}

    不变量：草稿字段忠实还原用户原话，校验 Agent 的历史价/习惯仓通过接地核验后放入
    check.patches_suggested（前端确认卡可一键采纳），绝不静默覆盖用户草稿。"""
    from . import llm as llmmod
    drafts, meta = llmmod.parse(text, erp, prev_draft=prev_draft)

    check = check_and_patch(drafts, erp, operator=operator, on_event=on_event, meta=meta,
                            auto_apply=False)
    check["skipped"] = bool(check.get("skipped"))

    # 规则追问优先沿用 meta.clarify，若校验 Agent 查到了历史参考价则自然融入追问文案
    cl_rule = meta.get("clarify") if isinstance(meta, dict) else None
    if cl_rule is None and len(drafts) == 1:
        c = llmmod._clarify_question(drafts[0])
        if c:
            cl_rule = {"question": c[0], "missing": c[1]}
    clarify_text = check.get("clarify")
    clarify_source = "agent" if clarify_text else None
    if not clarify_text and cl_rule:
        q = cl_rule.get("question") or ""
        if cl_rule.get("missing") == "price":
            price_sugg = [p for p in (check.get("patches_suggested") or []) if p.get("field") == "price"]
            if price_sugg:
                hints = "、".join("%g元（%s）" % (p["value"], p.get("reason") or "历史参考价")
                                  for p in price_sugg[:2])
                q = q + "（系统查到参考价：" + hints + "）"
        clarify_text = q
        clarify_source = "rule"
    need_clarify = bool(clarify_text) and not skip_clarify and len(drafts) == 1
    check_out = {k: check.get(k) for k in ("ok", "steps", "patches_applied", "patches_suggested",
                                           "patches_rejected", "risks", "grounded", "tokens",
                                           "skipped", "prompt_version", "error", "answer_raw", "meta")}
    check_out["clarify_source"] = clarify_source
    return {
        "drafts": drafts,
        "meta": meta,
        "check": check_out,
        "clarify": {"question": clarify_text} if need_clarify else None,
        "clarify_all": clarify_text if (clarify_text and len(drafts) > 1 and not skip_clarify) else None,
    }
