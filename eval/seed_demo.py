#!/usr/bin/env python3
"""受控演示数据集：一键把 jshERP 业务数据重置成固定状态。

用途：
  - 基线对比实验的场景 A 起点（档案齐全、库存/单据数字确定）
  - 演示/录屏前的状态还原（谁弄乱了库，30 秒还原）
  - 课程"可复现"证据：任何人跑同一脚本得到同一份环境

做三件事：
  1. 备份：mysqldump 整库到 backups/seed-backup-<时间戳>.sql（可逆保证）
  2. 清场：仅软删演示租户（jsh，tenant 63）的业务数据——单据/明细/商品/往来单位；
     不动 用户、仓库、结算账户、计量单位（保证 config.json 的默认账户/默认仓仍有效）
  3. 播种：按下方 DATASET 建档案 + 走 draft.commit（与线上同一条写入路径）建 9 张单，
     日期相对今天分布（D-3 ~ D0），让看板/问答/欠款演示都有真实数字

用法：
    python3 eval/seed_demo.py            # 打印计划，交互确认后执行
    python3 eval/seed_demo.py --yes      # 跳过确认
    python3 eval/seed_demo.py --wipe-only

幂等：重复执行得到相同状态。种子建档/建单走官方 REST API，不直连数据库写。
"""
import argparse
import json
import os
import subprocess
import sys
import time
from datetime import date, timedelta

APP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app")
sys.path.insert(0, os.path.abspath(APP_DIR))
ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from core import draft as draftmod   # noqa: E402
from core import erp as erpmod       # noqa: E402

MYSQL_CONTAINER = "erp-mysql"
MYSQL_DB = "jsh_erp"
DEMO_TENANT = 63  # jsh 账号所在租户（脚本会先动态确认）

# ---------------- 演示数据集（与 eval/cases.jsonl 的名称体系一致） ----------------
SUPPLIERS = ["美宜佳", "农夫山泉", "日化批发部", "玖龙纸业"]
CUSTOMERS = ["永辉超市", "张记便利店", "校园超市"]
MATERIALS = [("可乐", "瓶"), ("矿泉水", "瓶"), ("方便面", "箱"), ("电池", "节"),
             ("纸箱", "个"), ("牛皮纸", "卷"), ("肥皂", "块")]

def _d(offset):
    return (date.today() + timedelta(days=offset)).strftime("%Y-%m-%d") + " 10:30:00"

BILLS = [
    # (相对天数, biz, 往来单位, 仓库, [(商品, 数量, 单价)...], 实付金额或 None)
    (-3, "purchase_in", "美宜佳",   "成品仓", [("可乐", 100, 3.5), ("矿泉水", 200, 2)], None),
    (-3, "purchase_in", "玖龙纸业", "原料仓", [("牛皮纸", 10, 80)], None),
    (-2, "purchase_in", "农夫山泉", "成品仓", [("矿泉水", 100, 2.2)], None),
    (-2, "purchase_in", "日化批发部", "原料仓", [("肥皂", 100, 4.5)], None),
    (-2, "purchase_in", "美宜佳",   "成品仓", [("方便面", 20, 40), ("电池", 100, 2.5)], None),
    (-1, "sale_out",    "永辉超市", "成品仓", [("可乐", 50, 4), ("矿泉水", 80, 2.5)], 300),
    (-1, "sale_out",    "张记便利店", "成品仓", [("方便面", 10, 44)], None),
    (0,  "sale_out",    "校园超市", "成品仓", [("电池", 30, 3.5), ("矿泉水", 50, 2.5)], None),
    (0,  "sale_return", "永辉超市", "成品仓", [("可乐", 2, 4)], None),
]

WIPE_TABLES = ["jsh_depot_item", "jsh_depot_head", "jsh_material", "jsh_supplier"]


def _sql(sql):
    """在 MySQL 容器里执行一条 SQL（只用于软删/查询，绝不写业务数据）。"""
    cmd = ["docker", "exec", MYSQL_CONTAINER, "mysql", "--default-character-set=utf8mb4",
           "-uroot", "-p123456", MYSQL_DB, "-e", sql]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("MySQL 执行失败：%s" % (r.stderr or r.stdout).strip()[:200])
    return r.stdout


