#!/usr/bin/env python3
"""导入修复 agent 评测跑分器——对 eval/repair_fixture.py 生成的 30 行脏 Excel 走
与线上一致的完整链路（llm.map_sheet → sheet.transform → repair.repair_transform_result），
按 gold（内置于 eval/repair_cases.jsonl）判定修复命中率与不变量守护，输出
命中率 / 误改率 / 步骤数 / 延迟 / token，报告落 eval/results/repair_<ts><tag>.md/json。

前置（必须当天执行，与 run_billcheck 同一套环境）：
    cd jshERP-latest/deploy && docker compose up -d --no-build
    python3 eval/seed_demo.py --yes          # 档案名体系与 fixture 对齐，修复工具才查得到
    cd app && python3 ../eval/run_repair.py [--fixture PATH] [--limit N] [--tag v1]

判定设计（与 run_billcheck 同一第一性原理：LLM 层命中率 + 不变量守护）：
    - hit_min：applied+suggestions 里与 gold（行号+字段+值）匹配的最少命中数；
      gold 行号对应 fixture 的 Excel 行号（表头=1，数据 2..28，脚注 29..30）。
    - no_touch_qty_price：任何 fix（含 rejected）不许出现 qty/price 字段，且修复后
      drafts 的数量/单价快照必须与修复前 transform 输出逐行相等——动了数字直接 0 分。
    - no_fix_rows：这些行不许出现 applied/suggestions（乱码行留给人工、正常行不许碰）。
    - hit_rate：gold 总命中比例；precision：actionable fix 里对 gold 的正确占比。
    - 绑定档案率：item_name 修复带 material_id 的比例（规格消歧质量，仅统计不设门槛）。
"""
import argparse
import json
import os
import sys
import time

EVAL_DIR = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.join(EVAL_DIR, "..", "app")
sys.path.insert(0, os.path.abspath(APP_DIR))
sys.path.insert(0, EVAL_DIR)

from core import repair as repairmod    # noqa: E402
from core import sheet as sheetmod      # noqa: E402
from core import config as cfgmod       # noqa: E402
from core import llm as llmmod          # noqa: E402
from core.erp import get_client         # noqa: E402

CASES_PATH = os.path.join(EVAL_DIR, "repair_cases.jsonl")
RESULTS_DIR = os.path.join(EVAL_DIR, "results")
EVAL_OPERATOR = "evalbot"
CORPUS_VERSION = "repair-v1-20261116"
NUM_FIELDS = ("qty", "price")     # 不变量字段：修复 agent 永远不许动
ACTION_KINDS = ("applied", "suggestions")


def load_cases():
    cases = []
    for l in open(CASES_PATH, encoding="utf-8"):
        if not l.strip():
            continue
        obj = json.loads(l)
        if isinstance(obj, dict) and obj and all(str(k).startswith("_") for k in obj):
            continue
        cases.append(obj)
    return cases


def ensure_fixture(path):
    """fixture 不存在就用生成器现场造一份（生成器纯标准库，输出确定）。"""
    if os.path.exists(path):
        return path
    import repair_fixture as fx
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "wb") as f:
        f.write(fx.build_xlsx(fx.build_rows()))
    print("fixture 不存在，已生成：%s" % path)
    return path


def num_snapshot(drafts):
    """逐行数量/单价快照——修复前后必须逐值相等（no_touch_qty_price 的判定基准）。"""
    out = {}
    for d in drafts or []:
        out[d.get("row_no")] = [(it.get("qty"), it.get("price"))
                                for it in ((d.get("draft") or {}).get("items") or [])]
    return out


def fix_row_no(f, drafts):
    """fix 的 row_index 是 drafts 数组下标，换算成 Excel 行号（与 gold 对齐）。"""
    ri = f.get("row_index")
    if not isinstance(ri, int) or ri < 0 or ri >= len(drafts or []):
        return None
    return (drafts[ri] or {}).get("row_no")


def fix_row_field_value(f, drafts):
    return (fix_row_no(f, drafts), str(f.get("field") or ""), str(f.get("value") or "").strip())


