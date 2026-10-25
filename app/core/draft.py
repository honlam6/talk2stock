#!/usr/bin/env python3
"""统一草稿层：任何入口（AI 解析 / 表单填写）都产出同一种 Draft，
经 resolve() 生成预览（标注哪些主数据将被新建），人工确认后 commit() 写库。

Draft 结构（前后端统一契约）：
{
  "biz": "purchase_in",            # 业务类型
  "party": "美宜佳",                # 往来单位名
  "depot": "原料仓",                # 仓库（可空，按业务取默认）
  "oper_time": "2026-08-01 10:00:00",
  "pay_type": "现付",
  "account_id": 25,
  "remark": "",
  "file_name": "",            # 附件：逗号分隔的相对路径串，如 "bill/a.jpg,bill/b.pdf"
  "items": [{"name":"可乐","qty":3,"unit":"瓶","price":3.5}]
}
"""
import time

from .erp import BILL_TYPES, get_client

BIZ_LABEL = {
    "purchase_in": "采购入库",
    "sale_out": "销售出库",
    "purchase_return": "采购退货",
    "sale_return": "销售退货",
    "other_in": "其他入库",
    "other_out": "其他出库",
}


def new_draft(biz="purchase_in"):
    return {
        "biz": biz, "party": "", "depot": "", "items": [],
        "oper_time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "pay_type": "现付", "account_id": None, "remark": "",
    }


def _num(v, default=0.0):
    try:
        if v in (None, ""):
            return default
        return float(v)
    except Exception:
        return default


def validate(draft):
    """返回错误列表（阻断提交）。"""
    errs = []
    biz = draft.get("biz")
    if biz not in BILL_TYPES:
        errs.append(f"未知业务类型：{biz}")
        return errs
    if not (draft.get("party") or "").strip():
        errs.append(f"缺少{'供应商' if BILL_TYPES[biz][2] == '供应商' else '客户'}名称")
    items = draft.get("items") or []
    if not items:
        errs.append("至少需要一项商品明细")
    for i, it in enumerate(items, 1):
        if not (it.get("name") or "").strip():
            errs.append(f"第{i}行缺商品名称")
        if _num(it.get("qty")) <= 0:
            errs.append(f"第{i}行数量必须大于 0")
        if _num(it.get("price")) < 0:
            errs.append(f"第{i}行单价不能为负")
    return errs


