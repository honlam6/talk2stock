#!/usr/bin/env python3
"""ERP 统一只读账务与统计层（Single Source of Truth）。

为什么独立出本模块：
1. jshERP 的部分 REST 列表/统计接口在本环境存在已知语义偏差或规模上限：
   - /depotHead/list 返回负金额的绝对值、翻页 >200 行不稳定；
   - /depotItem/buyIn & saleOut 的 `total` 是商品种数而非单据张数；
   - /depotItem/buyOrSalePrice 对 admin 账号恒返回空；
   - /depotHead/waitBillCount 不传 status 返回全量单据；
   - /depotHead/getNeedCount 只认 vendor/customer；
   - /account/getStatistics 对同时写了 accountId 与 accountIdList 的单据双计；
   - jsh_account.current_amount 是未维护的静态旧列，不可直接读取。
2. 所有汇总数字必须严格遵守同一套会计/业务口径，并经 dbread 只读事务护栏执行：
   - 只计 delete_flag='0' 且 status IN ('1','2','3') 的已审核单据；
   - 业务日期一律取 oper_time（绝不用 create_time）；
   - 单据金额按 ABS(CASE WHEN discount_last_money!=0 THEN discount_last_money ELSE total_price END)
     + sub_type 方向计算，同时兼容历史正号数据与 jshERP 官方负号数据；
   - 未结清欠款扣除 jsh_account_item.bill_id 已核销的收/付款金额；
   - 库存明细数量取 IFNULL(basic_number, oper_number)，实时库存取 jsh_material_current_stock。
"""
import calendar
import json
import time

from . import config as cfgmod
from . import dbread as dbmod

BUY_SUBS = ("采购",)
BUY_BACK_SUBS = ("采购退货",)
SELL_SUBS = ("销售", "零售")
SELL_BACK_SUBS = ("销售退货", "零售退货")

# 资金流出单据（jshERP Web 端以负数入库，账务流水按负方向计）
CASH_OUT_SUBS = ("采购", "销售退货", "零售退货", "采购订单")
CASH_IN_SUBS = ("销售", "零售", "采购退货", "销售订单")


def _f(v, default=0.0):
    try:
        if v in (None, ""):
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def _i(v, default=0):
    try:
        if v in (None, ""):
            return default
        return int(float(v))
    except (TypeError, ValueError):
        return default


def _esc(s):
    return str(s or "").replace("\\", "\\\\").replace("'", "''")


def _like_esc(s):
    return _esc(str(s or "").strip()).replace("%", "\\%").replace("_", "\\_")


def tenant_subquery():
    """当前配置 ERP 用户所属 tenant_id 的标量子查询。"""
    user = str((cfgmod.load().get("erp") or {}).get("user") or "jsh")
    return ("(SELECT tenant_id FROM jsh_user WHERE login_name='%s' ORDER BY id LIMIT 1)"
            % _esc(user))


def _q(sql, limit=5000):
    res = dbmod._run(sql, limit=limit)
    return res["rows"]


_FORCE_CACHE = {"at": 0.0, "val": None}


def force_approval():
    """读取 jsh_system_config.force_approval_flag，与 jshERP 过账口径保持一致。"""
    now = time.time()
    if _FORCE_CACHE["val"] is not None and now - _FORCE_CACHE["at"] < 60:
        return _FORCE_CACHE["val"]
    t = tenant_subquery()
    try:
        rows = _q(
            "SELECT force_approval_flag FROM jsh_system_config "
            f"WHERE IFNULL(delete_flag,'0')!='1' AND tenant_id={t} LIMIT 1",
            limit=1,
        )
        if rows and rows[0].get("force_approval_flag") is not None:
            val = str(rows[0].get("force_approval_flag")).strip() == "1"
            _FORCE_CACHE.update({"at": now, "val": val})
            return val
    except Exception:
        pass
    return True


def _posted_where(alias="h"):
    t = tenant_subquery()
    if force_approval():
        return (f"IFNULL({alias}.delete_flag,'0')!='1' "
                f"AND {alias}.status IN ('1','2','3') AND {alias}.tenant_id={t}")
    return f"IFNULL({alias}.delete_flag,'0')!='1' AND {alias}.tenant_id={t}"


def _fin_posted_where(alias="ah"):
    t = tenant_subquery()
    if force_approval():
        return (f"IFNULL({alias}.delete_flag,'0')!='1' "
                f"AND {alias}.status='1' AND {alias}.tenant_id={t}")
    return f"IFNULL({alias}.delete_flag,'0')!='1' AND {alias}.tenant_id={t}"


def _gross_expr(alias="h"):
    return (f"CASE WHEN {alias}.discount_last_money IS NOT NULL AND {alias}.discount_last_money != 0 "
            f"THEN ABS({alias}.discount_last_money) ELSE ABS(IFNULL({alias}.total_price, 0)) END")


