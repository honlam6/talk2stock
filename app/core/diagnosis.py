#!/usr/bin/env python3
"""AI 经营诊断：多步调查型 agent —— 从「规则算事实、LLM 组织语言」升级为
「LLM 自己决定查什么、查到哪一层」。

与日报（core/daily.py）的区别不是提示词长短，而是**调查链可审计**：
- 每个问题必须标注 evidence_steps（支撑它的工具步骤序号），至少一步；
- 问题的证据句里的每个数字必须能在那些步骤的观察里找到（严格接地，
  比「全观察池」更窄——只认模型自己声明的调查链）；
- 声明无效（索引不存在/指向失败步骤）时退化为全观察池校验并标 lax；
- 全部问题被丢 → 降级到 insight.insights 的规则模板（degraded=True）。

边界：全程只读；诊断不写库、不产生草稿；写入需求请走录单入口。
"""
import ast
import json
import time

from . import agent as agentmod
from . import ask as askmod
from .erp import get_client

DIAG_PROMPT_VERSION = "diag-v1-20261123"
MAX_STEPS = 8   # 概览四件（今日/本月/库存/欠款）+ 下钻两步 + 收口，实测 6~7 步落定

DIAG_PROMPT = """你是进销存系统的经营诊断师。目标不是「把看板数据复述一遍」，而是通过工具取证，
找出最值得老板关注的经营问题，并追到可解释的来源，最后给出可执行的改进建议。

# 工具目录（每轮只能调用一个）
{tool_catalog}

# 已完成的步骤（观察是工具返回的真实数据）
{history}

# 输出格式（严格 JSON，每轮恰好一个动作）
继续取证：{{"thought":"简短说明为什么查这个","tool":"工具名","args":{{...}}}}
诊断完成：{{"thought":"一句话总结","tool":"submit_diagnosis","args":{{"overview":"...","issues":[...]}}}}

issue 元素：{{"title":"12字以内","evidence":"引用观察里的数字，一到两句","evidence_steps":[1,3],
"cause":"可能原因（一到两句，不要编造数字）","action":"可执行的改进建议（一到两句）",
"severity":"warn|info|good"}}

# 规则
1. 最多 {max_steps} 轮。先取概览（today_stats / month_stats / debts / low_stock 一般各一次），
   再从概览里挑 1~3 个最值得追的问题下钻：销售额异常就查是哪个商品/哪天掉的
   （query_bills 或 sql_query 按商品/日期聚合），欠款集中就查是哪家客户、欠了哪些单。
   **只写概览没有下钻的诊断视为不合格**——每个 issue 的 evidence_steps 至少包含一步
   专门为它做的调查，不能全部指向同一轮概览。
2. evidence 里的每个数字必须原样来自 evidence_steps 对应的观察，禁止编造、禁止心算新数字。
   cause 与 action 是分析语言，可以不含数字；含数字时必须同样能在观察里找到。
3. severity 只能取 warn / info / good；没有异常就给经营现状类判断（采销规模、库存结构、
   欠款结构），不要硬编问题。
4. overview 一到两句，必须引用观察里的关键数字（同样受接地约束）。
5. issues 最多 3 条，按重要性排序；证据不足的问题宁可不写。
6. submit_diagnosis 必须调用恰好一次，调用后不要再输出其他动作。
7. 全程只读。不要给具体采购数量、价格或金额建议（那是采购建议与录单的职责），
   建议只描述动作方向（如「对 A 客户启动回款跟进」「对 X 商品复盘定价」）。

# 任务
{goal}"""

_SEVERITIES = ("warn", "info", "good")


class _Unavailable(Exception):
    """配置缺失等不可恢复场景（由 server 层转成提示语）。"""


