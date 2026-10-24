#!/usr/bin/env python3
"""手写 agent 运行时：协议受限网关上的 function-calling 循环。

为什么不用 OpenAI function-calling：自建网关/推理端点协议支持参差（丢 system
角色、不认 tools 参数），这里在应用层用「严格 JSON 动作 + observation 回喂」实现
同等循环——每一步可审计（trace）、可预算（步数 + token 双约束）、可回放。

循环：
    goal → [ LLM 决策 {thought, tool, args} → 执行工具 → observation 回喂 ] × N
         → LLM {final} → 数字接地校验 → 回答

不变量：
- final 里出现的每个数字都必须来自某步 observation（grounded），否则退回
  各工具模板句拼接——回答永远可追溯到 ERP 真实数据，与 /ask 的既有承诺一致。
- 预算（max_steps / max_tokens）耗尽时给模型一次「只准作答」的收尾机会，
  不再执行任何工具；收尾也失败则用模板句兜底，绝不空手而归。
"""
import json
import re
import time

from . import config as cfgmod
from . import llm as llmmod

OBS_MAX_CHARS = 2600   # 单步观察喂回 prompt 的上限（token 预算；trace 里存全量）
DEFAULT_MAX_STEPS = 4
# token 预算只是防失控保险丝，步数预算才是主约束——必须放宽到容得下
# 网关侧固定注入（实测 custom 模式每请求 ~8.5K 人格前缀，6000 会让第一步就熔断）
DEFAULT_MAX_TOKENS = 40000

LOOP_PROMPT = """你是进销存系统的经营数据 agent。通过逐步调用工具收集数据，回答用户问题。

# 工具目录（每轮只能调用一个）
{tool_catalog}

# 已完成的步骤（观察是工具返回的真实数据）
{history}

# 输出格式（严格 JSON，每轮恰好一个动作）
继续调用工具：{{"thought":"简短中文说明为什么调这个工具","tool":"工具名","args":{{...}}}}
信息已足够：{{"thought":"...","final":"给用户的中文最终回答"}}

# 规则
1. 最多 {max_steps} 轮工具调用，先想清楚一共需要哪几个数据，再按顺序调用。
1a. 用户问题常包含多个子问题：每个子问题都必须有对应的工具观察支撑，全部查完
    才允许 final；只查了一部分就作答、或声称"没有记录"而实际没查，都是不允许的。
2. final 里的每个数字、每个名称都必须来自上面某步的观察 JSON，禁止编造、禁止心算新数字。
3. final 先直接回答问题，再给关键数字（带单位），不超过四句话。
4. 观察里没有的信息可以再调工具；确实查不到就明说「系统里没有记录」。
5. 工具名和 args 的键必须与工具目录完全一致。

# 用户问题
{goal}"""


def _numbers_in(text):
    """抽出文本里所有数字（含千分位/小数），按数值归一化——100.00 与 100.0 视为同一个数。"""
    out = set()
    for raw in re.findall(r"-?\d[\d,]*(?:\.\d+)?", text or ""):
        try:
            out.add(round(float(raw.replace(",", "")), 2))
        except ValueError:
            pass
    return out


def numeric_grounded(ans, observations):
    """答案里的每个数字都必须能在某步观察 JSON 里找到（防 LLM 编数）。"""
    if not ans:
        return False
    allow = set()
    for obs in observations or []:
        if obs is None:
            continue
        allow |= _numbers_in(json.dumps(obs, ensure_ascii=False))
    return _numbers_in(ans) <= allow


def _hist(i, thought, name, args, obs):
    o = json.dumps(obs, ensure_ascii=False)
    if len(o) > OBS_MAX_CHARS:
        o = o[:OBS_MAX_CHARS] + "…（截断）"
    a = json.dumps(args if args else {}, ensure_ascii=False)
    return "[第%d步] 思考：%s；调用 %s(%s)；观察：%s" % (i, thought, name, a, o)