def _signed_cash_expr(alias="h"):
    out_subs = ",".join(f"'{s}'" for s in CASH_OUT_SUBS)
    in_subs = ",".join(f"'{s}'" for s in CASH_IN_SUBS)
    return (f"CASE WHEN {alias}.sub_type IN ({out_subs}) THEN -ABS(IFNULL({alias}.change_amount, 0)) "
            f"WHEN {alias}.sub_type IN ({in_subs}) THEN ABS(IFNULL({alias}.change_amount, 0)) "
            f"ELSE 0 END")


# --------------------------------------------------------------------------
# 1. 期间经营汇总（今日 / 本月 / 本年 / 累计，含单据张数与退货拆解）
# --------------------------------------------------------------------------
def period_stats(today=None, month=None, year=None):
    today = today or time.strftime("%Y-%m-%d")
    month = month or time.strftime("%Y-%m")
    year = year or time.strftime("%Y")
    sql = (
        "SELECT h.sub_type AS st, DATE_FORMAT(h.oper_time,'%Y-%m-%d') AS d, "
        f"SUM({_gross_expr('h')}) AS amt, COUNT(*) AS n "
        "FROM jsh_depot_head h "
        f"WHERE {_posted_where('h')} "
        "GROUP BY h.sub_type, d"
    )
    try:
        rows = _q(sql, limit=10000)
    except Exception:
        return None
    if rows is None:
        return None

    def _blank():
        return {
            "buy": 0.0, "sell": 0.0, "buy_n": 0, "sell_n": 0,
            "buy_return_n": 0, "buy_return_amt": 0.0,
            "sell_return_n": 0, "sell_return_amt": 0.0,
            "return_n": 0, "return_amt": 0.0,
        }

    acc = {"today": _blank(), "month": _blank(), "year": _blank(), "all": _blank()}
    total_n = 0
    for r in rows:
        st = str(r.get("st") or "")
        d = str(r.get("d") or "")
        amt = _f(r.get("amt"))
        n = _i(r.get("n"))
        total_n += n
        sign_buy = 1 if st in BUY_SUBS else (-1 if st in BUY_BACK_SUBS else 0)
        sign_sell = 1 if st in SELL_SUBS else (-1 if st in SELL_BACK_SUBS else 0)
        for scope, hit in (
            ("today", d == today),
            ("month", d.startswith(month)),
            ("year", d.startswith(year)),
            ("all", True),
        ):
            if not hit:
                continue
            if sign_buy:
                acc[scope]["buy"] += sign_buy * amt
                if sign_buy > 0:
                    acc[scope]["buy_n"] += n
                else:
                    acc[scope]["buy_return_n"] += n
                    acc[scope]["buy_return_amt"] += amt
                    acc[scope]["return_n"] += n
                    acc[scope]["return_amt"] += amt
            if sign_sell:
                acc[scope]["sell"] += sign_sell * amt
                if sign_sell > 0:
                    acc[scope]["sell_n"] += n
                else:
                    acc[scope]["sell_return_n"] += n
                    acc[scope]["sell_return_amt"] += amt
                    acc[scope]["return_n"] += n
                    acc[scope]["return_amt"] += amt

    for k in acc:
        for fk in ("buy", "sell", "buy_return_amt", "sell_return_amt", "return_amt"):
            acc[k][fk] = round(acc[k][fk], 2)
        acc[k]["profit"] = round(acc[k]["sell"] - acc[k]["buy"], 2)
    return {
        "today": acc["today"],
        "month_stat": acc["month"],
        "year": acc["year"],
        "all": acc["all"],
        "bill_count": total_n,
    }


def monthly_trend(months=6):
    """近 N 个月采销净额月趋势（绕开 /depotItem/buyOrSalePrice 对 admin 恒空的限制）。"""
    y, m = int(time.strftime("%Y")), int(time.strftime("%m"))
    keys = []
    for i in range(months - 1, -1, -1):
        mm = m - i
        yy = y
        while mm <= 0:
            mm += 12
            yy -= 1
        keys.append(f"{yy:04d}-{mm:02d}")
    start = keys[0] + "-01 00:00:00"
    sql = (
        "SELECT DATE_FORMAT(h.oper_time,'%Y-%m') AS ym, h.sub_type AS st, "
        f"SUM({_gross_expr('h')}) AS amt "
        "FROM jsh_depot_head h "
        f"WHERE {_posted_where('h')} AND h.oper_time >= '{start}' "
        "GROUP BY ym, h.sub_type"
    )
    try:
        rows = _q(sql, limit=2000)
    except Exception:
        return None
    buckets = {k: {"buy": 0.0, "sell": 0.0} for k in keys}
    for r in rows:
        ym = str(r.get("ym") or "")
        if ym not in buckets:
            continue
        st = str(r.get("st") or "")
        amt = _f(r.get("amt"))
        if st in BUY_SUBS:
            buckets[ym]["buy"] += amt
        elif st in BUY_BACK_SUBS:
            buckets[ym]["buy"] -= amt
        elif st in SELL_SUBS:
            buckets[ym]["sell"] += amt
        elif st in SELL_BACK_SUBS:
            buckets[ym]["sell"] -= amt
    return {
        "buy": [{"x": k, "y": round(buckets[k]["buy"], 2)} for k in keys],
        "sell": [{"x": k, "y": round(buckets[k]["sell"], 2)} for k in keys],
    }


