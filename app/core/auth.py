#!/usr/bin/env python3
"""登录鉴权 + 用户级模型档案。纯标准库。

设计（为什么不用自建密码库、也不用 JWT 库）：
- 账号体系直接联动 ERP：登录时服务端把账号密码转发给 jshERP /user/login 校验
  （MD5 在服务端做，密码不落盘、不进日志）。ERP 返回 ismanager/tenantId，
  角色判定不用自己造；tenantId 必须与服务账号同租户，防止登到别的租户实例。
- 会话令牌复用写入闸门（gate.py）的 HMAC 无状态机制：base64(payload).sig，
  默认 30 天过期，服务重启不失效；payload 只含登录名/显示名/角色/过期时间。
- 管理员 = ERP ismanager=1，或 T2S_ADMINS 名单（逗号分隔登录名）兜底；
  其余登录者一律 member。
- 用户级模型档案：每个登录者可配自己的档案（密钥存 data/user_profiles.json，
  0600，不出仓库），请求线程内 config.active_profile() 自动回退：
  我的档案 → 共享档案。成员永远看不到共享档案的 base_url / key。

被拒的登录不写审计（避免密码进日志），只返回统一错误文案。
"""
import base64
import hashlib
import hmac
import json
import os
import threading
import time
import urllib.error
import urllib.request

from . import config as cfgmod
from .gate import _hmac_key

DEFAULT_TTL_DAYS = 30

_pf_lock = threading.Lock()


# --------------------------------------------------------------------------
# ERP 登录校验（联动账号体系）
# --------------------------------------------------------------------------
def _erp_base():
    return (cfgmod.load().get("erp", {}).get("base")
            or "http://localhost:9999/jshERP-boot").rstrip("/")


