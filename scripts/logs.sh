#!/usr/bin/env bash
# AnyKB 日志 — 跟踪指定服务（默认 backend）的日志。
# 用法：
#   ./scripts/logs.sh              tail -f backend
#   ./scripts/logs.sh frontend     tail -f frontend
#   ./scripts/logs.sh all          tail -f 所有服务
set -euo pipefail

cd "$(dirname "$0")/.."

SERVICE="${1:-backend}"

if [[ "$SERVICE" == "all" ]]; then
  exec docker compose logs -f --tail=50
else
  exec docker compose logs -f --tail=50 "$SERVICE"
fi