def daily_trend(month=None):
    """指定月每日采购/销售净额（退货按负向冲减）。"""
    month = month or time.strftime("%Y-%m")
    sql = (
        "SELECT DATE_FORMAT(h.oper_time,'%Y-%m-%d') AS d, h.sub_type AS st, "
        f"SUM({_gross_expr('h')}) AS amt "
        "FROM jsh_depot_head h "
        f"WHERE {_posted_where('h')} AND DATE_FORMAT(h.oper_time,'%Y-%m')='{_esc(month)}' "
        "GROUP BY d, h.sub_type"
    )
    try:
        rows = _q(sql, limit=1000)
    except Exception:
        return None
    days = {}
    for r in rows:
        d = str(r.get("d") or "")
        st = str(r.get("st") or "")
        amt = _f(r.get("amt"))
        b = days.setdefault(d, {"buy": 0.0, "sell": 0.0})
        if st in BUY_SUBS:
            b["buy"] += amt
        elif st in BUY_BACK_SUBS:
            b["buy"] -= amt
        elif st in SELL_SUBS:
            b["sell"] += amt
        elif st in SELL_BACK_SUBS:
            b["sell"] -= amt
    y, n = int(month[:4]), int(month[5:7])
    last_day = calendar.monthrange(y, n)[1]
    today_n = int(time.strftime("%d")) if month == time.strftime("%Y-%m") else last_day
    out = []
    for i in range(1, today_n + 1):
        v = days.get(f"{month}-{i:02d}", {})
        out.append({"x": str(i), "buy": round(v.get("buy", 0.0), 2),
                    "sell": round(v.get("sell", 0.0), 2)})
    return out


def top_parties(top=5):
    """往来单位累计净采购/净销售排行。"""
    sql = (
        "SELECT h.sub_type AS st, IFNULL(s.supplier,'') AS name, "
        f"SUM({_gross_expr('h')}) AS amt "
        "FROM jsh_depot_head h "
        "LEFT JOIN jsh_supplier s ON s.id = h.organ_id "
        f"WHERE {_posted_where('h')} AND h.organ_id IS NOT NULL "
        "GROUP BY h.organ_id, s.supplier, h.sub_type"
    )
    try:
        rows = _q(sql, limit=5000)
    except Exception:
        return None
    sup, cus = {}, {}
    for r in rows:
        name = str(r.get("name") or "").strip()
        if not name:
            continue
        st = str(r.get("st") or "")
        amt = _f(r.get("amt"))
        if st in BUY_SUBS:
            sup[name] = sup.get(name, 0.0) + amt
        elif st in BUY_BACK_SUBS:
            sup[name] = sup.get(name, 0.0) - amt
        elif st in SELL_SUBS:
            cus[name] = cus.get(name, 0.0) + amt
        elif st in SELL_BACK_SUBS:
            cus[name] = cus.get(name, 0.0) - amt

    def topn(d):
        return [{"name": k, "amount": round(v, 2)}
                for k, v in sorted(d.items(), key=lambda x: -x[1])[:top] if v > 0]

    return {"suppliers": topn(sup), "customers": topn(cus)}


# --------------------------------------------------------------------------
# 2. 应收应付 / 欠款账龄（含 ABS 符号兼容与财务收付款单核销扣减）
# --------------------------------------------------------------------------
def _unpaid_rows():
    t = tenant_subquery()
    sql = (
        "SELECT h.id, h.sub_type, h.organ_id, IFNULL(s.supplier,'（未填单位）') AS organ_name, "
        "DATE_FORMAT(h.oper_time,'%Y-%m-%d') AS d, "
        f"DATEDIFF(CURDATE(), DATE(h.oper_time)) AS age_days, "
        f"GREATEST(({_gross_expr('h')} + IFNULL(h.other_money,0) - IFNULL(h.deposit,0) "
        "- ABS(IFNULL(h.change_amount,0)) - IFNULL(fin.settled,0)), 0) AS unpaid "
        "FROM jsh_depot_head h "
        "LEFT JOIN jsh_supplier s ON s.id = h.organ_id "
        "LEFT JOIN ("
        "  SELECT ai.bill_id, SUM(ABS(IFNULL(ai.each_amount,0))) AS settled "
        "  FROM jsh_account_item ai "
        "  JOIN jsh_account_head ah ON ah.id = ai.header_id "
        "  WHERE ai.bill_id IS NOT NULL "
        f"    AND IFNULL(ai.delete_flag,'0')!='1' AND {_fin_posted_where('ah')} "
        "  GROUP BY ai.bill_id"
        ") fin ON fin.bill_id = h.id "
        f"WHERE {_posted_where('h')} AND h.sub_type IN ('采购','销售','零售') "
        f"HAVING unpaid > 0.005"
    )
    return _q(sql, limit=10000)


