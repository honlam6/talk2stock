#!/usr/bin/env python3
"""看板数据聚合。全部基于 core.erp 的只读接口，不改任何数据。

单据字段（实测）：type/subType/number/operTime/organName/totalPrice/
changeAmount/debt/status/materialsList/materialCount/accountName
"""
import time
import urllib.parse
from collections import defaultdict

from .erp import get_client

# 计入"采购/销售"口径的子类型
BUY_SUBS = {"采购"}
BUY_BACK_SUBS = {"采购退货"}
SELL_SUBS = {"销售", "零售"}
SELL_BACK_SUBS = {"销售退货", "零售退货"}


def _f(v):
    try:
        return float(v or 0)
    except Exception:
        return 0.0


def _day(bill):
    """单据日期 YYYY-MM-DD。operTime 形如 2026-08-01T15:58:50.000+0000"""
    s = str(bill.get("operTime") or bill.get("operTimeStr") or "")
    if "T" in s:
        return s.split("T")[0]
    return s[:10]


def summary(erp=None, bills=None):
    """核心经营指标。bills 可传入已取好的单据列表（避免重复查询）。"""
    erp = erp or get_client()
    bills = bills if bills is not None else erp.all_bills()

    today = time.strftime("%Y-%m-%d")
    month = time.strftime("%Y-%m")

    acc = {
        "today": {"buy": 0.0, "sell": 0.0, "buy_n": 0, "sell_n": 0},
        "month": {"buy": 0.0, "sell": 0.0, "buy_n": 0, "sell_n": 0},
        "all": {"buy": 0.0, "sell": 0.0},
    }
    debt_pay = defaultdict(float)   # 我欠供应商（应付）
    debt_recv = defaultdict(float)  # 客户欠我（应收）

    for b in bills:
        if str(b.get("deleteFlag")) == "1":
            continue
        st = b.get("subType") or ""
        amt = _f(b.get("totalPrice"))
        d = _day(b)
        sign_buy = 1 if st in BUY_SUBS else (-1 if st in BUY_BACK_SUBS else 0)
        sign_sell = 1 if st in SELL_SUBS else (-1 if st in SELL_BACK_SUBS else 0)

        for scope, cond in (("today", d == today), ("month", d.startswith(month)), ("all", True)):
            if not cond:
                continue
            if sign_buy:
                acc[scope]["buy"] += sign_buy * amt
                if scope != "all" and sign_buy > 0:
                    acc[scope]["buy_n"] += 1
            if sign_sell:
                acc[scope]["sell"] += sign_sell * amt
                if scope != "all" and sign_sell > 0:
                    acc[scope]["sell_n"] += 1

        # 欠款：debt 字段为本单未付/未收
        dv = _f(b.get("debt"))
        if dv > 0:
            name = b.get("organName") or "（未填单位）"
            if sign_buy > 0:
                debt_pay[name] += dv
            elif sign_sell > 0:
                debt_recv[name] += dv

    def top(d, n=6):
        return [{"name": k, "amount": round(v, 2)}
                for k, v in sorted(d.items(), key=lambda x: -x[1])[:n]]

    for k in ("today", "month", "all"):
        acc[k]["buy"] = round(acc[k]["buy"], 2)
        acc[k]["sell"] = round(acc[k]["sell"], 2)
        acc[k]["profit"] = round(acc[k]["sell"] - acc[k]["buy"], 2)

    return {
        "ok": True,
        "date": today,
        "month": month,
        "today": acc["today"],
        "month_stat": acc["month"],
        "all": acc["all"],
        "bill_count": len(bills),
        "debt_pay": top(debt_pay),
        "debt_recv": top(debt_recv),
        "debt_pay_total": round(sum(debt_pay.values()), 2),
        "debt_recv_total": round(sum(debt_recv.values()), 2),
    }


