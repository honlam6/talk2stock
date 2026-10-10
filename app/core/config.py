#!/usr/bin/env python3
"""统一配置：ERP 连接 + 多 LLM 模型档案 + 演示口令。纯标准库。

配置来源优先级（后者覆盖前者）：
  1. DEFAULTS（本文件内）
  2. config.json（app/ 目录，**不放密钥**）
  3. 环境变量 / app/.env 文件

密钥一律不进 config.json：
  - ERP 密码   ← T2S_ERP_PASS
  - LLM Key    ← T2S_KEY_<档案名大写>（如 T2S_KEY_WORKBUDDY）
  - 演示口令    ← T2S_DEMO_PASS

config.json 结构：
{
  "erp": {"base": "...", "user": "jsh", "pass": ""},
  "llm": {"active": "workbuddy",
          "profiles": {"workbuddy": {"name":"...","base_url":"...","api_key":"","model":"..."}}},
  "ui": {"default_depot_in": "原料仓", "default_depot_out": "成品仓", "default_account_id": 25}
}
"""
import json
import os
import threading

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(APP_DIR, "config.json")
ENV_PATH = os.path.join(APP_DIR, ".env")
DATA_DIR = os.path.join(APP_DIR, "data")

_lock = threading.Lock()

DEFAULTS = {
    "erp": {
        "base": "http://localhost:9999/jshERP-boot",
        "user": "jsh",
        "pass": "",
    },
    "llm": {
        "active": "",
        "profiles": {},
    },
    "ui": {
        "default_depot_in": "原料仓",
        "default_depot_out": "成品仓",
        "default_account_id": 25,
    },
}


# --------------------------------------------------------------------------
# 环境变量 / .env
# --------------------------------------------------------------------------
def _read_env_file(path=ENV_PATH):
    out = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                v = v.strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
                    v = v[1:-1]
                out[k.strip()] = v
    except Exception:
        pass
    return out


_FILE_ENV = _read_env_file()


def _write_env_key(env_name, value):
    """把一条密钥写入 app/.env（保持其余行不变），并刷新内存缓存。

    config.json 永远不落密钥；页面里配置的 API Key 的持久化位置就是这里。
    """
    try:
        with open(ENV_PATH, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
    except Exception:
        lines = []
    out, found = [], False
    for ln in lines:
        if ln.strip().startswith(env_name + "="):
            out.append("%s=%s" % (env_name, value))
            found = True
        else:
            out.append(ln)
    if not found:
        if out and out[-1].strip():
            out.append("")
        out.append("%s=%s" % (env_name, value))
    tmp = ENV_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(out).rstrip("\n") + "\n")
    os.replace(tmp, ENV_PATH)
    _FILE_ENV[env_name] = value


def _remove_env_key(env_name):
    """从 app/.env 移除一条密钥（删除模型档案时）。"""
    try:
        with open(ENV_PATH, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
    except Exception:
        return
    out = [ln for ln in lines if not ln.strip().startswith(env_name + "=")]
    if len(out) == len(lines):
        return
    tmp = ENV_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(out).rstrip("\n") + "\n")
    os.replace(tmp, ENV_PATH)
    _FILE_ENV.pop(env_name, None)


def env(key, default=""):
    """环境变量优先，其次 app/.env，最后默认值。"""
    v = os.environ.get(key)
    if v is None or v == "":
        v = _FILE_ENV.get(key)
    return v if (v is not None and v != "") else default


def demo_password():
    """演示口令。默认值仅用于本地开发，部署时务必用 T2S_DEMO_PASS 覆盖。"""
    return env("T2S_DEMO_PASS", "talk2stock")


def require_auth():
    """是否要求访问口令：设 T2S_REQUIRE_AUTH=1/true/yes/on 时开启。默认关闭（本地开发无感）。"""
    return str(env("T2S_REQUIRE_AUTH", "")).strip().lower() in ("1", "true", "yes", "on")


def check_pass(given):
    """校验访问口令（与 T2S_DEMO_PASS 比对，常量时间）。"""
    import hmac
    want = demo_password() or ""
    if not want:
        return False
    return hmac.compare_digest(str(given or ""), str(want))


def data_dir():
    os.makedirs(DATA_DIR, exist_ok=True)
    return DATA_DIR


