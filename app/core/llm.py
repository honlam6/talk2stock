#!/usr/bin/env python3
"""LLM 解析层：自然语言 → 严格 JSON 草稿。OpenAI 兼容接口，纯标准库。

设计要点：
- 强制 JSON 输出（优先 response_format=json_object，失败自动降级并从文本剥 JSON）
- prompt 里明确列出「只需要 4 类信息」，其余字段禁止编造
- 附带 ERP 现有主数据（供应商/客户/商品/仓库名单）让模型优先复用已有名称，减少误建
- 输出后做规范化 + 自检，畸形则报错让上层回退
"""
import json
import re
import urllib.error
import urllib.request

from . import config as cfgmod
from .erp import BILL_TYPES

TIMEOUT = 120

BIZ_ENUM = list(BILL_TYPES.keys())

SYSTEM_PROMPT = """你是 jshERP 进销存系统的单据录入助理。把用户的一句话业务描述，转成一张单据的 JSON。

# 你只需要提取 4 类信息
1. 业务类型（买/卖/退）
2. 往来单位（供应商或客户的名字）
3. 商品明细（名称、数量、单位、单价）
4. 可选：仓库、日期、备注、实付金额

# 绝对不要编造这些字段
单据编号、totalPrice、金额合计、条码、materialId、organId、tenantId、账户ID、审核状态。
这些由系统自动生成。你只管上面 4 类。

# 业务类型判断（biz 字段，只能取这些值）
- purchase_in     采购入库：从别人那里买进来。关键词：买、采购、进货、进了、购入
- sale_out        销售出库：卖给别人。关键词：卖、销售、出货、发货给
- purchase_return 采购退货：把买来的货退回给供应商。关键词：退给供应商、退货给X（X是卖家）
- sale_return     销售退货：客户把货退回给我。关键词：客户退回、X退货回来
- other_in        其他入库：无供应商的入库，如盘盈、领用退回
- other_out       其他出库：无客户的出库，如报损、领用

# 输出 JSON 格式（严格遵守，不要加任何其他键）
{
  "biz": "purchase_in",
  "party": "美宜佳",
  "items": [
    {"name": "可乐", "qty": 3, "unit": "瓶", "price": 3.5}
  ],
  "depot": "",
  "oper_time": "",
  "remark": "",
  "pay_amount": null,
  "confidence": "high",
  "note": ""
}

# 字段规则
- party：只写单位名称本身，不要带"供应商"、"客户"、"从"、"给"等字。
- items[].name：只写商品名，绝不能包含数字、数量、单位、价格。"3瓶可乐"→ name是"可乐"，qty是3，unit是"瓶"。
- qty / price：纯数字，不带单位不带"元"。price 是单价不是总价；若用户只说了总价，用 总价÷数量 算单价。
- unit：中文量词，如 个/瓶/箱/kg/卷/台。没说就填"个"。
- depot：用户明确说了仓库才填，否则留空字符串（系统会用默认仓）。
- oper_time：用户明确说了日期才填，格式 "YYYY-MM-DD HH:MM:SS"，否则留空。
- pay_amount：用户明确提到"只付了X"、"付一半"、"欠款"时填实付数字；全款或没提就填 null。
- confidence：high/medium/low。信息不全或语义模糊时填 low，并在 note 里用中文说明缺什么。

# 示例
输入：从美宜佳买了3瓶可乐，单价3块5
输出：{"biz":"purchase_in","party":"美宜佳","items":[{"name":"可乐","qty":3,"unit":"瓶","price":3.5}],"depot":"","oper_time":"","remark":"","pay_amount":null,"confidence":"high","note":""}

输入：卖给永辉超市200个纸箱，一个5块，从成品仓出，先付了500
输出：{"biz":"sale_out","party":"永辉超市","items":[{"name":"纸箱","qty":200,"unit":"个","price":5}],"depot":"成品仓","oper_time":"","remark":"","pay_amount":500,"confidence":"high","note":""}

输入：玖龙纸业那批牛皮纸有问题，退10卷回去，每卷80
输出：{"biz":"purchase_return","party":"玖龙纸业","items":[{"name":"牛皮纸","qty":10,"unit":"卷","price":80}],"depot":"","oper_time":"","remark":"","pay_amount":null,"confidence":"high","note":""}

只输出 JSON，不要 markdown 代码块，不要解释。"""


