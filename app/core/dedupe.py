#!/usr/bin/env python3
"""业务防重：确认卡出卡前，检查近 N 分钟是否已有相似单据。

为什么 Nonce 不够：确认令牌只防「同一张卡片被点两次」，防不了业务层重复——
两个业务员先后口述同一笔「给张记发 100 箱方便面」，两次都会解析成功、各自
拿到新令牌、各自写进 ERP，最后是两笔应收和两笔出库。

判定口径（保守，宁可漏报也不误伤）：
- 同业务类型（采购入库/销售出库/采购退货/销售退货）才比较；
- 往来单位归一后相同（任一方为空则不判）；
- 商品名归一后至少一条相同，且数量完全一致或相差 ≤10%；
- 窗口默认 30 分钟，取 h.create_time（入库时间，不受用户手填 oper_time 影响）。

输出只做提示：不阻断提交、不改草稿，最终仍由人在确认卡上决定。
数据库不可达时返回 {hit: False, skipped: True}，绝不让防重把录单拖慢或拖挂。
"""
import threading
import time

from . import dbread as dbmod
from .erp import BILL_TYPES, _norm_text

WINDOW_MINUTES = 30
_CACHE_TTL = 45          # 同一业务类型+往来单位 45 秒内复用查询结果（出卡瞬间多次 resolve）
_CACHE_MAX = 32
_cache = {}              # key -> (at, rows)
_lock = threading.Lock()

_SQL = """
SELECT h.number AS number, h.create_time AS create_time, h.oper_time AS oper_time,
       h.organ_id AS organ_id,
       m.name AS material_name,
       i.basic_number AS basic_number, i.oper_number AS oper_number
FROM jsh_depot_head h
JOIN jsh_depot_item i ON i.header_id = h.id
LEFT JOIN jsh_material m ON m.id = i.material_id
WHERE h.delete_flag = '0' AND i.delete_flag = '0'
  AND h.tenant_id = %s
  AND h.type = '%s' AND h.sub_type = '%s'
  AND h.organ_id = %d
  AND h.create_time >= DATE_SUB(NOW(), INTERVAL %d MINUTE)
ORDER BY h.create_time DESC
"""


def _q(sql):
    res = dbmod._run(sql, limit=500)
    return res["rows"]


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _qty_close(a, b):
    """数量一致或相差 ≤10%（同一笔业务被拆行/改包装时留的余量）。"""
    if a == b:
        return True
    base = max(abs(a), abs(b))
    return base > 0 and abs(a - b) / base <= 0.10


def _recent_rows(btype, sub_type, window, organ_id):
    key = (btype, sub_type, window, organ_id)
    now = time.time()
    with _lock:
        hit = _cache.get(key)
        if hit is not None and now - hit[0] < _CACHE_TTL:
            return hit[1]
    from . import ledger as ledgermod
    rows = _q(_SQL % (ledgermod.tenant_subquery(),
                      btype.replace("'", "''"), sub_type.replace("'", "''"),
                      int(organ_id), int(window)))
    with _lock:
        if len(_cache) >= _CACHE_MAX:
            _cache.clear()
        _cache[key] = (now, rows)
    return rows


def fingerprint(draft):
    """草稿指纹：往来单位 + 商品名/数量（归一后），供日志与前端调试。"""
    party = _norm_text(draft.get("party") or "")
    items = sorted(
        (_norm_text(it.get("name") or ""), round(_num(it.get("qty")), 4))
        for it in (draft.get("items") or []))
    return {"party": party, "items": items}


def check(draft, organ_id=None, window_minutes=WINDOW_MINUTES):
    """返回 {hit, window_minutes, matches, skipped}。永远不抛异常。

    organ_id：预览 resolve 出来的往来单位 id（必填）。按 id 精确匹配，
    不按名字——jshERP 的客户/供应商分散在 jsh_user / jsh_supplier，join 表名
    在不同版本不一致，按名字反而容易漏判或误判。
    """
    try:
        biz = (draft or {}).get("biz") or ""
        if biz not in ("purchase_in", "sale_out", "purchase_return", "sale_return"):
            return {"hit": False, "skipped": True, "reason": "该业务类型不做防重"}
        try:
            organ = int(organ_id)
        except (TypeError, ValueError):
            return {"hit": False, "skipped": True, "reason": "缺少往来单位ID（新单位不判重）"}
        party = str(draft.get("party") or "").strip()
        items = [(n, round(_num(it.get("qty")), 4)) for it in (draft.get("items") or [])
                 for n in [_norm_text(it.get("name") or "")] if n]
        if not items:
            return {"hit": False, "skipped": True, "reason": "草稿缺少商品"}

        btype, sub_type, _, _ = BILL_TYPES[biz]
        rows = _recent_rows(btype, sub_type, window_minutes, organ)

        groups = {}
        for r in rows:
            groups.setdefault(str(r.get("number") or ""), {
                "number": r.get("number") or "",
                "time": str(r.get("create_time") or r.get("oper_time") or "")[:19],
                "party": party,
                "items": [],
            })["items"].append({
                "name": r.get("material_name") or "",
                "qty": round(_num(r.get("basic_number") or r.get("oper_number")), 4),
            })

        matches = []
        for g in groups.values():
            hits = []
            for ri in g["items"]:
                rn, rq = ri["name"], ri["qty"]
                norm = _norm_text(rn)
                for dn, dq in items:
                    if norm and norm == dn and _qty_close(rq, dq):
                        hits.append({"name": rn, "qty": rq})
                        break
            if hits:
                g["hits"] = hits
                matches.append(g)
        return {"hit": bool(matches), "window_minutes": int(window_minutes),
                "matches": matches[:3], "skipped": False}
    except Exception:
        return {"hit": False, "skipped": True, "reason": "防重查询不可用"}


def invalidate():
    """写入成功后清缓存，避免刚提交的单据在下一轮出卡时被当成历史重复。"""
    with _lock:
        _cache.clear()
