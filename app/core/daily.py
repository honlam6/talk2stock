#!/usr/bin/env python3
"""老板日报 agent（P-D）：当日懒触发 + 缓存，给老板一份可审计的经营早报。

流程（morning_report，主控接线 GET /api/daily/agent 与 POST /api/daily/regenerate）：
    当日缓存命中 → 直接返回（cached=True）
      → run_agent（复用 core/agent.py 引擎，只换提示词与工具箱，max_steps=5）
        工具：today_stats / month_stats / debts / low_stock / stock_value /
             recent_bills / query_bills / sql_query（全部只读，desc/args_hint
             与问一问同源——直接取自 ask._tools，不自己编文案）
      → submit_verdict 收口：结论走工具 args 通道而不是 final（与 billcheck 同模式：
        结论里若混入观察之外的自报数字，走 final 会被数字接地校验整体退回）
      → 逐点校验：points 的 title+detail 复用 agent.numeric_grounded 对观察 JSON
        校验，不合格的点直接丢弃（宁缺勿错）；suggest_drafts 结构合法化
        （biz 限 purchase_in、party 必须在观察里出现过、items 只留 name/qty/unit）
      → 全部点被丢 / LLM 失败 → 规则模板兜底（degraded=True，句子复用问一问
        各统计工具的模板句，只用函数返回的数字——零编造有结构保证）

不变量：全程只读；日报本身不写库；补货建议只是草稿（无 price），真正写入必须
由用户在确认卡确认（/api/resolve 换令牌 → /api/commit 过闸门）。
"""
import ast
import json
import threading
import time

from . import agent as agentmod
from . import ask as askmod
from . import config as cfgmod
from .erp import get_client

DAILY_PROMPT_VERSION = "daily-v1-20261116"
MAX_STEPS = 6   # 四个基础面各查一次 + 可选深挖一步 + submit_verdict 收口——
                # 实测 5 步会被「想多看一眼」烧满，被迫走降级模板（v1 评测教训）

DAILY_PROMPT = """你是给老板看的经营早报编辑。通过调用只读工具收集经营数据，
自主决定最值得告诉老板的 3~5 个点，并给出至多 2 条补货建议。

# 工具目录（每轮只能调用一个）
{tool_catalog}

# 已完成的步骤（观察是工具返回的真实数据）
{history}

# 输出格式（严格 JSON，每轮恰好一个动作）
继续取证：{{"thought":"简短说明为什么查这个","tool":"工具名","args":{{...}}}}
早报完成：{{"thought":"一句话总结","tool":"submit_verdict","args":{{"points":[...],"suggest_drafts":[...]}}}}

point 元素：{{"title":"10字以内标题","detail":"一到两句中文，必须引用观察里的具体数字","severity":"info|warn|good"}}
suggest_draft 元素：{{"biz":"purchase_in","party":"供应商名","items":[{{"name":"商品名","qty":建议补货量,"unit":"单位"}}]}}

# 规则
1. 最多 {max_steps} 轮：today_stats / month_stats / debts / low_stock 一般各查一次就够；
   要挖「最近谁在进货 / 某天卖了什么」用 recent_bills 或 query_bills。观察已经足够
   就立即 submit_verdict 收尾，不要为查而查。
2. 候选角度：库存紧张（负库存/低于安全库存）、欠款集中（应收/应付大头）、最近销量
   异常、今日现金差额（销售-采购）。有异常优先说异常；没有异常就给经营现状类点
   （采销规模/库存货值/欠款结构），不要硬编问题。
3. 每个 point 的 detail 必须引用观察里的数字（原样照抄，禁止编造、禁止心算新数字），
   数字带单位（元/瓶/种/笔）。没有数字支撑的角度不要写。
4. severity 只能取 info / warn / good：风险用 warn，向好用 good，中性用 info。
5. suggest_drafts 最多 2 条补货建议：party 必须是观察里出现过的供应商（采购单对手方），
   items 的 qty 是依据观察（销量/库存）给的建议补货量，items 里绝不含 price——
   价格由确认卡与校验 agent 补。观察里没有可靠依据就不出建议，空数组完全合法。
6. submit_verdict 必须调用恰好一次，调用后不要再输出其他动作。

# 任务
{goal}"""

# 当日缓存：date 非当天即失效；invalidate() 供 server 在 commit 单据后调用
_cache = {"date": None, "data": None}
_lock = threading.Lock()

_SEVERITIES = ("info", "warn", "good")


