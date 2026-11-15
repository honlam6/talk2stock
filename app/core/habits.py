#!/usr/bin/env python3
"""操作员习惯记忆层（P-B / ADR-11）：确认写入的单据沉淀为操作员偏好，
录单校验 agent 用它补默认值（常用仓库 / 历史成交价）。

设计：
- 只增不改的 jsonl（app/data/habits.jsonl），一行一条；损坏行跳过不抛错；
- record 在 commit 成功后调用（server 钩子），失败绝不阻断业务；
- summary 按操作员聚合：depot 习惯（按业务方向，优先同往来单位的口径）、
  历史成交价（优先「往来单位×商品」，退回「商品」全口径）——校验 agent 的
  habit_defaults 工具直接吃这份摘要。
"""
import json
import os
import threading
import time

from . import config as cfgmod
from .draft import BILL_TYPES

_PATH = os.path.join(cfgmod.DATA_DIR, "habits.jsonl")
_LOCK = threading.Lock()


def _norm(s):
    return str(s or "").strip().lower()


def record(operator, draft, res):
    """commit 成功后沉淀一条习惯（失败静默——记忆是增益，不是依赖）。"""
    try:
        rec = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "operator": operator or "",
            "biz": draft.get("biz") or "",
            "party": draft.get("party") or "",
            "depot": draft.get("depot") or "",
            "items": [{"name": it.get("name"), "unit": it.get("unit"), "price": it.get("price")}
                      for it in (draft.get("items") or [])],
            "number": res.get("number") or "",
        }
        with _LOCK:
            with open(_PATH, "a", encoding="utf8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _load():
    recs = []
    if not os.path.isfile(_PATH):
        return recs
    try:
        with open(_PATH, encoding="utf8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                    if isinstance(r, dict):
                        recs.append(r)
                except Exception:
                    continue
    except Exception:
        return []
    return recs


def _direction(biz):
    try:
        return BILL_TYPES[biz][3]
    except Exception:
        return ""


def summary(operator="", party="", biz=""):
    """聚合习惯摘要。

    operator 不传 = 全局习惯（新操作员冷启动也有合理默认）；
    party 不传 = 全局 depot 口径；biz 用于过滤业务方向（买/卖的仓库习惯分开）。
    """
    recs = _load()
    if operator:
        mine = [r for r in recs if r.get("operator") == operator]
        recs = mine or recs          # 本人没记录时退回全局口径
    want_dir = _direction(biz)
    if want_dir:
        recs = [r for r in recs if _direction(r.get("biz")) == want_dir]

    # ---- depot 习惯：优先同往来单位，退回全口径 ----
    depot = ""
    if party:
        same_party = [r for r in recs if _norm(r.get("party")) == _norm(party) and (r.get("depot") or "").strip()]
        if same_party:
            counts = {}
            for r in same_party:
                counts[r["depot"]] = counts.get(r["depot"], 0) + 1
            depot = max(counts, key=counts.get)
    if not depot:
        counts = {}
        for r in recs:
            d = (r.get("depot") or "").strip()
            if d:
                counts[d] = counts.get(d, 0) + 1
        if counts:
            depot = max(counts, key=counts.get)

    # ---- 价格习惯：最近一笔优先（jsonl 追加序 = 时间序，倒序遍历先命中最近的）；----
    # ----（往来单位×商品）优先于（商品）全口径 ----
    price_pair, price_global = {}, {}
    for r in reversed(recs):
        same_party_rec = bool(party) and _norm(r.get("party")) == _norm(party)
        for it in r.get("items") or []:
            try:
                price = float(it.get("price"))
            except (TypeError, ValueError):
                continue
            if price <= 0:
                continue
            key = _norm(it.get("name"))
            if not key:
                continue
            if same_party_rec:
                price_pair.setdefault(key, price)      # 倒序先到 = 最近一笔
            price_global.setdefault(key, price)

    prices = {}
    if party:
        for k, v in price_pair.items():
            prices[k] = {"price": v, "scope": "该往来单位"}
    for k, v in price_global.items():
        prices.setdefault(k, {"price": v, "scope": "全口径"})

    return {"depot": depot, "prices": prices, "records": len(recs)}