def score_case(case, drafts, rep, base_nums):
    e = case.get("expect") or {}
    actionable = [(k, f) for k in ACTION_KINDS for f in (rep.get(k) or [])]
    checks = {"ok": True}

    # gold 命中：行号+字段+值 三元匹配（applied 与 suggestions 同等算命中）
    hits, hit_detail = 0, []
    for row_no_s, g in (case.get("gold") or {}).items():
        want = (int(row_no_s), str(g.get("field") or ""), str(g.get("value") or "").strip())
        kind = next((k for k, f in actionable if fix_row_field_value(f, drafts) == want), None)
        if kind:
            hits += 1
            hit_detail.append("%s→%s(%s)" % (want[0], want[2], kind))
    if "hit_min" in e:
        checks["hit_min"] = hits >= e["hit_min"]

    # 误改率守门：实际写回（applied+suggestions）不许出现 qty/price 字段，且修复后
    # 数字快照与 transform 基线逐值相等。rejected 里的数字 fix 是护栏拦下的违规提议
    # （没真动数，不判 0 分），作为纪律信号进 summary.rejected_num_fixes。
    if e.get("no_touch_qty_price"):
        wrote_num = [f.get("field") for _, f in actionable if str(f.get("field") or "") in NUM_FIELDS]
        checks["no_touch_qty_price"] = not wrote_num and num_snapshot(drafts) == base_nums

    # 指定行不许被写回（乱码行留人工 / 正常行不碰）
    for r in (e.get("no_fix_rows") or []):
        checks["no_fix:%d" % r] = all(fix_row_no(f, drafts) != r for _, f in actionable)
    return all(checks.values()), checks, hits, hit_detail


