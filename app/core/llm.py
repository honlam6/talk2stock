#!/usr/bin/env python3
"""LLM 解析层：自然语言 → 严格 JSON 草稿。OpenAI 兼容接口，纯标准库。

设计要点：
- 强制 JSON 输出（优先 response_format=json_object，失败自动降级并从文本剥 JSON）
- prompt 里明确列出「只需要 4 类信息」，其余字段禁止编造
- 附带 ERP 现有主数据（供应商/客户/商品/仓库名单）让模型优先复用已有名称，减少误建
- 输出后做规范化 + 自检，畸形则报错让上层回退

用量与可靠性（Phase 1a）：
- 每次调用记录 token 用量到 data/usage.jsonl（流式带 stream_options.include_usage；
  网关不支持时按字符粗估并标注 estimated，统计失败绝不阻断业务）
- 网络错误 / 5xx 指数退避重试（4xx 参数问题不重试）
"""
import json
import os
import re
import ssl
import threading
import time
import urllib.error
import urllib.request

from . import config as cfgmod
from .erp import BILL_TYPES

TIMEOUT = 120

# 提示词版本号：每次改动 SYSTEM_PROMPT 都要更新，并写进审计日志（迭代留痕）。
# 变更记录：
#   v1-20261010  初始版本（4 类信息抽取 + 禁止编造 + confidence/note）
#   v2-20261010  修相对日期失分：注入当前日期，要求今天/昨天/明天/后天换算成绝对日期
#   v3-20261011  多轮修正：用户对上一稿说「改成50瓶」时，把上一稿 JSON 随输入带入，
#                只改提到的字段、其余保留，输出完整新草稿
#   v4-20261025  规格/型号专项（issue #1「一个配件多个规格对不上」）：商品名自带的
#                型号规格必须保留（M8螺丝/6号电池/2.5平方电线，旧规则会把数字全剥掉）；
#                主数据名单带型号展示照抄；「各N」多规格拆多行；一句话多笔业务只处理
#                第一笔并压低 confidence
PROMPT_VERSION = "v4-20261025"

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
- items[].name：只写商品名本身，不要把数量/单价混进名字（"3瓶可乐"→name是"可乐"，qty是3，unit是"瓶"）。但商品名自带的型号、规格必须完整保留——型号里的数字是名字的一部分："M8螺丝"→"M8螺丝"、"6号电池"→"6号电池"、"2.5平方电线"→"2.5平方电线"、"304不锈钢管"→"304不锈钢管"，绝不能缩成"螺丝/电池/电线/钢管"。主数据名单里怎么写（含型号）就照抄。
- 同一句话里同一商品多个规格各来N个（"4分和6分的PPR管各20根"）→ items 拆成多行，每行一个完整规格名，qty 各自 N，单价对应各自的。
- 一句话里含多笔不同业务（又买又卖、又进又退）→ 只处理语序上的第一笔，confidence 填 low，note 写"检测到多笔业务，已只处理第一笔，其余请分开逐笔录入"；后一笔提到的单价/金额属于后一笔，第一笔没说的字段照旧留空（price 留 0）。
- qty / price：纯数字，不带单位不带"元"。price 是单价不是总价；若用户只说了总价，用 总价÷数量 算单价。
- unit：中文量词，如 个/瓶/箱/kg/卷/台。没说就填"个"。箱/盒这类副单位要原样保留，系统会按单位组自动折算基本数量。
- items[].tax_rate：税率百分比数字。"含税13%"→13，"13个点"→13，"不含税"或没提→0。不要自己算税额。
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

输入：从美宜佳进2箱可乐，一箱含税120，13个点
输出：{"biz":"purchase_in","party":"美宜佳","items":[{"name":"可乐","qty":2,"unit":"箱","price":120,"tax_rate":13}],"depot":"","oper_time":"","remark":"","pay_amount":null,"confidence":"high","note":""}

输入：从日化批发部进6号电池100节，每节2块5
输出：{"biz":"purchase_in","party":"日化批发部","items":[{"name":"6号电池","qty":100,"unit":"节","price":2.5}],"depot":"","oper_time":"","remark":"","pay_amount":null,"confidence":"high","note":""}

输入：卖给张记便利店4分和6分的PPR管各20根，4分的一根3块，6分的一根4块
输出：{"biz":"sale_out","party":"张记便利店","items":[{"name":"4分PPR管","qty":20,"unit":"根","price":3},{"name":"6分PPR管","qty":20,"unit":"根","price":4}],"depot":"","oper_time":"","remark":"","pay_amount":null,"confidence":"high","note":""}

