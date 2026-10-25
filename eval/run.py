#!/usr/bin/env python3
"""Talk2Stock 评测打分脚本（纯标准库）。

对 eval/cases.jsonl 里的冻结语料逐条调用解析层（与线上 /api/parse 完全同路径：
llm.parse + draft.resolve 的前置逻辑），与 gold 标注比对，输出：

  - 整单关键字段全对率（biz / party / 每行 商品×数量×单位 / 单价 / 仓库 全对才算对）
  - 字段级准确率（party、商品名、数量、单位、单价，按「该字段被提及的条数」算）
  - 幻觉字段数（gold 里没提、草稿里却填了的内容——提案要求为 0）
  - 低置信度标注率（对抗样本上模型是否自知信息不全）
  - 延迟（均值 / p95）

用法：
    cd app && python3 ../eval/run.py                # 全量 60 条
    python3 ../eval/run.py --limit 10               # 冒烟
    python3 ../eval/run.py --type adversarial       # 只跑对抗
    python3 ../eval/run.py --profile deepseek       # 临时换模型档案（不改配置）

只调用解析（只读），绝不写库。结果落 eval/results/。
"""
import argparse
import json
import os
import re
import statistics
import sys
import time
from datetime import date, timedelta

APP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app")
sys.path.insert(0, os.path.abspath(APP_DIR))

from core import config as cfgmod   # noqa: E402
from core import erp as erpmod      # noqa: E402
from core import llm as llmmod      # noqa: E402

CASES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cases.jsonl")
RESULTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results")
CORPUS_VERSION = "v2-20261025"  # v1-20261010 冻结 60 条；v2 追加 spec 组 8 条（issue #1 多规格场景）

UNIT_SYNONYM = {"公斤": "kg", "千克": "kg", "kg": "kg", "KG": "kg", "克": "g", "g": "g"}


def norm(s):
    return re.sub(r"\s+", "", str(s or "")).lower()


def unit_norm(u):
    u = norm(u)
    return UNIT_SYNONYM.get(u, u)


def num_eq(a, b, tol=0.01):
    try:
        return abs(float(a) - float(b)) <= tol
    except (TypeError, ValueError):
        return False


def is_zero(v):
    try:
        return float(v or 0) == 0
    except (TypeError, ValueError):
        return True


def fnum(v):
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def resolve_rel_date(token):
    """gold 里的相对日期 → 具体日期（评测当天为基准）。"""
    t = token.strip().upper()
    today = date.today()
    if t == "REL:YESTERDAY":
        return str(today - timedelta(days=1))
    if t == "REL:TODAY":
        return str(today)
    if t == "REL:TOMORROW":
        return str(today + timedelta(days=1))
    m = re.match(r"REL:([+-]?\d+)$", t)
    if m:
        return str(today + timedelta(days=int(m.group(1))))
    return token[:10]


def date_of(v):
    return norm(str(v or ""))[:10]


def party_hit(gold_party, aliases, draft_party):
    """party 判定：完全相等 / 别名 / 互相包含（模型可能补全成档案里的全名）。"""
    d, g = norm(draft_party), norm(gold_party)
    if not g:
        return not d, False
    if not d:
        return False, False
    if d == g or any(d == norm(a) for a in (aliases or [])):
        return True, False
    if g in d or d in g:
        return True, True  # 命中但是包含匹配（记录下来供人工复核）
    return False, False


def name_pair(gn, dn):
    """商品名等价判定：精确 / 互相包含 / 字符多重集相同（「4分PPR管」vs「PPR管 4分」
    只是规格词序不同，字符集合一样，算同一商品）。"""
    if not dn:
        return False
    return dn == gn or gn in dn or dn in gn or sorted(gn) == sorted(dn)