# --------------------------------------------------------------------------
# 工具箱：日报取证 8 件 + submit_verdict 收口
# --------------------------------------------------------------------------
def _tools(erp):
    """日报工具箱。desc/args_hint 与问一问完全同源（取自 ask._tools 构造结果，
    不自己编文案防漂移）；run 沿用 ask 的 erp 绑定——单测把 patch 打在 askmod
    命名空间（official_stats / summary / stock_overview / _rb）即可生效。"""
    base = askmod._tools(erp)
    keep = ("today_stats", "month_stats", "debts", "low_stock", "stock_value",
            "recent_bills", "query_bills", "sql_query")
    tools = {}
    for name in keep:
        t = base.get(name)
        if t:
            tools[name] = {"desc": t["desc"], "args_hint": t["args_hint"], "run": t["run"]}

    verdict_box = {}

    def _submit(args):
        verdict_box.update({
            "points": (args or {}).get("points") or [],
            "suggest_drafts": (args or {}).get("suggest_drafts") or [],
        })
        return ({"received": True, "points": len(verdict_box["points"])},
                "日报结论已收到。")

    tools["submit_verdict"] = {
        "desc": "提交日报结论（结束取证，恰好调用一次）",
        "args_hint": ('{"points":[{"title":"..","detail":"..（引用观察里的数字）",'
                      '"severity":"info|warn|good"}],'
                      '"suggest_drafts":[{"biz":"purchase_in","party":"供应商",'
                      '"items":[{"name":"..","qty":建议量,"unit":".."}]}]}'),
        "run": _submit,
    }
    return tools, verdict_box


# --------------------------------------------------------------------------
# 结论解析 + 逐点校验（宁缺勿错）
# --------------------------------------------------------------------------
def _parse_verdict(answer):
    """模型 final → {points, suggest_drafts}。final 可能是 JSON 字符串或 dict 的
    repr（run_agent 统一 str()），依次尝试 json.loads / ast.literal_eval，
    再退化到抠最后一个平衡大括号（与 billcheck._parse_verdict 同套路）。"""
    if not answer:
        return None
    raw = str(answer).strip()
    for parser in (json.loads, ast.literal_eval):
        try:
            v = parser(raw)
            if isinstance(v, dict) and ("points" in v or "suggest_drafts" in v):
                return {"points": v.get("points") or [],
                        "suggest_drafts": v.get("suggest_drafts") or []}
        except Exception:
            continue
    idx = raw.rfind("}")
    while idx > 0:
        try:
            v = json.loads(raw[:idx + 1])
            if isinstance(v, dict) and "points" in v:
                return {"points": v.get("points") or [],
                        "suggest_drafts": v.get("suggest_drafts") or []}
        except Exception:
            pass
        idx = raw.rfind("}", 0, idx)
    return None


def _valid_points(raw, observations):
    """逐点校验：severity 归一白名单 + title/detail 数字接地（复用 agent 的
    numeric_grounded 对观察 JSON 校验）。不合格的点直接丢弃——全掉光则上层走
    规则模板兜底。severity 本身无数字，不参与接地。"""
    numeric_grounded = agentmod.numeric_grounded
    out = []
    for p in (raw or []):
        if not isinstance(p, dict):
            continue
        title = str(p.get("title") or "").strip()[:30]
        detail = str(p.get("detail") or "").strip()[:300]
        if not title or not detail:
            continue
        sev = str(p.get("severity") or "info").strip().lower()
        if sev not in _SEVERITIES:
            sev = "info"
        if not numeric_grounded(title + detail, observations or []):
            continue
        out.append({"title": title, "detail": detail, "severity": sev})
    return out[:6]


def _valid_drafts(raw, observations):
    """补货建议合法化：biz 限 purchase_in；party 必须在观察 JSON 里出现过
    （防编造往来单位）；items 只留 name/qty/unit——price 即使模型给了也剥掉
    （价格由确认卡与校验 agent 补，日报建议不该带价）；最多 2 条。"""
    obs_text = json.dumps(observations or [], ensure_ascii=False)
    out = []
    for d in (raw or []):
        if not isinstance(d, dict) or len(out) >= 2:
            continue
        if str(d.get("biz") or "").strip() != "purchase_in":
            continue
        party = str(d.get("party") or "").strip()[:60]
        if not party or party not in obs_text:
            continue
        items = []
        for it in (d.get("items") or []):
            if not isinstance(it, dict):
                continue
            name = str(it.get("name") or "").strip()[:60]
            try:
                qty = round(float(it.get("qty")), 2)
            except (TypeError, ValueError):
                continue
            if not name or qty <= 0:
                continue
            items.append({"name": name, "qty": qty,
                          "unit": str(it.get("unit") or "").strip()[:10]})
        if items:
            out.append({"biz": "purchase_in", "party": party, "items": items})
    return out[:2]