def debt_summary(top_n=6):
    try:
        rows = _unpaid_rows()
    except Exception:
        return None
    recv_by, pay_by = {}, {}
    recv_tot = pay_tot = 0.0
    need_recv = need_pay = 0
    for r in rows:
        st = str(r.get("sub_type") or "")
        amt = _f(r.get("unpaid"))
        if amt <= 0.005:
            continue
        name = str(r.get("organ_name") or "（未填单位）")
        if st in SELL_SUBS:
            recv_tot += amt
            need_recv += 1
            recv_by[name] = recv_by.get(name, 0.0) + amt
        elif st in BUY_SUBS:
            pay_tot += amt
            need_pay += 1
            pay_by[name] = pay_by.get(name, 0.0) + amt

    def top(d):
        return [{"name": k, "amount": round(v, 2)}
                for k, v in sorted(d.items(), key=lambda x: -x[1])[:top_n]]

    return {
        "debt_recv_total": round(recv_tot, 2),
        "debt_pay_total": round(pay_tot, 2),
        "debt_recv": top(recv_by),
        "debt_pay": top(pay_by),
        "need_recv": need_recv,
        "need_pay": need_pay,
    }


def debt_aging():
    try:
        rows = _unpaid_rows()
    except Exception:
        return None
    recv, pay = {}, {}
    for r in rows:
        amt = _f(r.get("unpaid"))
        if amt <= 0.005:
            continue
        age = _i(r.get("age_days"), -1)
        bucket = "≤30天" if 0 <= age <= 30 else ("31~60天" if age <= 60 else "60天以上")
        st = str(r.get("sub_type") or "")
        if st in SELL_SUBS:
            recv[bucket] = recv.get(bucket, 0.0) + amt
        elif st in BUY_SUBS:
            pay[bucket] = pay.get(bucket, 0.0) + amt
    order = ("≤30天", "31~60天", "60天以上")

    def tolist(d):
        return [{"name": k, "amount": round(d[k], 2)} for k in order if d.get(k, 0) > 0]

    return {"recv": tolist(recv), "pay": tolist(pay)}


# --------------------------------------------------------------------------
# 3. 资金账户真实余额（禁用 jsh_account.current_amount 静态列；消除双计）
# --------------------------------------------------------------------------
def _parse_id_money_lists(id_str, money_str):
    def split_vals(raw):
        s = str(raw or "").strip().strip("[]").replace('"', "").replace("'", "")
        return [x.strip() for x in s.split(",") if x.strip()]

    ids = split_vals(id_str)
    monies = split_vals(money_str)
    out = []
    for aid, m in zip(ids, monies):
        try:
            out.append((int(aid), float(m)))
        except (TypeError, ValueError):
            continue
    return out