def score_case(case, draft, meta, latency):
    gold = case["gold"]
    r = {"id": case["id"], "type": case["type"], "latency_s": round(latency, 2),
         "confidence": meta.get("confidence", ""), "note": meta.get("note", ""),
         "fields": {}, "hallucinations": [], "fuzzy_party": False}

    # biz
    r["fields"]["biz"] = norm(draft.get("biz")) == norm(gold["biz"])

    # party
    ok, fuzzy = party_hit(gold.get("party"), gold.get("party_aliases"), draft.get("party"))
    r["fields"]["party"] = ok
    r["fuzzy_party"] = fuzzy

    # 明细逐行对齐：gold 每行找一条未用过的 draft 行（名字等价即配对，
    # 含 spec 词序等价与 gold 声明的 aliases——模型把「M8螺丝」规范成主数据
    # 写法「螺丝 M8×20」是正确行为，靠 aliases 认账）
    ditems = list(draft.get("items") or [])
    used = set()
    item_detail = []
    for g in gold["items"]:
        gn = norm(g["name"])
        g_aliases = [norm(a) for a in (g.get("aliases") or []) if a]
        best, best_i = None, None
        for i, d in enumerate(ditems):
            if i in used:
                continue
            dn = norm(d.get("name"))
            if name_pair(gn, dn) or any(name_pair(a, dn) for a in g_aliases):
                if best is None or abs(fnum(d.get("qty")) - fnum(g.get("qty"))) < abs(fnum(best.get("qty")) - fnum(g.get("qty"))):
                    best, best_i = d, i
        if best is None:
            item_detail.append({"gold": g, "draft": None,
                                "name": False, "qty": False, "unit": False, "price": False})
            continue
        used.add(best_i)
        dq, dp = best.get("qty"), best.get("price")
        fq = num_eq(dq, g["qty"]) if g["qty"] is not None else is_zero(dq)
        fp = num_eq(dp, g["price"]) if g["price"] is not None else is_zero(dp)
        fu = True if g["unit"] is None else unit_norm(best.get("unit")) == unit_norm(g["unit"])
        item_detail.append({"gold": g, "draft": best,
                            "name": True, "qty": fq, "unit": fu, "price": fp})
    for i, d in enumerate(ditems):
        if i not in used:
            r["hallucinations"].append(f"多余商品行:{d.get('name')}")
            item_detail.append({"gold": None, "draft": d,
                                "name": False, "qty": False, "unit": False, "price": False})
    r["item_detail"] = item_detail
    r["fields"]["item_name"] = all(d["name"] for d in item_detail) and len(item_detail) == len(gold["items"])
    r["fields"]["item_qty"] = all(d["qty"] for d in item_detail) and len(item_detail) == len(gold["items"])
    r["fields"]["item_unit"] = all(d["unit"] for d in item_detail)
    r["fields"]["item_price"] = all(d["price"] for d in item_detail)

    # 幻觉：行内缺提字段（qty/price gold 为 null 而草稿 >0）已由上面字段判定覆盖；
    # 这里只查「整条字段不该有却被填」的情况（仓库/日期/实付）。
    d_depot = norm(draft.get("depot"))
    g_depot = norm(gold.get("depot"))
    if not g_depot and d_depot:
        r["hallucinations"].append(f"仓库:{draft.get('depot')}")
    r["fields"]["depot"] = (d_depot == g_depot)

    g_date = gold.get("oper_time") or ""
    d_date = date_of(draft.get("oper_time"))
    if g_date.startswith("REL:"):
        r["fields"]["date"] = d_date == resolve_rel_date(g_date)
    elif g_date:
        r["fields"]["date"] = d_date == norm(g_date)[:10]
    else:
        r["fields"]["date"] = d_date == ""
        if d_date:
            r["hallucinations"].append(f"日期:{d_date}")

    d_pay = draft.get("pay_amount")
    g_pay = gold.get("pay_amount")
    if g_pay is None:
        r["fields"]["pay_amount"] = d_pay in (None, "", 0)
        if d_pay not in (None, "", 0):
            r["hallucinations"].append(f"实付:{d_pay}")
    else:
        r["fields"]["pay_amount"] = num_eq(d_pay, g_pay)

    key = ["biz", "party", "item_name", "item_qty", "item_unit", "item_price",
           "depot", "date", "pay_amount"]
    r["all_correct"] = all(r["fields"][k] for k in key)
    return r


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--type", choices=["normal", "adversarial", "spec", "all"], default="all")
    ap.add_argument("--profile", default="", help="临时切换模型档案 key（不改配置文件）")
    ap.add_argument("--tag", default="")
    ap.add_argument("--sleep", type=float, default=0.0)
    args = ap.parse_args()

    cases = [json.loads(l) for l in open(CASES_PATH, encoding="utf-8") if l.strip()]
    if args.type != "all":
        cases = [c for c in cases if c["type"] == args.type]
    if args.limit:
        cases = cases[:args.limit]

    real_active = None
    if args.profile:
        real_active = cfgmod.load()["llm"].get("active")
        cfgmod.set_active(args.profile)
    if not llmmod.is_configured():
        print("未配置模型档案，先在页面设置里加一个，或用 --profile 指定。")
        return 2

    # ERP 上下文可选：连不上就用无上下文模式跑（记录下来）
    erp = None
    try:
        erp = erpmod.get_client()
        print(f"ERP 上下文：已连接 {cfgmod.load()['erp']['base']}")
    except Exception as e:
        print(f"ERP 上下文：不可用（{e}），按无主数据上下文模式评测")

    prof = cfgmod.active_profile()
    model_name = prof.get("model", "?")
    fp = llmmod.PROMPT_VERSION + "/" + __import__("hashlib").sha256(
        llmmod.SYSTEM_PROMPT.encode()).hexdigest()[:8]
    print(f"语料：{CORPUS_VERSION} · {len(cases)} 条 · 模型：{model_name} · prompt：{fp}")

    results, failures = [], []
    for i, c in enumerate(cases, 1):
        t0 = time.time()
        err = ""
        try:
            draft, meta = llmmod.parse(c["text"], erp)
        except Exception as e:
            try:  # 瞬时网络错误重试一次
                draft, meta = llmmod.parse(c["text"], erp)
            except Exception as e2:
                err = str(e2)[:200]
                draft, meta = {"biz": "", "party": "", "items": []}, {}
        lat = time.time() - t0
        toks = (meta or {}).get("tokens") or {}
        if err:
            failures.append({"id": c["id"], "error": err})
            results.append({"id": c["id"], "type": c["type"], "latency_s": round(lat, 2),
                            "fields": {}, "all_correct": False, "error": err,
                            "hallucinations": [], "confidence": "", "note": "", "item_detail": [],
                            "tokens": toks})
            print(f"[{i:>2}/{len(cases)}] {c['id']}  ERROR {err[:60]}")
        else:
            r = score_case(c, draft, meta, lat)
            r["tokens"] = toks
            results.append(r)
            tok_str = ("  %.0ftok" % (toks.get("prompt_tokens", 0) + toks.get("completion_tokens", 0))) if toks else ""
            mark = "✓" if r["all_correct"] else "✗"
            bad = [k for k, v in r["fields"].items() if not v]
            print(f"[{i:>2}/{len(cases)}] {c['id']}  {mark}  {'' if not bad else '错:' + ','.join(bad)}{tok_str}"
                  + (f"  幻觉:{';'.join(r['hallucinations'])}" if r["hallucinations"] else ""))
        if args.sleep:
            time.sleep(args.sleep)

    if real_active is not None:
        cfgmod.set_active(real_active)

    # ---------- 汇总 ----------
    n = len(results)
    ok_all = sum(1 for r in results if r.get("all_correct"))
    lat_list = [r["latency_s"] for r in results if "latency_s" in r]
    lat_sorted = sorted(lat_list)
    p95 = lat_sorted[min(len(lat_sorted) - 1, int(round(0.95 * len(lat_sorted))))] if lat_sorted else 0

    # token 维度：每单 prompt/completion 用量（网关返回精确值或估算，见 llm.usage 台账）
    ptoks = [r["tokens"]["prompt_tokens"] for r in results if (r.get("tokens") or {}).get("prompt_tokens")]
    ctoks = [r["tokens"]["completion_tokens"] for r in results if (r.get("tokens") or {}).get("completion_tokens")]
    per_case_total = [t["prompt_tokens"] + t["completion_tokens"] for t in
                      (r.get("tokens") or {} for r in results) if t.get("prompt_tokens")]

    def acc(field, subset=None):
        rows = [r for r in results if (subset is None or r["type"] == subset) and field in r.get("fields", {})]
        return (sum(1 for r in rows if r["fields"].get(field)), len(rows))

    summary = {
        "corpus_version": CORPUS_VERSION,
        "model": model_name,
        "prompt": fp,
        "n": n,
        "whole_bill_acc": round(ok_all / n, 4) if n else 0,
        "field_acc": {f: acc(f) for f in
                      ["biz", "party", "item_name", "item_qty", "item_unit", "item_price",
                       "depot", "date", "pay_amount"]},
        "hallucination_total": sum(len(r.get("hallucinations", [])) for r in results),
        "hallucination_cases": sum(1 for r in results if r.get("hallucinations")),
        "errors": failures,
        "latency_avg_s": round(statistics.mean(lat_list), 2) if lat_list else 0,
        "latency_p95_s": round(p95, 2),
        "tokens_prompt_avg": round(statistics.mean(ptoks)) if ptoks else 0,
        "tokens_completion_avg": round(statistics.mean(ctoks)) if ctoks else 0,
        "tokens_total_avg": round(statistics.mean(per_case_total)) if per_case_total else 0,
        "conf_low_on_adversarial": "{}/{}".format(
            sum(1 for r in results if r["type"] == "adversarial" and r.get("confidence") == "low"),
            sum(1 for r in results if r["type"] == "adversarial")),
        "conf_low_on_spec": "{}/{}".format(
            sum(1 for r in results if r["type"] == "spec" and r.get("confidence") == "low"),
            sum(1 for r in results if r["type"] == "spec")),
    }

    os.makedirs(RESULTS_DIR, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    tag = ("-" + args.tag) if args.tag else ""
    jpath = os.path.join(RESULTS_DIR, f"run_{stamp}{tag}.json")
    mpath = os.path.join(RESULTS_DIR, f"run_{stamp}{tag}.md")
    json.dump({"summary": summary, "results": results}, open(jpath, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)

    with open(mpath, "w", encoding="utf-8") as f:
        f.write(f"# 评测结果 {stamp}{tag}\n\n"
                f"- 语料：{CORPUS_VERSION}（{n} 条）· 模型：{model_name} · prompt：{fp}\n"
                f"- **整单关键字段全对率：{summary['whole_bill_acc']*100:.1f}%**（目标 ≥85%）\n"
                f"- 幻觉字段：{summary['hallucination_total']} 处 / 影响 {summary['hallucination_cases']} 条（目标 0）\n"
                f"- 解析报错：{len(failures)} 条\n"
                f"- 延迟：均值 {summary['latency_avg_s']}s · p95 {summary['latency_p95_s']}s（目标 p95≤3s）\n"
                f"- 对抗样本上标 low 置信度：{summary['conf_low_on_adversarial']}\n"
                f"- 规格组（issue #1 场景）标 low 置信度：{summary['conf_low_on_spec']}\n\n")
        f.write("| 字段 | 对/总 | 准确率 |\n|---|---|---|\n")
        for k, (a, b) in summary["field_acc"].items():
            f.write(f"| {k} | {a}/{b} | {a/b*100:.1f}% |\n" if b else f"| {k} | - | - |\n")
        f.write("\n## 未通过用例\n\n")
        for r in results:
            if not r.get("all_correct"):
                bad = [k for k, v in r.get("fields", {}).items() if not v]
                f.write(f"- {r['id']}（{r['type']}）：{','.join(bad)}"
                        + (f"；幻觉：{';'.join(r['hallucinations'])}" if r.get("hallucinations") else "")
                        + (f"；解析报错：{r.get('error','')[:80]}" if r.get("error") else "")
                        + "\n")

    print("\n" + "=" * 62)
    print(f" 整单全对率 {summary['whole_bill_acc']*100:.1f}%（{ok_all}/{n}）  "
          f"幻觉 {summary['hallucination_total']}  报错 {len(failures)}  "
          f"延迟均值 {summary['latency_avg_s']}s / p95 {summary['latency_p95_s']}s")
    print(f" 明细 → {mpath}")
    print("=" * 62)
    return 0


if __name__ == "__main__":
    sys.exit(main())