def render_md(points, date=""):
    """points → 人话 markdown 明细（主控/前端 report_md 直接用）。"""
    lines = ["# 老板早报" + (" · %s" % date if date else ""), ""]
    ico = {"warn": "⚠", "good": "✓", "info": "•"}
    for p in (points or []):
        lines.append("- %s **%s**：%s" % (ico.get(p.get("severity"), "•"),
                                          p.get("title") or "", p.get("detail") or ""))
    if len(lines) == 2:
        lines.append("-（今天没有特别要说的）")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# 规则模板兜底（LLM 失败 / 结论全被接地校验拒收时）
# --------------------------------------------------------------------------
def _degraded(erp):
    """零编造兜底：直接复用问一问各统计工具的模板句——它们由 official_stats
    （today/month）、summary（debt_recv/debt_pay）、stock_overview（warn_list）
    确定性拼出，只用函数返回的数字，接地有结构保证（模板句取最紧张前 5，
    覆盖「warn_list 前 3」口径）。"""
    pts = []
    for fn, title, sev in ((askmod._today_stats, "今日经营", "info"),
                           (askmod._month_stats, "本月经营", "info"),
                           (askmod._debts, "应收应付", "info")):
        try:
            data, note = fn({}, erp)
        except Exception:
            continue
        if note:
            pts.append({"title": title, "detail": note, "severity": sev})
    try:
        data, note = askmod._low_stock({}, erp)
        if note and ((data.get("negative") or 0) > 0 or (data.get("low") or 0) > 0):
            pts.append({"title": "库存预警", "detail": note, "severity": "warn"})
    except Exception:
        pass
    return pts


# --------------------------------------------------------------------------
# 编排入口
# --------------------------------------------------------------------------
def morning_report(erp, operator="", on_event=None, force=False):
    """当日懒触发 + 缓存；force=True 跳过缓存重跑（/api/daily/regenerate 用）。
    返回 {ok, cached, date, report_md, points, suggest_drafts, steps, tokens,
    grounded, prompt_version[, degraded]}；
    未配置模型 → {ok: False, message}；LLM 失败 → 规则模板兜底（ok=True, degraded=True）。"""
    today = time.strftime("%Y-%m-%d")
    erp = erp or get_client()
    if not force:
        with _lock:
            hit = _cache.get("data")
            if _cache.get("date") == today and isinstance(hit, dict):
                return dict(hit, cached=True)
    if not cfgmod.agent_profile():
        return {"ok": False, "message": "尚未配置 AI 模型，请先在「设置」里添加。"}

    goal = "为 %s 生成一份给老板看的经营早报" % today
    try:
        tools, verdict_box = _tools(erp)
        out = agentmod.run_agent(
            goal, tools, max_steps=MAX_STEPS,
            context_block="操作员：%s" % (operator or "（未登录）"),
            on_event=on_event, prompt_template=DAILY_PROMPT)
    except Exception as e:
        out = {"ok": False, "error": str(e)[:120], "steps": [], "tokens": {},
               "observations": []}
        verdict_box = {}
    observations = out.get("observations") or []
    # 结论优先取 submit_verdict 的 args 通道（键存在即已收口，points 空数组也是
    # 合法结论）；模型忘了收口时退回解析 final 文本
    verdict = dict(verdict_box) if "points" in verdict_box \
        else _parse_verdict(out.get("answer"))
    points, drafts = [], []
    if verdict is not None:
        points = _valid_points(verdict.get("points"), observations)
        drafts = _valid_drafts(verdict.get("suggest_drafts"), observations)

    if points:
        result = {
            "ok": True, "cached": False, "date": today,
            "report_md": render_md(points, today),
            "points": points, "suggest_drafts": drafts,
            "steps": out.get("steps") or [], "tokens": out.get("tokens") or {},
            "grounded": out.get("grounded"), "degraded": False,
            "prompt_version": DAILY_PROMPT_VERSION,
        }
    else:
        # LLM 失败 / 结论不可用 → 规则模板兜底（数字全部来自官方统计函数）
        pts = _degraded(erp)
        if not pts:
            return {"ok": False,
                    "message": "日报生成失败且基础统计不可用（%s）"
                               % (out.get("error") or "模型无结论")}
        result = {
            "ok": True, "cached": False, "date": today,
            "report_md": render_md(pts, today),
            "points": pts, "suggest_drafts": [],
            "steps": out.get("steps") or [], "tokens": out.get("tokens") or {},
            "grounded": True, "degraded": True,
            "prompt_version": DAILY_PROMPT_VERSION,
        }
    with _lock:
        _cache["date"] = today
        _cache["data"] = result
    return dict(result, cached=False)


def invalidate():
    """清当日缓存（server 在 commit 单据成功后调用，下次拉取即重新取证）。"""
    with _lock:
        _cache["date"] = None
        _cache["data"] = None
