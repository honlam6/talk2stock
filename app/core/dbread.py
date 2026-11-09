#!/usr/bin/env python3
"""ERP 全库只读查询（零第三方依赖）——问答 agent 的「全局视野」。

为什么需要它：/ask 早期工具箱只有库存/单据统计/文档检索几个窄工具，
「有多少个供应商」「上个月谁买得最多」「某商品的采购明细」这类问题
模型没有可用工具，只能在无关工具里空转，最终答不上来。

做法：通过 `docker exec -i` 调用 erp-mysql 容器内的 mysql CLI（纯标准库
subprocess，不引入 pymysql，保持项目「零第三方依赖」承诺），把模型生成的
只读 SQL 送进数据库。四道护栏保证「有权限了解」但「绝不写」：

1. SQL 文本校验：只 SELECT/WITH、单语句、无注释、无客户端命令、无系统库。
2. 只读事务：连接即 `START TRANSACTION READ ONLY`，任何 DML/DDL 直接被
   MySQL 拒绝（ERROR 1792），校验被绕过也写不进去。
3. 语句超时：SET SESSION MAX_EXECUTION_TIME=8000 + subprocess timeout。
4. 行数/单元格上限：结果最多 200 行，超长文本截断，避免撑爆 prompt。

表结构（schema）从 information_schema 动态读取并缓存——不硬编码表清单，
换库/加表后自动可见；低基数字段的取值由 db_schema 按需 DISTINCT 采样，
让模型自己从数据里学口径，而不是靠人写死的说明书。
"""
import json
import os
import re
import subprocess
import threading
import time

from . import config as cfgmod

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
COMPOSE_PATH = os.path.join(REPO_ROOT, "jshERP-latest", "deploy", "docker-compose.yml")

ROW_LIMIT = 200
CELL_MAX = 300
SQL_TIMEOUT = 25
SCHEMA_TTL = 600
MAX_SQL_CHARS = 2000

# 只读白名单之外的动作词（只读事务是兜底，这里是第一道快速拦截）
_DENY = re.compile(
    r"\b(insert|update|delete|drop|alter|create|truncate|grant|revoke|call|"
    r"load_file|outfile|dumpfile|into|lock|handler|benchmark|sleep|shutdown|"
    r"kill|flush|reset|optimize|repair|rename|execute|prepare|deallocate|delimiter)\b",
    re.I)
_SYS_DB = re.compile(r"\b(mysql|sys|performance_schema|information_schema)\s*\.", re.I)
_CLIENT_CMD = re.compile(r"(?:^|\s)\\[gGsS!]")

_lock = threading.Lock()
_cache = {"schema": None, "schema_at": 0.0, "tables": None}


class DbError(Exception):
    pass


# --------------------------------------------------------------------------
# 连接配置：config.json 的 db 段 > compose 文件 > 内置默认
# --------------------------------------------------------------------------
def _creds():
    conf = (cfgmod.load() or {}).get("db") or {}
    pwd, dbname, user = conf.get("pass") or conf.get("password"), conf.get("name") or conf.get("database"), conf.get("user")
    container = conf.get("container")
    try:
        with open(COMPOSE_PATH, encoding="utf-8") as f:
            text = f.read()
        m = re.search(r"MYSQL_ROOT_PASSWORD:\s*[\"']?([^\"'\s]+)", text)
        n = re.search(r"MYSQL_DATABASE:\s*[\"']?([^\"'\s]+)", text)
        if not pwd and m:
            pwd = m.group(1)
        if not dbname and n:
            dbname = n.group(1)
    except OSError:
        pass
    return {
        "container": container or "erp-mysql",
        "user": user or "root",
        "password": pwd or "123456",
        "name": dbname or "jsh_erp",
    }


def _mysql_cmd(extra_init=""):
    c = _creds()
    init = "START TRANSACTION READ ONLY; SET SESSION MAX_EXECUTION_TIME=8000"
    if extra_init:
        init += "; " + extra_init
    cmd = [
        "docker", "exec", "-i", "-e", "MYSQL_PWD=" + c["password"], c["container"],
        "mysql", "-u" + c["user"], "-D" + c["name"],
        "--default-character-set=utf8mb4", "--batch", "--raw",
        "--connect-timeout=5", "--init-command=" + init,
    ]
    return cmd, c


def _unescape(s):
    """mysql --batch 会把值里的 tab/换行/反斜杠转义成 \\t \\n \\\\。"""
    return s.replace("\\t", "\t").replace("\\n", "\n").replace("\\\\", "\\")


