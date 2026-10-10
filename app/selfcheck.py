#!/usr/bin/env python3
"""一键自检：确认 ERP 可连、核心链路可用。
用法：python3 selfcheck.py
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

OK, FAIL = "\033[32m✓\033[0m", "\033[31m✗\033[0m"
results = []


def check(name, fn):
    try:
        detail = fn()
        results.append((True, name, detail))
        print(f"{OK} {name}　{detail}")
        return True
    except Exception as e:
        results.append((False, name, str(e)))
        print(f"{FAIL} {name}　{e}")
        return False


def main():
    print("=" * 56)
    print(" Talk2Stock · 应用层自检")
    print("=" * 56)

    from core import config
    cfg = config.load()
    print(f"\n配置文件：{config.CONFIG_PATH}")
    print(f"ERP 地址：{cfg['erp']['base']}\n")

    from core import erp as erpmod
    holder = {}

    def _login():
        c = erpmod.get_client(force=True)
        holder["c"] = c
        return f"token {c.token[:10]}…"

    if not check("ERP 登录", _login):
        print("\n→ ERP 没起来？执行：")
        print("  cd ~/Desktop/jshERP-suite/jshERP-latest/deploy && docker compose up -d --no-build")
        return 1

    c = holder["c"]
    check("供应商/客户", lambda: f"供应商 {len(c.suppliers('供应商'))} 个，客户 {len(c.suppliers('客户'))} 个")
    check("商品档案", lambda: f"{len(c.materials())} 个")
    check("仓库", lambda: "、".join(d["name"] for d in c.depots()[:6]))
    check("结算账户", lambda: "、".join(a["name"] for a in c.accounts()[:4]))
    check("单据查询", lambda: f"{len(c.all_bills())} 张")

    from core import draft
    d = {"biz": "purchase_in", "party": "自检用供应商", "depot": "原料仓",
         "items": [{"name": "自检用商品", "qty": 1, "unit": "个", "price": 1}]}
    check("预览层（只读，不写库）",
          lambda: f"将新建 {len(draft.resolve(d, c)['to_create'])} 项主数据")

    from core import llm
    prof = config.active_profile()
    if prof:
        ok, msg = llm.test_profile(prof)
        (check if ok else check)("AI 模型 " + (prof.get("name") or ""),
                                 lambda: msg if ok else (_ for _ in ()).throw(Exception(msg)))
    else:
        print(f"\033[33m—\033[0m AI 模型　未配置（表单录入不受影响，AI 录入需配置）")

    bad = [r for r in results if not r[0]]
    print("\n" + "=" * 56)
    if bad:
        print(f"{len(bad)} 项失败：" + "、".join(r[1] for r in bad))
        return 1
    print("全部通过。启动服务：python3 server.py 8090")
    return 0


if __name__ == "__main__":
    sys.exit(main())