只输出 JSON，不要 markdown 代码块，不要解释。"""


def _ngrams(s):
    s = (s or "").lower()
    return {s[i:i + 2] for i in range(max(len(s) - 1, 1))} or {s}


def _mat_label(m):
    """主数据名单里的商品写法：带型号的拼成「名字 型号」整串展示，
    模型照抄后才能对上「同名不同规格」的档案。"""
    name = str(m.get("name") or "").strip()
    model = str(m.get("model") or "").strip()
    return f"{name} {model}" if name and model else name


def _rank_master(names, text, top_k=12):
    """主数据预筛：按与输入的 2-gram Jaccard 相关度取 top-K。
    全都不相关时返回空（调用方回退全量名单）——宁可多塞不能漏。"""
    q = _ngrams(text)
    scored = []
    for n in names:
        if not n:
            continue
        g = _ngrams(n)
        inter = len(q & g)
        if inter:
            scored.append((inter / len(q | g), n))
    scored.sort(key=lambda x: -x[0])
    return [n for _, n in scored[:top_k]]


def _context_block(erp, text=""):
    """把 ERP 现有主数据塞进 prompt，让模型优先复用已有名称。

    token 效率：带用户输入时先做相关度预筛（每类 top-K 而非全量名单），
    预期把该段从 ~400 token 压到 ~100；无任何相关命中则回退全量（不漏配）。
    """
    try:
        sup = [s.get("supplier") for s in erp.suppliers("供应商")][:60]
        cus = [s.get("supplier") for s in erp.suppliers("客户")][:60]
        mat = [_mat_label(m) for m in erp.materials()][:120]
        dep = [d.get("name") for d in erp.depots()][:30]
    except Exception:
        return ""

    if text:
        sup_f = _rank_master(sup, text)
        cus_f = _rank_master(cus, text)
        mat_f = _rank_master(mat, text)
        dep_f = _rank_master(dep, text, top_k=6)
        if sup_f or cus_f or mat_f:
            sup, cus, mat, dep = sup_f, cus_f, mat_f, dep_f
        else:
            pass   # 输入与全部主数据都不相关：保持全量，避免漏配

    parts = ["\n# 系统里已有的主数据（名称相同/相近时请直接用这里的写法（商品含型号的整串照抄），避免重复新建）"]
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


def _insecure_context():
    """档案声明 insecure 时跳过证书校验（IP 直连 / 自签证书的自建网关）。"""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _post(url, payload, api_key, stream=False, ssl_context=None):
    """发请求，返回 (文本, usage)。usage 为 None 表示网关未返回精确用量。"""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
        "Accept": "text/event-stream" if stream else "application/json",
    }
    req = urllib.request.Request(url, data=body, method="POST", headers=headers)
    kw = {"timeout": TIMEOUT}
    if ssl_context is not None:
        kw["context"] = ssl_context
    with urllib.request.urlopen(req, **kw) as r:
        if not stream:
            j = json.loads(r.read().decode("utf-8", "replace"))
            content = j["choices"][0]["message"]["content"]
            return content, _norm_usage(j.get("usage"))
        # SSE 流式：逐行读取 "data: {...}"，拼接 choices[0].delta.content；
        # include_usage 时 usage 挂在最后一个 chunk（choices 为空），先抓再跳过
        text = ""
        usage = None
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
            u = _norm_usage(obj.get("usage"))
            if u:
                usage = u
            choices = obj.get("choices") or []
            if not choices:
                continue
            delta = choices[0].get("delta") or {}
            piece = delta.get("content")
            if isinstance(piece, str):
                text += piece
        return text, usage


def _post_retry(url, payload, api_key, stream=False, ssl_context=None, retries=2):
    """带重试的 POST：仅网络错误 / 5xx 指数退避重试，4xx（参数问题）重试无意义。"""
    delay = 0.6
    for attempt in range(retries + 1):
        try:
            return _post(url, payload, api_key, stream=stream, ssl_context=ssl_context)
        except urllib.error.HTTPError as e:
            if e.code >= 500 and attempt < retries:
                time.sleep(delay)
                delay *= 2.5
                continue
            raise
        except (urllib.error.URLError, TimeoutError, OSError):
            # URLError(超时/DNS/拒连) 与读取中途断流；POST 幂等（纯生成），重试安全
            if attempt < retries:
                time.sleep(delay)
                delay *= 2.5
                continue
            raise


# 网关能力记忆：不认 stream_options 的端点 400 一次后记住，后续请求不再带
_quirks = set()


def _norm_usage(u):
    if isinstance(u, dict) and u.get("prompt_tokens") is not None:
        try:
            return {"prompt_tokens": int(u.get("prompt_tokens") or 0),
                    "completion_tokens": int(u.get("completion_tokens") or 0),
                    "estimated": False}
        except (TypeError, ValueError):
            return None
    return None


def _estimate_tokens(text):
    """拿不到精确 usage 时的粗估：中文约 1 字 1 token、英文约 4 字符 1 token，混合取 0.8。"""
    return max(1, int(round(len(text or "") * 0.8)))


# --------------------------------------------------------------------------
# 用量记录（data/usage.jsonl）：统计失败绝不阻断业务
# --------------------------------------------------------------------------
_USAGE_MAX_BYTES = 10 * 1024 * 1024
_usage_lock = threading.Lock()


def _record_usage(prof, purpose, usage, latency_ms):
    try:
        d = cfgmod.data_dir()
        os.makedirs(d, exist_ok=True)
        full = os.path.join(d, "usage.jsonl")
        rec = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "profile": prof.get("name") or prof.get("key") or "",
            "model": prof.get("model", ""),
            "purpose": purpose or "",
            "prompt_tokens": usage.get("prompt_tokens", 0),
            "completion_tokens": usage.get("completion_tokens", 0),
            "estimated": bool(usage.get("estimated")),
            "latency_ms": int(latency_ms),
        }
        with _usage_lock:
            try:
                if os.path.getsize(full) > _USAGE_MAX_BYTES:
                    os.replace(full, full + ".1")
            except OSError:
                pass
            with open(full, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def usage_summary():
    """聚合今日（本地时区）LLM 用量，供看板/设置页成本卡展示。"""
    today = time.strftime("%Y-%m-%d")
    out = {"date": today, "calls": 0, "prompt_tokens": 0, "completion_tokens": 0,
           "estimated_calls": 0, "by_purpose": {}, "avg_latency_ms": 0}
    lat_total = 0
    try:
        full = os.path.join(cfgmod.data_dir(), "usage.jsonl")
        with open(full, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if not str(r.get("ts") or "").startswith(today):
                    continue
                out["calls"] += 1
                out["prompt_tokens"] += int(r.get("prompt_tokens") or 0)
                out["completion_tokens"] += int(r.get("completion_tokens") or 0)
                if r.get("estimated"):
                    out["estimated_calls"] += 1
                p = str(r.get("purpose") or "?")
                agg = out["by_purpose"].setdefault(p, {"calls": 0, "tokens": 0})
                agg["calls"] += 1
                agg["tokens"] += int(r.get("prompt_tokens") or 0) + int(r.get("completion_tokens") or 0)
                lat_total += int(r.get("latency_ms") or 0)
    except OSError:
        pass
    if out["calls"]:
        out["avg_latency_ms"] = lat_total // out["calls"]
        out["total_tokens"] = out["prompt_tokens"] + out["completion_tokens"]
    else:
        out["total_tokens"] = 0
    return out


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


def _chat(prof, messages, force_json=True, purpose="", collect=None):
    """单次对话调用（三级降级 + 重试 + 用量记录），返回模型文本。
    purpose 标记调用来源（parse/ask_select/ask_answer/agent/insight/test），进用量台账。
    collect 传入 dict 时，把本次 usage 写入其中（调用方做会话级合计/预算用）。"""
    started = time.time()
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
    ctx = _insecure_context() if prof.get("insecure") else None

    text, usage, last_err = None, None, None

    # 1) 流式 + response_format（优先；很多推理端点非流式会挂起）+ include_usage
    try:
        payload = dict(base, stream=True)
        if "no_stream_options" not in _quirks:
            payload["stream_options"] = {"include_usage": True}
        text, usage = _post_retry(url, payload, prof["api_key"], stream=True, ssl_context=ctx)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        if "stream_options" in detail:
            _quirks.add("no_stream_options")   # 该端点不认此参数，记住并立即降级
        last_err = "HTTP %s %s" % (e.code, detail)
    except Exception as e:
        last_err = e

    # 2) 流式，不带 response_format / stream_options（兼容不支持这些参数的端点）
    if not (text and text.strip()):
        try:
            payload = {k: v for k, v in base.items() if k != "response_format"}
            payload["stream"] = True
            text, usage = _post_retry(url, payload, prof["api_key"], stream=True, ssl_context=ctx)
        except Exception as e:
            last_err = e

    # 3) 非流式兜底
    if not (text and text.strip()):
        try:
            payload = {k: v for k, v in base.items() if k != "response_format"}
            text, usage = _post_retry(url, payload, prof["api_key"], stream=False, ssl_context=ctx)
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            raise RuntimeError(f"AI 接口报错 {e.code}：{detail}")
        except Exception as e:
            raise RuntimeError(f"AI 接口连接失败：{e}（流式也失败：{last_err}）")

    # 用量记录：拿不到精确 usage 时按字符粗估并标注，统计绝不阻断业务
    if usage is None:
        usage = {
            "prompt_tokens": _estimate_tokens(json.dumps(messages, ensure_ascii=False)),
            "completion_tokens": _estimate_tokens(text or ""),
            "estimated": True,
        }
    _record_usage(prof, purpose, usage, int((time.time() - started) * 1000))
    if collect is not None:
        collect.clear()
        collect.update(usage)
    return text


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
        tax_rate = _to_num(it.get("tax_rate"), 0)
        if tax_rate < 0:
            tax_rate = 0
        items.append({"name": name, "qty": qty, "unit": unit, "price": price,
                      "tax_rate": tax_rate})

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


def _date_block():
    """注入当前日期：模型不知道今天几号，相对日期（昨天/明天）必须由我们供给基准。"""
    return ("\n\n# 当前日期\n今天是 %s。用户说「今天／昨天／明天／后天／大后天」等相对日期时，"
            "oper_time 必须换算成对应的绝对日期，格式 YYYY-MM-DD HH:MM:SS。"
            % time.strftime("%Y-%m-%d"))


def _revision_block(prev_draft):
    """多轮修正：把上一稿带给模型，本轮输入是对它的增量修改指令。"""
    return ("\n\n# 修正上一张草稿\n"
            "用户本轮输入是对下面这张已有草稿的修改要求（改数量/单价/仓库/往来单位/日期，或增删商品行），"
            "不是一笔新业务。只修改用户提到的部分，其余字段原样保留，输出修改后的完整草稿 JSON。\n"
            "上一轮草稿：" + json.dumps(prev_draft, ensure_ascii=False))


def _clarify_question(draft):
    """主动澄清：草稿缺关键信息时生成一个追问（纯规则，零 LLM）。

    一次只问最关键的一个缺失，优先级：无明细 > 数量 > 往来单位 > 单价。
    返回 (question, missing_tag) 或 None（信息完整/属非阻断缺失）。
    日期与仓库不追问——有系统默认值，问了反而打断录入节奏。
    """
    items = draft.get("items") or []
    if not items:
        return ("没听出商品明细——说下商品和数量好吗？比如「3瓶可乐」", "items")
    zero_qty = [i for i in items if _to_num(i.get("qty")) <= 0]
    if zero_qty:
        names = "、".join((i.get("name") or "该商品") for i in zero_qty)
        return (f"「{names}」的数量没说清，是多少？", "qty")
    biz = draft.get("biz") or ""
    needs_party = biz in ("purchase_in", "sale_out", "purchase_return", "sale_return")
    if needs_party and not (draft.get("party") or "").strip():
        who = "供应商" if "purchase" in biz else "客户"
        return (f"这是哪家{who}的业务？说个名称", "party")
    no_price = [i for i in items if not _to_num(i.get("price"))]
    if no_price:
        names = "、".join((i.get("name") or "该商品") for i in no_price)
        return (f"「{names}」的单价没提到——是赠品按 0 元入账，还是补充单价？"
                f"可直接回复如「3块5一瓶」", "price")
    return None


def parse(text, erp=None, prev_draft=None):
    """自然语言 → (draft, meta)。未配置模型或调用失败时抛 RuntimeError。
    prev_draft 非空时表示多轮修正：本轮输入是对该草稿的增量修改。
    meta.clarify 非空时表示草稿缺关键信息，建议上层主动追问（可跳过）。"""
    prof = cfgmod.active_profile()
    if not prof:
        raise RuntimeError("尚未配置 AI 模型，请在「设置」里添加一个模型档案")
    sys_prompt = SYSTEM_PROMPT + _date_block() + (_context_block(erp, text) if erp else "")
    user_content = text + (_revision_block(prev_draft) if prev_draft else "")
    u = {}
    content = _chat(prof, [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": user_content},
    ], purpose="parse", collect=u)
    raw = _extract_json(content)
    draft, problems = normalize(raw)
    cl = _clarify_question(draft)
    meta = {
        "model": prof["model"],
        "profile": prof.get("name") or prof.get("key"),
        "confidence": raw.get("confidence") or "",
        "note": raw.get("note") or "",
        "problems": problems,
        "revision": bool(prev_draft),
        "raw": raw,
        "tokens": u,
        "clarify": {"question": cl[0], "missing": cl[1]} if cl else None,
    }
    return draft, meta


def test_profile(prof):
    """测试一个模型档案是否可用。返回 (ok, message)。"""
    try:
        c = _chat(prof, [
            {"role": "system", "content": "只回复 JSON：{\"ok\":true}"},
            {"role": "user", "content": "测试"},
        ], purpose="test")
        _extract_json(c)
        return True, "连接成功"
    except Exception as e:
        return False, str(e)