def run_agent(goal, tools, max_steps=DEFAULT_MAX_STEPS, max_tokens=DEFAULT_MAX_TOKENS,
              context_block="", on_event=None):
    """执行一个 agent 会话。返回 {ok, answer, steps, tokens, meta}。

    tools: {name: {"desc": str, "args_hint": str, "run": callable(args) -> (data, note)}}
           run 必须只读；data 是喂回模型的结构化观察，note 是确定性模板句（接地兜底）。
    on_event: 可选回调 on_event(evt)，SSE 流式前端用——
        {"type":"action","tool":..,"args":..} 决策出要调的工具、执行前推（前端显示"正在查询…"）
        {"type":"step", ...完整 step 记录}  该步执行完、trace 落定后推。
    """
    prof = cfgmod.active_profile()
    if not prof:
        return {"ok": False, "message": "尚未配置 AI 模型，请先在「设置」里添加。",
                "steps": [], "tokens": {}}

    catalog = "\n".join(
        "- %s：%s args: %s" % (name, t.get("desc", ""), t.get("args_hint", "{}"))
        for name, t in tools.items())
    steps = []
    tokens = {"prompt": 0, "completion": 0, "estimated": False}
    history_lines = []
    notes = []            # 每个成功工具的模板句（接地兜底拼接用）
    observations = []     # 所有成功观察（数字接地的合法数字池）
    started = time.time()
    budget_out = False

    def decide(extra=""):
        u = {}
        prompt = LOOP_PROMPT.format(
            tool_catalog=catalog, history="\n".join(history_lines) or "（还没有）",
            max_steps=max_steps, goal=goal) + extra
        raw = llmmod._chat(prof, [
            {"role": "system", "content": prompt},
            {"role": "user", "content": (context_block + "\n请给出下一轮动作。").strip()},
        ], purpose="agent", collect=u)
        tokens["prompt"] += u.get("prompt_tokens", 0)
        tokens["completion"] += u.get("completion_tokens", 0)
        tokens["estimated"] = tokens["estimated"] or bool(u.get("estimated"))
        return llmmod._extract_json(raw)

    for i in range(1, max_steps + 1):
        if tokens["prompt"] + tokens["completion"] >= max_tokens:
            budget_out = True   # token 预算耗尽，进入强制收尾
            break
        try:
            act = decide()
        except Exception as e:
            return {"ok": False, "message": "AI 决策失败：%s" % str(e)[:120],
                    "steps": steps, "tokens": tokens,
                    "meta": {"model": prof.get("model", "")}}

        thought = str(act.get("thought") or "")[:200]
        final = act.get("final")
        if final:
            steps.append({"step": i, "thought": thought, "tool": None,
                          "final": str(final)})
            return _finish(goal, str(final), steps, tokens, started,
                           observations, notes, prof)

        name = str(act.get("tool") or "").strip()
        tool = tools.get(name)
        if not tool:
            obs = {"error": "没有这个工具：%s。可用：%s" % (name, "、".join(tools))}
            rec = {"step": i, "thought": thought, "tool": name,
                   "args": act.get("args"), "ok": False, "observation": obs}
            steps.append(rec)
            _emit(on_event, {"type": "step", "step": rec})
            history_lines.append(_hist(i, thought, name, act.get("args"), obs))
            continue   # 错误作为观察喂回，让下一轮自我纠正

        args = act.get("args") if isinstance(act.get("args"), dict) else {}
        _emit(on_event, {"type": "action", "tool": name, "args": args})
        t0 = time.time()
        try:
            data, note = tool["run"](args)
            ok = True
            observations.append(data)
            if note:
                notes.append(str(note))
        except Exception as e:
            data, note, ok = {"error": str(e)[:200]}, None, False
        lat = int((time.time() - t0) * 1000)
        rec = {"step": i, "thought": thought, "tool": name, "args": args,
               "ok": ok, "latency_ms": lat, "observation": data}
        steps.append(rec)
        _emit(on_event, {"type": "step", "step": rec})
        history_lines.append(_hist(i, thought, name, args, data))

    # ---- 预算耗尽（步数 / token）：只准作答的收尾机会，不再执行任何工具 ----
    try:
        tail = ("\n\n# 预算已用完（%d 步 / token %d）。不许再调用工具，"
                "只能输出：{{\"thought\":\"...\",\"final\":\"基于以上观察的回答\"}}"
                % (max_steps, tokens["prompt"] + tokens["completion"]))
        act = decide(tail)
        final = act.get("final")
        if final:
            steps.append({"step": len(steps) + 1, "thought": str(act.get("thought") or "")[:200],
                          "tool": None, "forced_final": True, "final": str(final)})
            return _finish(goal, str(final), steps, tokens, started,
                           observations, notes, prof)
    except Exception:
        pass

    # ---- 收尾也失败：模板句拼接兜底，绝不空手而归（同句去重保序）----
    answer = "；".join(dict.fromkeys(notes)) if notes else \
        "查询了 %d 步但没有得到可用的数据，请稍后再试。" % len(steps)
    steps.append({"step": len(steps) + 1, "tool": None, "fallback": True, "final": answer})
    return _finish(goal, answer, steps, tokens, started, observations, notes, prof,
                   forced=True)


def _emit(on_event, evt):
    """推事件给调用方（SSE 流式）；回调异常绝不影响 agent 主流程。"""
    if on_event is None:
        return
    try:
        on_event(evt)
    except Exception:
        pass


def _finish(goal, final, steps, tokens, started, observations, notes, prof, forced=False):
    """数字接地校验 + 汇总。final 不可信时退回模板句拼接（保留原 final 供审计）。"""
    grounded = numeric_grounded(final, observations)
    answer = final
    if not grounded and notes:
        answer = "；".join(dict.fromkeys(notes))   # 模板句去重保序；本身只用观察里的数字，天然接地
    return {
        "ok": True,
        "answer": answer,
        "final_raw": final,
        "grounded": grounded,
        "steps": steps,
        "tokens": tokens,
        "observations": observations,
        "meta": {
            "model": prof.get("model", ""),
            "steps_used": len([s for s in steps if s.get("tool")]),
            "latency_ms": int((time.time() - started) * 1000),
            "forced": forced,
        },
    }
