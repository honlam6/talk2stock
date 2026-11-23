#!/usr/bin/env python3
"""问答会话持久化：ChatGPT / DeepSeek 风格侧栏的数据层。

按登录用户分文件：app/data/conversations/<sha1(login)[:16]>.json
文档结构：{"active": "<id>", "items": [{"id","title","created","updated","turns": [...]}]}
turns 与前端 ask.html 的形状一致（kind=user|answer，含 steps / drafts）。

写入时剥离三类内容（存储只留可复读的部分）：
  - 图片 dataUrl（一张几十 KB，刷新后从草稿卡重新取）
  - 草稿 confirm_token（一次性令牌，刷新后由 /api/sheet/resolve 重签）
  - 超长文本（单条消息与单会话设上限，防止把 2GB 小机器磁盘打满）

纯标准库；服务是单进程多线程，用模块级锁串行化读写。
"""
import hashlib
import json
import os
import secrets
import threading
import time

from . import config as cfgmod

CONV_DIR = os.path.join(cfgmod.DATA_DIR, "conversations")

MAX_ITEMS = 60       # 每用户保留的会话数（超出淘汰最久未更新的）
MAX_TURNS = 200      # 单会话保留的消息条数（超出保留最近）
MAX_TITLE = 40
MAX_TEXT = 4000      # 单条用户消息
MAX_ANSWER = 8000    # 单条回答
MAX_DRAFT_JSON = 20000

_lock = threading.Lock()


def _user_key(login):
    login = (login or "local").strip().lower() or "local"
    return hashlib.sha1(login.encode("utf-8")).hexdigest()[:16]


def _path(login):
    return os.path.join(CONV_DIR, _user_key(login) + ".json")


def _new_id():
    return "c%d%s" % (int(time.time() * 1000), secrets.token_hex(3))


def _clip(v, n):
    s = v if isinstance(v, str) else ("" if v is None else str(v))
    return s[:n]


def _lean_draft(p):
    """草稿载荷瘦身：去掉确认令牌，限制体积；形状保持前端 draftView 可用。"""
    if not isinstance(p, dict):
        return None
    bills = []
    for b in (p.get("bills") or [])[:6]:
        if not isinstance(b, dict):
            continue
        preview = dict(b.get("preview") or {})
        preview.pop("confirm_token", None)
        draft = b.get("draft") if isinstance(b.get("draft"), dict) else {}
        bills.append({
            "draft": draft,
            "preview": preview,
            "attach": [{"path": _clip(a.get("path"), 300), "name": _clip(a.get("name"), 200)}
                       for a in (b.get("attach") or []) if isinstance(a, dict)][:6],
        })
    out = {
        "batch_id": _clip(p.get("batch_id"), 80),
        "raw_text": _clip(p.get("raw_text"), 2000),
        "meta": p.get("meta") if isinstance(p.get("meta"), dict) else {},
        "bills": bills,
        "attachments": [{"name": _clip(a.get("name"), 200)} for a in (p.get("attachments") or []) if isinstance(a, dict)][:6],
        "clarify": p.get("clarify") if isinstance(p.get("clarify"), dict) else None,
    }
    try:
        if len(json.dumps(out, ensure_ascii=False)) > MAX_DRAFT_JSON:
            for b in out["bills"]:
                b["preview"] = {k: v for k, v in (b.get("preview") or {}).items()
                                if k in ("total", "warnings", "errors")}
    except (TypeError, ValueError):
        pass
    return out


def _lean_turn(t):
    if not isinstance(t, dict):
        return None
    kind = t.get("kind")
    if kind == "user":
        out = {"kind": "user", "text": _clip(t.get("text"), MAX_TEXT)}
        files = [{"name": _clip(f.get("name"), 200)} for f in (t.get("files") or []) if isinstance(f, dict)][:6]
        if files:
            out["files"] = files
        return out
    if kind == "answer":
        out = {"kind": "answer", "answer": _clip(t.get("answer"), MAX_ANSWER)}
        for k in ("error", "grounded"):
            if t.get(k) is not None:
                out[k] = bool(t.get(k))
        for k in ("tokens", "elapsed"):
            if isinstance(t.get(k), (int, float)):
                out[k] = t.get(k)
        meta = t.get("meta")
        if isinstance(meta, dict) and meta.get("model"):
            out["meta"] = {"model": _clip(meta.get("model"), 80)}
        if t.get("route"):
            out["route"] = _clip(t.get("route"), 40)
        steps = []
        for s in (t.get("steps") or [])[:30]:
            if not isinstance(s, dict):
                continue
            item = {k: s.get(k) for k in ("tool", "ok", "latency_ms", "thought", "correction", "observation")
                    if s.get(k) is not None}
            if "observation" in item:
                try:
                    if len(json.dumps(item["observation"], ensure_ascii=False)) > 3000:
                        item.pop("observation")
                except (TypeError, ValueError):
                    item.pop("observation")
            steps.append(item)
        if steps:
            out["steps"] = steps
        drafts = [d for d in (_lean_draft(p) for p in (t.get("drafts") or [])[:3]) if d]
        if drafts:
            out["drafts"] = drafts
        return out
    return None


