#!/usr/bin/env bash
# 本地部署端到端验证：运行时 → 容器 → HTTP 入口 → ERP 登录 → AI 壳 HTTP 链路
# 用法：bash verify-local.sh
#
# 与 app/selfcheck.py 互补：
#   selfcheck.py  直连 core 模块，验「应用层」逻辑
#   本脚本        走真实 HTTP，验「部署层 + 端到端」
set -uo pipefail
export PATH=/opt/homebrew/bin:$PATH
ROOT="$(cd "$(dirname "$0")" && pwd)"

PASS=0; WARN=0; FAIL=0
ok(){   printf "  \033[32m✓\033[0m %s  %s\n" "$1" "${2:-}"; PASS=$((PASS+1)); }
warn(){ printf "  \033[33m!\033[0m %s  %s\n" "$1" "${2:-}"; WARN=$((WARN+1)); }
no(){   printf "  \033[31m✗\033[0m %s  %s\n" "$1" "${2:-}"; FAIL=$((FAIL+1)); }
# 管道内联 python 解析 JSON
py(){ python3 -c "$1" 2>/dev/null; }

echo "========================================================"
echo " jshERP-suite · 本地部署端到端验证    $(date '+%F %T')"
echo "========================================================"

# ── 1) 容器运行时 ───────────────────────────────────────
echo; echo "[1/6] 容器运行时"
if docker info >/dev/null 2>&1; then
  ctx=$(docker context show 2>/dev/null || echo -)
  ok "docker/colima" "在线（context=${ctx}）"
else
  no "docker/colima" "docker info 失败 → 先跑 bash start-all.sh"
fi

# ── 2) ERP 容器健康 ─────────────────────────────────────
echo; echo "[2/6] ERP 容器"
for c in erp-mysql erp-redis erp-backend erp-web; do
  st=$(docker inspect -f '{{.State.Status}}' "$c" 2>/dev/null || echo missing)
  hl=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}n/a{{end}}' "$c" 2>/dev/null || echo n/a)
  if [ "$st" = running ]; then ok "$c" "running · $hl"; else no "$c" "$st"; fi
done

# ── 3) HTTP 入口 ────────────────────────────────────────
echo; echo "[3/6] HTTP 入口"
check_url(){
  local name="$1" url="$2"
  local code; code=$(curl -s -o /dev/null -m 8 -w '%{http_code}' "$url" 2>/dev/null); code=${code:-000}
  if [ "$code" = 200 ]; then ok "$name" "HTTP $code  $url"; else no "$name" "HTTP $code  $url"; fi
}
check_url "ERP 前端(8080)"   "http://localhost:8080/"
check_url "ERP 后端文档(9999)" "http://localhost:9999/jshERP-boot/doc.html"
check_url "AI 录入壳(8090)"  "http://localhost:8090/ai"

# ── 4) ERP 登录（密码 MD5） ─────────────────────────────
echo; echo "[4/6] ERP 登录链路"
PW=$(grep -E '^T2S_ERP_PASS=' "$ROOT/app/.env" 2>/dev/null | cut -d= -f2-)
PW=${PW:-123456}
MD5=$(printf '%s' "$PW" | md5 2>/dev/null | awk '{print $NF}')
LOGIN=$(curl -s -m 8 -X POST http://localhost:9999/jshERP-boot/user/login \
  -H 'Content-Type: application/json' \
  -d "{\"loginName\":\"jsh\",\"password\":\"$MD5\"}")
TOKEN=$(printf '%s' "$LOGIN" | py 'import sys,json;print((json.load(sys.stdin).get("data") or {}).get("token") or "")')
if [ -n "$TOKEN" ]; then ok "jsh 登录" "token ${TOKEN:0:12}…"; else no "jsh 登录" "${LOGIN:0:90}"; fi

# ── 5) AI 壳 → ERP 主数据 ───────────────────────────────
echo; echo "[5/6] AI 壳 → ERP"
MASTER=$(curl -s -m 12 http://localhost:8090/api/master)
CNT=$(printf '%s' "$MASTER" | py 'import sys,json;d=json.load(sys.stdin);print("供应商%d 客户%d 商品%d 仓库%d 账户%d"%tuple(len(d.get(k) or []) for k in ("suppliers","customers","materials","depots","accounts")))')
if [ -n "$CNT" ]; then ok "/api/master" "$CNT"; else no "/api/master" "无响应"; fi

# ── 6) AI 壳 → LLM 端到端（自然语言 → 草稿 → 只读预览）──
echo; echo "[6/6] AI 壳 → LLM 端到端（依赖外部模型，失败记为告警）"
TXT="向供应商1采购商品1 5个，单价10元，入原料仓"
PARSED=$(curl -s -m 90 -X POST http://localhost:8090/api/parse \
  -H 'Content-Type: application/json' -d "{\"text\":\"$TXT\"}")
POK=$(printf '%s' "$PARSED" | py 'import sys,json;print(json.load(sys.stdin).get("ok"))')
if [ "$POK" = True ]; then
  BIZ=$(printf '%s' "$PARSED" | py 'import sys,json;d=json.load(sys.stdin);print((d.get("draft") or {}).get("biz") or "?")')
  NIT=$(printf '%s' "$PARSED" | py 'import sys,json;d=json.load(sys.stdin);print(len((d.get("draft") or {}).get("items") or []))')
  ok "/api/parse" "biz=$BIZ items=$NIT"
else
  warn "/api/parse" "$(printf '%s' "$PARSED" | py 'import sys,json;d=json.load(sys.stdin);print(d.get("message") or "解析失败/超时")')"
fi

# 只读预览：把 parse 的 draft 回灌 /api/resolve（不写库）
DRAFT=$(printf '%s' "$PARSED" | py 'import sys,json;d=json.load(sys.stdin);print(json.dumps({"draft": d.get("draft") or {}}, ensure_ascii=False))')
if [ -n "$DRAFT" ] && [ "$DRAFT" != '{"draft": {}}' ]; then
  RESOLVED=$(curl -s -m 15 -X POST http://localhost:8090/api/resolve \
    -H 'Content-Type: application/json' -d "$DRAFT")
  ROK=$(printf '%s' "$RESOLVED" | py 'import sys,json;print(json.load(sys.stdin).get("ok"))')
  if [ "$ROK" = True ]; then
    CRE=$(printf '%s' "$RESOLVED" | py 'import sys,json;p=json.load(sys.stdin).get("preview") or {};print("待建主数据 %d 项"%len(p.get("to_create") or []))')
    ok "/api/resolve(只读)" "$CRE"
  else
    no "/api/resolve(只读)" "预览失败"
  fi
fi

# ── 汇总 ────────────────────────────────────────────────
echo
echo "========================================================"
printf " 通过 %d · 告警 %d · 失败 %d\n" "$PASS" "$WARN" "$FAIL"
echo "========================================================"
if [ "$FAIL" -eq 0 ]; then
  echo " 结论：本地部署可用 ✓  （入口：8080 前端 / 9999 后端 / 8090 AI 壳）"
  exit 0
else
  echo " 结论：存在失败项 ✗  请按上面 ✗ 行排查"
  exit 1
fi
