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
import hashlib
import json
import os
import secrets
import threading
import time

from . import config as cfgmod
from . import llm as llmmod

TOKEN_TTL = 1800  # 确认令牌有效期（秒）

_lock = threading.Lock()
_store = {}  # token -> {"exp": ts, "digest": hex, "meta": {...}}


def digest_of(draft):
    """草稿的规范哈希（键排序、紧凑分隔符），用于把令牌绑定到具体内容。"""
    canon = json.dumps(draft, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def issue(draft, meta=None):
    """签发一次性确认令牌。meta 存签发时的上下文（原话、模型等），供审计用。"""
    token = secrets.token_urlsafe(24)
    now = time.time()
    with _lock:
        for t in [k for k, v in _store.items() if v["exp"] < now]:
            _store.pop(t, None)
        _store[token] = {
            "exp": now + TOKEN_TTL,
            "digest": digest_of(draft),
            "meta": meta or {},
        }
    return token


def check(token, draft):
    """校验令牌。返回 (ok, meta, reason)。有效令牌随即作废（一次性）。"""
    token = (token or "").strip()
    now = time.time()
    with _lock:
        rec = _store.pop(token, None) if token else None
    if rec is None:
        return False, {}, "缺少有效确认令牌（请重新预览并勾选确认）"
    if rec["exp"] < now:
        return False, rec.get("meta", {}), "确认令牌已过期（超过 30 分钟），请重新预览确认"
    if rec["digest"] != digest_of(draft):
        return False, rec.get("meta", {}), "草稿在确认后被改动，请重新预览确认"
    return True, rec.get("meta", {}), ""


# --------------------------------------------------------------------------
# 审计日志
# --------------------------------------------------------------------------
def _append(path, record):
    try:
        d = cfgmod.data_dir()
        with _lock:
            with open(os.path.join(d, path), "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass  # 审计失败不阻断业务（但也意味着磁盘/权限有问题，应关注）


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


def log_write(meta, draft, result, diff=None, source="", raw_text=""):
    """成功写入的审计记录。"""
    rec = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "event": "commit_ok",
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


def log_rejected(reason, draft, source="", raw_text=""):
    """被闸门拒绝的写入尝试（证据：未经确认的写入为 0）。"""
    _append("audit.jsonl", {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "event": "commit_rejected",
        "reason": reason,
        "source": source,
        "raw_text": raw_text,
        "draft": draft,
    })
