#!/usr/bin/env python3
"""真实边界 Mock 数据集播种器（仅用于本地 Docker ERP 测试，不触碰生产）。

构造覆盖全链路已知死角的受控测试环境：
1. 期初库存 > 0 且流水净额为负但真实库存为正（复现可乐 -233 → +103 问题）；
2. 仅有期初库存 + 销售出库、零采购单（复现农夫山泉「没有进货记录」问题）；
3. 多仓库存：总仓低于安全线但退货仓有余量使合计看似正常，及零库存仓有安全线；
4. 多单位（瓶 / 箱=12瓶）：用副单位「箱」采购和销售，验证 basic_number 折算；
5. 带乘号/重量规格的商品（维达抽纸 3层*24包、牛皮纸 80g）；
6. 跨月（上月 / 本月 / 昨天 / 今天）采购、销售、采购退货、销售退货单；
7. 2 张未审核草稿单（status='0'），验证 wait_bills=2 且不混入经营额/欠款；
8. 月结赊账 + 部分付款 + 1 张收款核销单（create_payment bill_id），验证欠款扣减与账龄；
9. 高频客户（26 张销售 + 1 张销售退货），验证 query_bills 截断提示与净额；
10. 2 个资金账户（账户1 / 账户2），验证单账户写入后 ERP 与 Talk2Stock 余额 0 误差。

用法：
    python3 eval/seed_realistic.py --yes
"""
import argparse
import os
import subprocess
import sys
import time
from datetime import date, timedelta

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
APP_DIR = os.path.join(ROOT, "app")
sys.path.insert(0, APP_DIR)

from core import config as cfgmod   # noqa: E402
from core import erp as erpmod      # noqa: E402
from core import gate as gatemod    # noqa: E402

MYSQL_CONTAINER = "erp-mysql"
MYSQL_DB = "jsh_erp"


def _sql(sql):
    cmd = ["docker", "exec", "-i", MYSQL_CONTAINER, "mysql",
           "--default-character-set=utf8mb4", "-uroot", "-p123456", MYSQL_DB]
    p = subprocess.run(cmd, input=sql.encode("utf-8"), capture_output=True)
    if p.returncode != 0:
        raise RuntimeError("MySQL 执行失败：%s" % p.stderr.decode("utf-8", "replace")[:200])
    return p.stdout.decode("utf-8", "replace")


def _backup():
    os.makedirs(os.path.join(ROOT, "backups"), exist_ok=True)
    path = os.path.join(ROOT, "backups", "pre-realistic-local-%s.sql" % time.strftime("%Y%m%d-%H%M%S"))
    with open(path, "wb") as f:
        p = subprocess.run(["docker", "exec", MYSQL_CONTAINER, "mysqldump",
                            "--default-character-set=utf8mb4", "-uroot", "-p123456", MYSQL_DB],
                           stdout=f, stderr=subprocess.PIPE)
    if p.returncode != 0:
        raise RuntimeError("备份失败：%s" % p.stderr.decode("utf-8", "replace")[:200])
    return path