def resolve(draft, erp=None):
    """只读匹配，不写任何数据。返回预览结构，标出 will_create 的主数据。"""
    erp = erp or get_client()
    biz = draft.get("biz", "purchase_in")
    if biz not in BILL_TYPES:
        return {"ok": False, "errors": [f"未知业务类型：{biz}"], "draft": draft}

    btype, sub_type, party_type, direction = BILL_TYPES[biz]
    errors = validate(draft)
    warnings = []
    to_create = []

    # 往来单位
    party_name = (draft.get("party") or "").strip()
    prow, pkind = erp.find_party(party_name, party_type) if party_name else (None, "none")
    party_info = {
        "name": party_name, "type": party_type,
        "id": prow.get("id") if prow else None,
        "matched": bool(prow), "match_kind": pkind,
        "matched_name": prow.get("supplier") if prow else None,
        "will_create": bool(party_name) and not prow,
        # 没匹配上时给 top3 相似候选，确认卡上可点选（点选后重新预览）
        "candidates": erp.party_candidates(party_name, party_type)
                      if (party_name and not prow) else [],
    }
    if party_info["will_create"]:
        to_create.append(f"新建{party_type}：{party_name}")
    if pkind == "fuzzy":
        warnings.append(f"{party_type}「{party_name}」模糊匹配到「{prow.get('supplier')}」，请确认")

    # 仓库
    from . import config as cfgmod
    ui = cfgmod.load()["ui"]
    depot_name = (draft.get("depot") or "").strip() or (
        ui["default_depot_in"] if direction == "in" else ui["default_depot_out"])
    drow, dkind = erp.find_depot(depot_name)
    depot_info = {
        "name": depot_name, "id": drow.get("id") if drow else None,
        "matched": bool(drow), "match_kind": dkind,
        "matched_name": drow.get("name") if drow else None,
        "will_create": not drow,
    }
    if depot_info["will_create"]:
        to_create.append(f"新建仓库：{depot_name}")

    # 商品明细
    items = []
    total = 0.0
    tax_inclusive = erp.price_tax_flag()
    for idx, it in enumerate(draft.get("items") or [], 1):
        name = (it.get("name") or "").strip()
        unit = (it.get("unit") or "个").strip()
        qty = _num(it.get("qty"))
        price = _num(it.get("price"))
        tax_rate = _num(it.get("tax_rate"))
        amount = round(qty * price, 2)
        # 含税/未税：line_total 是该行实际应付金额
        if tax_rate > 0 and tax_inclusive:
            tax_amount = round(amount / (1 + tax_rate / 100.0) * tax_rate / 100.0, 2)
            line_total = amount
        elif tax_rate > 0:
            tax_amount = round(amount * tax_rate / 100.0, 2)
            line_total = round(amount + tax_amount, 2)
        else:
            tax_amount = 0.0
            line_total = amount
        total += line_total
        # 商品匹配：点选过候选（material_id）直达档案，其余按名字匹配。
        # 同名多规格档案返回 ambiguous，绝不静默挑第一个——靠候选点选消歧。
        mrow, mkind = None, "none"
        picked_id = it.get("material_id")
        if picked_id:
            try:
                mrow = erp.material_by_id(int(picked_id))
            except (TypeError, ValueError):
                mrow = None
            if mrow:
                mkind = "picked"
                if name and str(mrow.get("name") or "").strip() != name:
                    warnings.append(f"第{idx}行按点选档案「{mrow.get('name')}」入账（与输入名「{name}」略有出入，请确认）")
            else:
                warnings.append(f"第{idx}行点选的商品档案已不存在，已改回按名称匹配")
        if not mrow:
            mrow, mkind = erp.find_material(name) if name else (None, "none")
        will_create = bool(name) and not mrow and mkind == "none"
        if will_create:
            to_create.append(f"新建商品：{name}（{unit}）")
        if mkind == "ambiguous":
            warnings.append(f"商品「{name}」在系统里有多个同名档案（规格不同），请点选对应规格再确认")
        if mkind == "fuzzy":
            warnings.append(f"商品「{name}」模糊匹配到「{mrow.get('name')}」，请确认")
        price_missing = qty > 0 and price == 0
        if price_missing:
            warnings.append(f"第{idx}行「{name}」单价为 0：原话里可能没说价格，将按 0 元入账；若非赠品请先补单价")

        # 单位换算：用户单位属于该商品的单位组时，按 ratio 折算基本数量
        group = erp.unit_group_by_id(mrow.get("unitId") or mrow.get("unit_id")) if mrow else None
        unit_ok = bool(group) and erp.unit_allowed(group, unit)
        base_qty = round(qty * erp.unit_ratio(group, unit), 6) if unit_ok else None
        if group and not unit_ok and mrow and mrow.get("unit") and erp._norm(unit) != erp._norm(mrow.get("unit")):
            warnings.append(
                f"「{name}」的单位组是「{group.get('name')}」，'{unit}' 不在其中，"
                f"将按基本单位「{mrow.get('unit')}」计 {qty}，请确认")

        row = {
            "name": name, "qty": qty, "unit": unit, "price": price, "amount": amount,
            "tax_rate": tax_rate, "tax_amount": tax_amount, "line_total": line_total,
            "base_qty": base_qty,
            "material_id": mrow.get("id") if mrow else None,
            "matched": bool(mrow), "match_kind": mkind,
            "matched_name": mrow.get("name") if mrow else None,
            "matched_model": (mrow.get("model") or "").strip() if mrow else "",
            "will_create": will_create,
            "price_missing": price_missing,
            "candidates": erp.material_candidates(name) if (name and not mrow) else [],
            "stock": None,
        }
        # 出库业务：查库存给出负库存提醒（按基本数量比较）
        if direction == "out" and mrow and drow:
            st = erp.stock(mrow["id"], drow["id"])
            row["stock"] = st
            cmp_qty = base_qty if base_qty is not None else qty
            if st is not None and _num(st) < cmp_qty:
                warnings.append(f"「{name}」当前库存 {st}，本次出 {cmp_qty}，将产生负库存")
        items.append(row)

    return {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "biz": biz,
        "biz_label": BIZ_LABEL.get(biz, biz),
        "bill_type": btype,
        "sub_type": sub_type,
        "party": party_info,
        "depot": depot_info,
        "items": items,
        "total": round(total, 2),
        "to_create": to_create,
        "oper_time": draft.get("oper_time") or time.strftime("%Y-%m-%d %H:%M:%S"),
        "pay_type": draft.get("pay_type") or ("现付" if direction == "in" else "现收"),
        "remark": draft.get("remark") or "",
        "draft": draft,
    }


