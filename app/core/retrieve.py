#!/usr/bin/env python3
"""轻量检索（RAG-lite）：纯标准库 BM25，零外部依赖、命中可解释。

为什么不是向量 RAG：本场景检索目标是操作文档与历史单据（小语料、需要可
解释的命中理由），BM25 词频检索足够且完全可审计；embedding 依赖外部 API
或本地模型，与「纯标准库自建」的项目承诺冲突。这是 domain-aware 的选择：
结构化经营数据走 tool-use + 数字接地，非结构化文本走轻检索。

两个索引（均为进程内懒构建 + 源文件 mtime 失效）：
- docs    操作文档：jshERP 快速上手 + 本仓库交互/字段文档，按段落切块
- history 历史单据：audit.jsonl 里 commit_ok 的原话与单据摘要
          （「上次那批可乐跟谁买的」这类问题的数据面）
"""
import json
import math
import os
import re
import threading
import time

from . import config as cfgmod

# 检索语料：操作向文档白名单（相对仓库根；过程性/评估类文档不进检索）
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DOC_FILES = [
    ("jshERP 使用指南", "jshERP-latest/包装厂快速上手指南.md"),
    ("jshERP 功能说明", "jshERP-latest/README.md"),
    ("Talk2Stock 交互与边界", "docs/交互流程与边界情况.md"),
    ("Talk2Stock 字段说明", "docs/字段必填清单.md"),
]

_BLOCK_MAX = 380   # 文档块目标长度（字符）


# --------------------------------------------------------------------------
# 分词：中文按字符 bigram（伪分词，对 BM25 足够），ASCII 连续段按词
# --------------------------------------------------------------------------
def tokenize(text):
    out = []
    for seg in re.findall(r"[\u4e00-\u9fff]+|[A-Za-z0-9_.%/-]+", text or ""):
        if re.match(r"^[\u4e00-\u9fff]+$", seg):
            out.extend(seg[i:i + 2] for i in range(len(seg) - 1))
            if len(seg) == 1:
                out.append(seg)
        else:
            out.append(seg.lower())
    return out


# --------------------------------------------------------------------------
# BM25
# --------------------------------------------------------------------------
class BM25:
    def __init__(self, docs, k1=1.5, b=0.75):
        """docs: [{"id":.., "title":.., "text":.., "source":..}, ...]"""
        self.docs = docs
        self.k1, self.b = k1, b
        self.tf, self.df = [], {}
        self.len_avg = 0.0
        if docs:
            self._build()   # len_avg/_tf/_df 均在 _build 内生成

    def _build(self):
        for d in self.docs:
            d["_terms"] = tokenize(d["text"])
        self.len_avg = sum(len(d["_terms"]) for d in self.docs) / len(self.docs)
        for d in self.docs:
            tf = {}
            for t in d["_terms"]:
                tf[t] = tf.get(t, 0) + 1
            self.tf.append(tf)
            for t in tf:
                self.df[t] = self.df.get(t, 0) + 1

    def search(self, query, top_k=3):
        if not self.docs:
            return []
        q = set(tokenize(query))
        n = len(self.docs)
        scored = []
        for i, d in enumerate(self.docs):
            tf, dl = self.tf[i], len(d["_terms"])
            s = 0.0
            for t in q:
                if t not in tf:
                    continue
                idf = math.log(1 + (n - self.df[t] + 0.5) / (self.df[t] + 0.5))
                s += idf * tf[t] * (self.k1 + 1) / (tf[t] + self.k1 * (1 - self.b + self.b * dl / self.len_avg))
            if s > 0:
                scored.append((s, d))
        scored.sort(key=lambda x: -x[0])
        return [{"title": d["title"], "snippet": d["text"][:200], "source": d.get("source", ""),
                 "score": round(s, 3)} for s, d in scored[:top_k]]


# --------------------------------------------------------------------------
# 文档索引（懒构建 + mtime 失效）
# --------------------------------------------------------------------------
_lock = threading.Lock()
_cache = {}   # name -> {"index": BM25, "stamp": mtime_tuple}