def recent_bills(erp=None, limit=12):
    """最近单据流水。"""
    erp = erp or get_client()
    bills = [b for b in erp.all_bills() if str(b.get("deleteFlag")) != "1"]
    bills.sort(key=lambda b: str(b.get("operTime") or ""), reverse=True)
    out = []
    for b in bills[:limit]:
        out.append({
            "number": b.get("number"),
            "type": (b.get("type") or "") + "·" + (b.get("subType") or ""),
            "party": b.get("organName") or "—",
            "goods": (b.get("materialsList") or "").strip()[:40],
            "count": b.get("materialCount"),
            "total": _f(b.get("totalPrice")),
            "debt": _f(b.get("debt")),
            "time": _day(b),
            "status": "已审核" if str(b.get("status")) == "1" else "草稿",
        })
    return out


def stock_overview(erp=None, low_threshold=10, limit=40, goods=None):
    """库存总览 + 低库存预警。material/list 行自带 stock 字段。

    goods 为 in_out_stats 的结果（可选）：传入时给预警行补库存货值；
    每商品预警阈值优先取商品自己的 lowDecimal（未设则用 low_threshold）。
    """
    erp = erp or get_client()
    mats = erp.materials()
    vals = {}
    for g in (goods or {}).get("all_rows", []):
        vals[g["name"]] = g["value"]
    rows, total_kinds, neg, low = [], 0, 0, 0
    for m in mats:
        st = m.get("stock")
        if st is None:
            continue
        st = _f(st)
        try:
            thr = _f(m.get("lowDecimal")) or low_threshold
        except Exception:
            thr = low_threshold
        total_kinds += 1
        if st < 0:
            neg += 1
        elif st <= thr:
            low += 1
        rows.append({
            "name": m.get("name"),
            "unit": m.get("unit") or "",
            "stock": st,
            "category": m.get("categoryName") or "",
            "threshold": thr,
            "value": round(vals.get(m.get("name")) or 0, 2),
        })
    rows.sort(key=lambda r: (r["stock"] - r["threshold"]) / max(r["threshold"], 1))
    return {
        "kinds": total_kinds,
        "negative": neg,
        "low": low,
        "threshold": low_threshold,
        "warn_list": [r for r in rows if r["stock"] <= r["threshold"]][:limit],
    }


def official_stats(erp=None):
    """用 jshERP 官方统计接口取金额，比自己遍历单据更权威。
    实测可用：/depotItem/buyIn（采购）/saleOut（销售）/buyOrSalePrice（月趋势）
             /depotItem/getInOutStockCountMoney（库存货值）/account/getStatistics
    """
    import urllib.parse
    erp = erp or get_client()

    def q(path, params):
        s = "&".join("%s=%s" % (k, urllib.parse.quote(str(v)))
                     for k, v in params.items())
        try:
            r = erp._req(path + ("?" + s if s else ""))
            return r.get("data") if r.get("code") == 200 else None
        except Exception:
            return None

    today = time.strftime("%Y-%m-%d")
    m0 = time.strftime("%Y-%m-01")
    y0 = time.strftime("%Y-01-01")
    end = time.strftime("%Y-%m-%d") + " 23:59:59"

    def rng(b):
        return {"beginTime": b + " 00:00:00", "endTime": end,
                "currentPage": 1, "pageSize": 1, "materialParam": "", "organId": ""}

    out = {}
    for scope, begin in (("today", today), ("month", m0), ("year", y0)):
        buy = q("/depotItem/buyIn", rng(begin)) or {}
        sell = q("/depotItem/saleOut", rng(begin)) or {}
        out[scope] = {
            "buy": _f(buy.get("realityPriceTotal")),
            "sell": _f(sell.get("realityPriceTotal")),
            "buy_n": int(buy.get("total") or 0),
            "sell_n": int(sell.get("total") or 0),
        }
        out[scope]["profit"] = round(out[scope]["sell"] - out[scope]["buy"], 2)

    trend = q("/depotItem/buyOrSalePrice", {}) or {}
    out["trend"] = {
        "buy": trend.get("buyPriceList") or [],
        "sell": trend.get("salePriceList") or [],
    }

    stock_money = q("/depotItem/getInOutStockCountMoney",
                    {"beginTime": y0 + " 00:00:00", "endTime": end,
                     "materialParam": "", "depotId": ""}) or {}
    out["stock_money"] = {
        "total_stock": _f(stock_money.get("totalStock")),
        "total_value": _f(stock_money.get("totalCount")),
    }

    accs = q("/account/getStatistics", {"name": "", "serialNo": ""}) or {}
    out["account"] = {
        "current": _f(accs.get("allCurrentAmount")),
        "month": _f(accs.get("allMonthAmount")),
    }

    wait = q("/depotHead/waitBillCount", {}) or {}
    out["wait_bills"] = int(wait.get("total") or 0)

    for t in ("收款", "付款"):
        d = q("/depotHead/getNeedCount", {"type": t}) or {}
        out["need_" + ("recv" if t == "收款" else "pay")] = int(d.get("needCount") or 0)

    return out


