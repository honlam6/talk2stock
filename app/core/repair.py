#!/usr/bin/env python3
"""导入修复 agent（P-C / ADR-12）：Excel/图片导入后「需处理」行的智能处置。

定位（与 multi-entry 设计哲学一致）：确定性变换保正确性（可单测、500 行与 5 行
同成本），agent 只做修复环 v2——对变换后仍带 problems 的行做只读取证（查商品
档案、查往来单位、搜历史单据原话），提出修复建议；写回是确定性代码。

不变量：
- fix 白名单只有 item_name / party（商品名与往来单位），qty/price 永远不动
  ——数字正确性由确定性变换负责，agent 不得碰；
- material_id 只有在 erp.material_by_id 真实存在时才写回（直达档案消歧义）；
- confidence < 0.85 的建议只进 suggestions（前端「采纳」按钮），不自动改；
- agent 只建议、不写库，导入确认仍走逐单令牌闸门。
"""
import json

from . import agent as agentmod
from . import config as cfgmod

REPAIR_PROMPT_VERSION = "repair-v1-20261116"

REPAIR_PROMPT = """你是导入修复 agent。表格导入后，下面这些单据行无法通过校验（problems 列出每行的问题）：
{goal}

# 工具目录（每轮只能调用一个）
{tool_catalog}

# 已完成的步骤（观察是工具返回的真实数据）
{history}

# 输出格式（严格 JSON，每轮恰好一个动作）
继续取证：{{"thought":"简短说明为什么查这个","tool":"工具名","args":{{...}}}}
修复结论：{{"thought":"一句话总结","tool":"submit_verdict","args":{{"fixes":[...]}}}}

fix 元素：{{"row_index":0,"item_index":0,"field":"item_name|party","value":"系统里写得对的名字","material_id":123,"confidence":0.0~1.0,"reason":"依据"}}

# 规则
1. 最多 {max_steps} 轮。problems 说商品没匹配 → find_material（把表里的写法近似成
   系统档案名，型号列进去区分同名规格）；往来单位没匹配 → find_party；
   表格写法太野查不到 → search_history 用关键词搜历史单据原话。
2. fixes 只许改 item_name / party 两个字段；数量、单价、日期永远不动。
3. value 必须来自工具观察（档案名 / 历史单据原话），禁止凭空造名；查不到就不出
   fix——那行留给人工处理。material_id 只在 find_material 返回的候选里有就带上。
4. 有把握（观察里有明确对应档案）confidence 给 0.9+；只是猜测给 0.5 以下。
5. 每个问题行至多一个 fix；行内有多个问题字段就分多个 fix（item_index 对应明细行）。
6. submit_verdict 恰好调用一次。"""

MAX_REPAIR_ROWS = 30   # 一次最多修这么多行——超出说明映射本身坏了，该让用户改映射


def _sql_escape(s):
    return str(s or "").strip()


def _tool_find_material(args, erp):
    name = str(args.get("name") or "").strip()
    if not name:
        return ({"error": "name 必填"}, "find_material 需要 name 参数。")
    mrow, kind = erp.find_material(name)
    cands = erp.material_candidates(name) if not mrow else []
    data = {"query": name, "found": bool(mrow), "match_kind": kind or "",
            "matched": {"id": mrow.get("id"), "name": mrow.get("name"), "model": mrow.get("model") or ""} if mrow else None,
            "candidates": cands[:5]}
    if mrow:
        label = (mrow.get("name") or "") + (f"（{mrow.get('model')}）" if mrow.get("model") else "")
        note = "系统档案：%s（id=%s，匹配方式 %s）。" % (label, mrow.get("id"), kind or "exact")
    elif cands:
        labels = "；".join("%s%s id=%s" % (c.get("name"), f"（{c['model']}）" if isinstance(c, dict) and c.get("model") else "",
                                           c.get("id") if isinstance(c, dict) else "?") for c in cands[:5])
        note = "没有唯一档案，最接近的候选：%s。" % labels
    else:
        note = "系统里没有与「%s」接近的商品档案。" % name
    return data, note


def _tool_find_party(args, erp):
    name = str(args.get("name") or "").strip()
    ptype = str(args.get("type") or "").strip()
    if not name:
        return ({"error": "name 必填"}, "find_party 需要 name 参数。")
    prow, kind = erp.find_party(name, ptype or None)
    cands = erp.party_candidates(name, ptype or None) if not prow else []
    data = {"query": name, "type": ptype, "found": bool(prow), "match_kind": kind or "",
            "matched": {"id": prow.get("id"), "name": prow.get("supplier")} if prow else None,
            "candidates": cands[:5]}
    if prow:
        note = "往来单位档案：%s（id=%s，匹配方式 %s）。" % (prow.get("supplier"), prow.get("id"), kind or "exact")
    elif cands:
        labels = "；".join((c.get("supplier") if isinstance(c, dict) else str(c)) for c in cands[:5])
        note = "没有唯一档案，最接近的候选：%s。" % labels
    else:
        note = "系统里没有与「%s」接近的往来单位（将自动新建，或建议改映射）。" % name
    return data, note


def _tool_search_history(args, erp):
    from . import retrieve as rmod
    query = str(args.get("query") or "").strip()
    if not query:
        return ({"error": "query 必填"}, "search_history 需要 query 参数。")
    return rmod.search_history(query)