def commit(draft, erp=None, auto_create=True):
    """人工确认后执行：先补主数据，再建单。返回结果 + 实际创建了什么。"""
    erp = erp or get_client()
    biz = draft.get("biz", "purchase_in")
    errs = validate(draft)
    if errs:
        return {"ok": False, "message": "；".join(errs), "errors": errs}

    btype, sub_type, party_type, direction = BILL_TYPES[biz]
    created = []

    # 预检：同名多规格且未点选档案的明细先拦下——ensure_material 遇歧义会抛错，
    # 预检能保证此时一行都还没写（半截单/孤儿档案都不留）。
    for it in draft["items"]:
        if it.get("material_id"):
            continue
        _n = (it.get("name") or "").strip()
        if _n:
            _row, _kind = erp.find_material(_n)
            if _kind == "ambiguous":
                return {"ok": False,
                        "message": f"商品「{_n}」有多个同名档案（规格不同），请回预览点选对应规格后再写入"}

    try:
        # 1. 往来单位
        party_name = draft["party"].strip()
        prow, was_new = erp.ensure_party(party_name, party_type)
        if was_new:
            created.append(f"{party_type}「{party_name}」")

        # 2. 仓库
        from . import config as cfgmod
        ui = cfgmod.load()["ui"]
        depot_name = (draft.get("depot") or "").strip() or (
            ui["default_depot_in"] if direction == "in" else ui["default_depot_out"])
        drow, was_new = erp.ensure_depot(depot_name)
        if was_new:
            created.append(f"仓库「{depot_name}」")

        # 3. 商品 + 条码
        rows = []
        for it in draft["items"]:
            name = it["name"].strip()
            unit = (it.get("unit") or "个").strip()
            qty = _num(it.get("qty"))
            price = _num(it.get("price"))
            was_new = False
            mrow = None
            picked_id = it.get("material_id")
            if picked_id:
                # 预览里点选过候选：直达该档案（同名多规格不靠名字猜）
                try:
                    mrow = erp.material_by_id(int(picked_id))
                except (TypeError, ValueError):
                    mrow = None
            if mrow is None:
                mrow, was_new = erp.ensure_material(name, unit)
            else:
                bc = mrow.get("barCode") or erp.material_barcode(
                    mrow["id"], mrow.get("unit") or unit)
                mrow = dict(mrow, barCode=bc)
            if was_new:
                created.append(f"商品「{name}」")
            # 单位：用户单位属于该商品单位组时保留原样，后端按 ratio 自动折算 basicNumber
            use_unit = mrow.get("unit") or unit
            group = erp.unit_group_by_id(mrow.get("unitId") or mrow.get("unit_id"))
            if group and erp.unit_allowed(group, unit):
                use_unit = unit
            tax_rate = _num(it.get("tax_rate"))
            row = {
                "materialId": mrow["id"],
                "barCode": mrow["barCode"],
                "name": mrow.get("name") or name,
                "operNumber": qty,
                "unit": use_unit,
                "unitPrice": price,
                "allPrice": round(qty * price, 2),
                "depotId": drow["id"],
                "remark": it.get("remark") or "",
            }
            if tax_rate > 0:
                row["taxRate"] = tax_rate
            rows.append(row)

        # 4. 建单
        acc = draft.get("account_id") or ui.get("default_account_id") or 25
        # 附件：file_name 为逗号分隔的相对路径串（来自前端上传，已由服务端代理 ERP 拿到）
        file_name = (draft.get("file_name") or "").strip()
        # 本次实付：填了则按实付建单，欠款由 ERP 后端自动算
        pay_amount = draft.get("pay_amount")
        if pay_amount in (None, ""):
            pay_amount = None
        else:
            try:
                pay_amount = float(pay_amount)
            except Exception:
                pay_amount = None
        res = erp.create_bill(
            biz, prow["id"], rows,
            oper_time=draft.get("oper_time"),
            account_id=acc,
            pay_type=draft.get("pay_type") or ("现付" if direction == "in" else "现收"),
            remark=draft.get("remark") or "",
            file_name=file_name,
            pay_amount=pay_amount,
        )
        res["created"] = created
        res["biz_label"] = BIZ_LABEL.get(biz, biz)
        if res["ok"]:
            res["message"] = f"{res['biz_label']} {res['number']} 已写入，合计 {res['total']}"
        return res
    except Exception as e:
        return {"ok": False, "message": str(e), "created": created}