# --------------------------------------------------------------------------
# 配置读写
# --------------------------------------------------------------------------
def _deep_merge(base, override):
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _apply_env(cfg):
    """把环境变量里的密钥覆盖进配置（不落盘）。

    档案的 base_url 也可用环境变量覆盖（T2S_BASE_<档案名大写>），
    这样公开仓库里的 config.json 只放占位地址，真实网关地址留在 .env。
    """
    erp = cfg.setdefault("erp", {})
    erp["base"] = env("T2S_ERP_BASE", erp.get("base") or DEFAULTS["erp"]["base"])
    erp["user"] = env("T2S_ERP_USER", erp.get("user") or DEFAULTS["erp"]["user"])
    erp["pass"] = env("T2S_ERP_PASS", erp.get("pass") or "")
    for key, prof in (cfg.get("llm", {}).get("profiles") or {}).items():
        if not isinstance(prof, dict):
            continue
        k = env("T2S_KEY_" + key.upper().replace("-", "_"), "")
        if k:
            prof["api_key"] = k
        b = env("T2S_BASE_" + key.upper().replace("-", "_"), "")
        if b:
            prof["base_url"] = b.rstrip("/")
    return cfg


def load():
    """读取配置，缺失字段用默认值补齐，再叠加环境变量密钥。"""
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        raw = {}
    return _apply_env(_deep_merge(DEFAULTS, raw))


def save(cfg):
    """整体写回配置（**剔除密钥**，只落非敏感字段）。"""
    with _lock:
        merged = _deep_merge(DEFAULTS, cfg)
        merged.setdefault("erp", {})["pass"] = ""
        for prof in (merged.get("llm", {}).get("profiles") or {}).values():
            if isinstance(prof, dict):
                prof["api_key"] = ""
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(merged, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CONFIG_PATH)
    return _apply_env(merged)


def update_section(section, values):
    """局部更新某一节（erp / llm / ui）。"""
    cfg = load()
    cfg[section] = _deep_merge(cfg.get(section, {}), values)
    return save(cfg)


def upsert_profile(key, profile):
    """新增/更新一个 LLM 模型档案。api_key 为空表示「不修改」。

    前端填入的新 Key 不落 config.json，而是写入 app/.env 的 T2S_KEY_<档案名大写>，
    这样页面里配置的模型重启后依然生效，而 config.json 始终保持无密钥。
    """
    cfg = load()
    profiles = cfg["llm"].setdefault("profiles", {})
    old = profiles.get(key, {})
    env_name = "T2S_KEY_" + key.upper().replace("-", "_")
    api_key = profile.get("api_key") or ""
    if api_key:
        try:
            _write_env_key(env_name, api_key)
        except Exception:
            pass
    else:
        api_key = old.get("api_key") or env(env_name, "")
    profiles[key] = {
        "name": profile.get("name") or key,
        "base_url": (profile.get("base_url") or "").rstrip("/"),
        "api_key": api_key,
        "model": profile.get("model") or "",
        "temperature": profile.get("temperature", 0),
    }
    if not cfg["llm"].get("active"):
        cfg["llm"]["active"] = key
    return save(cfg)


def delete_profile(key):
    cfg = load()
    cfg["llm"].get("profiles", {}).pop(key, None)
    if cfg["llm"].get("active") == key:
        remain = list(cfg["llm"].get("profiles", {}).keys())
        cfg["llm"]["active"] = remain[0] if remain else ""
    try:
        _remove_env_key("T2S_KEY_" + key.upper().replace("-", "_"))
    except Exception:
        pass
    return save(cfg)


def set_active(key):
    cfg = load()
    if key in cfg["llm"].get("profiles", {}):
        cfg["llm"]["active"] = key
        return save(cfg)
    return cfg


def active_profile():
    """返回当前启用的 LLM 档案；未配置返回 None。缺 Key 也返回（便于明确提示）。"""
    cfg = load()
    key = cfg["llm"].get("active")
    prof = cfg["llm"].get("profiles", {}).get(key)
    if prof and prof.get("base_url") and prof.get("model"):
        return dict(prof, key=key)
    return None


def public(cfg=None):
    """给前端的安全副本：api_key / 密码脱敏。"""
    cfg = cfg or load()
    out = json.loads(json.dumps(cfg, default=str))
    out["erp"]["pass"] = "******" if out["erp"].get("pass") else ""
    for k, p in out.get("llm", {}).get("profiles", {}).items():
        key = p.get("api_key") or ""
        p["api_key"] = (key[:5] + "****" + key[-4:]) if len(key) > 12 else ("******" if key else "")
        p["has_key"] = bool(key)
    return out