def _backup():
    os.makedirs(os.path.join(ROOT, "backups"), exist_ok=True)
    path = os.path.join(ROOT, "backups", "seed-backup-%s.sql" % time.strftime("%Y%m%d-%H%M%S"))
    with open(path, "w") as f:
        r = subprocess.run(["docker", "exec", MYSQL_CONTAINER, "mysqldump",
                            "--default-character-set=utf8mb4", "-uroot", "-p123456", MYSQL_DB],
                           stdout=f, stderr=subprocess.PIPE, text=True)
    if r.returncode != 0:
        raise RuntimeError("备份失败：%s" % (r.stderr or "")[:200])
    return path


def wipe():
    out = _sql("SELECT tenant_id FROM jsh_user WHERE login_name='jsh'")
    tenant = None
    for line in out.strip().splitlines()[1:]:
        tenant = int(line.strip())
    if tenant != DEMO_TENANT:
        print("⚠ jsh 租户是 %s（预期 %d），按实际租户执行" % (tenant, DEMO_TENANT))
    for t in WIPE_TABLES:
        _sql("UPDATE %s SET delete_flag='1' WHERE tenant_id=%d AND delete_flag='0'" % (t, tenant))
    return tenant


def seed():
    erp = erpmod.get_client(force=True)
    created = {"parties": 0, "materials": 0, "bills": 0}

    for s in SUPPLIERS:
        _, was_new = erp.ensure_party(s, "供应商")
        created["parties"] += bool(was_new)
    for c in CUSTOMERS:
        _, was_new = erp.ensure_party(c, "客户")
        created["parties"] += bool(was_new)
    for name, unit in MATERIALS:
        _, was_new = erp.ensure_material(name, unit)
        created["materials"] += bool(was_new)
    erp.invalidate("")

    for offset, biz, party, depot, items, pay in BILLS:
        d = {
            "biz": biz, "party": party, "depot": depot,
            "items": [{"name": n, "qty": q, "unit": dict(MATERIALS)[n], "price": p}
                      for n, q, p in items],
            "oper_time": _d(offset),
        }
        if pay is not None:
            d["pay_amount"] = pay
        res = draftmod.commit(d, erp)
        if not res.get("ok"):
            raise RuntimeError("建单失败（%s %s）：%s" % (party, d["oper_time"][:10], res.get("message")))
        created["bills"] += 1
    erp.invalidate("")
    return created


def verify():
    erp = erpmod.get_client()
    bills = [b for b in erp.all_bills() if str(b.get("deleteFlag")) != "1"]
    mats = erp.materials()
    parties = erp.suppliers("供应商") + erp.suppliers("客户")
    print("\n──────── 播种结果 ────────")
    print("档案：供应商/客户 %d 个，商品 %d 个" % (len(parties), len(mats)))
    print("单据：%d 张" % len(bills))
    for b in sorted(bills, key=lambda x: str(x.get("operTime")), reverse=True)[:12]:
        print("  %s %s·%s  %-8s %10.2f 元" % (
            str(b.get("operTime"))[:10], b.get("type"), b.get("subType"),
            (b.get("organName") or "-"), float(b.get("totalPrice") or 0)))
    neg = [m for m in mats if m.get("stock") is not None and float(m["stock"]) < 0]
    print("负库存商品：%d 个（应为 0）" % len(neg))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--yes", action="store_true", help="跳过确认直接执行")
    ap.add_argument("--wipe-only", action="store_true", help="只清场不播种")
    args = ap.parse_args()

    print("将执行：① 整库备份 → ② 软删演示租户业务数据（%s）→ ③ %s" %
          ("、".join(WIPE_TABLES), "只清场" if args.wipe_only else "按 DATASET 重建 %d 个往来单位 / %d 个商品 / %d 张单" %
           (len(SUPPLIERS) + len(CUSTOMERS), len(MATERIALS), len(BILLS))))
    if not args.yes:
        if input("确认执行？(yes/no) ").strip().lower() not in ("y", "yes"):
            print("已取消。")
            return 1

    try:
        bk = _backup()
        print("✓ 备份：%s" % os.path.relpath(bk, ROOT))
    except Exception as e:
        print("✗ %s\n（MySQL 容器没起？先 bash start-all.sh）" % e)
        return 2

    wipe()
    print("✓ 清场完成")
    if not args.wipe_only:
        created = seed()
        print("✓ 播种：新建档案 %s，商品 %s，单据 %d 张" %
              (created["parties"], created["materials"], created["bills"]))
        verify()
    print("\n还原方式：docker exec -i erp-mysql mysql -uroot -p123456 jsh_erp < %s" % os.path.relpath(bk, ROOT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