def run_repair(rows, erp, operator="", on_event=None):
    """rows: sheet.transform 产出的 drafts（[{row_no, draft, problems}]）。
    只把带 problems 的行交给模型；返回 {ok, fixes, steps, tokens, rows_context}。"""
    prof = cfgmod.agent_profile()
    empty = {"ok": False, "fixes": [], "steps": [], "tokens": {}, "skipped": True}
    if not prof:
        return empty
    targets = [(i, r) for i, r in enumerate(rows) if r.get("problems")]
    if not targets or len(targets) > MAX_REPAIR_ROWS:
        return dict(empty, skipped=True,
                    reason="no_problems" if not targets else "too_many_rows(%d)" % len(targets))
    ctx = []
    for i, r in targets:
        d = r.get("draft") or {}
        ctx.append({"row_index": i, "row_no": r.get("row_no"), "problems": r.get("problems"),
                    "biz": d.get("biz"), "party": d.get("party"), "depot": d.get("depot"),
                    "items": d.get("items")})
    goal = json.dumps(ctx, ensure_ascii=False)

    verdict_box = {}

    def _submit(args):
        verdict_box["fixes"] = (args or {}).get("fixes") or []
        return ({"received": True, "fixes": len(verdict_box["fixes"])}, "修复结论已收到。")

    def bind(fn):
        return lambda args: fn(args, erp)

    tools = {
        "find_material": {"desc": "按表格里的写法找系统商品档案（同名规格看型号），返回候选 id",
                          "args_hint": '{"name":"表格里的商品名写法"}',
                          "run": bind(_tool_find_material)},
        "find_party": {"desc": "按表格里的写法找往来单位档案（type 给客户或供应商，不确定可空）",
                       "args_hint": '{"name":"表格里的往来单位写法","type":"客户或供应商，可空"}',
                       "run": bind(_tool_find_party)},
        "search_history": {"desc": "按关键词搜历史单据原话（表格写法太野时找参照）",
                           "args_hint": '{"query":"关键词"}',
                           "run": bind(_tool_search_history)},
        "submit_verdict": {"desc": "提交修复结论（结束取证，恰好调用一次）",
                           "args_hint": '{"fixes":[{"row_index":0,"item_index":0,"field":"item_name|party","value":"..","material_id":可选,"confidence":0.0-1.0,"reason":".."}]}',
                           "run": _submit},
    }
    template = REPAIR_PROMPT.replace("{n}", "%d" % len(targets))
    try:
        out = agentmod.run_agent(
            goal, tools, max_steps=3,
            context_block="操作员：%s" % (operator or "（未登录）"),
            on_event=on_event, prompt_template=template)
    except Exception as e:
        return dict(empty, error=str(e)[:120])
    fixes = verdict_box.get("fixes")
    if fixes is None:
        fixes = []
    return {"ok": True, "fixes": fixes, "steps": out.get("steps") or [],
            "tokens": out.get("tokens") or {}, "skipped": False,
            "grounded": out.get("grounded"), "targets": len(targets),
            "prompt_version": REPAIR_PROMPT_VERSION}


FIX_MIN_CONFIDENCE = 0.85
_FIX_FIELDS = {"item_name", "party"}


def _int_or(v, default):
    """索引解析：0 是合法索引值，不能用 `or` 兜底（0 是假值会吞掉）。"""
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def apply_fixes(rows, fixes, erp):
    """确定性写回：白名单字段 + 置信度阈值 + material_id 存在性校验。
    返回 (applied, suggestions, rejected)。"""
    applied, suggestions, rejected = [], [], []
    for f in fixes or []:
        if not isinstance(f, dict):
            continue
        ri = _int_or(f.get("row_index"), -1)
        field = str(f.get("field") or "").strip()
        try:
            conf = float(f.get("confidence") or 0)
        except (TypeError, ValueError):
            conf = 0.0
        base = {"row_index": ri, "item_index": f.get("item_index"), "field": field,
                "value": f.get("value"), "material_id": f.get("material_id"),
                "reason": str(f.get("reason") or "")[:120], "confidence": conf}
        if ri < 0 or ri >= len(rows):
            rejected.append({**base, "why": "row_index 越界"})
            continue
        if field not in _FIX_FIELDS:
            rejected.append({**base, "why": "字段不在白名单（只许 item_name/party）"})
            continue
        if conf < FIX_MIN_CONFIDENCE:
            suggestions.append(base)      # 低置信 → 前端「采纳」按钮
            continue
        d = rows[ri].get("draft") or {}
        value = str(f.get("value") or "").strip()
        if not value:
            rejected.append({**base, "why": "空值"})
            continue
        if field == "party":
            if (d.get("party") or "").strip():
                rejected.append({**base, "why": "已有往来单位，不许覆盖"})
                continue
            d["party"] = value
        else:   # item_name
            ii = _int_or(f.get("item_index"), -1)
            items = d.get("items") or []
            if ii < 0 or ii >= len(items):
                rejected.append({**base, "why": "item_index 越界"})
                continue
            items[ii]["name"] = value
            mid = f.get("material_id")
            if mid:
                try:
                    mrow = erp.material_by_id(int(mid))
                except Exception:
                    mrow = None
                if mrow:
                    items[ii]["material_id"] = int(mid)   # 点选直达语义，resolve 精确匹配
                else:
                    rejected.append({**base, "why": "material_id 不存在，已改名但未绑定档案"})
                    continue
        applied.append(base)
    return applied, suggestions, rejected


def repair_transform_result(tr, erp, operator="", on_event=None):
    """对 transform 结果跑修复 agent + 确定性写回。返回 (tr, repair 摘要)。
    tr 的 drafts 原地更新（已采纳 fix 后 problems 重算交给后续 resolve）。"""
    rows = tr.get("drafts") or []
    rep = run_repair(rows, erp, operator=operator, on_event=on_event)
    applied, suggestions, rejected = apply_fixes(rows, rep.get("fixes"), erp)
    rep["applied"] = applied
    rep["suggestions"] = suggestions
    rep["rejected"] = rejected
    return tr, rep