def account_summary(month=None):
    """按 jshERP 会计流水口径计算账户余额与本月发生额（含单/多账户与财务单）。"""
    month = month or time.strftime("%Y-%m")
    month_start = month + "-01 00:00:00"
    t = tenant_subquery()
    try:
        accs = _q(
            f"SELECT id, name, IFNULL(initial_amount,0) AS init_amt "
            f"FROM jsh_account WHERE IFNULL(delete_flag,'0')!='1' AND tenant_id={t} "
            "ORDER BY sort ASC, id ASC",
            limit=500,
        )
        single_rows = _q(
            "SELECT h.account_id AS aid, "
            f"SUM({_signed_cash_expr('h')}) AS total_chg, "
            f"SUM(CASE WHEN h.oper_time >= '{month_start}' THEN {_signed_cash_expr('h')} ELSE 0 END) AS month_chg "
            "FROM jsh_depot_head h "
            f"WHERE {_posted_where('h')} AND IFNULL(h.pay_type,'')!='预付款' "
            "  AND h.account_id IS NOT NULL AND h.account_id != 0 "
            "GROUP BY h.account_id",
            limit=500,
        )
        multi_rows = _q(
            "SELECT h.sub_type AS st, h.oper_time AS ot, h.account_id_list, h.account_money_list "
            "FROM jsh_depot_head h "
            f"WHERE {_posted_where('h')} AND IFNULL(h.pay_type,'')!='预付款' "
            "  AND (h.account_id IS NULL OR h.account_id = 0) "
            "  AND IFNULL(h.account_id_list,'') != ''",
            limit=5000,
        )
        fin_head = _q(
            "SELECT ah.account_id AS aid, ah.type AS t, "
            "SUM(ABS(IFNULL(ah.change_amount,0))) AS total_chg, "
            f"SUM(CASE WHEN ah.bill_time >= '{month_start}' THEN ABS(IFNULL(ah.change_amount,0)) ELSE 0 END) AS month_chg "
            "FROM jsh_account_head ah "
            f"WHERE {_fin_posted_where('ah')} "
            "  AND ah.account_id IS NOT NULL AND ah.account_id != 0 "
            "GROUP BY ah.account_id, ah.type",
            limit=500,
        )
        fin_item = _q(
            "SELECT ai.account_id AS aid, ah.type AS t, "
            "SUM(ABS(IFNULL(ai.each_amount,0))) AS total_chg, "
            f"SUM(CASE WHEN ah.bill_time >= '{month_start}' THEN ABS(IFNULL(ai.each_amount,0)) ELSE 0 END) AS month_chg "
            "FROM jsh_account_item ai "
            "JOIN jsh_account_head ah ON ah.id = ai.header_id "
            f"WHERE IFNULL(ai.delete_flag,'0')!='1' AND {_fin_posted_where('ah')} "
            "  AND ai.account_id IS NOT NULL AND ai.account_id != 0 "
            "GROUP BY ai.account_id, ah.type",
            limit=500,
        )
    except Exception:
        return None

    bal = {}
    mon = {}
    meta = {}
    for a in accs:
        aid = _i(a.get("id"))
        init_amt = _f(a.get("init_amt"))
        meta[aid] = {"id": aid, "name": str(a.get("name") or "（未命名账户）"), "initial": round(init_amt, 2)}
        bal[aid] = init_amt
        mon[aid] = 0.0

    for r in single_rows:
        aid = _i(r.get("aid"))
        bal[aid] = bal.get(aid, 0.0) + _f(r.get("total_chg"))
        mon[aid] = mon.get(aid, 0.0) + _f(r.get("month_chg"))

    for r in multi_rows:
        st = str(r.get("st") or "")
        sign = -1.0 if st in CASH_OUT_SUBS else (1.0 if st in CASH_IN_SUBS else 0.0)
        if not sign:
            continue
        in_month = str(r.get("ot") or "") >= month_start
        for aid, m in _parse_id_money_lists(r.get("account_id_list"), r.get("account_money_list")):
            delta = sign * abs(m)
            bal[aid] = bal.get(aid, 0.0) + delta
            if in_month:
                mon[aid] = mon.get(aid, 0.0) + delta

    for r in fin_head:
        aid = _i(r.get("aid"))
        sign = -1.0 if str(r.get("t") or "") in ("付款", "支出", "转账") else 1.0
        bal[aid] = bal.get(aid, 0.0) + sign * _f(r.get("total_chg"))
        mon[aid] = mon.get(aid, 0.0) + sign * _f(r.get("month_chg"))

    for r in fin_item:
        aid = _i(r.get("aid"))
        sign = -1.0 if str(r.get("t") or "") == "支出" else 1.0
        bal[aid] = bal.get(aid, 0.0) + sign * _f(r.get("total_chg"))
        mon[aid] = mon.get(aid, 0.0) + sign * _f(r.get("month_chg"))

    out_accs = []
    for aid, info in meta.items():
        out_accs.append({
            "id": aid,
            "name": info["name"],
            "initial": info["initial"],
            "amount": round(bal.get(aid, 0.0), 2),
            "month": round(mon.get(aid, 0.0), 2),
        })
    out_accs.sort(key=lambda x: -x["amount"])
    return {
        "accounts": out_accs,
        "current": round(sum(x["amount"] for x in out_accs), 2),
        "month": round(sum(x["month"] for x in out_accs), 2),
    }


def ops_counts():
    """待审核单据数（status='0'）与全部有效单据数。"""
    t = tenant_subquery()
    sql = (
        "SELECT "
        "SUM(CASE WHEN h.status='0' THEN 1 ELSE 0 END) AS wait_bills, "
        "COUNT(*) AS bill_total "
        "FROM jsh_depot_head h "
        f"WHERE IFNULL(h.delete_flag,'0')!='1' AND h.tenant_id={t}"
    )
    try:
        rows = _q(sql, limit=10)
    except Exception:
        return None
    if not rows:
        return None
    r = rows[0]
    return {"wait_bills": _i(r.get("wait_bills")), "bill_total": _i(r.get("bill_total"))}


# --------------------------------------------------------------------------
# 4. 分仓库存 / 期初流水 / 分仓安全库存预警
# --------------------------------------------------------------------------
def depot_stock_sql(material_id):
    """DB 直查 jsh_material_current_stock（含期初），作为 REST 异常时的回退。"""
    try:
        mid = int(material_id)
    except (TypeError, ValueError):
        return None
    try:
        rows = _q(
            "SELECT depot_id, current_number FROM jsh_material_current_stock "
            f"WHERE material_id={mid} AND IFNULL(delete_flag,'0')!='1'",
            limit=200,
        )
    except Exception:
        return None
    return {str(r.get("depot_id")): _f(r.get("current_number")) for r in rows}


