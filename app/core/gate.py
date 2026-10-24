#!/usr/bin/env python3
"""写入闸门 + 审计日志。

不变量（提案的核心设计点）：**没有人工确认事件的写入请求一律拒绝。**

机制：
- /api/parse 与 /api/resolve 在返回预览的同时签发一次性确认令牌（confirm_token），
  令牌与该草稿内容的 sha256 绑定，默认 30 分钟过期，用一次即作废。
- /api/commit 必须携带有效令牌，且提交的草稿内容哈希必须与签发时一致；
  任何缺令牌 / 令牌过期 / 草稿被篡改的写入尝试都被拒绝，并记录进审计日志。

审计：每次写入尝试（无论成功被拒）都向 app/data/audit.jsonl 追加一行：
  时间、事件类型、来源入口、原话、模型、提示词版本、草稿、与签发时的 diff、结果。
用户在确认卡上改过的字段同时沉淀到 app/data/regression.jsonl，作为回归测试集。
"""
import base64
import hashlib
import hmac
import json
import os
import secrets
import sys
import threading
import time

from . import config as cfgmod
from . import llm as llmmod

TOKEN_TTL = 1800  # 确认令牌有效期（秒）

_lock = threading.Lock()   # 审计日志写入锁（_append 用）


def digest_of(draft):
    """草稿的规范哈希（键排序、紧凑分隔符），用于把令牌绑定到具体内容。"""
    canon = json.dumps(draft, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# 无状态确认令牌：base64(payload).HMAC——服务重启不失效、多进程部署可用。
# 「一次性」由 nonce 黑名单（data/used_tokens.jsonl 持久化）保证，重启不丢。
# --------------------------------------------------------------------------
def _hmac_key():
    """签名密钥：首次生成存 data/.gatekey（0600），此后复用。"""
    path = os.path.join(cfgmod.data_dir(), ".gatekey")
    try:
        with open(path, encoding="utf-8") as f:
            k = f.read().strip()
            if len(k) >= 32:
                return k.encode()
    except OSError:
        pass
    k = secrets.token_hex(32)
    try:
        os.makedirs(cfgmod.data_dir(), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(k)
        os.chmod(path, 0o600)
    except OSError:
        pass   # 写不进（只读卷）则退化为进程级随机密钥——重启失效但不阻断业务
    return k.encode()


_USED_LOCK = threading.Lock()


def _used_path():
    return os.path.join(cfgmod.data_dir(), "used_tokens.jsonl")


def _mark_used(nonce, exp):
    """nonce 进黑名单（持久化），顺带清理已过期行，文件不无限增长。"""
    with _USED_LOCK:
        try:
            os.makedirs(cfgmod.data_dir(), exist_ok=True)
            with open(_used_path(), "a", encoding="utf-8") as f:
                f.write("%s %d\n" % (nonce, exp))
            _sweep_used()
        except OSError:
            pass


def _sweep_used():
    """黑名单只需覆盖 TTL 窗口：清掉已过期行。"""
    try:
        with open(_used_path(), encoding="utf-8") as f:
            lines = [l for l in f if l.strip()]
        now = time.time()
        keep = []
        for l in lines:
            parts = l.split()
            if len(parts) == 2:
                try:
                    if int(parts[1]) > now:
                        keep.append(l)
                except ValueError:
                    continue
        if len(keep) != len(lines):
            with open(_used_path(), "w", encoding="utf-8") as f:
                f.writelines(keep)
    except OSError:
        pass


def _is_used(nonce):
    try:
        with open(_used_path(), encoding="utf-8") as f:
            for l in f:
                parts = l.split()
                if len(parts) == 2 and parts[0] == nonce:
                    return True
    except OSError:
        pass
    return False


def issue(draft, meta=None):
    """签发一次性确认令牌。meta 存签发上下文（原话、模型等），随令牌自描述。"""
    payload = {"d": digest_of(draft), "e": int(time.time()) + TOKEN_TTL,
               "n": secrets.token_hex(8)}
    if meta:
        payload["m"] = meta
    body = base64.urlsafe_b64encode(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).decode().rstrip("=")
    sig = hmac.new(_hmac_key(), body.encode("utf-8"), hashlib.sha256).hexdigest()[:32]
    return body + "." + sig


def _parse_token(token):
    try:
        body, sig = token.split(".", 1)
        expect = hmac.new(_hmac_key(), body.encode("utf-8"), hashlib.sha256).hexdigest()[:32]
        if not hmac.compare_digest(sig, expect):
            return None
        pad = "=" * (-len(body) % 4)
        return json.loads(base64.urlsafe_b64decode(body + pad).decode("utf-8"))
    except Exception:
        return None


def check(token, draft):
    """校验令牌。返回 (ok, meta, reason)。有效令牌随即作废（一次性）。"""
    token = (token or "").strip()
    if not token or "." not in token:
        return False, {}, "缺少有效确认令牌（请重新预览并勾选确认）"
    payload = _parse_token(token)
    if payload is None:
        return False, {}, "确认令牌签名无效，请重新预览确认"
    if int(payload.get("e") or 0) < time.time():
        return False, payload.get("m") or {}, "确认令牌已过期（超过 30 分钟），请重新预览确认"
    if payload.get("d") != digest_of(draft):
        return False, payload.get("m") or {}, "草稿在确认后被改动，请重新预览确认"
    nonce = str(payload.get("n") or "")
    if not nonce or _is_used(nonce):
        return False, payload.get("m") or {}, "该确认令牌已被使用，请重新预览确认"
    _mark_used(nonce, int(payload.get("e") or 0))
    return True, payload.get("m") or {}, ""


# --------------------------------------------------------------------------
# 审计日志
# --------------------------------------------------------------------------
_AUDIT_MAX_BYTES = 10 * 1024 * 1024  # 单个审计文件上限 10MB，超过即轮转
_AUDIT_KEEP = 5                       # 保留 5 份历史


def _rotate_if_needed(full):
    """审计文件按大小轮转：audit.jsonl -> .1 -> .2 ... 只保留最近 N 份。"""
    try:
        if os.path.getsize(full) <= _AUDIT_MAX_BYTES:
            return
    except OSError:
        return
    for i in range(_AUDIT_KEEP - 1, 0, -1):
        src, dst = f"{full}.{i}", f"{full}.{i + 1}"
        if os.path.exists(src):
            try:
                os.replace(src, dst)
            except OSError:
                pass
    try:
        os.replace(full, full + ".1")
    except OSError:
        pass


def _append(path, record):
    try:
        d = cfgmod.data_dir()
        full = os.path.join(d, path)
        with _lock:
            _rotate_if_needed(full)
            with open(full, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except Exception as e:
        # 审计失败不阻断业务，但必须留痕到 stderr——静默丢失等于证据链断裂
        try:
            sys.stderr.write("[gate] 审计写入失败 %s: %s\n" % (path, e))
        except Exception:
            pass


def diff_drafts(before, after):
    """两份草稿的字段级 diff，返回 [{field, before, after}]。"""
    out = []

    def cmp(path, a, b):
        if a != b:
            out.append({"field": path, "before": a, "after": b})

    for k in ("biz", "party", "depot", "oper_time", "pay_type", "remark"):
        cmp(k, (before or {}).get(k), (after or {}).get(k))
    cmp("pay_amount", (before or {}).get("pay_amount"), (after or {}).get("pay_amount"))
    bi = (before or {}).get("items") or []
    ai = (after or {}).get("items") or []
    for i in range(max(len(bi), len(ai))):
        b = bi[i] if i < len(bi) else {}
        a = ai[i] if i < len(ai) else {}
        for k in ("name", "qty", "unit", "price"):
            cmp(f"items[{i}].{k}", b.get(k), a.get(k))
    if len(bi) != len(ai):
        out.append({"field": "items.length", "before": len(bi), "after": len(ai)})
    return out


def prompt_fingerprint():
    """当前提示词的版本号 + 内容短哈希（迭代留痕）。"""
    h = hashlib.sha256(llmmod.SYSTEM_PROMPT.encode("utf-8")).hexdigest()[:8]
    return {"prompt_version": getattr(llmmod, "PROMPT_VERSION", ""), "prompt_hash": h}


def log_write(meta, draft, result, diff=None, source="", raw_text="", operator=""):
    """成功写入的审计记录。"""
    rec = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "event": "commit_ok",
        "operator": operator or meta.get("operator", ""),
        "source": source or meta.get("source", ""),
        "raw_text": raw_text if raw_text is not None else meta.get("raw_text", ""),
        "model": meta.get("model", ""),
        "prompt_version": meta.get("prompt_version", ""),
        "prompt_hash": meta.get("prompt_hash", ""),
        "diff": diff or [],
        "result": {k: result.get(k) for k in ("ok", "number", "total", "biz_label", "created")},
        "draft": draft,
    }
    _append("audit.jsonl", rec)
    # 用户改过字段 + 有原话 → 沉淀成回归用例
    if diff and rec["raw_text"]:
        _append("regression.jsonl", {
            "ts": rec["ts"], "utterance": rec["raw_text"],
            "draft_parsed": meta.get("draft_resolved", {}),
            "draft_final": draft,
            "changed_fields": [d["field"] for d in diff],
        })


def log_rejected(reason, draft, source="", raw_text="", operator=""):
    """被闸门拒绝的写入尝试（证据：未经确认的写入为 0）。"""
    _append("audit.jsonl", {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "event": "commit_rejected",
        "operator": operator or "",
        "reason": reason,
        "source": source,
        "raw_text": raw_text,
        "draft": draft,
    })
