#!/usr/bin/env python3
"""智能采购建议：确定性补货算法 + 采购入库单草稿。

为什么单开一层：补货量是「算出来的」不是「说出来的」。LLM 擅长解释，不擅长
按口径算数——日报里让模型给建议数量，接地校验只能防编造，防不了算错。这里
把算法写死成纯函数，输入全部来自 ERP 真实数据（只读 SQL + REST 主数据），
输出可被任何人用同一公式复算；LLM 只负责把结果组织成人话。

数据口径（全部只读，经 dbread 的只读事务护栏）：
- 当前库存  jsh_material_current_stock.current_number（分仓，ERP 审核单据后维护）
- 在途采购  采购订单（其它·采购订单，status 1/3）明细 − 已关联入库量
- 近期销量  jsh_depot_item × jsh_depot_head：出库·销售 − 入库·销售退货，按商品汇总
- 采购历史  同上：入库·采购，近 180 天（最近单价/供应商/采购间隔中位数）
- 安全库存  jsh_material_initial_stock.low_safe_stock（每仓下限，跨仓求和）

算法（每个数字都可复算）：
    d = 近 N 天净销量 / N                     日均销量
    L = 该商品采购间隔中位数（样本≥2）否则默认 7 天
    S = 安全库存（跨仓求和；未设 = 0）
    T = d × (L + 覆盖天数) + S                 目标库存
    A = 当前库存 + 在途采购量                   可支配库存
    建议量 = ceil(max(0, T − A))
分级：库存为负 / 周转期都撑不到 / 低于安全线 → urgent；建议量 > 0 → reorder；其余 ok。
护栏：日均销量为 0 的商品不催货（负库存改提示盘点，避免账实不符被当成补货需求）。

写边界：本模块只产出草稿（Draft），绝不直接写库；写入仍走 draft.prepare_bills
的逐单令牌与人工确认闸门。
"""
import math
import threading
import time
import uuid

from . import config as cfgmod
from . import dbread as dbmod
from . import draft as draftmod
from .erp import get_client

PROMPT_VERSION = "procurement-v1-20261123"

DEFAULT_DAYS = 30        # 销量窗口（天）
DEFAULT_COVER_DAYS = 30  # 到货后要覆盖的天数
DEFAULT_LEAD_TIME = 7    # 无历史采购记录时的采购周期（天），结果里标注 assumed

LEVEL_URGENT = "urgent"
LEVEL_REORDER = "reorder"
LEVEL_OK = "ok"


# --------------------------------------------------------------------------
# 只读取数：一次聚合，缺表/缺库整体降级（上层据此走「不可用」分支）
# --------------------------------------------------------------------------
_SQL_STOCK = """
SELECT material_id, depot_id, current_number
FROM jsh_material_current_stock
WHERE delete_flag = '0'
"""

_SQL_SOLD = """
SELECT di.material_id AS material_id,
       SUM(CASE WHEN dh.type = '出库' AND dh.sub_type = '销售' THEN di.basic_number
                WHEN dh.type = '入库' AND dh.sub_type = '销售退货' THEN -di.basic_number
                ELSE 0 END) AS sold
FROM jsh_depot_item di
JOIN jsh_depot_head dh ON dh.id = di.header_id
WHERE dh.delete_flag = '0' AND di.delete_flag = '0'
  AND ((dh.type = '出库' AND dh.sub_type = '销售')
    OR (dh.type = '入库' AND dh.sub_type = '销售退货'))
  AND dh.oper_time >= DATE_SUB(CURDATE(), INTERVAL %d DAY)
GROUP BY di.material_id
"""

_SQL_PURCHASE = """
SELECT di.material_id AS material_id, dh.organ_id AS organ_id,
       dh.oper_time AS oper_time, di.unit_price AS unit_price
FROM jsh_depot_item di
JOIN jsh_depot_head dh ON dh.id = di.header_id
WHERE dh.type = '入库' AND dh.sub_type = '采购'
  AND dh.delete_flag = '0' AND di.delete_flag = '0'
  AND dh.oper_time >= DATE_SUB(CURDATE(), INTERVAL %d DAY)
ORDER BY di.material_id, dh.oper_time
"""