def _normalize_turns(turns):
    lean = [x for x in (_lean_turn(t) for t in (turns or []) if isinstance(t, dict)) if x]
    return lean[-MAX_TURNS:]


def _empty():
    return {"active": "", "items": []}


def load(login):
    try:
        with open(_path(login), "r", encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError):
        return _empty()
    if not isinstance(doc, dict) or not isinstance(doc.get("items"), list):
        return _empty()
    doc.setdefault("active", "")
    return doc


def _save(login, doc):
    os.makedirs(CONV_DIR, exist_ok=True)
    tmp = _path(login) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False)
    os.replace(tmp, _path(login))


def _summary(item):
    return {
        "id": item.get("id", ""),
        "title": item.get("title") or "未命名对话",
        "created": item.get("created", 0),
        "updated": item.get("updated", 0),
        "count": len(item.get("turns") or []),
    }


def list_items(login):
    """会话列表（不含消息正文，侧栏渲染用）。"""
    with _lock:
        doc = load(login)
    items = sorted(doc["items"], key=lambda x: x.get("updated", 0), reverse=True)
    return {"active": doc.get("active") or (items[0]["id"] if items else ""),
            "items": [_summary(i) for i in items]}


def get_item(login, cid):
    with _lock:
        doc = load(login)
    for item in doc["items"]:
        if item.get("id") == cid:
            return dict(item)
    return None


def upsert(login, cid, title, turns, activate=True):
    """新建或覆盖一个会话；cid 为空则新建。返回会话摘要。"""
    turns = _normalize_turns(turns)
    with _lock:
        doc = load(login)
        now = int(time.time() * 1000)
        item = None
        for it in doc["items"]:
            if cid and it.get("id") == cid:
                item = it
                break
        if item is None:
            item = {"id": cid or _new_id(), "title": "", "created": now, "updated": now, "turns": []}
            doc["items"].append(item)
        if turns:
            item["turns"] = turns
        elif not item.get("turns"):
            item["turns"] = []
        title = _clip(title, MAX_TITLE).strip()
        if title:
            item["title"] = title
        elif not item.get("title"):
            first = next((t.get("text") for t in item["turns"] if t.get("kind") == "user"), "")
            item["title"] = _clip(first, MAX_TITLE).strip() or "未命名对话"
        item["updated"] = now
        if activate:
            doc["active"] = item["id"]
        # 淘汰最久未更新的会话
        if len(doc["items"]) > MAX_ITEMS:
            doc["items"].sort(key=lambda x: x.get("updated", 0), reverse=True)
            doc["items"] = doc["items"][:MAX_ITEMS]
        _save(login, doc)
        return _summary(item)


def delete(login, cid):
    with _lock:
        doc = load(login)
        before = len(doc["items"])
        doc["items"] = [i for i in doc["items"] if i.get("id") != cid]
        removed = len(doc["items"]) != before
        if doc.get("active") == cid:
            remaining = sorted(doc["items"], key=lambda x: x.get("updated", 0), reverse=True)
            doc["active"] = remaining[0]["id"] if remaining else ""
        if removed:
            _save(login, doc)
        return removed


def rename(login, cid, title):
    with _lock:
        doc = load(login)
        for it in doc["items"]:
            if it.get("id") == cid:
                it["title"] = _clip(title, MAX_TITLE).strip() or it.get("title") or "未命名对话"
                it["updated"] = int(time.time() * 1000)
                _save(login, doc)
                return _summary(it)
    return None
