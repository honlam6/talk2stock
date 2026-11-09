#!/usr/bin/env python3
"""问一问规模稳定性基准（第一性原理：小数据掩盖数据层缺陷，这里灌大数揭）。

只跑本地（容器 erp-mysql）；服务器数据永不触碰。流程：
  1) mysqldump 备份当前库 → backups/pre-stress-<ts>.sql
  2) 直插合成数据（SQL 批量）：商品 / 供应商 / 单据（跨越 90 天，半数 debt>0 且
     落在「最新 200 条窗口」之外——探测 all_bills 截断漏报老账）
  3) 探测（全部走 app 只读函数 = 线上同路径）：
     P1 find_material 越过 500 条窗口的商品是否还能命中（list 单页截断）
     P2 len(materials) vs SQL count(*)（静默截断差值）
     P3 ask._master_count 商品计数正确性
     P4 summary 应收合计 vs SQL sum(debt)（老账漏报差值）
     P5 stock_table 全量构建延迟（O(N) 次 getDetailList HTTP）
     P6 端到端 ask（真实 LLM）在大数据下的延迟与正确性（--llm 开启）
  4) 报告落 eval/results/stress_<ts>.md
  5) 默认从最新 seed-backup 还原本地库（--skip-restore 跳过）

用法：
    python3 eval/stress_scale.py                     # 800 商品 / 1200 单据 / 120 供应商
    python3 eval/stress_scale.py --mats 900 --bills 3000
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

from core import ask as askmod        # noqa: E402
from core import report as rp         # noqa: E402
from core.erp import get_client       # noqa: E402

MYSQL = ["docker", "exec", "erp-mysql", "mysql", "--default-character-set=utf8mb4",
         "-uroot", "-p123456", "jsh_erp", "-e"]
TENANT = 63
STRESS_TAG = "STRESS"


def _sql(sql):
    r = subprocess.run(MYSQL + [sql], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip()[:300])
    return r.stdout


def _sql_rows(sql):
    out = _sql(sql)
    lines = [l.split("\t") for l in out.strip().splitlines() if l.strip()]
    return lines[1:] if len(lines) > 1 else []


def _batch(prefix, rows, size=300):
    """多行 INSERT 分块执行（逐条 docker exec 灌千级单据要十几分钟，分块后秒级）。"""
    for i in range(0, len(rows), size):
        _sql(prefix + ",".join(rows[i:i + size]))


def backup(tag):
    os.makedirs(os.path.join(ROOT, "backups"), exist_ok=True)
    path = os.path.join(ROOT, "backups", "%s-%s.sql" % (tag, time.strftime("%Y%m%d-%H%M%S")))
    with open(path, "w") as f:
        subprocess.run(["docker", "exec", "erp-mysql", "mysqldump",
                        "--default-character-set=utf8mb4", "-uroot", "-p123456", "jsh_erp"],
                       stdout=f, stderr=subprocess.PIPE, text=True)
    return path


def latest_seed_backup():
    cands = sorted(p for p in os.listdir(os.path.join(ROOT, "backups"))
                   if p.startswith("seed-backup-"))
    return os.path.join(ROOT, "backups", cands[-1]) if cands else None


def restore(path):
    with open(path) as f:
        subprocess.run(["docker", "exec", "-i", "erp-mysql", "mysql", "-uroot", "-p123456", "jsh_erp"],
                       stdin=f, capture_output=True, text=True)
    erp = get_client(force=True)
    erp.invalidate("")
    print("✓ 已还原：%s" % os.path.relpath(path, ROOT))


def insert_real_unpaid(n=260):
    """真路径未付采购单（走 draft.commit，与线上同口径）：欠款测试的 ground truth。
    手工 SQL 造行拿不到 jshERP 服务端的欠款计算（多轮夹具迭代的教训），只有真写入
    才带正确的 debt 语义；260 张 > 旧版每类 200 条窗口，专测 all_bills 截断。"""
    from core import draft as draftmod
    from core import gate as gatemod
    erp = get_client()
    today = date.today()
    made = 0
    for i in range(n):
        age = i * 90 // max(n, 1)
        d = {"biz": "purchase_in", "party": "美宜佳", "depot": "成品仓",
             "items": [{"name": "肥皂", "qty": 30, "unit": "块", "price": 3.5}],
             "oper_time": (today - timedelta(days=age)).strftime("%Y-%m-%d") + " 08:30:00",
             "pay_amount": 45}
        res = draftmod.commit(d, erp)
        if res.get("ok"):
            gatemod.log_write({"model": "stress", "prompt_version": "stress"}, d, res,
                              source="stress_scale",
                              raw_text="从美宜佳进肥皂30块，付了45（压测欠款单%d）" % i)
            made += 1
        if made and made % 50 == 0 and made == i + 1:
            pass
    erp.invalidate("")
    return made


def insert_bulk(mats, bills, suppliers):
    erp = get_client()
    kele = erp.find_material("可乐")[0]
    depot = erp.find_depot("成品仓")[0]
    organ = erp.find_party("美宜佳", "供应商")[0]
    base_id = 900000

    chunks = []
    for i in range(1, mats + 1):
        chunks.append("(%d, NULL, '压测商品%d', 'ST-%d', '个', b'1', %d, b'0')"
                      % (base_id + i, i, i, TENANT))
    _batch("INSERT IGNORE INTO jsh_material (id, category_id, name, model, unit, enabled, tenant_id, delete_flag) VALUES ", chunks)

    chunks = []
    for i in range(1, suppliers + 1):
        chunks.append("(%d, '压测供应商%d', '供应商', b'1', %d, b'0')"
                      % (base_id + 500000 + i, i, TENANT))
    _batch("INSERT IGNORE INTO jsh_supplier (id, supplier, type, enabled, tenant_id, delete_flag) VALUES ", chunks)

    today = date.today()
    heads, items = [], []
    for i in range(1, bills + 1):
        # 前 1/4 是最新的「今天」（在 200 条窗口内），其余向 90 天前摊开：
        # 越老的单 debt 越可能 >0 → 探测 all_bills 每类只取一页时的老账漏报
        age = 0 if i <= bills // 4 else (i * 90 // max(bills, 1))
        d = (today - timedelta(days=age)).strftime("%Y-%m-%d") + " 0%d:30:00" % (i % 9 + 1)
        # 欠款语义由 insert_real_unpaid 的真路径单承载；SQL 灌量单全部全款
        change = 105.0
        hid = base_id + 700000 + i
        # 端点按 accountMoneyList 计欠款（真单 105-[45]=60）；不写账户列会落
        # 「debt列-change」退化分支算出负数——夹具必须完全模仿真实写入路径
        heads.append("(%d, '入库', '采购', 'CGRK-ST%d', '%s', %d, %.2f, 105.0, NULL, '1', %d, b'0', '[25]', '[%.1f]', NOW())"
                     % (hid, i, d, organ["id"], change, TENANT, change))
        items.append("(%d, %d, 30, 30, 3.5, 105.0, %d, %d, b'0')"
                     % (hid, kele["id"], depot["id"], TENANT))
    _batch("INSERT IGNORE INTO jsh_depot_head (id, type, sub_type, number, oper_time, organ_id, "
           "change_amount, total_price, debt, status, tenant_id, delete_flag, account_id_list, "
           "account_money_list, create_time) VALUES ", heads)
    _batch("INSERT IGNORE INTO jsh_depot_item (header_id, material_id, oper_number, basic_number, "
           "unit_price, all_price, depot_id, tenant_id, delete_flag) VALUES ", items)
    get_client(force=True).invalidate("")


def probe(llm, n_bills=0):
    erp = get_client(force=True)
    erp.invalidate("")
    out = {}

    sql_cnt = int(_sql_rows("SELECT COUNT(*) FROM jsh_material WHERE tenant_id=%d AND delete_flag=0" % TENANT)[0][0])
    out["P2_商品计数"] = {"sql真实": sql_cnt, "REST materials()": len(erp.materials()),
                          "截断丢失": sql_cnt - len(erp.materials())}

    t0 = time.time()
    row, kind = erp.find_material("压测商品777")
    out["P1_越窗商品命中"] = {"命中": bool(row), "match_kind": kind, "耗时s": round(time.time() - t0, 2)}

    data, note = askmod._master_count({"entity": "商品"}, erp)
    out["P3_主数据计数"] = {"规则直答计数": data.get("count"), "sql真实": sql_cnt,
                            "口径差": sql_cnt - (data.get("count") or 0)}

    s = rp.summary(erp)
    # 合成欠款挂在采购单上=应付方向；真实口径=sql SUM(total-change) vs summary 应付合计
    expect_unpaid = 260 * 60.0
    out["P4_欠款合计(应付)"] = {"真路径未付单应计": round(expect_unpaid, 2),
                                 "summary应付合计": s["debt_pay_total"],
                                 "漏报": round(max(0.0, expect_unpaid - s["debt_pay_total"]), 2)}

    erp._stock_tbl = None
    erp._stock_tbl_at = 0
    t0 = time.time()
    tbl = erp.stock_table()
    n_req = n_bills  # 每单一次 getDetailList
    out["P5_分仓聚合构建"] = {"耗时s": round(time.time() - t0, 2),
                              "单据HTTP请求数": n_req,
                              "均摊ms/单": round((time.time() - t0) * 1000 / max(n_req, 1), 1)}

    if llm:
        t0 = time.time()
        r = askmod.ask("压测商品777还有多少库存", erp)
        out["P6_端到端ask"] = {"耗时s": round(time.time() - t0, 1),
                               "ok": r.get("ok"), "grounded": r.get("grounded"),
                               "answer": str(r.get("answer") or "")[:80]}
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mats", type=int, default=800)
    ap.add_argument("--bills", type=int, default=1200)
    ap.add_argument("--suppliers", type=int, default=120)
    ap.add_argument("--llm", action="store_true", help="含一次真实 LLM 端到端 ask")
    ap.add_argument("--skip-restore", action="store_true")
    ap.add_argument("--skip-insert", action="store_true", help="数据已在库里（上次中断），只补探测")
    args = ap.parse_args()

    print("① 备份当前库（还原保证）+ 还原 seed 基线（自愈上轮中断残留）")
    bk = backup("pre-stress")
    print("   %s" % os.path.relpath(bk, ROOT))
    seed0 = latest_seed_backup()
    if seed0:
        restore(seed0)
    if args.skip_insert:
        print("② 跳过灌数（--skip-insert：沿用上次已灌数据）")
    else:
        print("② 灌入合成数据：商品 %d / 供应商 %d / 单据 %d（本地）" % (args.mats, args.suppliers, args.bills))
        t0 = time.time()
        insert_bulk(args.mats, args.bills, args.suppliers)
        print("   灌入耗时 %.1fs" % (time.time() - t0))
        t0 = time.time()
        made = insert_real_unpaid(260)
        print("   真路径未付单 %d 张（%.0fs）——欠款 ground truth" % (made, time.time() - t0))
    print("③ 探测")
    res = probe(args.llm, args.bills)
    for k, v in res.items():
        print("   %s: %s" % (k, json.dumps(v, ensure_ascii=False)))

    stamp = time.strftime("%Y%m%d-%H%M%S")
    os.makedirs(os.path.join(ROOT, "eval", "results"), exist_ok=True)
    with open(os.path.join(ROOT, "eval", "results", "stress_%s.md" % stamp), "w", encoding="utf-8") as f:
        f.write("# 规模稳定性基准 %s\n\n- 参数：商品 %d / 供应商 %d / 单据 %d · 模型：hy4\n\n```\n" %
                (stamp, args.mats, args.suppliers, args.bills))
        for k, v in res.items():
            f.write("%s: %s\n" % (k, json.dumps(v, ensure_ascii=False)))
        f.write("```\n")
    print("④ 报告 → eval/results/stress_%s.md" % stamp)
    if not args.skip_restore:
        print("⑤ 还原本地库")
        seed = latest_seed_backup()
        if seed:
            restore(seed)
        else:
            print("   ⚠ 没找到 seed-backup-*.sql，请手动还原")
    return 0


if __name__ == "__main__":
    sys.exit(main())