_SQL_SAFE = """
SELECT material_id, SUM(IFNULL(low_safe_stock, 0)) AS safe_stock
FROM jsh_material_initial_stock
WHERE delete_flag = '0'
GROUP BY material_id
"""

# 在途采购：已审核但未入库的采购订单明细，扣除已关联入库的数量。
# status 语义见 jsh_depot_head DDL：0未审核、1已审核、2完成采购、3部分采购。
# 入库单明细的 link_id 指回订单明细 id；没有 link_id 的老数据按未入库计
# （保守：宁可少扣，也不把已到货的量当成在途）。
_SQL_PENDING = """
SELECT oi.material_id AS material_id,
       SUM(GREATEST(IFNULL(oi.basic_number, oi.oper_number) - IFNULL(done.qty, 0), 0)) AS pending
FROM jsh_depot_item oi
JOIN jsh_depot_head oh ON oh.id = oi.header_id
LEFT JOIN (
    SELECT di.link_id AS link_id, SUM(IFNULL(di.basic_number, di.oper_number)) AS qty
    FROM jsh_depot_item di
    JOIN jsh_depot_head dh ON dh.id = di.header_id
    WHERE di.link_id IS NOT NULL AND di.delete_flag = '0' AND dh.delete_flag = '0'
      AND dh.type = '入库' AND dh.sub_type = '采购'
    GROUP BY di.link_id
) done ON done.link_id = oi.id
WHERE oh.type = '其它' AND oh.sub_type = '采购订单'
  AND oh.delete_flag = '0' AND oi.delete_flag = '0'
  AND oh.status IN ('1', '3')
GROUP BY oi.material_id
"""

_SQL_SUPPLIER = """
SELECT id, supplier FROM jsh_supplier WHERE delete_flag = '0'
"""


def _q(sql, limit=None):
    """只读查询（dbread 的护栏全部生效）。失败抛异常，由 fetch 统一捕获。"""
    res = dbmod._run(sql, limit=limit or 2000)
    return res["rows"]


def _to_float(v, default=0.0):
    try:
        if v in (None, ""):
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def _median(nums):
    if not nums:
        return None
    s = sorted(nums)
    n = len(s)
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2.0


def _parse_dt(s):
    """oper_time 形如 '2026-11-01 10:00:00'（mysql --batch 原样输出）。"""
    t = str(s or "").strip()[:19]
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return time.mktime(time.strptime(t, fmt))
        except Exception:
            continue
    return None


def _days_between(a, b):
    ta, tb = _parse_dt(a), _parse_dt(b)
    if ta is None or tb is None:
        return None
    return abs(tb - ta) / 86400.0


# --------------------------------------------------------------------------
# 取数 + 计算
# --------------------------------------------------------------------------
def _cfg():
    conf = (cfgmod.load() or {}).get("procurement") or {}
    return {
        "days": int(conf.get("days") or DEFAULT_DAYS),
        "cover_days": int(conf.get("cover_days") or DEFAULT_COVER_DAYS),
        "lead_time_days": int(conf.get("lead_time_days") or DEFAULT_LEAD_TIME),
    }


def _materials(erp):
    """商品主数据（REST，带缓存）：id → name/unit/model 等。停用/软删已被 ERP 过滤。"""
    out = {}
    for m in erp.materials() or []:
        try:
            mid = int(m.get("id"))
        except (TypeError, ValueError):
            continue
        out[mid] = {
            "name": str(m.get("name") or "").strip(),
            "model": str(m.get("model") or "").strip(),
            "unit": str(m.get("unit") or "").strip(),
        }
    return out


