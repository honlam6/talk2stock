#!/usr/bin/env python3
"""录单校验 agent 评测跑分器——对 eval/billcheck_cases.jsonl 逐条走
core.billcheck.parse_and_check()（与线上 /api/parse 完全同路径，含校验 loop），
按「补丁采纳是否正确、宁缺勿错是否守住」判定，输出采纳率/延迟/token。

前置（必须当天执行）：
    cd jshERP-latest/deploy && docker compose up -d --no-build
    python3 eval/seed_demo.py --yes
    cd app && python3 ../eval/run_billcheck.py [--limit N] [--tag v1]

判定设计（与 run_ask 同一第一性原理：LLM 层准确率 + 不变量守护）：
    - applied_min：至少 N 个补丁被采纳（校验 agent 真在干活）
    - applied：期望字段→数值映射（历史价回填正确性，容差 0.01）
    - no_applied_fields：禁止出现该字段补丁——无历史价时宁缺勿错
    - steps_min：校验 loop 至少 N 轮取证（不是走过场）
跑分器会为操作员 evalbot 预播种 habits.jsonl（习惯补全路径可测），只追加不改历史。
模型口径：hy4。
"""
import argparse
import json
import os
import statistics
import sys
import time

APP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app")
sys.path.insert(0, os.path.abspath(APP_DIR))

from core import billcheck as bcmod   # noqa: E402
from core import config as cfgmod     # noqa: E402
from core import habits as hb         # noqa: E402
from core.erp import get_client       # noqa: E402

CASES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "billcheck_cases.jsonl")
RESULTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results")
EVAL_OPERATOR = "evalbot"


def seed_habits():
    """为评测操作员预播种习惯（幂等：重复跑只是追加同值记录，argmax 不变）。"""
    for _ in range(3):
        hb.record(EVAL_OPERATOR,
                  {"biz": "sale_out", "party": "永辉超市", "depot": "成品仓",
                   "items": [{"name": "可乐", "unit": "瓶", "price": 4.0}]}, {"number": "eval"})
    for _ in range(2):
        hb.record(EVAL_OPERATOR,
                  {"biz": "sale_out", "party": "张记便利店", "depot": "成品仓",
                   "items": [{"name": "方便面", "unit": "箱", "price": 44}]}, {"number": "eval"})


def _applied_by_field(applied):
    out = {}
    for p in applied or []:
        try:
            out.setdefault(p.get("field"), []).append(float(p.get("value")))
        except (TypeError, ValueError):
            continue
    return out


def score_case(case, out):
    e = case.get("expect") or {}
    check = out.get("check") or {}
    applied = check.get("patches_applied") or []
    steps = [s for s in (check.get("steps") or []) if s.get("tool")]
    by_field = _applied_by_field(applied)

    checks = {"ok": True}
    if "applied_min" in e:
        checks["applied_min"] = len(applied) >= e["applied_min"]
    for field, want in (e.get("applied") or {}).items():
        vals = by_field.get(field) or []
        checks["applied:" + field] = any(abs(v - want) <= 0.01 for v in vals)
    for field in (e.get("no_applied_fields") or []):
        checks["no_applied:" + field] = len(by_field.get(field) or []) == 0
    if e.get("steps_min"):
        checks["steps_min"] = len(steps) >= e["steps_min"]
    return all(checks.values()), checks


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--tag", default="")
    args = ap.parse_args()

    cases = []
    for l in open(CASES_PATH, encoding="utf-8"):
        if not l.strip():
            continue
        obj = json.loads(l)
        if isinstance(obj, dict) and obj and all(str(k).startswith("_") for k in obj):
            continue
        cases.append(obj)
    if args.limit:
        cases = cases[:args.limit]

    prof = cfgmod.active_profile()
    if not prof:
        print("未配置模型档案")
        return 2
    seed_habits()
    erp = get_client()

    results, t0 = [], time.time()
    for i, c in enumerate(cases, 1):
        t = time.time()
        err = ""
        try:
            out = bcmod.parse_and_check(c["text"], erp, operator=EVAL_OPERATOR)
        except Exception as e:
            out, err = {"check": {}}, str(e)[:120]
        lat = time.time() - t
        check = out.get("check") or {}
        ok, checks = score_case(c, out)
        toks = check.get("tokens") or {}
        results.append({"id": c["id"], "ok": ok, "checks": checks,
                        "latency_s": round(lat, 2),
                        "tokens": toks.get("prompt", 0) + toks.get("completion", 0),
                        "applied": check.get("patches_applied"),
                        "rejected": check.get("patches_rejected"),
                        "clarify": (out.get("clarify") or {}).get("question") or out.get("clarify_all"),
                        "error": err})
        mark = "✓" if ok else "✗"
        bad = [k for k, v in (checks or {}).items() if not v]
        print(f"[{i}/{len(cases)}] {c['id']} {mark} {lat:5.1f}s"
              + (f"  错:{','.join(bad)}" if bad else "") + (f"  ERR:{err[:40]}" if err else ""))

    n = len(results)
    passed = [r for r in results if r["ok"]]
    summary = {"corpus_version": "billcheck-v1-20261116", "model": "hy4", "n": n,
               "acc": round(len(passed) / n, 4) if n else 0,
               "latency_avg_s": round(statistics.mean([r["latency_s"] for r in results]), 2) if results else 0,
               "tokens_avg": round(statistics.mean([r["tokens"] for r in results])) if results else 0}
    stamp = time.strftime("%Y%m%d-%H%M%S")
    tag = ("-" + args.tag) if args.tag else ""
    os.makedirs(RESULTS_DIR, exist_ok=True)
    json.dump({"summary": summary, "results": results},
              open(os.path.join(RESULTS_DIR, f"billcheck_{stamp}{tag}.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    with open(os.path.join(RESULTS_DIR, f"billcheck_{stamp}{tag}.md"), "w", encoding="utf-8") as f:
        f.write(f"# 录单校验 agent 评测 {stamp}{tag}\n\n"
                f"- 语料：billcheck-v1-20261116（{n} 条）· 模型：hy4 · 耗时 {round(time.time()-t0)}s\n"
                f"- **通过率：{summary['acc']*100:.1f}%** · 延迟均值 {summary['latency_avg_s']}s · token 均值 {summary['tokens_avg']}\n\n"
                f"## 未通过\n\n")
        for r in results:
            if not r["ok"]:
                bad = [k for k, v in r["checks"].items() if not v]
                f.write(f"- {r['id']}：{','.join(bad)}｜采纳：{json.dumps(r['applied'], ensure_ascii=False)[:120]}\n")
    print("=" * 60)
    print(f" 通过 {len(passed)}/{n} = {summary['acc']*100:.1f}%  延迟均值 {summary['latency_avg_s']}s")
    print(f" 明细 → eval/results/billcheck_{stamp}{tag}.md")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
