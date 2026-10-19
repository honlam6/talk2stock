#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Talk2Stock 对话式进销存智能体 —— 统一 HTTP 服务（纯标准库 http.server，无第三方依赖）。

启动：
    python3 app/server.py            # 默认 0.0.0.0:8090
    PORT=8099 python3 app/server.py

路由：
    GET    /                      → 302 跳转 /ai
    GET    /ai                    → AI 录入页面
    GET    /static/*              → 静态文件（app/web/ 下）
    GET    /api/config            → 读配置（脱敏）
    POST   /api/config/erp        → 保存 ERP 连接
    GET    /api/config/llm        → LLM 档案列表
    POST   /api/config/llm        → 新增/更新/切换启用（action=save|activate|delete）
    DELETE /api/config/llm?key=x  → 删除档案
    POST   /api/config/llm/test   → 测试档案连通性
    POST   /api/parse             → 自然语言 → draft + 预览 + 确认令牌
    POST   /api/resolve           → draft → 预览（只读）+ 确认令牌
    POST   /api/commit            → draft + 确认令牌 → 真正写入 ERP（无令牌一律拒绝）
    GET    /api/master            → 主数据（供应商/客户/商品/仓库/账户/单位）
    GET    /api/dashboard         → 经营看板全量数据（?lite=1 为工作台 KPI 精简版）
    GET    /api/history           → 最近录单（审计日志 commit_ok 的只读视图）

所有 /api 响应均为 UTF-8 JSON；任何异常都被捕获成
{"ok": false, "message": "中文提示"}，不会向客户端抛栈。
"""
import json
import os
import sys
import tempfile
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

APP_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(APP_DIR, "web")
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

from core import config as cfgmod          # noqa: E402
from core import draft as draftmod          # noqa: E402
from core import erp as erpmod              # noqa: E402
from core import gate as gatemod            # noqa: E402
from core import llm as llmmod              # noqa: E402

PORT = int(os.environ.get("PORT", "8090"))
HOST = os.environ.get("HOST", "0.0.0.0")

MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
}


# --------------------------------------------------------------------------
# 工具
# --------------------------------------------------------------------------
def friendly(e):
    """把异常转成给非技术用户看的中文提示。"""
    msg = str(e) or e.__class__.__name__
    low = msg.lower()
    if isinstance(e, erpmod.ErpError):
        return msg
    if "urlopen error" in low or "connection refused" in low or "timed out" in low:
        return "无法连接 ERP 服务（%s），请确认 Docker 容器已启动。" % cfgmod.load()["erp"]["base"]
    return msg


def _erp():
    """取 ERP 客户端；连不上时抛出中文异常。"""
    return erpmod.get_client()


def _pick_draft(body):
    """请求体可能是 draft 本身，也可能是 {"draft": {...}}。"""
    if isinstance(body, dict) and isinstance(body.get("draft"), dict):
        return body["draft"]
    return body if isinstance(body, dict) else {}


# --------------------------------------------------------------------------
# API 实现（每个函数返回 dict，异常统一由 dispatch 捕获）
# --------------------------------------------------------------------------
def api_config_get(_q, _body):
    return {"ok": True, "config": cfgmod.public()}


def api_config_erp(_q, body):
    base = (body.get("base") or "").strip().rstrip("/")
    user = (body.get("user") or "").strip()
    pwd = body.get("pass")
    if not base or not user:
        return {"ok": False, "message": "ERP 地址和用户名不能为空"}
    cur = cfgmod.load()["erp"]
    # 密码留空或仍是脱敏串 → 不修改
    if not pwd or pwd == "******":
        pwd = cur.get("pass") or ""
    cfgmod.update_section("erp", {"base": base, "user": user, "pass": pwd})
    try:
        erpmod.get_client(force=True)
        return {"ok": True, "message": "ERP 连接已保存，登录测试通过", "config": cfgmod.public()}
    except Exception as e:
        return {"ok": False, "message": "已保存，但连接测试失败：%s" % friendly(e),
                "config": cfgmod.public()}


def api_llm_get(_q, _body):
    pub = cfgmod.public()
    return {"ok": True, "active": pub["llm"].get("active", ""),
            "profiles": pub["llm"].get("profiles", {})}


def api_llm_post(_q, body):
    action = (body.get("action") or "save").strip()
    key = (body.get("key") or "").strip()
    if action == "activate":
        if not key:
            return {"ok": False, "message": "缺少要启用的模型档案"}
        cfgmod.set_active(key)
        return {"ok": True, "message": "已切换启用模型", **api_llm_get(None, None)}
    if action == "delete":
        if not key:
            return {"ok": False, "message": "缺少要删除的模型档案"}
        cfgmod.delete_profile(key)
        return {"ok": True, "message": "已删除模型档案", **api_llm_get(None, None)}

    prof = body.get("profile") if isinstance(body.get("profile"), dict) else body
    name = (prof.get("name") or "").strip()
    base_url = (prof.get("base_url") or "").strip()
    model = (prof.get("model") or "").strip()
    if not key:
        key = name or model
    if not key:
        return {"ok": False, "message": "请填写显示名"}
    if not base_url:
        return {"ok": False, "message": "请填写 Base URL（如 https://api.openai.com/v1）"}
    if not model:
        return {"ok": False, "message": "请填写模型名（如 gpt-4o-mini）"}
    api_key = prof.get("api_key") or ""
    if "****" in api_key:      # 前端回传的脱敏串 → 视为不修改
        api_key = ""
    try:
        temp = float(prof.get("temperature", 0) or 0)
    except Exception:
        temp = 0.0
    cfgmod.upsert_profile(key, {
        "name": name or key, "base_url": base_url, "api_key": api_key,
        "model": model, "temperature": temp,
    })
    if body.get("activate"):
        cfgmod.set_active(key)
    return {"ok": True, "message": "模型档案已保存", **api_llm_get(None, None)}


def api_llm_delete(q, body):
    key = (q.get("key", [""])[0] or (body or {}).get("key") or "").strip()
    if not key:
        return {"ok": False, "message": "缺少要删除的模型档案"}
    cfgmod.delete_profile(key)
    return {"ok": True, "message": "已删除模型档案", **api_llm_get(None, None)}


def api_llm_test(_q, body):
    key = (body.get("key") or "").strip()
    prof = body.get("profile") if isinstance(body.get("profile"), dict) else dict(body)
    stored = cfgmod.load()["llm"].get("profiles", {}).get(key, {})
    api_key = prof.get("api_key") or ""
    if not api_key or "****" in api_key:
        api_key = stored.get("api_key") or ""
    merged = {
        "base_url": (prof.get("base_url") or stored.get("base_url") or "").strip(),
        "model": (prof.get("model") or stored.get("model") or "").strip(),
        "api_key": api_key,
        "temperature": prof.get("temperature", stored.get("temperature", 0)),
    }
    if not merged["base_url"] or not merged["model"]:
        return {"ok": False, "message": "请先填写 Base URL 和模型名"}
    if not merged["api_key"]:
        return {"ok": False, "message": "缺少 API Key，请填写后再测试"}
    ok, msg = llmmod.test_profile(merged)
    return {"ok": bool(ok), "message": msg}


def api_master(_q, _body):
    erp = _erp()
    def names(rows, field):
        out = []
        for r in rows:
            v = r.get(field)
            if v:
                out.append({"id": r.get("id"), "name": v,
                            "unit": r.get("unit") or "", "model": r.get("model") or ""})
        return out
    suppliers = names(erp.suppliers("供应商"), "supplier")
    customers = names(erp.suppliers("客户"), "supplier")
    materials = names(erp.materials(), "name")
    depots = names(erp.depots(), "name")
    accounts = names(erp.accounts(), "name")
    try:
        units = sorted({(u.get("basicUnit") or u.get("name") or "").strip()
                        for u in erp.units()} - {""})
    except Exception:
        units = []
    return {
        "ok": True,
        "suppliers": suppliers, "customers": customers,
        "materials": materials, "depots": depots, "accounts": accounts,
        "units": units,
        "counts": {"suppliers": len(suppliers), "customers": len(customers),
                   "materials": len(materials), "depots": len(depots),
                   "accounts": len(accounts)},
        "biz_options": [{"value": k, "label": v} for k, v in draftmod.BIZ_LABEL.items()],
    }


def _confirm_meta(source, raw_text="", model="", extra=None):
    """签发确认令牌时随令牌保存的审计上下文。"""
    meta = {"source": source, "raw_text": raw_text or "", "model": model or ""}
    meta.update(gatemod.prompt_fingerprint())
    meta.update(extra or {})
    return meta


def api_parse(_q, body):
    text = (body.get("text") or "").strip()
    if not text:
        return {"ok": False, "message": "请先输入一句话描述，例如「从美宜佳买了3瓶可乐单价3块5」"}
    if not llmmod.is_configured():
        return {"ok": False, "need_config": True,
                "message": "尚未配置 AI 模型，请点右上角「设置」添加一个模型档案（Base URL / API Key / 模型名）后再试。"}
    # 多轮修正：body.draft 非空时，本轮输入是对该草稿的增量修改指令
    prev = body.get("draft")
    prev = prev if isinstance(prev, dict) and prev else None
    erp = _erp()
    try:
        draft, meta = llmmod.parse(text, erp, prev_draft=prev)
    except Exception as e:
        return {"ok": False, "message": "AI 解析失败：%s" % friendly(e)}
    preview = draftmod.resolve(draft, erp)
    preview["ai"] = {"confidence": meta.get("confidence", ""),
                     "note": meta.get("note", ""), "problems": meta.get("problems", [])}
    meta_info = _confirm_meta("ai", text, model=meta.get("model", ""),
                              extra={"draft_resolved": draft, "profile": meta.get("profile", ""),
                                     "revision": bool(prev)})
    preview["confirm_token"] = gatemod.issue(draft, meta_info)
    return {"ok": True, "draft": draft, "meta": meta, "preview": preview}


def api_resolve(_q, body):
    d = _pick_draft(body)
    if not d:
        return {"ok": False, "message": "缺少草稿数据"}
    erp = _erp()
    preview = draftmod.resolve(d, erp)
    meta_info = _confirm_meta((body.get("source") or "form").strip(),
                              extra={"draft_resolved": d})
    preview["confirm_token"] = gatemod.issue(d, meta_info)
    return {"ok": True, "draft": d, "preview": preview}


def api_commit(_q, body):
    d = _pick_draft(body)
    if not d:
        return {"ok": False, "message": "缺少草稿数据"}
    token = (body.get("confirm_token") or "").strip()
    source = (body.get("source") or "").strip()
    raw_text = (body.get("raw_text") or "").strip()
    operator = (body.get("_operator") or body.get("operator") or "").strip()[:40]
    ok, meta, reason = gatemod.check(token, d)
    if not ok:
        # 闸门拒绝：不写库，记录审计（这正是「未经确认写入为 0」的取证位置）
        gatemod.log_rejected(reason, d, source=source, raw_text=raw_text, operator=operator)
        return {"ok": False, "need_confirm": True, "message": reason}
    erp = _erp()
    res = draftmod.commit(d, erp)
    if not res.get("ok") and not res.get("message"):
        res["message"] = "写入失败，请检查数据后重试"
    if res.get("ok"):
        diff = gatemod.diff_drafts(meta.get("draft_resolved") or {}, d)
        gatemod.log_write(meta, d, res, diff=diff, source=source, raw_text=raw_text, operator=operator)
    # 写库后主数据/单据可能变了，清缓存让下次预览和看板拿到最新
    try:
        erp.invalidate("")
    except Exception:
        pass
    try:
        from core import report as reportmod
        reportmod.invalidate()
    except Exception:
        pass
    return res


def api_ask(_q, body):
    """只读问答：选工具 → 查数据 → 依数据作答。绝不写库。"""
    text = (body.get("text") or "").strip()
    if not text:
        return {"ok": False, "message": "请输入问题，例如「可乐还有多少库存？」"}
    if not llmmod.is_configured():
        return {"ok": False, "need_config": True,
                "message": "尚未配置 AI 模型，请点右上角「设置」添加。"}
    erp = _erp()
    from core import ask as askmod
    return askmod.ask(text, erp)


def api_dashboard(q, _body):
    from core import report as reportmod
    lite = (q.get("lite") or [""])[0].strip().lower() in ("1", "true", "yes")
    return reportmod.dashboard(_erp(), lite=lite)


def api_history(q, _body):
    """最近录单：读审计日志里 commit_ok 的记录（只读），供「最近录单」卡片展示。
    只回传白名单字段，不带草稿全量与提示词指纹。"""
    try:
        limit = min(max(int((q.get("limit") or ["30"])[0]), 1), 100)
    except Exception:
        limit = 30
    path = os.path.join(cfgmod.DATA_DIR, "audit.jsonl")
    if not os.path.isfile(path):
        return {"ok": True, "items": []}
    recs = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if rec.get("event") == "commit_ok":
                    recs.append(rec)
    except Exception as e:
        return {"ok": False, "message": friendly(e)}
    out = []
    for rec in reversed(recs):                      # 最新在前
        d = rec.get("draft") or {}
        r = rec.get("result") or {}
        out.append({
            "ts": rec.get("ts", ""),
            "source": rec.get("source", ""),
            "raw_text": rec.get("raw_text", ""),
            "model": rec.get("model", ""),
            "number": r.get("number", ""),
            "total": r.get("total", 0),
            "biz_label": r.get("biz_label", ""),
            "created": r.get("created", []) or [],
            "party": d.get("party", ""),
            "depot": d.get("depot", ""),
            "items": [{"name": (it or {}).get("name", ""), "qty": (it or {}).get("qty"),
                       "unit": (it or {}).get("unit", ""), "price": (it or {}).get("price")}
                      for it in (d.get("items") or [])[:20]],
            "changed": [x.get("field") for x in (rec.get("diff") or [])],
        })
        if len(out) >= limit:
            break
    return {"ok": True, "items": out}


def api_upload(q, body):
    """代理 ERP 附件上传：接收 multipart 文件，暂存后调 erp.upload_file。
    请求体为 multipart/form-data（字段 file + 可选 biz），由 _handle 解析后传入。
    """
    import cgi
    up = body.get("_upload") if isinstance(body, dict) else None
    if not up or not up.get("file_path"):
        return {"ok": False, "message": "没有收到文件"}
    biz = (up.get("biz") or "bill").strip()
    if biz not in ("bill", "financial", "material"):
        return {"ok": False, "message": "文件分类非法（应为 bill/financial/material）"}
    erp = _erp()
    try:
        path = erp.upload_file(up["file_path"], biz)
        return {"ok": True, "path": path, "name": up.get("filename", "")}
    except Exception as e:
        return {"ok": False, "message": friendly(e)}
    finally:
        # 清理临时文件
        try:
            if up.get("tmp") and os.path.isfile(up["file_path"]):
                os.remove(up["file_path"])
        except Exception:
            pass


ROUTES = {
    ("GET", "/api/config"): api_config_get,
    ("POST", "/api/config/erp"): api_config_erp,
    ("GET", "/api/config/llm"): api_llm_get,
    ("POST", "/api/config/llm"): api_llm_post,
    ("DELETE", "/api/config/llm"): api_llm_delete,
    ("POST", "/api/config/llm/test"): api_llm_test,
    ("GET", "/api/master"): api_master,
    ("GET", "/api/dashboard"): api_dashboard,
    ("GET", "/api/history"): api_history,
    ("POST", "/api/ask"): api_ask,
    ("POST", "/api/parse"): api_parse,
    ("POST", "/api/resolve"): api_resolve,
    ("POST", "/api/commit"): api_commit,
    ("POST", "/api/upload"): api_upload,
}


# --------------------------------------------------------------------------
# HTTP Handler
# --------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "jshERP-AI/1.0"
    protocol_version = "HTTP/1.1"

    # ---- 输出 ----
    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,DELETE,OPTIONS")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False, default=str))

    def _redirect(self, to):
        self._send(302, b"", "text/plain; charset=utf-8", {"Location": to})

    # ---- 鉴权 ----
    def _auth_ok(self):
        given = self.headers.get("X-T2S-Pass") or ""
        if not given:
            auth = self.headers.get("Authorization") or ""
            if auth.lower().startswith("bearer "):
                given = auth[7:].strip()
        return cfgmod.check_pass(given)

    # ---- 输入 ----
    def _body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except Exception:
            n = 0
        if n <= 0:
            return {}
        raw = self.rfile.read(n)
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8", "replace"))
        except Exception:
            raise ValueError("请求数据不是合法的 JSON")
        return data if isinstance(data, dict) else {"_": data}

    # ---- 静态 ----
    def _static(self, relpath):
        relpath = relpath.lstrip("/")
        full = os.path.normpath(os.path.join(WEB_DIR, relpath))
        if not full.startswith(WEB_DIR) or not os.path.isfile(full):
            self._send(404, "404 页面不存在", "text/plain; charset=utf-8")
            return
        ext = os.path.splitext(full)[1].lower()
        with open(full, "rb") as f:
            data = f.read()
        self._send(200, data, MIME.get(ext, "application/octet-stream"))

    # ---- 分发 ----
    def _handle(self, method):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        q = urllib.parse.parse_qs(parsed.query)

        if path.startswith("/api"):
            # 可选访问口令：设 T2S_REQUIRE_AUTH=1 时，所有 /api 请求必须带对口令
            if cfgmod.require_auth() and not self._auth_ok():
                self._json({"ok": False, "need_auth": True,
                            "message": "该服务已开启访问口令（请检查 T2S_DEMO_PASS）"}, 401)
                return
            fn = ROUTES.get((method, path))
            if not fn:
                self._json({"ok": False, "message": "接口不存在：%s %s" % (method, path)}, 404)
                return
            # 附件上传：multipart/form-data，单独解析
            if method == "POST" and path == "/api/upload":
                try:
                    ctype = self.headers.get("Content-Type") or ""
                    if "multipart/form-data" not in ctype:
                        self._json({"ok": False, "message": "请用 multipart/form-data 上传文件"}, 400)
                        return
                    import cgi
                    form = cgi.FieldStorage(
                        fp=self.rfile,
                        headers=self.headers,
                        environ={
                            "REQUEST_METHOD": "POST",
                            "CONTENT_TYPE": ctype,
                            "CONTENT_LENGTH": self.headers.get("Content-Length", "0"),
                        },
                    )
                    if "file" not in form:
                        self._json({"ok": False, "message": "没有收到文件（字段名应为 file）"}, 400)
                        return
                    item = form["file"]
                    fname0 = getattr(item, "filename", "") or ""
                    if not fname0:
                        self._json({"ok": False, "message": "没有收到文件（字段名应为 file）"}, 400)
                        return
                    # 暂存到临时文件
                    fname0 = getattr(item, "filename", "") or "upload.bin"
                    ext = os.path.splitext(fname0)[1] or ""
                    fd, tmp = tempfile.mkstemp(suffix=ext)
                    with os.fdopen(fd, "wb") as f:
                        f.write(item.file.read())
                    biz = (form.getvalue("biz") or "bill").strip()
                    try:
                        self._json(fn(q, {"_upload": {
                            "file_path": tmp, "filename": item.filename, "biz": biz, "tmp": True
                        }}))
                    except Exception as e:
                        sys.stderr.write(traceback.format_exc())
                        self._json({"ok": False, "message": friendly(e)}, 200)
                    return
                except Exception as e:
                    sys.stderr.write(traceback.format_exc())
                    self._json({"ok": False, "message": friendly(e)}, 200)
                    return
            try:
                body = self._body() if method in ("POST", "PUT", "DELETE") else {}
            except ValueError as e:
                self._json({"ok": False, "message": str(e)}, 400)
                return
            try:
                # 操作人标识（基线实验归因用）：请求头优先，其次 body
                op = (self.headers.get("X-Operator") or "").strip()[:40]
                if op and isinstance(body, dict):
                    body.setdefault("_operator", op)
                self._json(fn(q, body))
            except Exception as e:
                sys.stderr.write(traceback.format_exc())
                self._json({"ok": False, "message": friendly(e)}, 200)
            return

        if method != "GET":
            self._json({"ok": False, "message": "不支持的请求方式"}, 405)
            return

        if path == "/":
            self._static("home.html")
        elif path in ("/new", "/bill", "/bill.html"):
            self._static("bill.html")
        elif path in ("/ai", "/index", "/index.html", "/ai.html", "/form", "/form.html"):
            # 旧入口合并：对话录单 / 表单录单 → /new
            self._redirect("/new")
        elif path in ("/settings", "/settings.html", "/config"):
            self._static("settings.html")
        elif path in ("/ask", "/ask.html"):
            self._static("ask.html")
        elif path in ("/dashboard", "/dashboard.html", "/board"):
            self._static("dashboard.html")
        elif path == "/style.css":
            self._static("style.css")
        elif path == "/favicon.ico":
            self._send(204, b"", "image/x-icon")
        elif path.startswith("/static/"):
            self._static(path[len("/static/"):])
        else:
            self._static(path)

    def do_GET(self):
        self._handle("GET")

    def do_HEAD(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_DELETE(self):
        self._handle("DELETE")

    def do_OPTIONS(self):
        self._send(204, b"", "text/plain")

    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))


def main():
    os.makedirs(WEB_DIR, exist_ok=True)
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    httpd.daemon_threads = True
    cfg = cfgmod.load()
    print("=" * 58)
    print(" jshERP AI 录入助手已启动")
    print("   本机访问 : http://localhost:%d/ai" % PORT)
    print("   ERP 后端 : %s" % cfg["erp"]["base"])
    prof = cfgmod.active_profile()
    print("   AI 模型  : %s" % (prof["model"] if prof else "未配置（页面右上角「设置」里添加）"))
    print("=" * 58)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
        httpd.server_close()


if __name__ == "__main__":
    main()