# --------------------------------------------------------------------------
# 工具箱：只读取证 8 件 + submit_diagnosis 收口
# --------------------------------------------------------------------------
def _tools(erp):
    """与日报同源（desc/args_hint 直接取自 ask._tools，不自己编文案防漂移）。"""
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
            "overview": (args or {}).get("overview") or "",
            "issues": (args or {}).get("issues") or [],
        })
        return ({"received": True, "issues": len(verdict_box["issues"])},
                "诊断结论已收到。")

    tools["submit_diagnosis"] = {
        "desc": "提交诊断结论（结束取证，恰好调用一次）",
        "args_hint": ('{"overview":"一到两句（引用观察数字）","issues":[{"title":"..",'
                      '"evidence":"..（引用观察数字）","evidence_steps":[步骤号],'
                      '"cause":"..","action":"..","severity":"warn|info|good"}]}'),
        "run": _submit,
    }
    return tools, verdict_box


# --------------------------------------------------------------------------
# 结论解析与逐条校验
# --------------------------------------------------------------------------
def _parse_verdict(answer):
    """final → {overview, issues}。兼容 JSON 字符串与 dict 的 repr（run_agent 统一 str()）。"""
    if not answer:
        return None
    raw = str(answer).strip()
    for parser in (json.loads, ast.literal_eval):
        try:
            v = parser(raw)
            if isinstance(v, dict) and ("issues" in v or "overview" in v):
                return {"overview": str(v.get("overview") or ""),
                        "issues": v.get("issues") or []}
        except Exception:
            continue
    idx = raw.rfind("}")
    while idx > 0:
        try:
            v = json.loads(raw[:idx + 1])
            if isinstance(v, dict) and "issues" in v:
                return {"overview": str(v.get("overview") or ""),
                        "issues": v.get("issues") or []}
        except Exception:
            pass
        idx = raw.rfind("}", 0, idx)
    return None


def _step_pool(steps):
    """{步骤号: 观察}——只收成功且有工具调用的步骤。"""
    pool = {}
    for s in steps or []:
        if s.get("tool") and s.get("ok") and s.get("observation") is not None:
            try:
                pool[int(s.get("step"))] = s["observation"]
            except (TypeError, ValueError):
                continue
    return pool


def _valid_issues(raw, steps, observations):
    """逐条校验：字段齐全 + evidence_steps 指向真实调查步骤 + 证据数字接地。

    返回 (issues, lax_flags)。lax=True 表示 evidence_steps 无效、退回了全观察池。
    """
    pool = _step_pool(steps)
    grounded = agentmod.numeric_grounded
    out, lax = [], []
    for p in (raw or []):
        if not isinstance(p, dict):
            continue
        title = str(p.get("title") or "").strip()[:30]
        evidence = str(p.get("evidence") or "").strip()[:300]
        cause = str(p.get("cause") or "").strip()[:300]
        action = str(p.get("action") or "").strip()[:300]
        if not title or not evidence:
            continue
        sev = str(p.get("severity") or "info").strip().lower()
        if sev not in _SEVERITIES:
            sev = "info"
        cited = []
        for x in (p.get("evidence_steps") or [])[:6]:
            try:
                n = int(x)
            except (TypeError, ValueError):
                continue
            if n in pool:
                cited.append(n)
        if cited:
            pool_obs = [pool[n] for n in cited]
            is_lax = False
        else:
            pool_obs = observations or []
            is_lax = True
        if not grounded(evidence, pool_obs):
            continue
        # 分析语言允许无数字；一旦出现观察外的数字同样丢弃（防「日均亏损 3000」式编造）
        if not grounded(title + cause + action, pool_obs):
            continue
        out.append({"title": title, "evidence": evidence, "evidence_steps": cited,
                    "cause": cause, "action": action, "severity": sev})
        if is_lax:
            lax.append(title)
        if len(out) >= 3:
            break
    return out, lax