def in_out_stats(erp=None, mats=None, top=10):
    """官方「进销存统计」接口（按商品，截至今天）：累计入库/出库/现存/库存货值。

    实测 /depotItem/getInOutStock 返回行含 materialName/unitName/inSum/outSum/
    thisSum/unitPrice（移动加权成本价）/thisAllPrice（现存货值）。
    """
    erp = erp or get_client()
    cats = {}
    for m in (mats if mats is not None else erp.materials()):
        cats[m.get("name")] = m.get("categoryName") or ""

    def q(params):
        s = "&".join("%s=%s" % (k, urllib.parse.quote(str(v)))
                     for k, v in params.items())
        try:
            r = erp._req("/depotItem/getInOutStock?" + s)
            return r.get("data") if r.get("code") == 200 else None
        except Exception:
            return None

    d = q({"currentPage": 1, "pageSize": 500, "beginTime": "2000-01-01",
           "endTime": time.strftime("%Y-%m-%d"), "materialParam": ""}) or {}
    rows = []
    for r in (d.get("rows") or []):
        name = r.get("materialName")
        rows.append({
            "name": name,
            "category": cats.get(name) or "",
            "unit": r.get("unitName") or "",
            "in_qty": _f(r.get("inSum")),
            "out_qty": _f(r.get("outSum")),
            "stock": _f(r.get("thisSum")),
            "price": _f(r.get("unitPrice")),
            "value": _f(r.get("thisAllPrice")),
        })
    rows.sort(key=lambda r: (-r["value"], -(r["in_qty"] + r["out_qty"])))
    return {
        "all_rows": rows,
        "rows": rows[:top],
        "total_value": round(sum(r["value"] for r in rows), 2),
        "total_in": round(sum(r["in_qty"] for r in rows), 2),
        "total_out": round(sum(r["out_qty"] for r in rows), 2),
    }


def daily_trend(bills, month=None):
    """本月每日采购/销售节奏（退货按负向冲减），用于日粒度柱状图。"""
    import calendar
    month = month or time.strftime("%Y-%m")
    days = {}

    def add(day, key, amt):
        days.setdefault(day, {"buy": 0.0, "sell": 0.0})
        days[day][key] += amt

    for b in bills:
        d = _day(b)
        if not d.startswith(month):
            continue
        st = b.get("subType") or ""
        amt = _f(b.get("totalPrice"))
        if st in BUY_SUBS:
            add(d, "buy", amt)
        elif st in BUY_BACK_SUBS:
            add(d, "buy", -amt)
        elif st in SELL_SUBS:
            add(d, "sell", amt)
        elif st in SELL_BACK_SUBS:
            add(d, "sell", -amt)

    y, n = int(month[:4]), int(month[5:7])
    last_day = calendar.monthrange(y, n)[1]
    today_n = int(time.strftime("%d")) if month == time.strftime("%Y-%m") else last_day
    out = []
    for i in range(1, today_n + 1):
        v = days.get("%s-%02d" % (month, i), {})
        out.append({"x": str(i), "buy": round(v.get("buy", 0), 2),
                    "sell": round(v.get("sell", 0), 2)})
    return out


