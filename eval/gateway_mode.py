#!/usr/bin/env python3
"""WorkBuddy2API 网关提示词模式一键切换（免开面板、免重启）。

为什么存在：网关的 prompt.mode（custom=注入 8.5K 人格指纹 / passthrough=透传）
是**全局**设置，没有按 key 分流。跑评测/测真实 token 要透传，平时保持 custom。
本脚本把「开面板→登录→找到设置→改→保存」压成一条命令，评测脚本前后各调一次。

用法：
    python3 eval/gateway_mode.py                 # 查看当前模式
    python3 eval/gateway_mode.py passthrough     # 切透传（评测口径）
    python3 eval/gateway_mode.py custom          # 切回人格注入（平时）

安全：读改写整份 config（只动 prompt.mode 一个字段，其余原样回传），
      密钥从 app/.env 的 T2S_KEY_WORKBUDDY 取，绝不入库。
"""
import argparse
import json
import os
import ssl
import sys
import urllib.request

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.join(ROOT, "app"))

from core import config as cfgmod   # noqa: E402  读 .env 里的网关地址与密钥

MODES = ("custom", "passthrough")


def _panel_base():
    prof = cfgmod.active_profile() or {}
    base = (prof.get("base_url") or "").strip()
    if not base:
        raise SystemExit("config.json 里没有激活档案的 base_url，无法定位网关")
    # https://host:28466/v1 -> https://host:28466
    base = base.rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    return base


def _key():
    env = cfgmod.load_env() if hasattr(cfgmod, "load_env") else {}
    for src in (os.environ.get("T2S_KEY_WORKBUDDY"), env.get("T2S_KEY_WORKBUDDY")):
        if src:
            return src
    # 兜底：.env 文件直读（config.py 未暴露 load_env 时）
    p = os.path.join(ROOT, "app", ".env")
    if os.path.exists(p):
        for line in open(p):
            if line.startswith("T2S_KEY_WORKBUDDY="):
                return line.split("=", 1)[1].strip()
    raise SystemExit("找不到 T2S_KEY_WORKBUDDY（app/.env）")


def _req(url, key, payload=None):
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE   # 网关是 IP + 自签证书
    data = json.dumps(payload).encode() if payload is not None else None
    r = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    r.add_header("Authorization", "Bearer " + key)
    if data:
        r.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(r, timeout=15, context=ctx) as resp:
        return json.loads(resp.read().decode())


def get_mode():
    cfg = _req(_panel_base() + "/panel/api/config", _key())["config"]
    return cfg.get("prompt", {}).get("mode", "?"), cfg


def set_mode(mode):
    if mode not in MODES:
        raise SystemExit(f"未知模式 {mode}，可选：{'/'.join(MODES)}")
    _, cfg = get_mode()
    cfg.setdefault("prompt", {})["mode"] = mode
    r = _req(_panel_base() + "/panel/api/config", _key(), cfg)
    return r


def probe_injection():
    """生效自检：发一个极简请求，看 prompt_tokens。
    custom 人格注入 ~7.8K；passthrough 应该只有几百。>2000 = 模式没生效
    （网关进程需重启，面板 restart_required 对该字段有漏报）。"""
    import urllib.error
    body = json.dumps({"model": "global:deepseek-v4.1-flash",
                       "messages": [{"role": "user", "content": "回复一个字：好"}],
                       "max_tokens": 5}).encode()
    r = urllib.request.Request(_panel_base() + "/v1/chat/completions", data=body, method="POST")
    r.add_header("Authorization", "Bearer " + _key())
    r.add_header("Content-Type", "application/json")
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(r, timeout=30, context=ctx) as resp:
            u = (json.loads(resp.read().decode()) or {}).get("usage") or {}
        return u.get("prompt_tokens")
    except Exception as e:
        return f"探针失败：{e}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", nargs="?", choices=MODES, help="不传=只查看")
    ap.add_argument("--probe", action="store_true", help="附带生效自检（发 1 个 5-token 请求）")
    a = ap.parse_args()
    if not a.mode:
        mode, _ = get_mode()
        print(f"当前提示词模式：{mode}")
    else:
        r = set_mode(a.mode)
        restart = r.get("restart_required") or []
        mode, _ = get_mode()
        print(f"已切换：prompt.mode = {mode}" + (f"（注意：{restart} 需重启才生效）" if restart else "（立即生效）"))
    if a.probe or a.mode:
        pt = probe_injection()
        if isinstance(pt, int):
            verdict = "✓ 已生效（透传口径）" if pt < 2000 else "✗ 未生效——网关进程需重启（面板 restart_required 漏报了 prompt.mode）"
            print(f"生效自检：探针 prompt_tokens = {pt} → {verdict}")
        else:
            print("生效自检：", pt)


if __name__ == "__main__":
    main()