def render_md(diag, date=""):
    lines = ["# 经营诊断" + (" · %s" % date if date else ""), ""]
    if diag.get("overview"):
        lines.append(diag["overview"])
        lines.append("")
    ico = {"warn": "⚠", "good": "✓", "info": "•"}
    for i, p in enumerate(diag.get("issues") or [], 1):
        lines.append("## %d. %s %s" % (i, ico.get(p.get("severity"), "•"), p.get("title")))
        lines.append("- 数据证据：%s" % p.get("evidence"))
        if p.get("cause"):
            lines.append("- 可能原因：%s" % p.get("cause"))
        if p.get("action"):
            lines.append("- 改进建议：%s" % p.get("action"))
        if p.get("evidence_steps"):
            lines.append("- 调查步骤：第 %s 步" % "、".join(str(x) for x in p["evidence_steps"]))
    if not diag.get("issues"):
        lines.append("（本次诊断没有给出可落地的结论）")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# 降级：规则洞察（不依赖 LLM，数字全部来自确定性统计）
# --------------------------------------------------------------------------
def _degraded(erp):
    from . import insight as insmod
    try:
        ins = insmod.insights(erp)
    except Exception:
        ins = {"ok": False}
    issues = []
    for c in (ins.get("insights") or [])[:3]:
        issues.append({"title": str(c.get("title") or "经营提示")[:30],
                       "evidence": str(c.get("detail") or "")[:300],
                       "evidence_steps": [], "cause": "", "action": "",
                       "severity": "warn" if c.get("level") == "warn" else "info"})
    return {
        "overview": "AI 调查未完成，以下为规则统计的诊断摘要（数字均来自 ERP 官方统计）。",
        "issues": issues,
    }


# --------------------------------------------------------------------------
# 编排入口
# --------------------------------------------------------------------------
def diagnose(erp, operator="", on_event=None):
    """执行一次经营诊断。返回
    {ok, date, overview, issues, steps, tokens, grounded, degraded, lax, source,
     report_md, prompt_version}。
    """
    from . import config as cfgmod
    today = time.strftime("%Y-%m-%d")
    erp = erp or get_client()
    if not cfgmod.agent_profile():
        return {"ok": False, "message": "尚未配置 AI 模型，请先在「设置」里添加。"}

    goal = "为 %s 的经营情况做一次诊断：找出最需要关注的问题，追查来源并给改进建议" % today
    verdict_box = {}
    try:
        tools, verdict_box = _tools(erp)
        out = agentmod.run_agent(
            goal, tools, max_steps=MAX_STEPS,
            context_block="操作员：%s" % (operator or "（未登录）"),
            on_event=on_event, prompt_template=DIAG_PROMPT)
    except Exception as e:
        out = {"ok": False, "error": str(e)[:120], "steps": [], "tokens": {},
               "observations": []}

    observations = out.get("observations") or []
    steps = out.get("steps") or []
    verdict = dict(verdict_box) if "issues" in verdict_box else _parse_verdict(out.get("answer"))

    issues, lax = [], []
    if verdict is not None:
        issues, lax = _valid_issues(verdict.get("issues"), steps, observations)
    overview_ok = False
    overview = ""
    if verdict is not None and issues:
        overview = str(verdict.get("overview") or "").strip()[:400]
        overview_ok = bool(overview) and agentmod.numeric_grounded(overview, observations)
        if not overview:
            overview = "共发现 %d 个需要关注的问题。" % len(issues)

    if issues:
        result = {
            "ok": True, "date": today, "overview": overview,
            "issues": issues, "lax": lax,
            "steps": steps, "tokens": out.get("tokens") or {},
            "meta": out.get("meta") or {},
            "grounded": out.get("grounded"), "degraded": False,
            "source": "llm", "prompt_version": DIAG_PROMPT_VERSION,
        }
    else:
        d = _degraded(erp)
        if not d["issues"]:
            return {"ok": False,
                    "message": "诊断未完成且基础统计不可用（%s）"
                               % (out.get("error") or "模型无有效结论")}
        result = {
            "ok": True, "date": today, "overview": d["overview"],
            "issues": d["issues"], "lax": [],
            "steps": steps, "tokens": out.get("tokens") or {},
            "meta": out.get("meta") or {},
            "grounded": True, "degraded": True,
            "source": "rule_fallback", "prompt_version": DIAG_PROMPT_VERSION,
        }
    result["report_md"] = render_md(result, today)
    return result
