#!/usr/bin/env python3
"""问一问评测打分器（纯标准库）——对 eval/ask_cases.jsonl 冻结语料逐条调用
core.ask.ask()（与线上 /api/ask 完全同路径），按期望判定，输出准确率/延迟/token。

前置（必须当天执行）：
    cd jshERP-latest/deploy && docker compose up -d --no-build   # 起 ERP
    python3 eval/seed_demo.py --yes                              # 播种受控数据集
    cd app && python3 ../eval/run_ask.py [--limit N] [--type rule|agent|compound|boundary]

评分设计（第一性原理：小数据只验证 LLM 层，规模缺陷由 eval/stress_scale.py 单测）：
    - numbers：答案必须含全部 gold 数字（归一化去千分位，容差 0.01）
    - contains / not_contains / contains_any：答案子串
    - route / no_tools / min_tools / tools_any：行为判定
    - grounded=True 硬要求（agent 路由），写库完整性：全部用例跑完校验可乐库存仍 52
模型口径：hy4（用户指定对外命名）。
"""
import argparse
import json
import os
import re
import statistics
import sys
import time

APP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app")
sys.path.insert(0, os.path.abspath(APP_DIR))

from core import ask as askmod       # noqa: E402
from core import config as cfgmod    # noqa: E402
from core.erp import get_client      # noqa: E402

CASES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ask_cases.jsonl")
RESULTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results")
CORPUS_VERSION = "ask-v1-20261109"


def _numbers(text):
    out = set()
    for raw in re.findall(r"-?\d[\d,]*(?:\.\d+)?", text or ""):
        try:
            out.add(round(float(raw.replace(",", "")), 2))
        except ValueError:
            pass
    return out