def erp_login(login_name, password):
    """用 ERP 校验账号密码。返回 (user_dict, "") 或 (None, 错误文案)。

    user_dict 含 id/loginName/username/ismanager/tenantId（密码字段丢弃）。
    """
    if not login_name or not password:
        return None, "请输入账号和密码"
    pwd = hashlib.md5(str(password).encode()).hexdigest()
    body = json.dumps({"loginName": str(login_name).strip(), "password": pwd}).encode()
    req = urllib.request.Request(
        _erp_base() + "/user/login", body,
        {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            j = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return None, "ERP 登录失败（HTTP %d）" % e.code
    except Exception as e:
        return None, "连不上 ERP：%s" % str(e)[:100]
    if not (j.get("code") == 200 and isinstance(j.get("data"), dict)):
        # 统一文案，不区分「账号不存在/密码错误」，避免帮猜号
        return None, "账号或密码不对"
    d = j["data"]
    u = d.get("user") or {}
    if not isinstance(u, dict) or not u.get("loginName"):
        return None, "ERP 返回异常，请稍后再试"
    # 租户绑定：只认与服务账号同租户的账号（防止登进同机其他租户实例）
    try:
        my_tenant = _service_tenant_id()
        if my_tenant is not None and int(u.get("tenantId") or -1) != int(my_tenant):
            return None, "账号或密码不对"
    except Exception:
        pass
    u.pop("password", None)
    return u, ""


def _service_tenant_id():
    """服务账号的 tenantId（进程内缓存）：登录校验的租户绑定基准。None = 不校验。"""
    global _SVC_TENANT
    if _SVC_TENANT["_v"] is not None:
        return _SVC_TENANT["_v"]
    try:
        from . import erp as erpmod
        cl = erpmod.get_client()
        info = cl._req("/user/getUserSession")
        _SVC_TENANT["_v"] = ((info or {}).get("data", {}).get("user") or {}).get("tenantId")
    except Exception:
        _SVC_TENANT["_v"] = None
    return _SVC_TENANT["_v"]


_SVC_TENANT = {"_v": None}


# --------------------------------------------------------------------------
# 角色判定
# --------------------------------------------------------------------------
def admins():
    """T2S_ADMINS 名单（逗号分隔登录名），环境变量优先。"""
    raw = cfgmod.env("T2S_ADMINS", "")
    return {x.strip() for x in str(raw).split(",") if x.strip()}


_ROLE_MAP_TS = [0.0, None]   # [上次拉取时间, {loginName: roleName}]（进程内缓存）


def role_map(max_age=60):
    """loginName → roleName 映射（服务账号查 /user/list，进程内缓存 60s）。

    为什么不用登录响应里的 ismanager：实测 jshERP 给租户内所有用户都写
    ismanager=1（admin 与 test1 无差别），该字段没有区分度——用它判定导致
    test1 拿到管理员会话（失败案例 C11）。roleName 才是 ERP 侧的真实角色。
    """
    now = time.time()
    if _ROLE_MAP_TS[1] is not None and now - _ROLE_MAP_TS[0] < max_age:
        return _ROLE_MAP_TS[1]
    try:
        from . import erp as erpmod
        rows = erpmod.get_client().list("/user/list", page_size=500)
        m = {str(r.get("loginName") or ""): str(r.get("roleName") or "")
             for r in rows if r.get("loginName")}
        if m:
            _ROLE_MAP_TS[0], _ROLE_MAP_TS[1] = now, m
        return _ROLE_MAP_TS[1] or {}
    except Exception:
        return _ROLE_MAP_TS[1] or {}   # ERP 抖动时用旧缓存；从未拿到过则空


def decide_role(login_name, role_name=None):
    """admin：T2S_ADMINS 名单，或 ERP 角色名含「管理员」；其余一律 member。

    role_name 缺省时查 role_map；查不到（ERP 不可用且无缓存）按 member 处理——
    宁可降权也不提权，admin 还有静态口令后备通道。
    """
    login_name = str(login_name or "")
    if login_name in admins():
        return "admin"
    if role_name is None:
        role_name = role_map().get(login_name, "")
    if "管理员" in str(role_name):
        return "admin"
    return "member"


def invalidate_role_map():
    _ROLE_MAP_TS[0], _ROLE_MAP_TS[1] = 0.0, None


# --------------------------------------------------------------------------
# 会话令牌（HMAC 无状态，复用 gate 的签名密钥）
# --------------------------------------------------------------------------
def issue_session(login_name, username, role, uid=None, ttl_days=None):
    days = ttl_days
    if days is None:
        try:
            days = float(cfgmod.env("T2S_SESSION_TTL_DAYS", DEFAULT_TTL_DAYS))
        except ValueError:
            days = DEFAULT_TTL_DAYS
    payload = {"v": 2, "u": str(login_name), "n": str(username or login_name),
               "r": role if role in ("admin", "member") else "member",
               "e": int(time.time() + days * 86400)}
    if uid:
        payload["uid"] = int(uid)   # ERP 侧 userId：录单时用于 ERP 操作日志归因
    body = base64.urlsafe_b64encode(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).decode().rstrip("=")
    sig = hmac.new(_hmac_key(), body.encode("utf-8"), hashlib.sha256).hexdigest()[:32]
    return body + "." + sig


def verify_session(token):
    """校验会话令牌。有效返回 payload {u, n, r, e, uid?}，否则 None（不消费，可重复用）。"""
    token = (token or "").strip()
    if not token or "." not in token:
        return None
    try:
        body, sig = token.split(".", 1)
        expect = hmac.new(_hmac_key(), body.encode("utf-8"), hashlib.sha256).hexdigest()[:32]
        if not hmac.compare_digest(sig, expect):
            return None
        pad = "=" * (-len(body) % 4)
        p = json.loads(base64.urlsafe_b64decode(body + pad).decode("utf-8"))
    except Exception:
        return None
    if int(p.get("e") or 0) < time.time():
        return None
    if p.get("v") != 2:   # 令牌版本：角色判定逻辑变更时 +1，旧令牌整体失效
        return None
    if p.get("r") not in ("admin", "member") or not p.get("u"):
        return None
    return p


# ---- 登录防爆破：同账号连错 N 次锁一段时间（进程内，重启即清，够用且零依赖） ----
_FAIL_MAX = 5
_FAIL_LOCK_SECONDS = 60
_fails = {}          # {login: [fail_count, lock_until_ts]}
_fails_lock = threading.Lock()


def _login_throttled(login_name):
    with _fails_lock:
        rec = _fails.get(str(login_name))
        if rec and rec[1] > time.time():
            return int(rec[1] - time.time()) + 1
    return 0


def _login_fail(login_name):
    with _fails_lock:
        rec = _fails.setdefault(str(login_name), [0, 0.0])
        rec[0] += 1
        if rec[0] >= _FAIL_MAX:
            rec[1] = time.time() + _FAIL_LOCK_SECONDS
            rec[0] = 0


def _login_ok(login_name):
    with _fails_lock:
        _fails.pop(str(login_name), None)


def login_and_issue(login_name, password):
    """完整登录流：限速 → ERP 校验 → 角色 → 签发会话。返回 {ok, ...}。"""
    login_name = str(login_name or "").strip()
    wait = _login_throttled(login_name)
    if wait:
        return {"ok": False, "message": "失败次数过多，请 %d 秒后再试" % wait}
    u, err = erp_login(login_name, password)
    if err:
        _login_fail(login_name)
        return {"ok": False, "message": err}
    _login_ok(login_name)
    role = decide_role(u.get("loginName"))
    token = issue_session(u.get("loginName"), u.get("username"), role, uid=u.get("id"))
    return {"ok": True, "token": token, "role": role,
            "login": u.get("loginName"), "username": u.get("username") or u.get("loginName")}


# --------------------------------------------------------------------------
# 用户级模型档案（data/user_profiles.json，0600）
# 结构：{login: {"profiles": {key: {...}}, "active": key}}
# --------------------------------------------------------------------------
def _pf_path():
    return os.path.join(cfgmod.data_dir(), "user_profiles.json")


def _pf_load():
    try:
        with open(_pf_path(), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _pf_save(d):
    tmp = _pf_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    os.replace(tmp, _pf_path())
    try:
        os.chmod(_pf_path(), 0o600)
    except OSError:
        pass


def _norm_prof(key, profile, old=None):
    """用户档案的字段规范化（与共享档案 upsert 同口径）。"""
    old = old or {}
    api_key = profile.get("api_key") or ""
    if not api_key or "****" in api_key:   # 空 / 前端回传的脱敏串 → 不修改
        api_key = old.get("api_key") or ""
    prof = {
        "name": profile.get("name") or key,
        "base_url": (profile.get("base_url") or "").rstrip("/"),
        "api_key": api_key,
        "model": profile.get("model") or "",
        "temperature": profile.get("temperature", 0),
    }
    for opt in ("merge_system", "insecure", "extra_headers"):
        if opt in old:
            prof[opt] = old[opt]
        elif profile.get(opt):
            prof[opt] = profile[opt]
    return prof


def user_profiles(login):
    """某用户的全部档案 + 激活键。返回 ({profiles: {...}, active: key})。"""
    d = _pf_load().get(str(login)) or {}
    return {"profiles": d.get("profiles") or {}, "active": d.get("active") or ""}


def save_user_profile(login, key, profile):
    key = _pf_key(key)
    if not key:
        return False, "档案名无效"
    with _pf_lock:
        d = _pf_load()
        rec = d.setdefault(str(login), {"profiles": {}, "active": ""})
        rec["profiles"][key] = _norm_prof(key, profile, rec["profiles"].get(key))
        if not rec.get("active"):
            rec["active"] = key
        _pf_save(d)
    return True, ""


def delete_user_profile(login, key):
    with _pf_lock:
        d = _pf_load()
        rec = d.get(str(login))
        if not rec or key not in rec.get("profiles", {}):
            return False, "没有这个档案"
        rec["profiles"].pop(key, None)
        if rec.get("active") == key:
            rec["active"] = next(iter(rec["profiles"]), "")
        _pf_save(d)
    return True, ""


def set_user_active(login, key):
    with _pf_lock:
        d = _pf_load()
        rec = d.get(str(login))
        if not rec or key not in rec.get("profiles", {}):
            return False
        rec["active"] = key
        _pf_save(d)
        return True


def user_active_profile(login):
    """该用户当前生效的私有档案；没有则 None（调用方回退共享档案）。"""
    rec = user_profiles(login)
    prof = (rec.get("profiles") or {}).get(rec.get("active") or "")
    if prof and prof.get("base_url") and prof.get("model"):
        return dict(prof, key=rec["active"])
    return None


def mask_key(k):
    k = k or ""
    if not k:
        return ""
    return (k[:5] + "****" + k[-4:]) if len(k) > 12 else "******"


def masked_user_profiles(login):
    """给前端的用户档案副本（key 脱敏，带 has_key）。"""
    rec = user_profiles(login)
    out = {}
    for k, p in (rec.get("profiles") or {}).items():
        q = dict(p)
        q["api_key"] = mask_key(p.get("api_key"))
        q["has_key"] = bool(p.get("api_key"))
        out[k] = q
    return {"profiles": out, "active": rec.get("active") or ""}


def _pf_key(raw):
    """档案键规范化：小写 + 仅 ASCII 字母数字与 -_（中文等替换为 -）。"""
    import re as _re
    k = _re.sub(r"[^a-z0-9\-_]", "-", str(raw or "").strip().lower())
    k = _re.sub(r"-{2,}", "-", k).strip("-")
    return k[:40]