def compute(erp, days=None, cover_days=None, lead_time_default=None, limit=30):
    """核心计算（纯函数式：所有外部数据一次性取齐后在这里算）。

    返回 {ok, generated_at, params, evidence, elapsed_ms, rows, totals, assumptions}；
    rows 每行都带完整推导字段，前端展示与任何人复核用同一份数。
    """
    cfg = _cfg()
    days = int(days or cfg["days"])
    cover_days = int(cover_days or cfg["cover_days"])
    lead_default = int(lead_time_default or cfg["lead_time_days"])
    started = time.time()

    mats = _materials(erp)
    if not mats:
        return {"ok": False, "message": "商品主数据为空或 ERP 不可达，无法生成采购建议"}

    try:
        stock_rows = _q(_SQL_STOCK)
        sold_rows = _q(_SQL_SOLD % days)
        purchase_rows = _q(_SQL_PURCHASE % 180, limit=5000)
        safe_rows = _q(_SQL_SAFE)
        supplier_rows = _q(_SQL_SUPPLIER)
    except Exception as e:
        return {"ok": False,
                "message": "经营数据查询失败（%s）——采购建议需要只读数据库通道" % str(e)[:120]}

    # 在途采购单独降级：订单表/字段在旧版本可能缺失，失败不拖垮整份建议，
    # 但必须让调用方看见「这批数没算在途」（assumptions 里标注）。
    try:
        pending_rows = _q(_SQL_PENDING, limit=5000)
        pending_ok = True
    except Exception:
        pending_rows, pending_ok = [], False

    # ---- 当前库存（分仓明细 + 合计）
    stock_by_mat, depot_of_mat = {}, {}
    for r in stock_rows:
        try:
            mid = int(r.get("material_id"))
        except (TypeError, ValueError):
            continue
        qty = _to_float(r.get("current_number"))
        stock_by_mat[mid] = stock_by_mat.get(mid, 0.0) + qty
        depot_of_mat.setdefault(mid, []).append(
            {"depot_id": r.get("depot_id"), "qty": round(qty, 2)})

    # ---- 近 N 天净销量
    sold_by_mat = {}
    for r in sold_rows:
        try:
            mid = int(r.get("material_id"))
        except (TypeError, ValueError):
            continue
        sold_by_mat[mid] = sold_by_mat.get(mid, 0.0) + _to_float(r.get("sold"))

    # ---- 安全库存（跨仓求和）
    safe_by_mat = {}
    for r in safe_rows:
        try:
            mid = int(r.get("material_id"))
        except (TypeError, ValueError):
            continue
        safe_by_mat[mid] = _to_float(r.get("safe_stock"))

    # ---- 在途采购（未入库订单量）
    pending_by_mat = {}
    for r in pending_rows:
        try:
            mid = int(r.get("material_id"))
        except (TypeError, ValueError):
            continue
        pending_by_mat[mid] = pending_by_mat.get(mid, 0.0) + _to_float(r.get("pending"))

    # ---- 采购历史：按商品聚合（最近日期/最近单价/供应商/间隔中位数）
    sup_name = {}
    for r in supplier_rows:
        try:
            sup_name[int(r.get("id"))] = str(r.get("supplier") or "").strip()
        except (TypeError, ValueError):
            continue
    hist = {}
    for r in purchase_rows:
        try:
            mid = int(r.get("material_id"))
        except (TypeError, ValueError):
            continue
        h = hist.setdefault(mid, {"times": [], "last_price": None,
                                  "last_time": None, "supplier": ""})
        t = str(r.get("oper_time") or "")
        h["times"].append(t)
        if t and (h["last_time"] is None or t > h["last_time"]):
            h["last_time"] = t
            pr = _to_float(r.get("unit_price"))
            if pr > 0:
                h["last_price"] = pr
            try:
                h["supplier"] = sup_name.get(int(r.get("organ_id")), "") or h["supplier"]
            except (TypeError, ValueError):
                pass

    assumptions = []
    rows = []
    for mid, meta in mats.items():
        stock = round(stock_by_mat.get(mid, 0.0), 2)
        sold = round(sold_by_mat.get(mid, 0.0), 2)
        daily = round(sold / days, 4) if days > 0 else 0.0
        safe = round(safe_by_mat.get(mid, 0.0), 2)
        h = hist.get(mid) or {}
        times = sorted(t for t in h.get("times") or [] if t)
        gaps = []
        for a, b in zip(times, times[1:]):
            gap = _days_between(a, b)
            if gap is not None and gap > 0:
                gaps.append(gap)
        med = _median(gaps)
        if med is not None:
            lead = max(1, int(round(med)))
            lead_src = "history"
        else:
            lead = lead_default
            lead_src = "default"

        target = round(daily * (lead + cover_days) + safe, 2)
        pending = round(pending_by_mat.get(mid, 0.0), 2)
        available = round(stock + pending, 2)
        if daily <= 0:
            # 无需求不催货；负库存是账实问题，不是采购需求
            suggest = 0
            level = LEVEL_URGENT if stock < 0 else LEVEL_OK
        else:
            need = max(0.0, target - available)
            suggest = int(math.ceil(need)) if need > 0 else 0
            if stock < 0 or available < daily * lead or (safe > 0 and available <= safe):
                level = LEVEL_URGENT
            elif suggest > 0:
                level = LEVEL_REORDER
            else:
                level = LEVEL_OK

        days_cover = round(available / daily, 1) if daily > 0 else None
        reason = _reason(stock, daily, safe, lead, cover_days, lead_src, days_cover,
                         suggest, level, pending=pending)
        if lead_src == "default" and daily > 0:
            assumptions.append("%s：无历史采购记录，采购周期按默认 %d 天计" % (meta["name"], lead))
        if safe <= 0 and daily > 0:
            assumptions.append("%s：未设安全库存，按 0 计" % meta["name"])

        rows.append({
            "material_id": mid,
            "name": meta["name"],
            "model": meta["model"],
            "unit": meta["unit"],
            "stock": stock,
            "pending_qty": pending,
            "available": available,
            "by_depot": depot_of_mat.get(mid, []),
            "sold": sold,
            "days": days,
            "daily": daily,
            "lead_time_days": lead,
            "lead_time_source": lead_src,
            "safe_stock": safe,
            "target": target,
            "suggest_qty": suggest,
            "level": level,
            "days_cover": days_cover,
            "supplier": h.get("supplier") or "",
            "last_price": h.get("last_price"),
            "last_purchase_at": (h.get("last_time") or "")[:10],
            "reason": reason,
        })

    order = {LEVEL_URGENT: 0, LEVEL_REORDER: 1, LEVEL_OK: 2}
    rows.sort(key=lambda r: (order.get(r["level"], 9), -r["suggest_qty"], r["name"]))

    # 取证清单：本次计算真实读了哪些源、各多少条（前端展示「Agent 取证」用，
    # 不是装饰——行数就是查询返回的记录数）
    evidence = [
        {"label": "当前库存", "table": "jsh_material_current_stock", "rows": len(stock_rows)},
        {"label": "在途采购（未入库订单）",
         "table": "jsh_depot_item × jsh_depot_head（采购订单）", "rows": len(pending_rows)},
        {"label": "近 %d 天销售单" % days,
         "table": "jsh_depot_item × jsh_depot_head", "rows": len(sold_rows)},
        {"label": "采购历史 180 天",
         "table": "jsh_depot_item × jsh_depot_head", "rows": len(purchase_rows)},
        {"label": "安全库存", "table": "jsh_material_initial_stock", "rows": len(safe_rows)},
    ]
    if not pending_ok:
        assumptions.append("在途采购查询不可用：建议量未扣减已下单未入库的采购订单，"
                           "下单前请人工核对在途量")
    todo = [r for r in rows if r["suggest_qty"] > 0]
    return {
        "ok": True,
        "generated_at": time.strftime("%Y-%m-%d %H:%M"),
        "params": {"days": days, "cover_days": cover_days,
                   "lead_time_default": lead_default},
        "evidence": evidence,
        "elapsed_ms": int((time.time() - started) * 1000),
        "rows": rows[:limit],
        "totals": {"kinds": len(rows), "urgent": sum(1 for r in rows if r["level"] == LEVEL_URGENT),
                   "reorder": sum(1 for r in rows if r["level"] == LEVEL_REORDER),
                   "to_buy": len(todo)},
        "assumptions": assumptions[:8],
    }