def score_case(case, r):
    """返回 (all_ok, 检查明细 dict)。数字判定：gold ⊆ 答案数字集合（容差匹配）。"""
    e = case.get("expect") or {}
    ans = str(r.get("answer") or "")
    steps = r.get("steps") or []
    used_tools = [s.get("tool") for s in steps if s.get("tool") and s.get("ok")]

    checks = {"ok": bool(r.get("ok"))}
    if e.get("route"):
        checks["route"] = r.get("route") == e["route"]
    if e.get("numbers"):
        got = _numbers(ans)
        checks["numbers"] = all(any(abs(g - x) <= 0.01 for x in got) for g in e["numbers"])
    if e.get("contains"):
        v = e["contains"]
        checks["contains"] = all(p in ans for p in (v if isinstance(v, list) else [v]))
    if e.get("not_contains"):
        v = e["not_contains"]
        checks["not_contains"] = all(p not in ans for p in (v if isinstance(v, list) else [v]))
    if e.get("contains_any"):
        checks["contains_any"] = any(p in ans for p in e["contains_any"])
    if e.get("no_tools"):
        checks["no_tools"] = len(used_tools) == 0
    if e.get("min_tools"):
        checks["min_tools"] = len(used_tools) >= e["min_tools"]
    if e.get("tools_any"):
        checks["tools_any"] = any(t in (e["tools_any"] or []) for t in used_tools)
    if e.get("route") == "agent" and r.get("ok"):
        checks["grounded"] = r.get("grounded") is True
    return all(checks.values()), checks


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--type", choices=["rule", "agent", "compound", "boundary", "all"], default="all")
    ap.add_argument("--tag", default="")
    args = ap.parse_args()

    cases = [json.loads(l) for l in open(CASES_PATH, encoding="utf-8") if l.strip() and not l.startswith('{"_comment')]
    if args.type != "all":
        cases = [c for c in cases if c["type"] == args.type]
    if args.limit:
        cases = cases[:args.limit]

    prof = cfgmod.active_profile()
    if not prof:
        print("未配置模型档案"); return 2
    erp = get_client()

    results, t0 = [], time.time()
    for i, c in enumerate(cases, 1):
        t = time.time()
        try:
            r = askmod.ask(c["text"], erp)
            err = ""
        except Exception as e:
            r, err = {"ok": False, "answer": "", "steps": []}, str(e)[:120]
        lat = time.time() - t
        ok, checks = score_case(c, r), None
        ok, checks = ok[0], ok[1]
        toks = r.get("tokens") or {}
        results.append({"id": c["id"], "type": c["type"], "ok": ok, "checks": checks,
                        "latency_s": round(lat, 2), "tokens": toks.get("prompt", 0) + toks.get("completion", 0),
                        "route": r.get("route"), "grounded": r.get("grounded"),
                        "answer": str(r.get("answer") or "")[:150], "error": err})
        mark = "✓" if ok else "✗"
        bad = [k for k, v in (checks or {}).items() if not v]
        print(f"[{i:>2}/{len(cases)}] {c['id']} {mark} {r.get('route') or '-':6} {lat:5.1f}s"
              + (f"  错:{','.join(bad)}" if bad else "") + (f"  ERR:{err[:40]}" if err else ""))

    n = len(results)
    passed = [r for r in results if r["ok"]]
    lats = [r["latency_s"] for r in results]
    agent_lats = [r["latency_s"] for r in results if r["type"] in ("agent", "compound", "boundary")]
    toks_list = [r["tokens"] for r in results if r["type"] in ("agent", "compound", "boundary")]
    by_type = {}
    for t in ("rule", "agent", "compound", "boundary"):
        rows = [r for r in results if r["type"] == t]
        if rows:
            by_type[t] = round(sum(1 for r in rows if r["ok"]) / len(rows), 3)

    summary = {
        "corpus_version": CORPUS_VERSION, "model": "hy4", "n": n,
        "acc": round(len(passed) / n, 4) if n else 0,
        "by_type": by_type,
        "route_acc": round(sum(1 for r in results if r.get("checks", {}).get("route", True)) / n, 3) if n else 0,
        "latency_avg_s": round(statistics.mean(lats), 2) if lats else 0,
        "latency_p95_s": round(sorted(lats)[min(len(lats) - 1, int(round(0.95 * len(lats))))], 2) if lats else 0,
        "agent_latency_avg_s": round(statistics.mean(agent_lats), 2) if agent_lats else 0,
        "agent_tokens_avg": round(statistics.mean(toks_list)) if toks_list else 0,
        "rule_tokens": sum(r["tokens"] for r in results if r["type"] == "rule"),
    }
    stamp = time.strftime("%Y%m%d-%H%M%S")
    tag = ("-" + args.tag) if args.tag else ""
    os.makedirs(RESULTS_DIR, exist_ok=True)
    json.dump({"summary": summary, "results": results},
              open(os.path.join(RESULTS_DIR, f"ask_{stamp}{tag}.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    with open(os.path.join(RESULTS_DIR, f"ask_{stamp}{tag}.md"), "w", encoding="utf-8") as f:
        f.write(f"# 问一问评测 {stamp}{tag}\n\n"
                f"- 语料：{CORPUS_VERSION}（{n} 条）· 模型：hy4 · 耗时 {round(time.time()-t0)}s\n"
                f"- **总通过率：{summary['acc']*100:.1f}%**  分类：" +
                " ".join(f"{k}={v*100:.0f}%" for k, v in by_type.items()) + "\n"
                f"- 延迟：均值 {summary['latency_avg_s']}s · p95 {summary['latency_p95_s']}s · agent 均值 {summary['agent_latency_avg_s']}s\n"
                f"- token：agent 均值 {summary['agent_tokens_avg']} · 规则直答合计 {summary['rule_tokens']}（应为 0）\n\n"
                f"## 未通过\n\n")
        for r in results:
            if not r["ok"]:
                bad = [k for k, v in r["checks"].items() if not v]
                f.write(f"- {r['id']}：{','.join(bad)}｜答：{r['answer'][:90]}\n")

    print("=" * 60)
    print(f" 通过 {len(passed)}/{n} = {summary['acc']*100:.1f}%  分类 {by_type}")
    print(f" 延迟均值 {summary['latency_avg_s']}s / p95 {summary['latency_p95_s']}s  agent token 均值 {summary['agent_tokens_avg']}")
    print(f" 明细 → eval/results/ask_{stamp}{tag}.md")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
