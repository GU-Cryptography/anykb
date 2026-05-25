"""Embedding provider abstraction.

Design goals (per project decoupling requirements):
  1. Swap model (BGE-M3 → text-embedding-3-small → …): change EMBEDDING_MODEL only
  2. Swap deployment (remote SiliconFlow → local Ollama): change EMBEDDING_PROVIDER only
  3. Swap entire backend (any OpenAI-compatible API): override EMBEDDING_BASE_URL + KEY

Resolution order for each field:
  base_url:    EMBEDDING_BASE_URL  →  PRESETS[provider].base_url
  api_key:     EMBEDDING_API_KEY   →  provider-specific fallback (openai_api_key, etc.)
  model:       EMBEDDING_MODEL     →  PRESETS[provider].model
  vector_size: EMBEDDING_VECTOR_SIZE  →  MODEL_DIMS[model]  →  probe live (1 call)

The Ollama native protocol (POST /api/embeddings) is preserved for backward compat;
modern Ollama (>= 0.1.17) also exposes /v1/embeddings which works via the openai
path — pick whichever you prefer.
"""
from __future__ import annotations

import hashlib
import math
from typing import Any, TYPE_CHECKING

import httpx

from src.settings import get_settings

if TYPE_CHECKING:
    from src.settings_user import UserEmbeddingConfig

# ---------------------------------------------------------------------------
# Provider presets — fill in defaults when explicit env vars are absent.
# Adding a new OpenAI-compatible provider is a one-line table entry.
# ---------------------------------------------------------------------------
PROVIDER_PRESETS: dict[str, dict[str, Any]] = {
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "model": "text-embedding-3-small",
        "protocol": "openai",
    },
    "siliconflow": {
        "base_url": "https://api.siliconflow.cn/v1",
        "model": "BAAI/bge-m3",
        "protocol": "openai",
    },
    "ollama": {
        # native non-OpenAI endpoint preserved as default for backward compat;
        # set EMBEDDING_BASE_URL=http://localhost:11434/v1 to use openai-compat path
        "base_url": "http://localhost:11434",
        "model": "bge-m3",
        "protocol": "ollama-native",
    },
    "hashmock": {
        "base_url": "",
        "model": "sha256-1024",
        "protocol": "hashmock",
    },
    # back-compat alias: old config used "deepseek" to mean "no real embedding"
    "deepseek": {
        "base_url": "",
        "model": "sha256-1024",
        "protocol": "hashmock",
    },
}

# Known model → vector dim. Keep this small and explicit; unknown models fall
# back to live probe (a single dummy embed call) on first invocation.
MODEL_DIMS: dict[str, int] = {
    # OpenAI
    "text-embedding-3-small": 1536,
    "text-embedding-3-large": 3072,
    "text-embedding-ada-002": 1536,
    # BGE family (SiliconFlow / Ollama / self-hosted)
    "BAAI/bge-m3": 1024,
    "bge-m3": 1024,
    "BAAI/bge-large-zh-v1.5": 1024,
    "BAAI/bge-large-en-v1.5": 1024,
    # Zhipu
    "embedding-3": 2048,
    # Mock
    "sha256-1024": 1024,
}


# ---------------------------------------------------------------------------
# Config resolution
# ---------------------------------------------------------------------------
def _resolve_config(cfg: "UserEmbeddingConfig | None" = None) -> dict[str, Any]:
    # User cfg wins outright when provided.
    if cfg is not None:
        # User-facing "openai-compat" maps onto the "openai" protocol; "ollama"
        # is the native-protocol variant.
        protocol = "ollama-native" if cfg.provider == "ollama" else "openai"
        return {
            "provider": cfg.provider,
            "protocol": protocol,
            "base_url": cfg.base_url.rstrip("/") if cfg.base_url else "",
            "api_key": cfg.api_key,
            "model": cfg.model,
        }

    s = get_settings()
    provider = (s.embedding_provider or "openai").lower()
    preset = PROVIDER_PRESETS.get(provider, PROVIDER_PRESETS["openai"])

    base_url = s.embedding_base_url or preset["base_url"]
    model = s.embedding_model or preset["model"]
    protocol = preset["protocol"]

    api_key = s.embedding_api_key
    if not api_key:
        # provider-specific fallback for back-compat
        if provider == "openai":
            api_key = s.openai_api_key

    return {
        "provider": provider,
        "protocol": protocol,
        "base_url": base_url.rstrip("/") if base_url else "",
        "api_key": api_key,
        "model": model,
    }


# ---------------------------------------------------------------------------
# Shared httpx client (avoids per-call TLS handshake during bulk ingest)
# ---------------------------------------------------------------------------
_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=30.0)
    return _client


async def aclose() -> None:
    """Optional cleanup (e.g. on app shutdown)."""
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
async def embed(text: str, cfg: "UserEmbeddingConfig | None" = None) -> list[float]:
    resolved = _resolve_config(cfg)
    if resolved["protocol"] == "hashmock":
        return _embed_hashmock(text)
    if resolved["protocol"] == "ollama-native":
        return await _embed_ollama_native(text, resolved)
    # openai-compatible (covers OpenAI, SiliconFlow, Together, Groq, vLLM, LMStudio, etc.)
    return await _embed_openai_compat(text, resolved)