def _reason(stock, daily, safe, lead, cover_days, lead_src, days_cover, suggest, level,
            pending=0.0):
    """确定性理由句：只用本行已有的数字，前端与日报直接引用。"""
    if daily <= 0:
        if stock < 0:
            return "当前库存为负且近期无销量，账实可能不符，建议先盘点核实。"
        return "近期无销售记录，暂不建议采购。"
    # 有在途时把「可支配」说清楚，否则用户看到 0 库存会误以为算法漏算
    shown = ("%g（在途 %g，可支配 %g）" % (stock, pending, stock + pending)
             if pending > 0 else "%g" % stock)
    cover = "，约可支撑 %.1f 天" % days_cover if days_cover is not None else ""
    if level == LEVEL_URGENT:
        return ("当前库存 %s%s，低于周转需求（日均 %g × 采购周期 %d 天%s），"
                "建议立即补 %d，否则可能断货。" % (
                    shown, cover, daily, lead,
                    "，按历史采购间隔" if lead_src == "history" else "，按默认周期", suggest))
    if level == LEVEL_REORDER:
        return ("当前库存 %s%s，按日均 %g、覆盖 %d 天+安全库存 %g 计，"
                "建议补 %d 维持周转。" % (shown, cover, daily, cover_days, safe, suggest))
    return ("当前库存 %s%s，可覆盖预期需求，暂不采购。" % (shown, cover))


