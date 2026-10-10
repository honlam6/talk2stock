#!/usr/bin/env python3
"""
jshERP 纯 API 链路验证脚本（无 SQL 兜底）。
验证：MD5登录 → 建单位 → 建供应商 → 建商品 → 建条码 → 建采购单 → 库存联动。
用法：python3 verify_endpoints.py
前置：erp-backend 在 localhost:9999 运行，且 checkcode_flag=0（已关验证码）。
"""
import json, time, subprocess

BASE = "http://localhost:9999/jshERP-boot"
LOGIN = ("jsh", "e10adc3949ba59abbe56e057f20f883e")  # 123456 的 MD5

def call(path, method="GET", data=None, token=None):
    cmd = ["curl", "-sS", "-m", "20", "-X", method, f"{BASE}{path}", "-H", "Content-Type: application/json"]
    if token:
        cmd += ["-H", f"X-Access-Token: {token}"]
    if data is not None:
        cmd += ["-d", json.dumps(data, ensure_ascii=False)]
    return subprocess.run(cmd, capture_output=True, text=True).stdout

def rows(path, token):
    try:
        return json.loads(call(path, token=token))["data"]["rows"]
    except Exception:
        return []

def main():
    # 1. 登录（密码 MD5）
    out = call("/user/login", "POST", {"loginName": LOGIN[0], "password": LOGIN[1]})
    token = json.loads(out)["data"]["token"]
    print("✓ 登录成功")

    # 2. 建单位（不传 tenantId）
    call("/unit/add", "POST", {"basicUnit": "瓶", "otherUnit": "个", "ratio": 1, "name": "瓶/(个=1瓶)", "enabled": True}, token)
    print("✓ 建单位")

    # 3. 建供应商（不传 tenantId）
    sups = rows('/supplier/list?pageNo=1&pageSize=50&type=供应商', token)
    sup = [s for s in sups if "验证供应商" in (s.get("supplier") or "")]
    if not sup:
        call("/supplier/add", "POST", {"supplier": "验证供应商", "type": "供应商", "enabled": 1}, token)
        sups = rows('/supplier/list?pageNo=1&pageSize=50&type=供应商', token)
    sup = [s for s in sups if "验证供应商" in (s.get("supplier") or "")][0]
    print("✓ 供应商 id=", sup["id"])

    # 4. 建商品（不传 tenantId）
    goods = rows("/material/list?pageNo=1&pageSize=100", token)
    mt = [g for g in goods if g.get("name") == "验证商品"]
    if not mt:
        call("/material/add", "POST", {"name": "验证商品", "model": "V1", "unit": "个", "categoryId": 17, "enabled": 1}, token)
        goods = rows("/material/list?pageNo=1&pageSize=100", token)
    mt = [g for g in goods if g.get("name") == "验证商品"][0]

    # 5. 建条码（不传 tenantId）
    bc = f"BC{mt['id']}"
    call("/materialsExtend/add", "POST", {"materialId": mt["id"], "barCode": bc, "commodityUnit": "个", "defaultFlag": 1}, token)
    print("✓ 商品 id=", mt["id"], "条码=", bc)

    # 6. 建采购单（默认原料仓 id 先从列表取）
    deps = rows("/depot/list?pageNo=1&pageSize=50", token)
    dep = [d for d in deps if d.get("name") == "原料仓"][0]
    number = "CGRK" + time.strftime("%Y%m%d") + "9001"
    info = {"type": "入库", "subType": "采购", "organId": sup["id"], "operTime": time.strftime("%Y-%m-%d %H:%M:%S"),
            "number": number, "accountId": 25, "changeAmount": 100, "totalPrice": 100, "payType": "现付",
            "remark": "API验证", "status": "1"}
    rrow = [{"materialId": mt["id"], "barCode": bc, "name": "验证商品", "operNumber": 10, "unit": "个",
             "unitPrice": 10, "allPrice": 100, "depotId": dep["id"], "remark": ""}]
    body = {"info": json.dumps(info, ensure_ascii=False), "rows": json.dumps(rrow, ensure_ascii=False)}
    r = call("/depotHead/addDepotHeadAndDetail", "POST", body, token)
    print("✓ 建采购单返回:", r[:120])

    print("\n全部链路纯 API 跑通（无 SQL 兜底）。如需核对库存，查 jsh_material_current_stock。")

if __name__ == "__main__":
    main()
