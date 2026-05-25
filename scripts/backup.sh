#!/usr/bin/env bash
# AnyKB 备份 — 打包 PostgreSQL volume + backend data volume 成 tarball。
# 用法：
#   ./scripts/backup.sh                  备份到 ./backups/anykb-YYYY-MM-DD.tgz
#   ./scripts/backup.sh /mnt/backups     备份到指定目录
set -euo pipefail

cd "$(dirname "$0")/.."

DEST_DIR="${1:-./backups}"
mkdir -p "$DEST_DIR"

STAMP=$(date +%F-%H%M)
PG_OUT="$DEST_DIR/anykb-pg-$STAMP.tgz"
DATA_OUT="$DEST_DIR/anykb-data-$STAMP.tgz"

echo "==> Backing up postgres volume → $PG_OUT"
docker run --rm \
  -v anykb_postgres-data:/src:ro \
  -v "$(realpath "$DEST_DIR")":/dst \
  alpine tar czf "/dst/anykb-pg-$STAMP.tgz" -C /src .

echo "==> Backing up backend data volume (Milvus Lite + uploads) → $DATA_OUT"
docker run --rm \
  -v anykb_backend-data:/src:ro \
  -v "$(realpath "$DEST_DIR")":/dst \
  alpine tar czf "/dst/anykb-data-$STAMP.tgz" -C /src .

echo
echo "Done:"
ls -lh "$PG_OUT" "$DATA_OUT"