_res_cache = {"at": 0.0, "key": None, "data": None}
_res_lock = threading.Lock()


def suggest(erp=None, days=None, cover_days=None, limit=30, max_age=30):
    """对外入口：30s 缓存（看板/对话/草稿三处共享同一批数，展示与草稿同源）。
    写入单据后由 server 调 invalidate() 清空。"""
    key = (int(days or 0), int(cover_days or 0), int(limit))
    now = time.time()
    with _res_lock:
        if _res_cache["data"] and _res_cache["key"] == key \
                and now - _res_cache["at"] < max_age:
            return _res_cache["data"]
    try:
        erp = erp or get_client()
        out = compute(erp, days=days, cover_days=cover_days, limit=limit)
    except Exception as e:
        return {"ok": False, "message": "采购建议生成失败：%s" % str(e)[:120]}
    if out.get("ok"):
        with _res_lock:
            _res_cache.update({"at": time.time(), "key": key, "data": out})
    return out


def invalidate():
    """清采购建议与安全库存缓存（单据写入后由 server 调用）。"""
    with _res_lock:
        _res_cache.update({"at": 0.0, "key": None, "data": None})
    _safe_cache.update({"at": 0.0, "data": {}})


# 安全库存映射缓存：库存预警口径统一用 material_initial_stock.low_safe_stock
# （数量），不再用 material/list 的 lowDecimal（那是「最低售价」，价格字段）。
_safe_cache = {"at": 0.0, "data": {}}


def safe_stock_map(erp=None, max_age=60):
    """{material_id: 最低库存数量}——跨仓求和。SQL 不可达时返回上次缓存/空，
    绝不因库存预警把问答或看板拖进超时。"""
    now = time.time()
    if _safe_cache["data"] and now - _safe_cache["at"] < max_age:
        return _safe_cache["data"]
    try:
        rows = _q(_SQL_SAFE)
    except Exception:
        _safe_cache["at"] = now - max_age + 10   # 失败只缓存 10 秒，避免每次请求都吃超时
        return _safe_cache["data"]
    out = {}
    for r in rows:
        try:
            out[int(r.get("material_id"))] = _to_float(r.get("safe_stock"))
        except (TypeError, ValueError):
            continue
    _safe_cache.update({"at": now, "data": out})
    return out