def _split_blocks(title, text, source):
    """md 按空行分块，块内超长按句号再切，聚合到 ~_BLOCK_MAX 字符。"""
    blocks, buf = [], ""
    for para in re.split(r"\n\s*\n", text):
        para = para.strip()
        if not para or para.startswith("---") or len(para) < 12:
            continue
        if len(buf) + len(para) + 2 <= _BLOCK_MAX:
            buf = (buf + "\n" + para).strip()
            continue
        if buf:
            blocks.append((title, buf, source))
        cap = int(_BLOCK_MAX * 1.6)
        buf = para if len(para) <= cap else para[:cap] + "…"
    if buf:
        blocks.append((title, buf, source))
    return blocks


def docs_index():
    with _lock:
        files = [(t, os.path.join(REPO_ROOT, p)) for t, p in DOC_FILES if os.path.isfile(os.path.join(REPO_ROOT, p))]
        stamp = tuple(os.path.getmtime(f) for _, f in files)
        hit = _cache.get("docs")
        if hit and hit["stamp"] == stamp:
            return hit["index"]
        blocks = []
        for title, path in files:
            try:
                with open(path, encoding="utf-8", errors="replace") as f:
                    text = f.read()
                # 逐个二级标题切主题（标题并入正文提升可检索性）
                parts = re.split(r"\n(?=#{1,3} )", text)
                for p in parts:
                    m = re.match(r"#{1,3} (.+)", p)
                    head = m.group(1).strip() if m else title
                    blocks.extend(_split_blocks(f"{title} · {head}", p, os.path.basename(path)))
            except OSError:
                continue
        idx = BM25([{"id": i, "title": b[0], "text": b[1], "source": b[2]}
                    for i, b in enumerate(blocks)])
        _cache["docs"] = {"index": idx, "stamp": stamp}
        return idx


# --------------------------------------------------------------------------
# 历史单据索引（audit.jsonl commit_ok；懒构建 + mtime 失效）
# --------------------------------------------------------------------------
def _audit_path():
    return os.path.join(cfgmod.data_dir(), "audit.jsonl")


def _bill_summary(rec):
    d = rec.get("draft") or {}
    items = "、".join("%s×%s%s" % (it.get("name", "?"), it.get("qty", "?"), it.get("unit", ""))
                     for it in (d.get("items") or [])[:4])
    res = rec.get("result") or {}
    return " ".join(filter(None, [
        rec.get("ts", ""), res.get("biz_label", ""), d.get("party", ""),
        items, "合计%s元" % res.get("total", "") if res.get("total") is not None else "",
        "单号%s" % res.get("number", "") if res.get("number") else "",
        rec.get("raw_text", "") or "", d.get("remark", "") or "",
    ]))


def history_index():
    with _lock:
        path = _audit_path()
        try:
            stamp = os.path.getmtime(path)
        except OSError:
            stamp = 0
        hit = _cache.get("history")
        if hit and hit["stamp"] == stamp:
            return hit["index"]
        docs = []
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    if rec.get("event") != "commit_ok":
                        continue
                    text = _bill_summary(rec)
                    if len(text) < 6:
                        continue
                    docs.append({"id": len(docs), "title": "%s %s" % (rec.get("ts", ""), (rec.get("result") or {}).get("biz_label", "")),
                                 "text": text, "source": "audit.jsonl", "ts": rec.get("ts", ""),
                                 "raw": rec.get("raw_text", "")})
        except OSError:
            pass
        idx = BM25(docs)
        _cache["history"] = {"index": idx, "stamp": stamp}
        return idx


# --------------------------------------------------------------------------
# 工具入口（注册进 ask agent 工具目录，全部只读）
# --------------------------------------------------------------------------
def search_docs(query, top_k=3):
    """操作文档检索。返回 (data, 模板句)。"""
    hits = docs_index().search(query, top_k=top_k)
    data = {"query": query, "hits": hits}
    if not hits:
        return data, f"文档里没有找到和「{query}」相关的内容。"
    tpl = "；".join(f"《{h['title']}》（{h['source']}，相关度 {h['score']}）" for h in hits[:3])
    return data, f"文档命中 {len(hits)} 处：{tpl}。"


def search_history(query, top_k=5):
    """历史单据检索（原话/往来单位/商品/单号）。返回 (data, 模板句)。"""
    idx = history_index()
    hits = idx.search(query, top_k=top_k)
    data = {"query": query, "hits": hits, "total_bills": len(idx.docs)}
    if not hits:
        return data, "历史单据里没有查到相关记录。"
    names = "；".join(h["title"][:44] for h in hits[:3])
    return data, f"历史单据命中 {len(hits)} 条（库里共 {len(idx.docs)} 张写入单）：{names}。"