async def embed_batch(
    texts: list[str],
    *,
    batch_size: int = 32,
    cfg: "UserEmbeddingConfig | None" = None,
) -> list[list[float]]:
    """Embed many strings with one API call per `batch_size` chunk.

    Cuts TLS / HTTP overhead in ingest (a 100-chunk PDF goes from 100 round-trips
    to ~4). For protocols that don't support array input (ollama-native, hashmock)
    we fall back to per-text calls.
    """
    if not texts:
        return []
    resolved = _resolve_config(cfg)
    if resolved["protocol"] == "hashmock":
        return [_embed_hashmock(t) for t in texts]
    if resolved["protocol"] == "ollama-native":
        # Ollama's /api/embeddings takes a single prompt; loop sequentially.
        return [await _embed_ollama_native(t, resolved) for t in texts]
    # OpenAI-compatible: batch input array
    out: list[list[float]] = []
    for i in range(0, len(texts), batch_size):
        batch = texts[i : i + batch_size]
        out.extend(await _embed_openai_batch(batch, resolved))
    return out


_probed_dims: int | None = None


def get_vector_size(cfg: "UserEmbeddingConfig | None" = None) -> int:
    """Resolution: user cfg.dim → env override → known model → cached probe → error."""
    global _probed_dims
    if cfg is not None and cfg.dim:
        return cfg.dim
    s = get_settings()
    if s.embedding_vector_size > 0:
        return s.embedding_vector_size
    resolved = _resolve_config(cfg)
    model = resolved["model"]
    if model in MODEL_DIMS:
        return MODEL_DIMS[model]
    if _probed_dims is not None:
        return _probed_dims
    raise RuntimeError(
        f"Cannot determine vector size for model '{model}'. "
        f"Set EMBEDDING_VECTOR_SIZE explicitly, or call probe_vector_size() once."
    )


async def probe_vector_size(cfg: "UserEmbeddingConfig | None" = None) -> int:
    """Live probe: embed a dummy string to discover dim of unknown models."""
    global _probed_dims
    vec = await embed("__probe__", cfg=cfg)
    _probed_dims = len(vec)
    return _probed_dims


# ---------------------------------------------------------------------------
# Protocol implementations
# ---------------------------------------------------------------------------
def _raise_with_upstream_detail(resp: httpx.Response) -> None:
    """Raise HTTPStatusError but with the upstream's error body folded into
    the message. httpx's default `raise_for_status` gives "Client error '403
    Forbidden'" which hides provider-specific reasons like SiliconFlow's
    `{"code":30001,"message":"account balance is insufficient"}` — surfacing
    those lets the user diagnose without hunting through server logs."""
    if resp.is_success:
        return
    try:
        body = resp.json()
        # Common shapes: {"message":...}, {"error":{"message":...}},
        # {"code":..., "message":...}
        msg = (
            body.get("message")
            or (body.get("error") or {}).get("message")
            or str(body)[:300]
        )
    except Exception:  # noqa: BLE001
        msg = (resp.text or "")[:300]
    raise httpx.HTTPStatusError(
        f"HTTP {resp.status_code} from {resp.request.url}: {msg}",
        request=resp.request,
        response=resp,
    )


async def _embed_openai_compat(text: str, cfg: dict[str, Any]) -> list[float]:
    url = f"{cfg['base_url']}/embeddings"
    headers = {"Content-Type": "application/json"}
    if cfg["api_key"]:
        headers["Authorization"] = f"Bearer {cfg['api_key']}"
    client = _get_client()
    resp = await client.post(
        url,
        headers=headers,
        json={"input": text, "model": cfg["model"]},
    )
    _raise_with_upstream_detail(resp)
    return resp.json()["data"][0]["embedding"]


async def _embed_openai_batch(texts: list[str], cfg: dict[str, Any]) -> list[list[float]]:
    """OpenAI-compatible endpoints accept `input` as a list of strings."""
    url = f"{cfg['base_url']}/embeddings"
    headers = {"Content-Type": "application/json"}
    if cfg["api_key"]:
        headers["Authorization"] = f"Bearer {cfg['api_key']}"
    client = _get_client()
    resp = await client.post(
        url,
        headers=headers,
        json={"input": texts, "model": cfg["model"]},
        timeout=60.0,  # batch can be slower than single
    )
    _raise_with_upstream_detail(resp)
    data = resp.json()["data"]
    # Some providers (notably OpenAI) return entries with `index` field; sort to be safe.
    data_sorted = sorted(data, key=lambda d: d.get("index", 0))
    return [d["embedding"] for d in data_sorted]


async def _embed_ollama_native(text: str, cfg: dict[str, Any]) -> list[float]:
    url = f"{cfg['base_url']}/api/embeddings"
    client = _get_client()
    resp = await client.post(url, json={"model": cfg["model"], "prompt": text})
    resp.raise_for_status()
    return resp.json()["embedding"]


def _embed_hashmock(text: str) -> list[float]:
    """Deterministic fake embedding for offline tests. 1024-dim normalized."""
    h = hashlib.sha256(text.encode()).digest()
    vec = [float((b - 128) / 128.0) for b in h[:128]]
    vec = vec * 8  # 128 * 8 = 1024
    norm = math.sqrt(sum(x * x for x in vec))
    return [x / (norm + 1e-9) for x in vec]
