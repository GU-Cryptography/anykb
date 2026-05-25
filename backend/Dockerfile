FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        curl \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps first for layer caching.
# Extras: milvus (Milvus Lite embedded vector store) + asyncpg (PostgreSQL async driver)
COPY pyproject.toml ./
RUN pip install --no-cache-dir -e '.[milvus]' asyncpg

# Source.
COPY src ./src

# Runtime data dir (PG handles app DB; this volume holds Milvus Lite .db + uploads).
RUN mkdir -p /app/data/uploads

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:8000/health || exit 1

CMD ["python", "-m", "uvicorn", "src.app:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips=*"]
