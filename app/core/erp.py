#!/usr/bin/env python3
"""jshERP REST 客户端 —— 纯标准库（urllib），带缓存与主数据 ensure 能力。

关键规范（实测固化，勿改）：
- 登录密码必须 MD5
- body 绝不传 tenantId
- 建商品后必须建 materialsExtend 条码，否则建单报 barCode cannot be null
- 列表查询条件必须包进 search JSON 参数
- info/rows 是"字符串化的 JSON"
"""
import hashlib
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

TIMEOUT = 30

# 单据前缀
PREFIX = {
    "采购": "CGRK", "销售": "XS", "采购退货": "CGTH", "销售退货": "XSTH",
    "其它": "QTRK", "调拨": "DBCK", "盘点录入": "PD",
    "采购订单": "CGDD", "销售订单": "XSDD",
}

# 业务类型 → (type, subType, 往来单位类型, 出入方向)
BILL_TYPES = {
    "purchase_in":     ("入库", "采购",     "供应商", "in"),
    "sale_out":        ("出库", "销售",     "客户",   "out"),
    "purchase_return": ("出库", "采购退货", "供应商", "out"),
    "sale_return":     ("入库", "销售退货", "客户",   "in"),
    "other_in":        ("入库", "其它",     "供应商", "in"),
    "other_out":       ("出库", "其它",     "客户",   "out"),
}

# 查单据时 subType 必传（后端 getBillCategory 空指针），这里列全
SUB_TYPES_OF = {
    "入库": ["采购", "销售退货", "其它", "盘点录入", "组装单", "拆卸单"],
    "出库": ["销售", "采购退货", "其它", "调拨", "组装单", "拆卸单"],
    "其它": ["采购订单", "销售订单", "采购申请"],
}
ALL_SUB_TYPES = ["采购", "销售", "采购退货", "销售退货", "其它", "调拨",
                 "盘点录入", "采购订单", "销售订单", "零售", "零售退货",
                 "组装单", "拆卸单", "采购申请"]


class ErpError(Exception):
    pass


def _safe(fn):
    try:
        return fn()
    except Exception:
        return None


def parallel(fns, workers=6):
    """并行执行一组无参函数，保持顺序；单个失败该位为 None，不中断其它。
    纯标准库（concurrent.futures），用于把对 ERP 的多次独立请求并行化。"""
    fns = list(fns)
    if len(fns) <= 1:
        return [_safe(fn) for fn in fns]
    with ThreadPoolExecutor(max_workers=min(workers, len(fns))) as ex:
        return list(ex.map(_safe, fns))