def material_flow(material_id, depot_id=None):
    """商品期初 / 累计入库 / 累计出库 / 现库存（基本单位口径，含调拨）。"""
    try:
        mid = int(material_id)
    except (TypeError, ValueError):
        return None
    did = None
    if depot_id not in (None, "", 0):
        try:
            did = int(depot_id)
        except (TypeError, ValueError):
            return None

    depot_s = f" AND s.depot_id={did}" if did is not None else ""
    depot_c = f" AND c.depot_id={did}" if did is not None else ""
    in_cond = (
        f"(h.type='入库' AND i.depot_id={did}) OR (h.sub_type='调拨' AND i.another_depot_id={did})"
        if did is not None else "h.type='入库'"
    )
    out_cond = (
        f"(h.type='出库' AND i.depot_id={did})"
        if did is not None else "h.type='出库' AND h.sub_type!='调拨'"
    )
    qty_expr = "IFNULL(i.basic_number, i.oper_number)"
    sql = (
        "SELECT "
        "(SELECT COALESCE(SUM(s.number),0) FROM jsh_material_initial_stock s "
        f" WHERE s.material_id={mid} AND IFNULL(s.delete_flag,'0')!='1'{depot_s}) AS init_qty, "
        f"(SELECT COALESCE(SUM(CASE WHEN ({in_cond}) THEN {qty_expr} ELSE 0 END),0) "
        " FROM jsh_depot_item i JOIN jsh_depot_head h ON h.id=i.header_id "
        f" WHERE i.material_id={mid} AND IFNULL(i.delete_flag,'0')!='1' "
        f"   AND {_posted_where('h')}) AS in_sum, "
        f"(SELECT COALESCE(SUM(CASE WHEN ({out_cond}) THEN {qty_expr} ELSE 0 END),0) "
        " FROM jsh_depot_item i JOIN jsh_depot_head h ON h.id=i.header_id "
        f" WHERE i.material_id={mid} AND IFNULL(i.delete_flag,'0')!='1' "
        f"   AND {_posted_where('h')}) AS out_sum, "
        "(SELECT COALESCE(SUM(c.current_number),0) FROM jsh_material_current_stock c "
        f" WHERE c.material_id={mid} AND IFNULL(c.delete_flag,'0')!='1'{depot_c}) AS cur_qty"
    )
    try:
        rows = _q(sql, limit=5)
    except Exception:
        return None
    if not rows:
        return None
    r = rows[0]
    return {
        "initial": _f(r.get("init_qty")),
        "in_sum": _f(r.get("in_sum")),
        "out_sum": _f(r.get("out_sum")),
        "current": _f(r.get("cur_qty")),
    }


def low_stock_by_depot():
    """以安全库存设置表为左表查分仓缺口（即使某仓当前无库存行也不会漏报）。"""
    t = tenant_subquery()
    sql = (
        "SELECT s.material_id, IFNULL(d.name,'') AS depot, "
        "IFNULL(c.current_number, 0) AS current_number, s.low_safe_stock "
        "FROM jsh_material_initial_stock s "
        "JOIN jsh_material m ON m.id = s.material_id AND IFNULL(m.delete_flag,'0')!='1' "
        "LEFT JOIN jsh_material_current_stock c ON c.material_id = s.material_id "
        "  AND c.depot_id = s.depot_id AND IFNULL(c.delete_flag,'0')!='1' "
        "LEFT JOIN jsh_depot d ON d.id = s.depot_id "
        f"WHERE IFNULL(s.delete_flag,'0')!='1' AND m.tenant_id={t} "
        "  AND s.low_safe_stock IS NOT NULL AND s.low_safe_stock > 0 "
        "  AND IFNULL(c.current_number, 0) < s.low_safe_stock"
    )
    try:
        rows = _q(sql, limit=5000)
    except Exception:
        return {}
    out = {}
    for r in rows:
        mid = _i(r.get("material_id"))
        if not mid:
            continue
        cur = _f(r.get("current_number"))
        low = _f(r.get("low_safe_stock"))
        gap = low - cur
        if mid not in out or gap > out[mid]["gap"]:
            out[mid] = {"depot": str(r.get("depot") or ""), "stock": cur, "low": low, "gap": gap}
    return out


