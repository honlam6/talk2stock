#!/usr/bin/env python3
"""统一业务 Agent 冒烟评测：不按旧“规则直答”口径评分，只验证核心能力。

前置：本地 ERP 已启动，eval/seed_demo.py --yes 已在当天执行；OpenRouter
模型档案已配置。本脚本只发起查询与起草，绝不提交写入。
"""
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))

from core import ask as askmod  # noqa: E402
from core.erp import get_client  # noqa: E402

CASES = [
    {"id": "query_stock", "text": "可乐还有多少库存？", "must": ["52"], "kind": "answer"},
    {"id": "query_stats", "text": "今天卖了多少？", "must": ["222"], "kind": "answer"},
    {"id": "draft", "text": "从美宜佳买30瓶可乐，单价3块5，入原料仓", "must": ["草稿"], "kind": "draft"},
    {"id": "clarify", "text": "卖给校园超市", "must_any": ["哪些", "商品", "数量"], "kind": "clarify"},
    {"id": "boundary", "text": "把昨天的单据删掉", "must_any": ["不能", "只支持", "已有数据"], "kind": "boundary"},
]


def score(case, out):
    ans = str(out.get("answer") or "")
    if case["kind"] == "draft":
        return bool(out.get("drafts")) and not out.get("drafts", [{}])[0].get("bills", [{}])[0].get("preview", {}).get("errors"), ans
    if case["kind"] == "clarify":
        payload = (out.get("drafts") or [{}])[-1]
        return bool(payload.get("clarify")) or any(x in ans for x in case["must_any"]), ans
    if "must" in case:
        return all(x in ans for x in case["must"]) and out.get("grounded") is not False, ans
    return any(x in ans for x in case.get("must_any", [])) and not out.get("drafts"), ans


def main():
    erp = get_client()
    rows = []
    for c in CASES:
        started = time.time()
        out = askmod.ask(c["text"], erp)
        ok, ans = score(c, out)
        rows.append({"id": c["id"], "ok": ok, "latency_s": round(time.time() - started, 2),
                     "route": out.get("route"), "grounded": out.get("grounded"),
                     "tools": [s.get("tool") for s in out.get("steps", []) if s.get("tool")],
                     "drafts": len(out.get("drafts") or []), "answer": ans[:160]})
        print(("✓" if ok else "✗"), c["id"], rows[-1])
    path = os.path.join(ROOT, "eval", "results", "unified_agent_smoke_%s.json" % time.strftime("%Y%m%d-%H%M%S"))
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"model": "deepseek/deepseek-v4.1-flash-1017", "cases": rows}, f, ensure_ascii=False, indent=2)
    print("结果：", path)
    return 0 if all(r["ok"] for r in rows) else 1


if __name__ == "__main__":
    sys.exit(main())