def _context_block(erp):
    """把 ERP 现有主数据塞进 prompt，让模型优先复用已有名称。"""
    try:
        sup = [s.get("supplier") for s in erp.suppliers("供应商")][:60]
        cus = [s.get("supplier") for s in erp.suppliers("客户")][:60]
        mat = [m.get("name") for m in erp.materials()][:120]
        dep = [d.get("name") for d in erp.depots()][:30]
    except Exception:
        return ""
    parts = ["\n# 系统里已有的主数据（名称相同/相近时请直接用这里的写法，避免重复新建）"]
    if sup:
        parts.append("供应商：" + "、".join(filter(None, sup)))
    if cus:
        parts.append("客户：" + "、".join(filter(None, cus)))
    if mat:
        parts.append("商品：" + "、".join(filter(None, mat)))
    if dep:
        parts.append("仓库：" + "、".join(filter(None, dep)))
    parts.append("如果用户说的名字不在上面，照用户说的填，系统会自动新建。")
    return "\n".join(parts)


def is_configured():
    return cfgmod.active_profile() is not None


def _post(url, payload, api_key, stream=False):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
        "Accept": "text/event-stream" if stream else "application/json",
    }
    req = urllib.request.Request(url, data=body, method="POST", headers=headers)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        if not stream:
            return json.loads(r.read().decode("utf-8", "replace"))
        # SSE 流式：逐行读取 "data: {...}"，拼接 choices[0].delta.content
        text = ""
        for raw in r:
            line = raw.decode("utf-8", "replace").strip()
            if not line or not line.startswith("data:"):
                continue
            data = line[len("data:"):].strip()
            if data == "[DONE]":
                break
            try:
                obj = json.loads(data)
            except Exception:
                continue
            choices = obj.get("choices") or []
            if not choices:
                continue
            delta = choices[0].get("delta") or {}
            piece = delta.get("content")
            if isinstance(piece, str):
                text += piece
        return text


def _merge_system(messages):
    """部分 OpenAI 兼容网关会丢弃 system 角色（请求被转给自带人格的 agent，返回闲聊）。
    把 system 内容并入首条 user 消息，保证抽取指令能真正到达模型。"""
    sys_parts = [(m.get("content") or "") for m in messages if m.get("role") == "system"]
    rest = [m for m in messages if m.get("role") != "system"]
    if not sys_parts:
        return messages
    head = "\n\n".join(p for p in sys_parts if p)
    if rest and rest[0].get("role") == "user":
        rest[0] = {"role": "user",
                   "content": head + "\n\n# 用户输入\n" + (rest[0].get("content") or "")}
    else:
        rest.insert(0, {"role": "user", "content": head})
    return rest


def _chat(prof, messages, force_json=True):
    if prof.get("merge_system"):
        messages = _merge_system(messages)
    url = prof["base_url"].rstrip("/") + "/chat/completions"
    base = {
        "model": prof["model"],
        "messages": messages,
        "temperature": prof.get("temperature", 0),
    }
    if force_json:
        base["response_format"] = {"type": "json_object"}

    last_err = None
    # 1) 流式 + response_format（优先；很多推理端点非流式会挂起）
    try:
        txt = _post(url, dict(base, stream=True), prof["api_key"], stream=True)
        if txt and txt.strip():
            return txt
    except Exception as e:
        last_err = e
    # 2) 流式，不带 response_format（兼容不支持该参数的端点）
    try:
        payload = {k: v for k, v in base.items() if k != "response_format"}
        payload["stream"] = True
        txt = _post(url, payload, prof["api_key"], stream=True)
        if txt and txt.strip():
            return txt
    except Exception as e:
        last_err = e
    # 3) 非流式兜底（去掉 response_format 重试一次）
    try:
        payload = {k: v for k, v in base.items() if k != "response_format"}
        j = _post(url, payload, prof["api_key"], stream=False)
        return j["choices"][0]["message"]["content"]
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        if force_json and ("response_format" in detail or e.code == 400):
            j = _post(url, payload, prof["api_key"], stream=False)
            return j["choices"][0]["message"]["content"]
        raise RuntimeError(f"AI 接口报错 {e.code}：{detail}")
    except Exception as e:
        raise RuntimeError(f"AI 接口连接失败：{e}（流式也失败：{last_err}）")


def _extract_json(text):
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    # 从文本里抠第一个平衡的大括号块
    depth, start = 0, -1
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                try:
                    return json.loads(text[start:i + 1])
                except Exception:
                    start = -1
    raise RuntimeError("AI 未返回可解析的 JSON")


# --------- 输出清洗 ---------
UNITS = ["瓶", "个", "箱", "卷", "包", "袋", "台", "件", "只", "张", "米", "斤",
         "公斤", "kg", "KG", "克", "g", "吨", "套", "本", "支", "根", "块", "片", "打", "桶", "罐", "条"]

_NUM = r"\d+(?:\.\d+)?"


def _to_num(v, default=0.0):
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v or "").strip().replace(",", "")
    if not s:
        return default
    # 中文金额："3块5" / "3块5毛" / "3元5" → 3.5；"3块" → 3
    m = re.match(rf"^({_NUM})\s*[块元]\s*({_NUM})?\s*[毛角分]?$", s)
    if m:
        whole = float(m.group(1))
        if m.group(2):
            frac = m.group(2)
            # "3块5" 的 5 是角
            return whole + float(frac) / (10 ** len(frac.split(".")[0]))
        return whole
    m = re.search(_NUM, s)
    return float(m.group()) if m else default


