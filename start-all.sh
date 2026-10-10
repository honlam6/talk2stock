#!/usr/bin/env bash
# 一键拉起 jshERP 全套服务：colima 容器运行时 + ERP 四容器 + AI 录入壳
# 用法：bash start-all.sh
set -euo pipefail
export PATH=/opt/homebrew/bin:$PATH
ROOT="$(cd "$(dirname "$0")" && pwd)"

# 1) 容器运行时（本机 Docker.app 已不存在，用 colima）
if ! docker info >/dev/null 2>&1; then
  echo "[1/3] 启动 colima ..."
  colima start --cpu 4 --memory 6 --disk 60 --vm-type vz --dns 223.5.5.5
else
  echo "[1/3] docker 已在运行"
fi

# 2) ERP 栈：mysql / redis / backend(9999) / web(8080)
echo "[2/3] 启动 ERP 容器 ..."
cd "$ROOT/jshERP-latest/deploy"
docker compose up -d
docker compose ps

# 3) AI 录入壳（纯标准库，8090）
echo "[3/3] 启动 AI 录入壳 ..."
cd "$ROOT"
if curl -sf -m 3 -o /dev/null http://localhost:8090/ai 2>/dev/null; then
  echo "  已在运行"
else
  # 8090 可能被僵死进程占着（能 listen 却不响应），先清理再起
  old=$(lsof -nP -iTCP:8090 -sTCP:LISTEN -t 2>/dev/null || true)
  if [ -n "$old" ]; then
    echo "  清理占用 8090 的旧进程：$old"
    kill $old 2>/dev/null || true
    sleep 1
    kill -9 $old 2>/dev/null || true
  fi
  nohup python3 app/server.py >>/tmp/t2s-app.log 2>&1 &
  disown 2>/dev/null || true
  sleep 2
  if curl -sf -m 3 -o /dev/null http://localhost:8090/ai; then
    echo "  已启动，日志 /tmp/t2s-app.log"
  else
    echo "  启动失败，请查 /tmp/t2s-app.log" >&2
  fi
fi

echo
echo "ERP 前端   http://localhost:8080/"
echo "ERP 后端   http://localhost:9999/jshERP-boot/doc.html"
echo "AI 录入壳  http://localhost:8090/ai"
echo "登录账号   jsh / 123456（admin 为超管，租户 0）"