# --------------------------------------------------------------------------
# 5. 单据明细检索（SQL 直查：不受 REST 翻页上限影响，先全量汇总再截断展示）
# --------------------------------------------------------------------------
def query_bills(party="", date="", date_from="", date_to="", material="", sub_type="", limit=20):
    t = tenant_subquery()
    wheres = [_posted_where("h")]
    if party:
        wheres.append(f"IFNULL(s.supplier,'') LIKE '%{_like_esc(party)}%'")
    if date:
        wheres.append(f"DATE_FORMAT(h.oper_time,'%Y-%m-%d')='{_esc(date)}'")
    else:
        if date_from:
            wheres.append(f"h.oper_time >= '{_esc(date_from)} 00:00:00'")
        if date_to:
            wheres.append(f"h.oper_time <= '{_esc(date_to)} 23:59:59'")
    if sub_type:
        wheres.append(f"h.sub_type='{_esc(sub_type)}'")
    if material:
        mat_like = _like_esc(material)
        wheres.append(
            "EXISTS (SELECT 1 FROM jsh_depot_item di "
            "JOIN jsh_material m ON m.id = di.material_id "
            "LEFT JOIN jsh_material_extend me ON me.id = di.material_extend_id "
            "WHERE di.header_id = h.id AND IFNULL(di.delete_flag,'0')!='1' "
            f"AND (m.name LIKE '%{mat_like}%' OR IFNULL(m.model,'') LIKE '%{mat_like}%' "
            f"OR IFNULL(me.bar_code,'') LIKE '%{mat_like}%'))"
        )

    sql = (
        "SELECT h.id, h.number, DATE_FORMAT(h.oper_time,'%Y-%m-%d') AS d, "
        "h.type, h.sub_type, IFNULL(s.supplier,'—') AS party, "
        f"{_gross_expr('h')} AS total, "
        f"GREATEST(({_gross_expr('h')} + IFNULL(h.other_money,0) - IFNULL(h.deposit,0) "
        "- ABS(IFNULL(h.change_amount,0)) - IFNULL(fin.settled,0)), 0) AS unpaid "
        "FROM jsh_depot_head h "
        "LEFT JOIN jsh_supplier s ON s.id = h.organ_id "
        "LEFT JOIN ("
        "  SELECT ai.bill_id, SUM(ABS(IFNULL(ai.each_amount,0))) AS settled "
        "  FROM jsh_account_item ai JOIN jsh_account_head ah ON ah.id = ai.header_id "
        "  WHERE ai.bill_id IS NOT NULL AND IFNULL(ai.delete_flag,'0')!='1' "
        f"    AND {_fin_posted_where('ah')} "
        "  GROUP BY ai.bill_id"
        ") fin ON fin.bill_id = h.id "
        "WHERE " + " AND ".join(wheres) + " "
        "ORDER BY h.oper_time DESC, h.id DESC"
    )
    try:
        all_rows = _q(sql, limit=5000)
    except Exception:
        return None

    hits = []
    sale_n = sale_amt = sale_ret_n = sale_ret_amt = 0.0
    buy_n = buy_amt = buy_ret_n = buy_ret_amt = 0.0
    for r in all_rows:
        st = str(r.get("sub_type") or "")
        tot = round(_f(r.get("total")), 2)
        unpaid = round(_f(r.get("unpaid")), 2)
        if st in SELL_SUBS:
            sale_n += 1
            sale_amt += tot
        elif st in SELL_BACK_SUBS:
            sale_ret_n += 1
            sale_ret_amt += tot
        elif st in BUY_SUBS:
            buy_n += 1
            buy_amt += tot
        elif st in BUY_BACK_SUBS:
            buy_ret_n += 1
            buy_ret_amt += tot
        hits.append({
            "id": _i(r.get("id")),
            "number": str(r.get("number") or ""),
            "time": str(r.get("d") or ""),
            "type": f"{r.get('type') or ''}·{st}",
            "sub_type": st,
            "party": str(r.get("party") or "—"),
            "total": tot,
            "debt": unpaid,
            "goods": "",
            "items": [],
        })

    shown = hits[:max(1, int(limit or 20))]
    if shown:
        id_list = ",".join(str(x["id"]) for x in shown if x["id"])
        if id_list:
            det_sql = (
                "SELECT di.header_id, IFNULL(m.name,'?') AS name, IFNULL(m.model,'') AS model, "
                "di.oper_number AS qty, IFNULL(di.material_unit, IFNULL(m.unit,'')) AS unit, "
                "IFNULL(di.basic_number, di.oper_number) AS basic_qty, "
                "di.unit_price AS price, di.all_price AS amount "
                "FROM jsh_depot_item di "
                "LEFT JOIN jsh_material m ON m.id = di.material_id "
                f"WHERE di.header_id IN ({id_list}) AND IFNULL(di.delete_flag,'0')!='1' "
                "ORDER BY di.id ASC"
            )
            try:
                drows = _q(det_sql, limit=2000)
            except Exception:
                drows = []
            by_hid = {}
            for dr in drows:
                hid = _i(dr.get("header_id"))
                nm = str(dr.get("name") or "?")
                mo = str(dr.get("model") or "").strip()
                label = f"{nm} {mo}".strip() if mo and mo not in nm else nm
                by_hid.setdefault(hid, []).append({
                    "name": label,
                    "qty": _f(dr.get("qty")),
                    "unit": str(dr.get("unit") or ""),
                    "basic_qty": _f(dr.get("basic_qty")),
                    "price": round(_f(dr.get("price")), 4),
                    "amount": round(_f(dr.get("amount")), 2),
                })
            for h in shown:
                its = by_hid.get(h["id"], [])
                h["items"] = its[:15]
                h["goods"] = "、".join(it["name"] for it in its[:5])[:60]

    for h in shown:
        h.pop("id", None)
        h.pop("sub_type", None)

    total_count = len(hits)
    total_amount = round(sum(x["total"] for x in hits), 2)
    return_amount = round(sale_ret_amt + buy_ret_amt, 2)
    sale_net = round(sale_amt - sale_ret_amt, 2)
    buy_net = round(buy_amt - buy_ret_amt, 2)
    if (buy_n or buy_ret_n) and not (sale_n or sale_ret_n):
        main_amt, net_amt = round(buy_amt, 2), buy_net
    elif (sale_n or sale_ret_n) and not (buy_n or buy_ret_n):
        main_amt, net_amt = round(sale_amt, 2), sale_net
    else:
        main_amt = round(sale_amt + buy_amt, 2)
        net_amt = round(sale_net + buy_net, 2)

    return {
        "filters": {"party": party, "date": date, "date_from": date_from,
                    "date_to": date_to, "material": material, "sub_type": sub_type},
        "count": len(shown),
        "total_count": total_count,
        "total_amount": total_amount,
        "sale_count": int(sale_n),
        "sale_amount": round(sale_amt, 2),
        "sale_return_count": int(sale_ret_n),
        "sale_return_amount": round(sale_ret_amt, 2),
        "sale_net_amount": sale_net,
        "buy_count": int(buy_n),
        "buy_amount": round(buy_amt, 2),
        "buy_return_count": int(buy_ret_n),
        "buy_return_amount": round(buy_ret_amt, 2),
        "buy_net_amount": buy_net,
        "non_return_amount": main_amt,
        "return_amount": return_amount,
        "net_amount": net_amt,
        "truncated": total_count > len(shown),
        "bills": shown,
    }


