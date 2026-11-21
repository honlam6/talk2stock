#!/usr/bin/env python3
"""附件感知层：把一次对话里的多个文件变成 Agent 可选择的目录项。

不把大文件塞进主 Agent 上下文：这里只生成名称、类型、文本/表格摘要；
真正的单据抽取在 draft_order 被选中后执行。图片保留 data URL 交给视觉模型，
表格保留原始 rows 交给确定性映射，PDF/DOCX 只做本地文本提取。
"""
import base64
import io
import os
import re
import shutil
import subprocess
import tempfile
import zipfile

MAX_ATTACHMENTS = 6
MAX_ATTACHMENT_BYTES = 8 * 1024 * 1024
MAX_TEXT_CHARS = 8000

IMAGE_MAGIC = (
    (b"\x89PNG", "image/png", ".png"),
    (b"\xff\xd8", "image/jpeg", ".jpg"),
    (b"GIF8", "image/gif", ".gif"),
    (b"RIFF", "image/webp", ".webp"),
)


def _image_info(data):
    for magic, mime, ext in IMAGE_MAGIC:
        if data.startswith(magic):
            return mime, ext
    return None


def _docx_text(data):
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            xml = zf.read("word/document.xml").decode("utf-8", "replace")
    except Exception as e:
        return "", "Word 文档读取失败：%s" % str(e)[:80]
    # 段落标记换成换行，去掉标签；这是本地提取，不上传原文
    xml = re.sub(r"</w:p>", "\n", xml)
    text = re.sub(r"<[^>]+>", "", xml)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text[:MAX_TEXT_CHARS], "" if text else "Word 文档没有可提取文字"


def _pdf_text(path):
    exe = shutil.which("pdftotext")
    if not exe:
        return "", "当前环境没有 pdftotext，无法读取 PDF 文本"
    try:
        p = subprocess.run([exe, "-layout", path, "-"], stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, timeout=15)
        if p.returncode != 0:
            return "", "PDF 文本提取失败：%s" % p.stderr.decode("utf-8", "replace")[:80]
        text = p.stdout.decode("utf-8", "replace").strip()
        return text[:MAX_TEXT_CHARS], "" if text else "PDF 没有可提取文字（可能是扫描件，请发图片）"
    except Exception as e:
        return "", "PDF 文本提取失败：%s" % str(e)[:80]


def _text_file(data):
    for enc in ("utf-8-sig", "utf-8", "gb18030", "big5"):
        try:
            return data.decode(enc)[:MAX_TEXT_CHARS], ""
        except Exception:
            continue
    return "", "文本文件编码无法识别"


def inspect_file(name, data, temp_path=None):
    """返回附件目录项。table_* 是供 draft_order 用的原始表格，不进入主上下文。"""
    name = os.path.basename(str(name or "附件"))
    ext = os.path.splitext(name)[1].lower()
    item = {"name": name, "ext": ext, "size": len(data), "kind": "unsupported",
            "text": "", "error": "", "path": None, "data_url": "",
            "table_rows": None, "table_name": ""}
    if len(data) > MAX_ATTACHMENT_BYTES:
        item["error"] = "文件超过 8MB"
        return item
    image = _image_info(data)
    if image:
        mime, _ = image
        item.update(kind="image", data_url="data:%s;base64,%s" % (
            mime, base64.b64encode(data).decode("ascii")))
        return item
    if ext in (".xlsx", ".xlsm", ".csv", ".tsv"):
        try:
            from . import sheet as sheetmod
            wb = sheetmod.read_workbook(name, data)
            sheet = max(wb["sheets"], key=lambda s: len(s.get("rows") or []))
            item.update(kind="table", table_rows=sheet.get("rows") or [],
                        table_name=sheet.get("name") or name,
                        text=sheetmod.grid_desc(sheet.get("rows") or [])[:MAX_TEXT_CHARS])
        except Exception as e:
            item["error"] = "表格读取失败：%s" % str(e)[:100]
        return item
    if ext in (".pdf",):
        path = temp_path
        if not path:
            fd, path = tempfile.mkstemp(suffix=".pdf")
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            temp_path = path
        item["text"], item["error"] = _pdf_text(path)
        item["kind"] = "text" if item["text"] else "unsupported"
        return item
    if ext in (".docx",):
        item["text"], item["error"] = _docx_text(data)
        item["kind"] = "text" if item["text"] else "unsupported"
        return item
    if ext in (".txt", ".md", ".log"):
        item["text"], item["error"] = _text_file(data)
        item["kind"] = "text" if item["text"] else "unsupported"
        return item
    if ext in (".doc", ".xls"):
        item["error"] = "旧版二进制 Office 文件暂不支持，请另存为 .docx / .xlsx / PDF"
        return item
    item["error"] = "该文件类型暂不支持读取"
    return item


def context_digest(items):
    """给主 Agent 的短目录：不暴露完整正文，只让模型能按编号/文件名选附件。"""
    lines = ["# 本次消息附带的文件（编号有序）"]
    for i, it in enumerate(items, 1):
        kind_zh = {"image": "图片/扫描件", "text": "文本文档", "table": "表格"}.get(
            it.get("kind"), "无法读取")
        text = re.sub(r"\s+", " ", it.get("text") or "")[:320]
        line = "%d. %s（%s，%d bytes）" % (i, it.get("name"), kind_zh, it.get("size") or 0)
        if text:
            line += "：%s" % text
        if it.get("error"):
            line += "；错误：%s" % it["error"]
        lines.append(line)
    lines.append("用户说「第一张/第二份/这个文件」时，按上面的编号和文件名对应；"
                 "调用 draft_order 时传 attachment 编号或文件名。不要把所有附件默认塞进同一张单。")
    return "\n".join(lines)


def pick(items, selector):
    """按编号、文件名或唯一文件名选择附件。"""
    selector = str(selector or "").strip()
    if selector.isdigit():
        i = int(selector) - 1
        return items[i] if 0 <= i < len(items) else None
    if selector:
        for it in items:
            if selector == it.get("name") or selector == os.path.splitext(it.get("name"))[0]:
                return it
        for it in items:
            if selector in it.get("name") or it.get("name") in selector:
                return it
    return None