class JshERP:
    def __init__(self, base="http://localhost:9999/jshERP-boot", user="jsh", password="123456"):
        self.base = base.rstrip("/")
        self.user = user
        self.password = password
        self.token = None
        self._cache = {}
        self._cache_at = {}
        self._login_lock = threading.Lock()
        self._stock_tbl = None          # (条码 → 仓库 → 净库存) 聚合表，见 stock_table()
        self._stock_tbl_at = 0.0
        self._stock_lock = threading.Lock()

    # ---------- 底层请求 ----------
    def _req(self, path, method="GET", data=None, retry_login=True):
        url = f"{self.base}{path}"
        body = None
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["X-Access-Token"] = self.token
        if data is not None:
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                raw = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
        except Exception as e:
            raise ErpError(f"无法连接 ERP（{self.base}）：{e}")
        try:
            j = json.loads(raw)
        except Exception:
            # 会话失效时后端可能直接回 "loginOut" 纯文本而非 JSON code——重登一次再试
            if retry_login and "loginOut" in raw:
                self.login()
                return self._req(path, method, data, retry_login=False)
            raise ErpError(f"ERP 返回非 JSON：{raw[:200]}")
        # token 过期自动重登
        if retry_login and isinstance(j, dict) and j.get("code") in (510, 401):
            self.login()
            return self._req(path, method, data, retry_login=False)
        return j

    def login(self):
        # 加锁：ThreadingHTTPServer 下多线程共用本实例，token 过期时
        # 并发 401 重登会互相踩踏（后者可能顶掉前者刚拿到的会话）
        with self._login_lock:
            pwd = hashlib.md5(self.password.encode()).hexdigest()
            self.token = None
            j = self._req("/user/login", "POST",
                          {"loginName": self.user, "password": pwd}, retry_login=False)
            tok = (j.get("data") or {}).get("token")
            if j.get("code") == 200 and tok:
                self.token = tok
                self._cache.clear()
                self._stock_tbl = None
                return True
            raise ErpError(f"ERP 登录失败：{j.get('data', {}).get('msgTip') or j}")

    def ensure_login(self):
        if not self.token:
            self.login()

    # ---------- 列表查询（带 60s 缓存） ----------
    def list(self, path, search=None, page_size=500, use_cache=True):
        ck = f"{path}|{json.dumps(search or {}, sort_keys=True, ensure_ascii=False)}"
        now = time.time()
        if use_cache and ck in self._cache and now - self._cache_at.get(ck, 0) < 60:
            return self._cache[ck]
        self.ensure_login()
        url = path
        if search:
            url += "?search=" + urllib.parse.quote(json.dumps(search, ensure_ascii=False))
        url += ("&" if "?" in url else "?") + f"pageNo=1&pageSize={page_size}"
        rows = (self._req(url).get("data") or {}).get("rows") or []
        self._cache[ck] = rows
        self._cache_at[ck] = now
        return rows

    def invalidate(self, prefix=""):
        for k in list(self._cache):
            if k.startswith(prefix):
                self._cache.pop(k, None)
                self._cache_at.pop(k, None)
        # 单据/明细可能变了，库存聚合表一并作废
        self._stock_tbl = None

    # ---------- 主数据读取 ----------
    def suppliers(self, stype=None):
        rows = self.list("/supplier/list", {"type": stype} if stype else {})
        return [r for r in rows if str(r.get("enabled", 1)) not in ("0", "False", "false")]

    def materials(self):
        return self.list("/material/list", {})

    def depots(self):
        return self.list("/depot/list", {})

    def accounts(self):
        return self.list("/account/list", {})

    def units(self):
        return self.list("/unit/list", {})

    def unit_group(self, unit_name):
        """按单位名找到它所属的单位组。

        两遍匹配：先认「名称 / 基本单位」，再认「副单位1-3」。
        否则 "箱" 会先命中别人的副单位（如 瓶/(箱=12瓶)），
        给新商品建错基本单位。
        """
        n = self._norm(unit_name)
        if not n:
            return None
        rows = self.units()
        for u in rows:
            if self._norm(u.get("name")) == n or self._norm(u.get("basicUnit")) == n:
                return u
        for u in rows:
            for k in ("otherUnit", "otherUnitTwo", "otherUnitThree"):
                if self._norm(u.get(k)) == n:
                    return u
        return None

    def unit_group_by_id(self, uid):
        if not uid:
            return None
        for u in self.units():
            if str(u.get("id")) == str(uid):
                return u
        return None

    def unit_allowed(self, group, unit_name):
        """该单位是否属于这个单位组（可参与换算）。"""
        if not group:
            return False
        n = self._norm(unit_name)
        return any(self._norm(group.get(k)) == n for k in
                   ("basicUnit", "otherUnit", "otherUnitTwo", "otherUnitThree") if group.get(k))

    def unit_ratio(self, group, unit_name):
        """单位相对基本单位的换算系数（基本单位=1）。"""
        if not group:
            return 1.0
        n = self._norm(unit_name)
        for key, ratio_key in (("otherUnit", "ratio"), ("otherUnitTwo", "ratioTwo"),
                               ("otherUnitThree", "ratioThree")):
            if self._norm(group.get(key)) == n:
                try:
                    return float(group.get(ratio_key) or 1) or 1.0
                except (TypeError, ValueError):
                    return 1.0
        return 1.0

    def price_tax_flag(self):
        """系统是否按含税价录入（materialPriceTaxFlag=1 表示含税）。"""
        if getattr(self, "_tax_flag", None) is None:
            flag = False
            try:
                j = self._req("/systemConfig/getCurrentInfo")
                flag = str((j.get("data") or {}).get("materialPriceTaxFlag") or "0") == "1"
            except Exception:
                flag = False
            self._tax_flag = flag
        return self._tax_flag

    def categories(self):
        """商品分类列表（扁平）。getAllList 需要 parentId，从根 0 递归取两层。"""
        out = []
        seen = set()

        def walk(pid, depth=0):
            if depth > 3:
                return
            j = self._req(f"/materialCategory/getAllList?parentId={pid}")
            rows = j.get("data")
            if not isinstance(rows, list):
                return
            for r in rows:
                rid = r.get("id")
                if rid in seen:
                    continue
                seen.add(rid)
                out.append(r)
                walk(rid, depth + 1)

        walk(0)
        return out

    def in_out_items(self):
        return self.list("/inOutItem/list", {})

    # ---------- 主数据匹配 ----------
    @staticmethod
    def _norm(s):
        return (str(s or "")).strip().lower().replace(" ", "")

    def _ranked_candidates(self, rows, field, name, n=3, min_score=0.35):
        """没匹配上时给 top-N 相似候选（编辑相似度 + 前缀加分），供用户点选。"""
        import difflib
        q = self._norm(name)
        if not q:
            return []
        scored = []
        for r in rows:
            v = str(r.get(field) or "").strip()
            nv = self._norm(v)
            if not nv or nv == q:
                continue
            score = difflib.SequenceMatcher(None, q, nv).ratio()
            if nv.startswith(q) or q.startswith(nv):
                score += 0.15
            scored.append((score, v))
        scored.sort(key=lambda x: (-x[0], x[1]))
        return [v for sc, v in scored[:n] if sc >= min_score]

    def party_candidates(self, name, ptype, n=3):
        return self._ranked_candidates(self.suppliers(ptype), "supplier", name, n)

    def material_candidates(self, name, n=3):
        return self._ranked_candidates(self.materials(), "name", name, n)

    def find_party(self, name, ptype):
        """精确 → 忽略大小写空格 → 包含。返回 (row, match_kind)"""
        if not name:
            return None, "none"
        rows = self.suppliers(ptype)
        n = self._norm(name)
        for r in rows:
            if str(r.get("supplier", "")).strip() == str(name).strip():
                return r, "exact"
        for r in rows:
            if self._norm(r.get("supplier")) == n:
                return r, "exact"
        cands = [r for r in rows if n and (n in self._norm(r.get("supplier")) or self._norm(r.get("supplier")) in n)]
        if len(cands) == 1:
            return cands[0], "fuzzy"
        return None, "none"

    def find_material(self, name):
        if not name:
            return None, "none"
        rows = self.materials()
        n = self._norm(name)
        for r in rows:
            if str(r.get("name", "")).strip() == str(name).strip():
                return r, "exact"
        for r in rows:
            if self._norm(r.get("name")) == n:
                return r, "exact"
        cands = [r for r in rows if n and (n in self._norm(r.get("name")) or self._norm(r.get("name")) in n)]
        if len(cands) == 1:
            return cands[0], "fuzzy"
        return None, "none"

    def find_depot(self, name):
        if not name:
            return None, "none"
        rows = self.depots()
        n = self._norm(name)
        for r in rows:
            if self._norm(r.get("name")) == n:
                return r, "exact"
        cands = [r for r in rows if n and n in self._norm(r.get("name"))]
        if len(cands) == 1:
            return cands[0], "fuzzy"
        return None, "none"

    # ---------- 主数据创建（ensure_*） ----------
    def ensure_unit(self, name):
        name = (name or "个").strip()
        u = self.unit_group(name)
        if u:
            return u
        self._req("/unit/add", "POST", {
            "basicUnit": name, "otherUnit": "", "ratio": 0,
            "name": name, "enabled": True,
        })
        self.invalidate("/unit/list")
        return self.unit_group(name) or {"name": name, "basicUnit": name}

    def ensure_party(self, name, ptype):
        row, _ = self.find_party(name, ptype)
        if row:
            return row, False
        res = self._req("/supplier/add", "POST",
                        {"supplier": name, "type": ptype, "enabled": 1})
        if res.get("code") != 200:
            raise ErpError(f"新建{ptype}「{name}」失败：{res}")
        self.invalidate("/supplier/list")
        row, _ = self.find_party(name, ptype)
        if not row:
            raise ErpError(f"新建{ptype}「{name}」后查不到")
        return row, True

    def ensure_depot(self, name):
        row, _ = self.find_depot(name)
        if row:
            return row, False
        res = self._req("/depot/add", "POST", {"name": name, "enabled": 1, "type": 0, "sort": "1"})
        if res.get("code") != 200:
            raise ErpError(f"新建仓库「{name}」失败：{res}")
        self.invalidate("/depot/list")
        row, _ = self.find_depot(name)
        if not row:
            raise ErpError(f"新建仓库「{name}」后查不到")
        return row, True

    def _default_category_id(self):
        cats = self.categories()
        if cats:
            return cats[0].get("id")
        return None

    def material_barcode(self, material_id, unit="个"):
        """取商品条码；没有则建一个 BC<id>。
        注意 getDetailList 用平铺 materialId 参数，不能包 search（会 500）。"""
        try:
            j = self._req(f"/materialsExtend/getDetailList?materialId={material_id}")
            rows = (j.get("data") or {}).get("rows") or []
            for r in rows:
                if r.get("barCode"):
                    return r["barCode"]
        except Exception:
            pass
        bc = f"BC{material_id}"
        self._req("/materialsExtend/add", "POST", {
            "materialId": material_id, "barCode": bc,
            "commodityUnit": unit, "defaultFlag": 1,
        })
        return bc

    def ensure_material(self, name, unit="个", category_id=None, model="", standard=""):
        row, _ = self.find_material(name)
        created = False
        if not row:
            urow = self.ensure_unit(unit)
            basic = urow.get("basicUnit") or unit
            body = {
                "name": name, "model": model, "standard": standard,
                "unit": basic, "enabled": 1,
                "categoryId": category_id or self._default_category_id(),
            }
            if urow.get("id"):
                # 关键：带上 unitId，后端才能按单位组的 ratio 自动折算 basicNumber
                body["unitId"] = urow["id"]
            res = self._req("/material/add", "POST", body)
            if res.get("code") != 200:
                raise ErpError(f"新建商品「{name}」失败：{res}")
            self.invalidate("/material/list")
            row, _ = self.find_material(name)
            if not row:
                raise ErpError(f"新建商品「{name}」后查不到")
            created = True
        bc = row.get("barCode") or self.material_barcode(row["id"], row.get("unit") or unit)
        row = dict(row, barCode=bc)
        return row, created

    # ---------- 库存 ----------
    # 分仓库存的扫描口径（type, subType, 方向），与出入库明细一致
    STOCK_SCAN = [
        ("入库", ["采购", "其它", "盘点录入", "销售退货"], 1.0),
        ("出库", ["销售", "采购退货", "其它", "调拨"], -1.0),
    ]

    def stock_table(self, max_age=60):
        """一次性把全部出入库明细拉平，按 (条码 → 仓库 → 净库存) 聚合。

        旧实现 _stock_by_depot 是 O(仓 × 子类型 × 单据) 次串行请求
        （按仓循环 × 每单一次 getDetailList，5 仓就能把一次库存问答拖到 10s+）。
        这里固定为 (子类型组 + 单据数) 次并行请求，结果缓存 60s，
        写入后由 invalidate() 清空。⚠️ getDetailList 的明细行没有 materialId，
        统一用 barCode（= BC<商品id>）锁定商品。
        """
        now = time.time()
        if self._stock_tbl is not None and now - self._stock_tbl_at < max_age:
            return self._stock_tbl
        with self._stock_lock:
            if self._stock_tbl is not None and time.time() - self._stock_tbl_at < max_age:
                return self._stock_tbl
            sweeps = [(btype, st, sign)
                      for btype, subs, sign in self.STOCK_SCAN for st in subs]
            bill_lists = parallel([
                lambda bt=bt, st=st: self.bills(btype=bt, sub_type=st, page_size=500)
                for bt, st, _ in sweeps
            ], workers=8)
            sign_of = {(bt, st): sign for bt, st, sign in sweeps}
            all_bills_ = []
            for rows in bill_lists:
                if rows:
                    all_bills_.extend(rows)
            details = parallel([
                lambda b=b: self._bill_details(b.get("id")) for b in all_bills_
            ], workers=8)
            tbl = {}
            for b, rows in zip(all_bills_, details):
                sign = sign_of.get((b.get("type") or "", b.get("subType") or ""), 0.0)
                if not sign:
                    continue
                for r in rows or []:
                    bc = str(r.get("barCode") or "")
                    if not bc:
                        continue
                    try:
                        qty = sign * float(r.get("operNumber") or 0)
                    except Exception:
                        continue
                    depot = str(r.get("depotId") or "")
                    tbl.setdefault(bc, {}).setdefault(depot, 0.0)
                    tbl[bc][depot] += qty
            self._stock_tbl = tbl
            self._stock_tbl_at = time.time()
            return tbl

    def _bill_details(self, header_id):
        try:
            j = self._req(f"/depotItem/getDetailList?headerId={header_id}")
            return (j.get("data") or {}).get("rows") or []
        except Exception:
            return []

    def _barcode_of(self, material_id):
        """商品条码（BC<id>），从 materials 列表行取，不额外发请求。"""
        for m in self.materials():
            if str(m.get("id")) == str(material_id):
                return str(m.get("mBarCode") or m.get("barCode") or "")
        return ""

    def stock(self, material_id, depot_id=None):
        """返回某商品（指定仓库时按仓库）的实时库存数量。

        ⚠️ 版本兼容：/depotItem/getStockByParam 在本环境 404，
        material 列表行自带全仓合计 stock 字段；分仓数据用 stock_table()
        聚合表查（一次拉平 + 缓存），不再逐仓逐单扫描。
        """
        try:
            if depot_id:
                bc = self._barcode_of(material_id)
                if not bc:
                    return None
                per_depot = self.stock_table().get(bc) or {}
                total = sum(v for k, v in per_depot.items() if str(k) == str(depot_id))
                return float(total)
            rows = self.materials()
            for m in rows:
                if str(m.get("id")) == str(material_id):
                    s = m.get("stock")
                    if s is not None:
                        return float(s)
            # material 行没有 stock 字段（理论上不该发生），降级到全仓聚合
            bc = self._barcode_of(material_id)
            if not bc:
                return None
            per_depot = self.stock_table().get(bc) or {}
            return float(sum(per_depot.values()))
        except Exception:
            return None

    # ---------- 建单 ----------
    def build_number(self, sub_type):
        pre = PREFIX.get(sub_type, "BILL")
        try:
            j = self._req(f"/sequence/buildNumber?type={pre}")
            n = (j.get("data") or {}).get("defaultNumber")
            if n:
                return n
        except Exception:
            pass
        return pre + time.strftime("%Y%m%d") + f"{int(time.time()) % 10000:04d}"

    def create_bill(self, biz, organ_id, rows, oper_time=None, number=None,
                    account_id=25, pay_type="现付", remark="", status="1",
                    another_depot_id=None, file_name="", pay_amount=None):
        """rows: [{materialId, barCode, name, operNumber, unit, unitPrice, allPrice, depotId, remark}]

        file_name: 附件路径逗号串（如 "bill/a.jpg,bill/b.pdf"），对应 DepotHead.fileName。
        对应原版 jshERP 单据附件功能（/systemConfig/upload 上传后回写），最多 4 份。

        pay_amount: 本次实付金额（收款/付款）。None 表示全款结清（changeAmount=total）。
        欠款由 ERP 后端自动计算（debt = 应收 - 预收/优惠 - 本次实付），无需前端传。
        原版要求 accountIdList / accountMoneyList 为数组（即使单账户），否则收付款流水缺失。
        """
        self.ensure_login()
        btype, sub_type, _, _ = BILL_TYPES[biz]

        # 含税/未税口径：以 ERP 的 materialPriceTaxFlag 为准
        #  - 含税（True）：录入价即含税价，allPrice 为含税总额，taxMoney = allPrice/(1+r)*r
        #  - 未税（False）：allPrice 为不含税金额，taxMoney = allPrice*r，taxLastMoney = allPrice+taxMoney
        tax_inclusive = self.price_tax_flag()
        for r in rows:
            try:
                rate = float(r.get("taxRate") or r.get("tax_rate") or 0) or 0.0
            except (TypeError, ValueError):
                rate = 0.0
            if rate <= 0:
                continue
            all_price = round(float(r.get("allPrice") or 0), 2)
            if tax_inclusive:
                real = round(all_price / (1 + rate / 100.0), 2)
                tax_money = round(real * rate / 100.0, 2)
                tax_last = all_price
            else:
                tax_money = round(all_price * rate / 100.0, 2)
                tax_last = round(all_price + tax_money, 2)
            r["taxRate"] = rate
            r["taxMoney"] = tax_money
            r["taxLastMoney"] = tax_last

        total = round(sum(
            float(r["taxLastMoney"]) if r.get("taxLastMoney") is not None
            else float(r.get("allPrice") or 0)
            for r in rows), 2)
        # 本次实付：没填则视为全额结清
        change = round(float(pay_amount), 2) if pay_amount not in (None, "") else total
        info = {
            "type": btype,
            "subType": sub_type,
            "organId": organ_id,
            "operTime": oper_time or time.strftime("%Y-%m-%d %H:%M:%S"),
            "number": number or self.build_number(sub_type),
            "accountId": account_id,
            "accountIdList": json.dumps([account_id], ensure_ascii=False),
            "accountMoneyList": json.dumps([change], ensure_ascii=False),
            "changeAmount": change,
            "discountLastMoney": total,
            "totalPrice": total,
            "payType": pay_type,
            "remark": remark,
            "status": status,
        }
        if file_name:
            info["fileName"] = file_name
        if another_depot_id:
            for r in rows:
                r["anotherDepotId"] = another_depot_id
        res = self._req("/depotHead/addDepotHeadAndDetail", "POST", {
            "info": json.dumps(info, ensure_ascii=False),
            "rows": json.dumps(rows, ensure_ascii=False),
        })
        ok = res.get("code") == 200
        return {"ok": ok, "number": info["number"], "total": total,
                "raw": res, "message": "" if ok else str(res)}

    def create_payment(self, ptype, organ_id, amount, bill_no="",
                       account_id=25, in_out_item_id=None, remark="货款"):
        """ptype: 收款 / 付款"""
        self.ensure_login()
        if in_out_item_id is None:
            items = self.in_out_items()
            in_out_item_id = items[0]["id"] if items else 1
        num = ("SK" if ptype == "收款" else "FK") + time.strftime("%Y%m%d") + f"{int(time.time()) % 10000:04d}"
        info = {"type": ptype, "organId": organ_id,
                "operTime": time.strftime("%Y-%m-%d %H:%M:%S"), "number": num,
                "billNo": bill_no, "accountId": account_id,
                "changeAmount": amount, "totalPrice": amount,
                "remark": remark, "status": "1"}
        rows = [{"inOutItemId": in_out_item_id, "eachAmount": amount, "remark": remark}]
        res = self._req("/accountHead/addAccountHeadAndDetail", "POST", {
            "info": json.dumps(info, ensure_ascii=False),
            "rows": json.dumps(rows, ensure_ascii=False),
        })
        return {"ok": res.get("code") == 200, "number": num, "raw": res}

    # ---------- 单据查询 ----------
    def bills(self, btype=None, sub_type=None, page_size=50, **extra):
        """查单据。
        ⚠️ subType 必传！后端 getBillCategory() 对 subType 直接 .equals()，
        为 null 时抛 NullPointerException → 返回 code:300 数据查询异常。
        不指定 sub_type 时自动遍历该 type 下所有子类型（并行请求）。"""
        search = dict(extra)
        if btype:
            search["type"] = btype
        if sub_type:
            search["subType"] = sub_type
            return self.list("/depotHead/list", search, page_size=page_size, use_cache=False)
        # 未指定 subType：并行遍历全部子类型合并
        subs = SUB_TYPES_OF.get(btype) if btype else ALL_SUB_TYPES
        results = parallel([
            lambda st=st: self.list("/depotHead/list", dict(search, subType=st),
                                    page_size=page_size, use_cache=False)
            for st in (subs or ALL_SUB_TYPES)
        ])
        out = []
        for rows in results:
            if rows:
                out.extend(rows)
        return out

    def all_bills(self, page_size=200, **extra):
        """全部单据（并行遍历所有 type/subType 组合）。"""
        results = parallel([
            lambda st=st: self.list("/depotHead/list", dict(extra, subType=st),
                                    page_size=page_size, use_cache=False)
            for st in ALL_SUB_TYPES
        ], workers=8)
        out = []
        for rows in results:
            if rows:
                out.extend(rows)
        seen, uniq = set(), []
        for b in out:
            if b.get("id") not in seen:
                seen.add(b.get("id"))
                uniq.append(b)
        return uniq

    # ---------- 附件上传（对应原版 /systemConfig/upload） ----------
    def upload_file(self, file_path, biz="bill"):
        """上传单据附件到 ERP。file_path 为本地文件路径。
        返回 ERP 保存的相对路径（如 "bill/xxx.jpg"），失败抛 ErpError。
        原版限制：biz ∈ {bill, financial, material}，单单据最多 4 份。
        """
        self.ensure_login()
        import os
        if not os.path.isfile(file_path):
            raise ErpError(f"附件不存在：{file_path}")
        url = f"{self.base}/systemConfig/upload?biz={biz}"
        boundary = "----jshERPboundary%d" % int(time.time() * 1000)
        fname = os.path.basename(file_path)
        # 文件名转 ASCII 安全（保留中文需用 RFC5987，这里用简单 filename*）
        from urllib.parse import quote
        safe_name = quote(fname)
        body = []
        body.append(("--" + boundary).encode())
        body.append(('Content-Disposition: form-data; name="file"; '
                     'filename="%s"; filename*=UTF-8\'\'%s' % (fname, safe_name)).encode("utf-8"))
        body.append(b"Content-Type: application/octet-stream")
        body.append(b"")
        with open(file_path, "rb") as f:
            body.append(f.read())
        body.append(("--" + boundary + "--").encode())
        body.append(b"")
        data = b"\r\n".join(body)
        req = urllib.request.Request(url, data=data, method="POST", headers={
            "Content-Type": "multipart/form-data; boundary=" + boundary,
            "X-Access-Token": self.token or "",
        })
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                raw = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
        except Exception as e:
            raise ErpError(f"附件上传失败：{e}")
        try:
            j = json.loads(raw)
        except Exception:
            raise ErpError(f"附件上传返回异常：{raw[:200]}")
        if j.get("code") == 200 and j.get("data"):
            return j["data"]
        raise ErpError(f"附件上传失败：{j.get('data') or j}")


_client = None


def get_client(force=False):
    """全局单例，从 config.py 读连接信息。"""
    global _client
    from . import config as cfgmod
    c = cfgmod.load()["erp"]
    if force or _client is None or _client.base != c["base"].rstrip("/") or _client.user != c["user"]:
        _client = JshERP(c["base"], c["user"], c["pass"])
        _client.login()
    return _client