def party_history_sql(party, limit=5):
    """往来单位最近单据 + 全量未结清欠款总额（给录单校验用）。"""
    res = query_bills(party=party, limit=limit)
    if res is None:
        return None
    t = tenant_subquery()
    sql = (
        "SELECT COUNT(*) AS unpaid_n, "
        f"COALESCE(SUM(GREATEST(({_gross_expr('h')} + IFNULL(h.other_money,0) - IFNULL(h.deposit,0) "
        "- ABS(IFNULL(h.change_amount,0)) - IFNULL(fin.settled,0)), 0)), 0) AS debt_total "
        "FROM jsh_depot_head h "
        "LEFT JOIN jsh_supplier s ON s.id = h.organ_id "
        "LEFT JOIN ("
        "  SELECT ai.bill_id, SUM(ABS(IFNULL(ai.each_amount,0))) AS settled "
        "  FROM jsh_account_item ai JOIN jsh_account_head ah ON ah.id = ai.header_id "
        "  WHERE ai.bill_id IS NOT NULL AND IFNULL(ai.delete_flag,'0')!='1' "
        f"    AND {_fin_posted_where('ah')} "
        "  GROUP BY ai.bill_id"
        ") fin ON fin.bill_id = h.id "
        f"WHERE {_posted_where('h')} AND h.sub_type IN ('采购','销售','零售') "
        f"AND IFNULL(s.supplier,'') LIKE '%{_like_esc(party)}%' "
        f"AND ({_gross_expr('h')} + IFNULL(h.other_money,0) - IFNULL(h.deposit,0) "
        "- ABS(IFNULL(h.change_amount,0)) - IFNULL(fin.settled,0)) > 0.005"
    )
    try:
        drows = _q(sql, limit=5)
        debt_total = round(_f(drows[0].get("debt_total")), 2) if drows else 0.0
        unpaid_n = _i(drows[0].get("unpaid_n")) if drows else 0
    except Exception:
        debt_total = round(sum(b.get("debt", 0.0) for b in res["bills"]), 2)
        unpaid_n = sum(1 for b in res["bills"] if b.get("debt", 0.0) > 0.005)
    bills = [{
        "number": b["number"], "time": b["time"],
        "type": b["type"].replace("·", ""), "total": b["total"], "debt": b["debt"],
    } for b in res["bills"]]
    return {
        "party": party,
        "count": len(bills),
        "total_count": res["total_count"],
        "bills": bills,
        "debt_total": debt_total,
        "unpaid_bills_count": unpaid_n,
    }