# --------------------------------------------------------------------------
# 草稿：按供应商分组 → 逐单令牌 → 人工确认（与录单同一套闸门）
# --------------------------------------------------------------------------
def to_drafts(result, erp=None, only=None):
    """把建议行（默认 urgent+reorder 且有建议量）转成采购入库单草稿。

    only: 可选的 material_id 列表（前端勾选哪些商品）。
    返回 {ok, bills, skipped, batch_id, message}——bills 带逐单 confirm_token，
    绝不写库。供应商缺失的行进 skipped，由用户在确认卡里手填，不猜供应商。
    """
    erp = erp or get_client()
    if not isinstance(result, dict) or not result.get("ok"):
        return {"ok": False, "bills": [], "skipped": [],
                "message": (result or {}).get("message") or "采购建议不可用"}
    rows = result.get("rows") or []
    if only is not None:
        wanted = set()
        for x in only:
            try:
                wanted.add(int(x))
            except (TypeError, ValueError):
                continue
        rows = [r for r in rows if r["material_id"] in wanted]
    picked = [r for r in rows if r.get("suggest_qty", 0) > 0]
    if not picked:
        return {"ok": False, "bills": [], "skipped": [], "message": "没有需要采购的商品"}

    groups, skipped = {}, []
    for r in picked:
        party = (r.get("supplier") or "").strip()
        if not party:
            skipped.append({"name": r["name"], "qty": r["suggest_qty"], "unit": r["unit"],
                            "reason": "无历史采购记录，未确定供应商"})
            continue
        groups.setdefault(party, []).append(r)

    if not groups:
        return {"ok": False, "bills": [], "skipped": skipped,
                "message": "所有待采购商品都缺少供应商记录，请先在确认卡里选择供应商"}

    drafts = []
    for party, items in groups.items():
        drafts.append({
            "biz": "purchase_in",
            "party": party,
            "depot": "",
            "oper_time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "pay_type": "现付",
            "account_id": None,
            "remark": "AI 采购建议（按销量/库存/采购周期计算）",
            "items": [{"name": r["name"], "qty": r["suggest_qty"], "unit": r["unit"],
                       "price": r.get("last_price") or 0} for r in items],
        })

    batch_id = uuid.uuid4().hex[:12]
    meta = {
        "model": "", "profile": "", "confidence": "high",
        "note": "采购建议按确定性算法生成，数量可复算",
        "problems": [],
        "bills": [{"confidence": "high", "note": "", "problems": []} for _ in drafts],
        "count": len(drafts),
    }
    raw = "；".join("%s %s" % (d["party"], "、".join(
        "%s×%s" % (it["name"], it["qty"]) for it in d["items"])) for d in drafts)
    bills = draftmod.prepare_bills(drafts, meta, erp, "procurement", raw, batch_id)
    return {"ok": True, "bills": bills, "skipped": skipped, "batch_id": batch_id,
            "message": "已生成 %d 张采购草稿，确认后才会写入" % len(bills)}


# --------------------------------------------------------------------------
# agent 工具（对话入口）：紧凑观察 + 模板句
# --------------------------------------------------------------------------
def tool_suggest(erp, args=None):
    """ask 工具箱用：返回 (data, note)。data 保留前 10 行供溯源，note 只用算出的数。"""
    args = args or {}
    try:
        days = int(args.get("days") or 0) or None
    except (TypeError, ValueError):
        days = None
    res = suggest(erp, days=days, limit=30)
    if not res.get("ok"):
        return {"error": res.get("message")}, None
    rows = res["rows"]
    compact = [{"name": r["name"], "stock": r["stock"],
                "pending_qty": r.get("pending_qty", 0), "available": r.get("available"),
                "sold": r["sold"],
                "unit": r["unit"], "daily": r["daily"],
                "lead_time_days": r["lead_time_days"],
                "safe_stock": r["safe_stock"], "suggest_qty": r["suggest_qty"],
                "level": r["level"], "reason": r["reason"],
                "supplier": r["supplier"], "last_price": r["last_price"]}
               for r in rows[:10]]
    t = res["totals"]
    note = ("按近 %d 天销量、库存与采购周期计算：%d 种商品需要采购（%d 种紧急）。"
            % (res["params"]["days"], t["to_buy"], t["urgent"]))
    if compact:
        note += "优先看：" + "、".join(
            "%s 建议 %d%s" % (r["name"], r["suggest_qty"], r["unit"] or "")
            for r in compact[:4] if r["suggest_qty"] > 0)
    data = {"params": res["params"], "totals": t, "rows": compact,
            "assumptions": res["assumptions"],
            "note": "建议数量由确定性算法算出；生成采购草稿需用户在前端确认"}
    return data, note