def _parse_rows(out, limit=ROW_LIMIT):
    lines = out.splitlines()
    if not lines:
        return [], [], False
    cols = [_unescape(x) for x in lines[0].split("\t")]
    rows, truncated = [], False
    for line in lines[1:]:
        if len(rows) >= limit:
            truncated = True
            break
        vals = [_unescape(x) for x in line.split("\t")]
        row = {}
        for i, col in enumerate(cols):
            v = vals[i] if i < len(vals) else None
            if v == "NULL":
                v = None
            if isinstance(v, str):
                if len(v) > CELL_MAX:
                    v = v[:CELL_MAX] + "…"
            row[col] = v
        rows.append(row)
    return cols, rows, truncated


def _run(sql, limit=ROW_LIMIT):
    """执行一条只读 SQL，返回 {columns, rows, row_count, truncated}。"""
    cmd, _ = _mysql_cmd()
    try:
        p = subprocess.run(cmd, input=(sql.rstrip(";") + ";\n").encode("utf-8"),
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           timeout=SQL_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise DbError("查询超时（超过 %d 秒），请加条件或 LIMIT 缩小范围" % SQL_TIMEOUT)
    except OSError as e:
        raise DbError("无法连接 ERP 数据库：%s" % e)
    err = p.stderr.decode("utf-8", "replace").strip()
    if p.returncode != 0:
        raise DbError((err or p.stdout.decode("utf-8", "replace"))[:300])
    cols, rows, truncated = _parse_rows(p.stdout.decode("utf-8", "replace"), limit)
    return {"columns": cols, "rows": rows, "row_count": len(rows), "truncated": truncated}


# --------------------------------------------------------------------------
# SQL 校验（模型生成 → 落地执行前的唯一入口）
# --------------------------------------------------------------------------
def validate_sql(sql):
    s = (sql or "").strip()
    if not s:
        raise DbError("SQL 不能为空")
    if len(s) > MAX_SQL_CHARS:
        raise DbError("SQL 过长（最多 %d 字符）" % MAX_SQL_CHARS)
    # 末尾最多一个分号；中间出现分号一律拒绝（多语句）
    s = s.rstrip()
    if s.endswith(";"):
        s = s[:-1].rstrip()
    if ";" in s:
        raise DbError("只允许一条 SELECT 语句（不要写分号）")
    if re.search(r"--|/\*|\*/|(?:^|\s)#", s):
        raise DbError("SQL 里不允许注释")
    if _CLIENT_CMD.search(s):
        raise DbError("不允许 mysql 客户端命令")
    if not re.match(r"^(select|with)\b", s, re.I):
        raise DbError("只允许 SELECT / WITH 查询")
    if _DENY.search(s):
        raise DbError("只允许只读查询（检测到写操作或危险函数）")
    if _SYS_DB.search(s):
        raise DbError("业务表结构已在上下文里，直接写业务表（jsh_*）的 SQL；information_schema 不可查，也不要再用探查语句")
    # 无 LIMIT 且非聚合的大结果集，自动加 LIMIT，避免拖垮 / 撑爆上下文
    if not re.search(r"\blimit\b", s, re.I) and not re.search(
            r"\b(count|sum|avg|min|max|group\s+by)\b", s, re.I):
        s += " LIMIT %d" % ROW_LIMIT
    return s


def sql_query(args):
    """agent 工具：只读 SQL 查全库。返回 (data, 模板句)。"""
    try:
        clean = validate_sql(str(args.get("sql") or ""))
        res = _run(clean)
    except DbError as e:
        return {"error": str(e), "hint": "只查业务表；计数用 COUNT(*)；软删除字段 delete_flag='0'"}, None
    data = {"sql": clean, "columns": res["columns"], "rows": res["rows"],
            "row_count": res["row_count"], "truncated": res["truncated"]}
    head = "只读查询返回 %d 行" % res["row_count"]
    if res["truncated"]:
        head += "（已截断到前 %d 行）" % ROW_LIMIT
    if res["rows"]:
        if res["row_count"] <= 12:
            compact = "；".join(json.dumps(r, ensure_ascii=False)[:150] for r in res["rows"])
            head += "，结果：%s" % compact[:900]
        else:
            sample = json.dumps(res["rows"][0], ensure_ascii=False)
            head += "，首行：%s" % sample[:CELL_MAX]
    return data, head + "。"


# --------------------------------------------------------------------------
# 表结构：动态从 information_schema 读，缓存 10 分钟
# --------------------------------------------------------------------------
_SCHEMA_SQL = """
SELECT t.table_name AS table_name, t.table_comment AS table_comment,
       c.column_name AS column_name, c.column_comment AS column_comment,
       c.data_type AS data_type, c.ordinal_position AS ordinal_position
FROM information_schema.tables t
JOIN information_schema.columns c
  ON c.table_schema = t.table_schema AND c.table_name = t.table_name
WHERE t.table_schema = '%s'
ORDER BY t.table_name, c.ordinal_position
"""


def _schema_rows():
    with _lock:
        if _cache["schema"] is not None and time.time() - _cache["schema_at"] < SCHEMA_TTL:
            return _cache["schema"]
        c = _creds()
        name = c["name"].replace("'", "''")
        # 内部固定 SQL 不走模型校验（要查 information_schema 才能拿到表结构）
        cmd, _ = _mysql_cmd()
        # 内部查询需要跨库读 information_schema，去掉只读事务之外的限制不影响
        p = subprocess.run(cmd, input=(_SCHEMA_SQL % name).encode("utf-8"),
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           timeout=SQL_TIMEOUT)
        if p.returncode != 0:
            raise DbError(p.stderr.decode("utf-8", "replace")[:200])
        _, rows, _ = _parse_rows(p.stdout.decode("utf-8", "replace"), limit=10000)
        _cache["schema"] = rows
        _cache["schema_at"] = time.time()
        return rows


def table_names():
    try:
        return list(dict.fromkeys(r["table_name"] for r in _schema_rows()))
    except Exception:
        return []


def schema_brief(max_cols=18):
    """注入 agent context 的压缩表结构：表名【表注释】：列名..."""
    try:
        rows = _schema_rows()
    except Exception:
        return ""      # 数据库不可达时绝不拖累问答，旧工具照常可用
    groups = {}
    for r in rows:
        g = groups.setdefault(r["table_name"], {"comment": r.get("table_comment") or "", "cols": []})
        g["cols"].append((r["column_name"], r.get("column_comment") or ""))
    lines = ["# ERP 全库只读表结构（共 %d 张表，用 sql_query 查任意表；字段取值不确定先用 SELECT DISTINCT 探查）" % len(groups)]
    for t, g in groups.items():
        cols = [c for c, _ in g["cols"][:max_cols]]
        more = "…（共%d列，用 db_schema 看全字段）" % len(g["cols"]) if len(g["cols"]) > max_cols else ""
        lines.append("- %s【%s】：%s%s" % (t, g["comment"], ", ".join(cols), more))
    lines.append("# 通用口径：软删除表用 delete_flag='0' 取有效数据；单据金额看 total_price/change_amount；查大表请带 LIMIT。")
    return "\n".join(lines)


def db_schema(args):
    """agent 工具：看某张表的完整字段 + 低基数字段真实取值采样。"""
    table = str(args.get("table") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_]+", table):
        return {"error": "表名非法（只允许字母数字下划线）"}, None
    try:
        rows = _schema_rows()
    except Exception as e:
        return {"error": "读取表结构失败：%s" % str(e)[:120]}, None
    mine = [r for r in rows if r["table_name"] == table]
    if not mine:
        return {"error": "没有这张表：%s；可用：%s" % (table, "、".join(table_names()[:40]))}, None
    cols = [{"name": r["column_name"], "type": r["data_type"],
             "comment": r["column_comment"]} for r in mine]
    # 低基数字段：从数据自动采样取值（不硬编码口径）
    samples = {}
    has_delete = any(c["name"] == "delete_flag" for c in cols)
    for c in cols:
        n = c["name"]
        if not re.search(r"(type|status|enabled|flag|state|level|kind)$", n, re.I) and c["type"] not in ("enum", "set"):
            continue
        where = " WHERE delete_flag='0'" if has_delete and n != "delete_flag" else ""
        try:
            r = _run("SELECT DISTINCT `%s` AS v FROM `%s`%s LIMIT 8" % (n, table, where), limit=8)
            samples[n] = [x.get("v") for x in r["rows"]]
        except Exception:
            continue
    data = {"table": table, "columns": cols, "column_count": len(cols), "value_samples": samples}
    return data, "「%s」共 %d 个字段，取值样本：%s。" % (table, len(cols), json.dumps(samples, ensure_ascii=False)[:400])