def _dt(days_offset, hour=10):
    d = date.today() + timedelta(days=days_offset)
    return f"{d:%Y-%m-%d} {hour:02d}:30:00"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--yes", action="store_true")
    args = ap.parse_args()
    if not args.yes:
        if input("将备份并重置本地 Docker MySQL 为高覆盖测试数据集，继续？(yes/no) ").strip().lower() not in ("y", "yes"):
            return 1

    bk = _backup()
    print("✓ 本地数据库已备份到：%s" % os.path.relpath(bk, ROOT))

    # 定位本地 jsh 租户
    user = str((cfgmod.load().get("erp") or {}).get("user") or "jsh")
    out = _sql(f"SELECT tenant_id FROM jsh_user WHERE login_name='{user}' ORDER BY id LIMIT 1;")
    tenant = int(out.strip().splitlines()[-1])

    # 清理租户业务表 + 重建主数据
    _sql(f"""
        DELETE FROM jsh_account_item WHERE tenant_id={tenant};
        DELETE FROM jsh_account_head WHERE tenant_id={tenant};
        DELETE FROM jsh_depot_item WHERE tenant_id={tenant};
        DELETE FROM jsh_depot_head WHERE tenant_id={tenant};
        DELETE FROM jsh_material_current_stock WHERE tenant_id={tenant};
        DELETE FROM jsh_material_initial_stock WHERE tenant_id={tenant};
        DELETE FROM jsh_material_extend WHERE tenant_id={tenant};
        DELETE FROM jsh_material WHERE tenant_id={tenant};
        DELETE FROM jsh_supplier WHERE tenant_id={tenant};
        UPDATE jsh_depot SET name='总仓', is_default=1, delete_flag='0' WHERE id=14;
        UPDATE jsh_depot SET name='门店仓', is_default=0, delete_flag='0' WHERE id=15;
        UPDATE jsh_depot SET name='退货仓', is_default=0, delete_flag='0' WHERE id=17;
        UPDATE jsh_depot SET delete_flag='1' WHERE tenant_id={tenant} AND id NOT IN (14,15,17);
        UPDATE jsh_account SET initial_amount=1000.00, current_amount=999999.00, delete_flag='0' WHERE id=17;
        UPDATE jsh_account SET initial_amount=500.00, current_amount=-88888.00, delete_flag='0' WHERE id=18;
    """)

    # 归档并重建 audit.jsonl，避免与本地旧记录混淆
    ad = os.path.join(cfgmod.data_dir(), "audit.jsonl")
    if os.path.isfile(ad) and os.path.getsize(ad) > 0:
        os.replace(ad, ad + ".bak-realistic-" + time.strftime("%Y%m%d-%H%M%S"))

    erp = erpmod.get_client(force=True)
    erp.invalidate("")

    # 创建单位（瓶/(箱=12瓶) id=21, 箱 id=22, 卷 id=24 等已存在）
    sup1, _ = erp.ensure_party("美宜佳商贸", "供应商")
    sup2, _ = erp.ensure_party("玖龙纸业", "供应商")
    cus1, _ = erp.ensure_party("永辉超市", "客户")
    cus2, _ = erp.ensure_party("张记便利店", "客户")

    # 建商品（含多单位与特殊规格）
    specs = [
        ("可口可乐 500ml", "瓶", "500ml*24", 21),
        ("农夫山泉 550ml", "瓶", "550ml*24", 21),
        ("维达抽纸 3层*24包", "箱", "3层*24包", 22),
        ("牛皮纸 80g", "卷", "1.2m*100m", 24),
        ("康师傅红烧牛肉面", "箱", "桶装*12", 22),
    ]
    mats = {}
    for nm, u, mo, uid in specs:
        res = erp._req("/material/add", "POST", {
            "name": nm, "model": mo, "standard": "", "unit": u,
            "unitId": uid, "enabled": 1, "categoryId": erp._default_category_id(),
        })
        assert res.get("code") == 200, res
        erp.invalidate("/material/list")
        row, _ = erp.find_material(nm)
        bc = row.get("barCode") or erp.material_barcode(row["id"], u)
        mats[nm] = dict(row, barCode=bc)

    # 写入期初库存 + 安全下限 + 初始 current_stock（完全模拟 jshERP 期初建账）
    # 可乐：总仓期初 336；农夫山泉：总仓期初 540；牛肉面：总仓期初 65（安全线 60），退货仓 9；
    # 维达抽纸：总仓期初 0（安全线 20，验证零库存安全仓不漏报！）
    init_rows = [
        (mats["可口可乐 500ml"]["id"], 14, 336, 126, 2.0),
        (mats["农夫山泉 550ml"]["id"], 14, 540, 204, 1.2),
        (mats["康师傅红烧牛肉面"]["id"], 14, 65, 60, 38.0),
        (mats["康师傅红烧牛肉面"]["id"], 17, 9, 0, 38.0),
        (mats["维达抽纸 3层*24包"]["id"], 14, 0, 20, 42.0),
        (mats["牛皮纸 80g"]["id"], 14, 60, 12, 118.0),
    ]
    for mid, did, qty, low_safe, cost in init_rows:
        _sql(f"""
            INSERT INTO jsh_material_initial_stock (material_id, depot_id, number, low_safe_stock, high_safe_stock, tenant_id, delete_flag)
            VALUES ({mid}, {did}, {qty}, {low_safe}, {max(qty, low_safe * 3)}, {tenant}, '0');
            INSERT INTO jsh_material_current_stock (material_id, depot_id, current_number, current_unit_price, tenant_id, delete_flag)
            VALUES ({mid}, {did}, {qty}, {cost}, {tenant}, '0');
        """)

    def bill(biz, party_row, depot_id, items, d_off, account_id=17, pay_amount=None, pay_type="现付", status="1"):
        rows = []
        for nm, qty, unit, price in items:
            m = mats[nm]
            rows.append({
                "materialId": m["id"], "barCode": m["barCode"], "name": m["name"],
                "operNumber": qty, "unit": unit, "unitPrice": price,
                "allPrice": round(qty * price, 2), "depotId": depot_id, "remark": "",
            })
        r = erp.create_bill(biz, party_row["id"], rows, oper_time=_dt(d_off),
                            account_id=account_id, pay_type=pay_type,
                            pay_amount=pay_amount, status=status)
        assert r["ok"], r
        gatemod.log_write({"model": "seed_realistic", "prompt_version": "test"},
                          {"biz": biz, "party": party_row["supplier"], "depot": "总仓",
                           "items": [{"name": nm, "qty": q, "unit": u, "price": p} for nm, q, u, p in items]},
                          r, source="seed_realistic", raw_text=f"{biz} {party_row['supplier']}")
        return r

    # 上月（-35天，若跨月则为上月）采购与销售
    last_month_off = -(date.today().day + 5)
    # 1) 可乐：采购 10 箱(=120瓶) @ 24元/箱 (240元，账户17付清)；销售 350 瓶 + 40 瓶；销售退货 10 瓶
    #    => 期初 336 + 入 120 + 退 10 - 出 390 = 76 瓶！流水净额 = 130 - 390 = -260（若漏期初必负！）
    bill("purchase_in", sup1, 14, [("可口可乐 500ml", 10, "箱", 24.0)], last_month_off, account_id=17)
    bill("sale_out", cus2, 14, [("可口可乐 500ml", 350, "瓶", 3.0)], last_month_off + 2, account_id=18, pay_amount=500.0, pay_type="月结")
    # 2) 农夫山泉：从未采购！仅期初 540，本月卖 400 瓶 => 剩 140 瓶（流水净额 -400）
    bill("sale_out", cus2, 14, [("农夫山泉 550ml", 400, "瓶", 2.0)], -3, account_id=17, pay_amount=800.0)
    # 3) 本月：可乐再卖 40 瓶（-2天），永辉退回 10 瓶（-1天）；牛肉面总仓卖 10 盒(=箱)使总仓 65->55 < 60（退货仓9使合计64>=60）
    bill("sale_out", cus1, 14, [("可口可乐 500ml", 40, "瓶", 3.0)], -2, account_id=17)
    bill("sale_return", cus1, 14, [("可口可乐 500ml", 10, "瓶", 3.0)], -1, account_id=17)
    bill("sale_out", cus2, 14, [("康师傅红烧牛肉面", 10, "箱", 46.0)], -2, account_id=18, pay_amount=0.0, pay_type="月结")
    # 4) 维达抽纸：采购 15 箱 @ 42 -> 现存 15 < 安全线 20；牛皮纸采购 10 卷再退 2 卷 -> 现存 68
    bill("purchase_in", sup2, 14, [("维达抽纸 3层*24包", 15, "箱", 42.0)], -1, account_id=18, pay_amount=300.0, pay_type="月结")
    bill("purchase_in", sup2, 14, [("牛皮纸 80g", 10, "卷", 118.0)], -1, account_id=17)
    bill("purchase_return", sup2, 14, [("牛皮纸 80g", 2, "卷", 118.0)], 0, account_id=17)

    # 5) 高频客户永辉超市：今天/昨天共补 25 张小额销售单（加前面共 26 张销售 + 1 张销售退货 = 27 张）
    #    其中第一张是部分收款（total 30, pay 10 -> 欠 20），随后用 create_payment 核销 15 -> 剩欠 5
    first_sale = bill("sale_out", cus1, 14, [("可口可乐 500ml", 10, "瓶", 3.0)], 0, account_id=17, pay_amount=10.0, pay_type="月结")
    pay_res = erp.create_payment("收款", cus1["id"], 15.0, target_bill_no=first_sale["number"], account_id=17, remark="部分回款核销")
    assert pay_res["ok"], pay_res

    for i in range(24):
        bill("sale_out", cus1, 14, [("农夫山泉 550ml", 1, "瓶", 2.0)], 0, account_id=17)

    # 6) 2 张未审核草稿单（status='0'）：不计入经营额、不计入欠款、wait_bills 必须等于 2
    bill("sale_out", cus1, 14, [("可口可乐 500ml", 5, "瓶", 3.0)], 0, account_id=17, status="0")
    bill("purchase_in", sup1, 14, [("牛皮纸 80g", 5, "卷", 118.0)], 0, account_id=17, status="0")

    erp.invalidate("")
    print("✓ 高覆盖真实边界 Mock 数据集播种完成（含期初/多仓/多单位/退货/核销/待审核/超20单）。")


if __name__ == "__main__":
    sys.exit(main())
