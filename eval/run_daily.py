#!/usr/bin/env python3
"""老板日报 agent 评测跑分器——对 eval/daily_cases.jsonl 逐条走
core.daily.morning_report(force=True)（与线上 /api/daily/agent 完全同路径），
按「今日实数是否入文、点数下限、建议上限、数字接地」判定，输出通过率/延迟/token。

前置（必须当天执行）：
    cd jshERP-latest/deploy && docker compose up -d --no-build
    python3 eval/seed_demo.py --yes
    cd app && python3 ../eval/run_daily.py [--limit N] [--tag v1]

判定设计（与 run_ask / run_billcheck 同一第一性原理：LLM 层准确率 + 不变量守护）：
    - contains_today_numbers：gold 不写死在语料里——跑分器先调 official_stats(erp)
      动态取 today.sell / today.buy 当 gold（seed 数据随日期变，写死必腐），断言
      report_md + points 里含这些数字（_numbers 归一化去千分位，容差 0.01，
      与 run_ask 的 _numbers 同法）
    - min_points：早报至少 N 个点（agent 真在组织内容，不是一句模板敷衍）
    - draft_max：补货建议至多 2 条（宁缺勿错上限，超了即判负）
    - no_fabricated：points 里每个数字必须出现在 steps 的观察 JSON 里
      （复用 core.agent.numeric_grounded 逐 point 校验——日报不变量的直接检验）
每条用例 force=True 独立重跑（缓存命中会让后续用例空转，测不出 agent 行为）。
模型口径：hy4。
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

from core import agent as agentmod   # noqa: E402
from core import config as cfgmod    # noqa: E402
from core import daily as dmod       # noqa: E402
from core.erp import get_client      # noqa: E402
from core.report import official_stats   # noqa: E402

CASES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "daily_cases.jsonl")
RESULTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results")
CORPUS_VERSION = "daily-v1-20261116"


def _is_comment(obj):
    """注释行（{"_comment": ...}）不作为用例。"""
    return isinstance(obj, dict) and obj and all(str(k).startswith("_") for k in obj)


def _numbers(text):
    """与 run_ask 同法：抽出全部数字，千分位归一、两位小数容差。"""
    out = set()
    for raw in re.findall(r"-?\d[\d,]*(?:\.\d+)?", text or ""):
        try:
            out.add(round(float(raw.replace(",", "")), 2))
        except ValueError:
            pass
    return out


def _gold_today(erp):
    """当天动态 gold：official_stats 的今日实卖实买（0 值剔除——空仓日断言 0 会误判）。"""
    st = official_stats(erp)
    t = st.get("today") or {}
    return [round(float(t[k]), 2) for k in ("sell", "buy") if float(t.get(k) or 0) > 0]


def score_case(case, out, gold):
    e = case.get("expect") or {}
    points = out.get("points") or []
    steps = out.get("steps") or []
    text = (out.get("report_md") or "") + json.dumps(points, ensure_ascii=False)

    checks = {"ok": bool(out.get("ok"))}
    if e.get("contains_today_numbers"):
        got = _numbers(text)
        checks["contains_today_numbers"] = bool(gold) and all(
            any(abs(g - x) <= 0.01 for x in got) for g in gold)
    if e.get("min_points"):
        checks["min_points"] = len(points) >= e["min_points"]
    if e.get("draft_max") is not None:
        checks["draft_max"] = len(out.get("suggest_drafts") or []) <= e["draft_max"]
    if e.get("no_fabricated"):
        observations = [s.get("observation") for s in steps
                        if isinstance(s, dict) and s.get("observation")]
        checks["no_fabricated"] = all(
            agentmod.numeric_grounded((p.get("title") or "") + (p.get("detail") or ""),
                                      observations)
            for p in points)
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
        if _is_comment(obj):
            continue
        cases.append(obj)
    if args.limit:
        cases = cases[:args.limit]

    prof = cfgmod.active_profile()
    if not prof:
        print("未配置模型档案")
        return 2
    erp = get_client()

    results, t0 = [], time.time()
    for i, c in enumerate(cases, 1):
        t = time.time()
        gold = []
        err = ""
        try:
            gold = _gold_today(erp)
            out = dmod.morning_report(erp, operator="evalbot", force=True)
        except Exception as e:
            out, err = {"ok": False, "points": [], "steps": []}, str(e)[:120]
        lat = time.time() - t
        ok, checks = score_case(c, out, gold)
        toks = out.get("tokens") or {}
        results.append({"id": c["id"], "type": c["type"], "ok": ok, "checks": checks,
                        "latency_s": round(lat, 2),
                        "tokens": toks.get("prompt", 0) + toks.get("completion", 0),
                        "gold_today": gold,
                        "points": len(out.get("points") or []),
                        "drafts": len(out.get("suggest_drafts") or []),
                        "degraded": bool(out.get("degraded")),
                        "grounded": out.get("grounded"),
                        "error": err})
        mark = "✓" if ok else "✗"
        bad = [k for k, v in (checks or {}).items() if not v]
        print(f"[{i}/{len(cases)}] {c['id']} {mark} {lat:5.1f}s"
              + (f"  错:{','.join(bad)}" if bad else "") + (f"  ERR:{err[:40]}" if err else ""))

    n = len(results)
    passed = [r for r in results if r["ok"]]
    summary = {"corpus_version": CORPUS_VERSION, "model": "hy4", "n": n,
               "acc": round(len(passed) / n, 4) if n else 0,
               "latency_avg_s": round(statistics.mean([r["latency_s"] for r in results]), 2) if results else 0,
               "tokens_avg": round(statistics.mean([r["tokens"] for r in results])) if results else 0}
    stamp = time.strftime("%Y%m%d-%H%M%S")
    tag = ("-" + args.tag) if args.tag else ""
    os.makedirs(RESULTS_DIR, exist_ok=True)
    json.dump({"summary": summary, "results": results},
              open(os.path.join(RESULTS_DIR, f"daily_{stamp}{tag}.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    with open(os.path.join(RESULTS_DIR, f"daily_{stamp}{tag}.md"), "w", encoding="utf-8") as f:
        f.write(f"# 老板日报 agent 评测 {stamp}{tag}\n\n"
                f"- 语料：{CORPUS_VERSION}（{n} 条）· 模型：hy4 · 耗时 {round(time.time()-t0)}s\n"
                f"- **通过率：{summary['acc']*100:.1f}%** · 延迟均值 {summary['latency_avg_s']}s · token 均值 {summary['tokens_avg']}\n\n"
                f"## 未通过\n\n")
        for r in results:
            if not r["ok"]:
                bad = [k for k, v in r["checks"].items() if not v]
                f.write(f"- {r['id']}：{','.join(bad)}｜gold 今日：{r['gold_today']}\n")
    print("=" * 60)
    print(f" 通过 {len(passed)}/{n} = {summary['acc']*100:.1f}%  延迟均值 {summary['latency_avg_s']}s")
    print(f" 明细 → eval/results/daily_{stamp}{tag}.md")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
