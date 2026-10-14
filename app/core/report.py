#!/usr/bin/env python3
"""看板数据聚合。全部基于 core.erp 的只读接口，不改任何数据。

单据字段（实测）：type/subType/number/operTime/organName/totalPrice/
changeAmount/debt/status/materialsList/materialCount/accountName
"""
import re
import threading
import time
import urllib.parse
from collections import defaultdict

from .erp import get_client, parallel

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


def recent_bills(erp=None, limit=12, bills=None):
    """最近单据流水。bills 可传入已取好的单据列表（避免重复拉全量）。

    除列表展示用的摘要字段外，额外带齐详情弹层要用的字段（实付/优惠/账户/
    备注/联系人/经手人/附件等），让前端点开单条即可看全，无需再请求一次。
    """
    erp = erp or get_client()
    bills = bills if bills is not None else erp.all_bills()
    bills = [b for b in bills if str(b.get("deleteFlag")) != "1"]
    bills.sort(key=lambda b: str(b.get("operTime") or ""), reverse=True)
    out = []
    for b in bills[:limit]:
        def _s(v):
            # ERP 有些字段空值返回字面量 "null"/"None"，一律当空串
            t = str(v or "").strip()
            return "" if t.lower() in ("null", "none") else t
        # 结算账户：ERP 的 accountName 形如「账户名(金额元)」，账户名缺失时是
        # 字面量 null。括号内金额是本次该账户的收付额，比 changeAmount 更细，
        # 单账户场景下就取它作为实付；名称取不到则回退到 accountIdList 个数表述。
        accts = [x for x in str(b.get("accountIdList") or "").split(",") if x]
        acct_name, acct_money = "", 0.0
        m = re.match(r"^\s*([^(]+)\(([\d.]+)元\)", _s(b.get("accountName")))
        if m:
            nm, amt = m.group(1).strip(), _f(m.group(2))
            if nm.lower() not in ("null", "none"):
                acct_name, acct_money = nm, amt
        account = acct_name or (("多账户 × %d" % len(accts)) if len(accts) > 1 else "")
        out.append({
            "number": b.get("number"),
            "type": (b.get("type") or "") + "·" + (b.get("subType") or ""),
            "party": b.get("organName") or "—",
            "goods": (b.get("materialsList") or "").strip()[:40],
            # materialCount 是商品总数量（件/瓶/箱），不是明细行数
            "count": b.get("materialCount"),
            "total": _f(b.get("totalPrice")),
            "debt": _f(b.get("debt")),
            "time": _day(b),
            "status": "已审核" if str(b.get("status")) == "1" else "草稿",
            # ---- 以下为详情弹层补充字段 ----
            "oper_time": (b.get("operTimeStr") or str(b.get("operTime") or ""))[:19].replace("T", " "),
            "create_time": (b.get("createTime") or "")[:19].replace("T", " "),
            "pay_type": _s(b.get("payType")),
            "paid": _f(b.get("changeAmount")),
            # 账户括号里的金额（无则回退 changeAmount），用于「这笔钱从哪出」
            "account_money": acct_money or _f(b.get("changeAmount")),
            "discount": _f(b.get("discount")),
            # ERP 的 discountLastMoney 是「折后最终金额」，非优惠额；无折扣时等于合计
            "discount_money": _f(b.get("discountLastMoney")) or _f(b.get("discountMoney")),
            "tax_rate": _f(b.get("taxNum")),
            "other_money": _f(b.get("otherMoney")),
            "account": account,
            "remark": _s(b.get("remark")),
            "contacts": _s(b.get("contacts")),
            "phone": _s(b.get("phoneNum")) or _s(b.get("telephone")),
            "salesman": _s(b.get("salesManStr")) or _s(b.get("salesMan")),
            "creator": _s(b.get("creatorName")),
            "attachments": _s(b.get("fileName")),
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
    13 个请求互相独立，一次并行发出（原先串行，随接口数线性变慢）。
    """
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

    jobs = []
    for scope, begin in (("today", today), ("month", m0), ("year", y0)):
        jobs.append((scope + "|buy", "/depotItem/buyIn", rng(begin)))
        jobs.append((scope + "|sell", "/depotItem/saleOut", rng(begin)))
    jobs.append(("trend", "/depotItem/buyOrSalePrice", {}))
    jobs.append(("stock_money", "/depotItem/getInOutStockCountMoney",
                 {"beginTime": y0 + " 00:00:00", "endTime": end,
                  "materialParam": "", "depotId": ""}))
    jobs.append(("account", "/account/getStatistics", {"name": "", "serialNo": ""}))
    jobs.append(("wait", "/depotHead/waitBillCount", {}))
    jobs.append(("need_recv", "/depotHead/getNeedCount", {"type": "收款"}))
    jobs.append(("need_pay", "/depotHead/getNeedCount", {"type": "付款"}))

    results = parallel([lambda p=path, pa=params: q(p, pa) for _, path, params in jobs],
                       workers=8)
    d = {jobs[i][0]: results[i] for i in range(len(jobs))}

    out = {}
    for scope in ("today", "month", "year"):
        buy = d[scope + "|buy"] or {}
        sell = d[scope + "|sell"] or {}
        out[scope] = {
            "buy": _f(buy.get("realityPriceTotal")),
            "sell": _f(sell.get("realityPriceTotal")),
            "buy_n": int(buy.get("total") or 0),
            "sell_n": int(sell.get("total") or 0),
        }
        out[scope]["profit"] = round(out[scope]["sell"] - out[scope]["buy"], 2)

    trend = d["trend"] or {}
    out["trend"] = {
        "buy": trend.get("buyPriceList") or [],
        "sell": trend.get("salePriceList") or [],
    }

    stock_money = d["stock_money"] or {}
    out["stock_money"] = {
        "total_stock": _f(stock_money.get("totalStock")),
        "total_value": _f(stock_money.get("totalCount")),
    }

    accs = d["account"] or {}
    out["account"] = {
        "current": _f(accs.get("allCurrentAmount")),
        "month": _f(accs.get("allMonthAmount")),
    }

    wait = d["wait"] or {}
    out["wait_bills"] = int(wait.get("total") or 0)

    out["need_recv"] = int((d["need_recv"] or {}).get("needCount") or 0)
    out["need_pay"] = int((d["need_pay"] or {}).get("needCount") or 0)

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


def _dashboard_full(erp):
    """完整看板数据（原 dashboard 逻辑）。"""
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
        # 复用已取好的单据，不再全量重拉；取 20 条配合看板表格吸顶表头使用
        s["recent"] = recent_bills(erp, limit=20, bills=bills)
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


def _dashboard_lite(erp):
    """工作台 KPI 精简版：核心指标 + 最近单据 + 预警计数，
    不跑趋势/账龄/货值等图表数据，接口数约为完整版的一半。"""
    try:
        bills = [b for b in erp.all_bills() if str(b.get("deleteFlag")) != "1"]
    except Exception as e:
        return {"ok": False, "message": "统计失败：%s" % e}
    out = {"ok": True}
    try:
        s = summary(erp, bills=bills)
        for k in ("date", "month", "today", "month_stat", "all", "bill_count",
                  "debt_pay", "debt_recv", "debt_pay_total", "debt_recv_total"):
            out[k] = s.get(k)
    except Exception:
        pass
    try:
        out["recent"] = recent_bills(erp, limit=6, bills=bills)
    except Exception:
        out["recent"] = []
    try:
        ov = stock_overview(erp)
        out["stock"] = {k: ov.get(k) for k in ("kinds", "negative", "low")}
        out["stock"]["warn_list"] = (ov.get("warn_list") or [])[:5]
    except Exception:
        out["stock"] = {"kinds": 0, "negative": 0, "low": 0, "warn_list": []}
    return out


_DASH_TTL = 30
_dash_cache = {"full": None, "lite": None}
_dash_lock = threading.Lock()


def invalidate():
    """写入后清看板聚合缓存（server 在 commit 成功后调用）。"""
    with _dash_lock:
        _dash_cache["full"] = None
        _dash_cache["lite"] = None


def dashboard(erp=None, lite=False):
    """看板数据（带 30s TTL 缓存；lite=True 回工作台 KPI 精简版）。"""
    erp = erp or get_client()
    key = "lite" if lite else "full"
    with _dash_lock:
        hit = _dash_cache[key]
        if hit and time.time() - hit["at"] < _DASH_TTL:
            return hit["data"]
    data = _dashboard_lite(erp) if lite else _dashboard_full(erp)
    with _dash_lock:
        _dash_cache[key] = {"at": time.time(), "data": data}
    return data


if __name__ == "__main__":
    import json
    import sys
    import os
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    print(json.dumps(dashboard(), ensure_ascii=False, indent=1))