def _clean_name(name):
    """把混进商品名的数量/单位/价格剥掉。返回 (name, qty, unit)"""
    s = str(name or "").strip()
    qty = unit = None
    # 前缀式 "3瓶可乐"
    m = re.match(rf"^({_NUM})\s*({'|'.join(UNITS)})?\s*(.+)$", s)
    if m and m.group(3):
        qty = float(m.group(1))
        unit = m.group(2)
        s = m.group(3).strip()
    # 后缀式 "可乐3瓶"
    m = re.match(rf"^(.+?)\s*({_NUM})\s*({'|'.join(UNITS)})$", s)
    if m:
        s = m.group(1).strip()
        qty = qty or float(m.group(2))
        unit = unit or m.group(3)
    s = re.sub(r"[，,。.、]+$", "", s).strip()
    s = re.sub(r"^(的|了|个)+", "", s).strip()
    return s, qty, unit


def _clean_party(name):
    s = str(name or "").strip()
    s = re.sub(r"^(从|向|给|跟|和|由)\s*", "", s)
    s = re.sub(r"(那里|这里|公司处)$", "", s)
    # 剥掉「供应商/客户」类型前缀；但「供应商1」这类本身是完整名称时保留
    m = re.match(r"^(供应商|客户)[:：]?\s*(.+)$", s)
    if m and not re.fullmatch(r"\d+", m.group(2).strip()):
        s = m.group(2).strip()
    s = re.sub(r"[，,。.、]+$", "", s)
    return s.strip()


def normalize(raw):
    """把 LLM 原始输出规范成标准 draft，并返回问题列表。"""
    problems = []
    biz = str(raw.get("biz") or "").strip()
    if biz not in BIZ_ENUM:
        problems.append(f"业务类型无法识别（{biz or '空'}）")
        biz = biz if biz in BIZ_ENUM else "purchase_in"

    party = _clean_party(raw.get("party") or raw.get("supplier") or raw.get("customer"))
    # 仅当整串是编号/数字开头时提示；「供应商1」这类合法名称不误报
    if re.fullmatch(r"[\d\s:：]+", party) or re.match(r"^\d", party):
        problems.append(f"往来单位「{party}」含数字，可能识别有误")

    items = []
    for it in (raw.get("items") or []):
        if not isinstance(it, dict):
            continue
        name, qty_in_name, unit_in_name = _clean_name(it.get("name"))
        qty = _to_num(it.get("qty"), 0) or qty_in_name or 0
        unit = str(it.get("unit") or unit_in_name or "个").strip() or "个"
        price = _to_num(it.get("price"), 0)
        # 只给了总价的情况
        if not price and it.get("amount") and qty:
            price = round(_to_num(it["amount"]) / qty, 4)
        if not name:
            problems.append("有一行商品没有名称")
            continue
        if qty <= 0:
            problems.append(f"商品「{name}」数量缺失或为 0")
        items.append({"name": name, "qty": qty, "unit": unit, "price": price})

    if not items:
        problems.append("没有解析出任何商品明细")

    draft = {
        "biz": biz,
        "party": party,
        "depot": str(raw.get("depot") or "").strip(),
        "items": items,
        "oper_time": str(raw.get("oper_time") or "").strip(),
        "remark": str(raw.get("remark") or "").strip(),
        "pay_amount": raw.get("pay_amount"),
    }
    return draft, problems


def parse(text, erp=None):
    """自然语言 → (draft, meta)。未配置模型或调用失败时抛 RuntimeError。"""
    prof = cfgmod.active_profile()
    if not prof:
        raise RuntimeError("尚未配置 AI 模型，请在「设置」里添加一个模型档案")
    sys_prompt = SYSTEM_PROMPT + (_context_block(erp) if erp else "")
    content = _chat(prof, [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": text},
    ])
    raw = _extract_json(content)
    draft, problems = normalize(raw)
    meta = {
        "model": prof["model"],
        "profile": prof.get("name") or prof.get("key"),
        "confidence": raw.get("confidence") or "",
        "note": raw.get("note") or "",
        "problems": problems,
        "raw": raw,
    }
    return draft, meta


def test_profile(prof):
    """测试一个模型档案是否可用。返回 (ok, message)。"""
    try:
        c = _chat(prof, [
            {"role": "system", "content": "只回复 JSON：{\"ok\":true}"},
            {"role": "user", "content": "测试"},
        ])
        _extract_json(c)
        return True, "连接成功"
    except Exception as e:
        return False, str(e)