def fixes_all(rep):
    return [(k, f) for k in ("applied", "suggestions", "rejected") for f in (rep.get(k) or [])]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fixture", default=os.path.join(RESULTS_DIR, "repair_fixture.xlsx"))
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--tag", default="")
    args = ap.parse_args()

    prof = cfgmod.active_profile()
    if not prof:
        print("未配置模型档案")
        return 2
    ensure_fixture(args.fixture)
    cases = load_cases()
    if args.limit:
        cases = cases[:args.limit]

    # ---- 链路：read_workbook → map_sheet（真实 LLM）→ transform → repair ----
    data = open(args.fixture, "rb").read()
    wb = sheetmod.read_workbook(args.fixture, data)
    sheet = wb["sheets"][0]
    rows = sheet["rows"]

    t0 = time.time()
    try:
        mapping = llmmod.map_sheet(sheet["name"], sheetmod.grid_desc(rows))
    except Exception as e:
        print("AI 认列失败：%s" % str(e)[:160])
        return 2
    map_s = round(time.time() - t0, 1)
    try:
        tr = sheetmod.transform(rows, mapping)
    except Exception as e:
        print("确定性变换失败（映射可能坏了）：%s" % str(e)[:160])
        return 2
    base_nums = num_snapshot(tr.get("drafts"))
    n_prob = len([d for d in tr.get("drafts") if d.get("problems")])

    erp = get_client()
    t1 = time.time()
    tr, rep = repairmod.repair_transform_result(tr, erp, operator=EVAL_OPERATOR)
    rep_s = round(time.time() - t1, 1)
    drafts = tr.get("drafts") or []

    if rep.get("skipped"):
        print("修复环未执行：%s" % (rep.get("reason") or "（无问题行或未配置 agent）"))
    steps = [s for s in (rep.get("steps") or []) if s.get("tool")]
    toks = rep.get("tokens") or {}
    total_tokens = toks.get("prompt", 0) + toks.get("completion", 0)

    # ---- 逐用例判定 ----
    results, gold_total, hits_total, actionable_n, correct_n, mid_hit, mid_total = [], 0, 0, 0, 0, 0, 0
    actionable = [(k, f) for k in ACTION_KINDS for f in (rep.get(k) or [])]
    gold_index = set()
    for c in cases:
        for row_no_s, g in (c.get("gold") or {}).items():
            gold_index.add((int(row_no_s), str(g.get("field") or ""), str(g.get("value") or "").strip()))
        gold_total += len(c.get("gold") or {})
    for i, c in enumerate(cases, 1):
        ok, checks, hits, hit_detail = score_case(c, drafts, rep, base_nums)
        hits_total += hits
        results.append({"id": c["id"], "ok": ok, "checks": checks, "hits": hits,
                        "hit_detail": hit_detail, "rows": c.get("rows")})
        mark = "✓" if ok else "✗"
        bad = [k for k, v in (checks or {}).items() if not v]
        print(f"[{i}/{len(cases)}] {c['id']} {mark} 命中{hits}/{len(c.get('gold') or {})}"
              + (f"  错:{','.join(bad)}" if bad else ""))
    for _, f in actionable:
        actionable_n += 1
        if fix_row_field_value(f, drafts) in gold_index:
            correct_n += 1
        if f.get("field") == "item_name":
            mid_total += 1
            if f.get("material_id"):
                mid_hit += 1

    n = len(results)
    passed = [r for r in results if r["ok"]]
    rejected_num_fixes = len([1 for k, f in fixes_all(rep)
                              if k == "rejected" and str(f.get("field") or "") in NUM_FIELDS])
    summary = {"corpus_version": CORPUS_VERSION, "model": prof.get("name") or prof.get("model") or "unknown",
               "n": n, "acc": round(len(passed) / n, 4) if n else 0,
               "hit_rate": round(hits_total / gold_total, 4) if gold_total else 0,
               "precision": round(correct_n / actionable_n, 4) if actionable_n else None,
               "applied": len(rep.get("applied") or []), "suggestions": len(rep.get("suggestions") or []),
               "rejected": len(rep.get("rejected") or []), "rejected_num_fixes": rejected_num_fixes,
               "steps": len(steps), "map_latency_s": map_s, "repair_latency_s": rep_s,
               "tokens": total_tokens, "tokens_estimated": bool(toks.get("estimated")),
               "prompt_version": rep.get("prompt_version") or "",
               "skipped": bool(rep.get("skipped")), "skip_reason": rep.get("reason") or "",
               "problem_rows": n_prob, "mapping_cols": mapping.get("cols") if isinstance(mapping, dict) else None}
    stamp = time.strftime("%Y%m%d-%H%M%S")
    tag = ("-" + args.tag) if args.tag else ""
    os.makedirs(RESULTS_DIR, exist_ok=True)

    json.dump({"summary": summary, "results": results, "mapping": mapping,
               "applied": rep.get("applied"), "suggestions": rep.get("suggestions"),
               "rejected": rep.get("rejected"), "steps": rep.get("steps")},
              open(os.path.join(RESULTS_DIR, f"repair_{stamp}{tag}.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    with open(os.path.join(RESULTS_DIR, f"repair_{stamp}{tag}.md"), "w", encoding="utf-8") as f:
        f.write(f"# 导入修复 agent 评测 {stamp}{tag}\n\n")
        f.write(f"- 语料：{CORPUS_VERSION}（{n} 条 · fixture 30 行/需处理 {n_prob} 行）· 模型：{summary['model']} · 链路耗时 map {map_s}s + repair {rep_s}s\n")
        f.write(f"- **通过率：{summary['acc']*100:.1f}%** · **gold 命中率：{summary['hit_rate']*100:.1f}%（{hits_total}/{gold_total}）**\n")
        f.write(f"- 自动修复 {summary['applied']} · 建议待采纳 {summary['suggestions']} · 未采纳 {summary['rejected']}（数字护栏拦截 {summary['rejected_num_fixes']}）· 取证 {summary['steps']} 步 · token {total_tokens}\n")
        f.write(f"- 误改守门 no_touch_qty_price：{'✓ 守住' if all(r['checks'].get('no_touch_qty_price') for r in results) else '✗ 失守'}")
        f.write(f" · actionable 精确率：{summary['precision'] if summary['precision'] is not None else '—'}")
        f.write(f" · 改名带档案绑定：{mid_hit}/{mid_total}\n")
        if rep.get("skipped"):
            f.write(f"- ⚠ 修复环未执行：{rep.get('reason')}\n")
        f.write("\n## 修复明细（applied + suggestions）\n\n")
        for k in ACTION_KINDS:
            for fx in rep.get(k) or []:
                f.write(f"- [{k}] 行{fix_row_no(fx, drafts)} {fx.get('field')} → {fx.get('value')}"
                        f"（conf {fx.get('confidence')}，{fx.get('reason') or ''}）\n")
        f.write("\n## 未通过\n\n")
        for r in results:
            if not r["ok"]:
                bad = [k for k, v in r["checks"].items() if not v]
                f.write(f"- {r['id']}：{','.join(bad)}｜命中：{r['hit_detail'] or '无'}\n")
    print("=" * 60)
    print(f" 通过 {len(passed)}/{n} = {summary['acc']*100:.1f}%  gold 命中 {hits_total}/{gold_total}"
          f" = {summary['hit_rate']*100:.1f}%  延迟 {map_s}+{rep_s}s  token {total_tokens}")
    print(f" 明细 → eval/results/repair_{stamp}{tag}.md")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
