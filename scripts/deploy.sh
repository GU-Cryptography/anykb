#!/usr/bin/env bash
# AnyKB 部署 — 构建镜像（增量，秒级 cache）+ 创建/更新容器。
# 用法：
#   ./scripts/deploy.sh           首次部署 / 代码改了重新部署
#   ./scripts/deploy.sh backend   只重建 backend 服务
set -euo pipefail

cd "$(dirname "$0")/.."

if [[ ! -f .env ]]; then
  echo "✗ .env not found. Copy env.docker.example to .env and fill in secrets first." >&2
  exit 1
fi

SERVICE="${1:-}"

echo "==> Building $([[ -n $SERVICE ]] && echo "$SERVICE" || echo "all images")..."
docker compose build ${SERVICE:+"$SERVICE"}

echo "==> Bringing stack up..."
docker compose up -d --remove-orphans ${SERVICE:+"$SERVICE"}

echo "==> Waiting 10s for healthchecks..."
sleep 10

echo "==> Status:"
docker compose ps --format "table {{.Name}}\t{{.Status}}\t{{.Ports}}"

echo
echo "==> Recent backend logs:"
docker compose logs --tail=15 backend 2>&1 | tail -20

echo
echo "Done. Public: http://$(hostname -I | awk '{print $1}')/"
