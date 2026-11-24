#!/usr/bin/env python3
"""反向单据（红冲/退货）草稿：把已写入的单据翻成方向相反的新单。

为什么是「新建」不是「改单」：系统的写入边界只有一条——只能新增，不能修改
或删除 ERP 已有单据。客户退货、采购退回、录错冲销都落在这条边界内：
生成一张反向草稿，走同一套人工确认 + 一次性令牌，确认后才写库。
原单保持不动，账目上由正负两笔自然抵消，审计链完整。

对调规则：
    采购入库 ↔ 采购退货     销售出库 ↔ 销售退货
其它类型（其它入库/出库）没有对应的红字业务，直接拒绝，不猜。
"""
import time

from .draft import BIZ_LABEL

REVERSE_OF = {
    "purchase_in": "purchase_return",
    "purchase_return": "purchase_in",
    "sale_out": "sale_return",
    "sale_return": "sale_out",
}

REMARK_MAX = 900


def _num(v, default=0.0):
    try:
        if v in (None, ""):
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def build(draft, bill_no="", reason="退货/冲销"):
    """返回 {ok, draft, message}。纯函数，不碰 ERP、不写库。"""
    draft = draft or {}
    biz = str(draft.get("biz") or "")
    rb = REVERSE_OF.get(biz)
    if not rb:
        return {"ok": False, "message": "该业务类型没有对应的反向单据（仅支持采购/销售的出入库与退货）"}

    items = []
    for it in draft.get("items") or []:
        name = str(it.get("name") or "").strip()
        qty = _num(it.get("qty"))
        if not name or qty <= 0:
            continue
        items.append({"name": name, "qty": qty,
                      "unit": it.get("unit") or "",
                      "price": _num(it.get("price"))})
    if not items:
        return {"ok": False, "message": "原单没有可冲销的商品明细（名称或数量为空）"}

    note = "%s：原单 %s" % (str(reason or "退货/冲销"), bill_no or "未提供单号")
    out = {
        "biz": rb,
        "party": draft.get("party") or "",
        "depot": draft.get("depot") or "",
        "oper_time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "pay_type": draft.get("pay_type") or "",
        "account_id": draft.get("account_id"),
        "remark": note[:REMARK_MAX],
        "items": items,
    }
    return {"ok": True, "draft": out,
            "message": "已生成%s草稿（原单 %s），确认后才会写入" % (
                BIZ_LABEL.get(rb, rb), bill_no or "未提供单号")}