def top_parties(bills, top=5):
    """往来单位排行（累计采购额/销售额），供横向条形图。"""
    sup, cus = defaultdict(float), defaultdict(float)
    for b in bills:
        st = b.get("subType") or ""
        amt = _f(b.get("totalPrice"))
        name = b.get("organName") or ""
        if not name:
            continue
        if st in BUY_SUBS:
            sup[name] += amt
        elif st in BUY_BACK_SUBS:
            sup[name] -= amt
        elif st in SELL_SUBS:
            cus[name] += amt
        elif st in SELL_BACK_SUBS:
            cus[name] -= amt

    def topn(d):
        return [{"name": k, "amount": round(v, 2)}
                for k, v in sorted(d.items(), key=lambda x: -x[1])[:top] if v > 0]

    return {"suppliers": topn(sup), "customers": topn(cus)}


def debt_aging(bills):
    """欠款账龄：把未结清单据按账龄分桶（≤30 天 / 31~60 天 / 60 天以上）。"""
    recv = defaultdict(float)
    pay = defaultdict(float)
    now = time.time()
    for b in bills:
        dv = _f(b.get("debt"))
        if dv <= 0:
            continue
        st = b.get("subType") or ""
        try:
            t = time.mktime(time.strptime(_day(b), "%Y-%m-%d"))
            age_days = max(0, int((now - t) / 86400))
        except Exception:
            age_days = -1
        bucket = "≤30天" if 0 <= age_days <= 30 else ("31~60天" if age_days <= 60 else "60天以上")
        if st in SELL_SUBS:
            recv[bucket] += dv
        elif st in BUY_SUBS:
            pay[bucket] += dv
    order = ["≤30天", "31~60天", "60天以上"]

    def tolist(d):
        return [{"name": k, "amount": round(d.get(k, 0), 2)}
                for k in order if d.get(k, 0) > 0]

    return {"recv": tolist(recv), "pay": tolist(pay)}


def account_breakdown(erp=None):
    """资金账户明细（每个账户的当前余额）。"""
    erp = erp or get_client()
    try:
        rows = erp.accounts()
    except Exception:
        return []
    out = []
    for a in rows:
        try:
            if str(a.get("enabled", 1)) in ("0", "False", "false"):
                continue
        except Exception:
            pass
        out.append({"name": a.get("name") or "（未命名账户）",
                    "amount": round(_f(a.get("currentAmount")), 2)})
    out.sort(key=lambda x: -x["amount"])
    return out


def dashboard(erp=None):
    """看板一次性数据。"""
    erp = erp or get_client()
    try:
        bills = [b for b in erp.all_bills() if str(b.get("deleteFlag")) != "1"]
    except Exception as e:
        return {"ok": False, "message": "统计失败：%s" % e}
    try:
        s = summary(erp, bills=bills)
    except Exception as e:
        return {"ok": False, "message": "统计失败：%s" % e}
    try:
        s["official"] = official_stats(erp)
    except Exception:
        s["official"] = {}
    try:
        s["recent"] = recent_bills(erp)
    except Exception:
        s["recent"] = []
    try:
        s["goods"] = in_out_stats(erp)
    except Exception:
        s["goods"] = {"rows": [], "all_rows": []}
    try:
        s["stock"] = stock_overview(erp, goods=s["goods"])
    except Exception:
        s["stock"] = {"kinds": 0, "negative": 0, "low": 0, "warn_list": []}
    try:
        s["daily"] = daily_trend(bills)
    except Exception:
        s["daily"] = []
    try:
        s["parties"] = top_parties(bills)
    except Exception:
        s["parties"] = {"suppliers": [], "customers": []}
    try:
        s["aging"] = debt_aging(bills)
    except Exception:
        s["aging"] = {"recv": [], "pay": []}
    try:
        s["accounts"] = account_breakdown(erp)
    except Exception:
        s["accounts"] = []
    try:
        s["master"] = {
            "suppliers": len(erp.suppliers("供应商")),
            "customers": len(erp.suppliers("客户")),
            "materials": len(erp.materials()),
            "depots": len(erp.depots()),
        }
    except Exception:
        s["master"] = {}
    return s


if __name__ == "__main__":
    import json
    import sys
    import os
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    print(json.dumps(dashboard(), ensure_ascii=False, indent=1))
