"""Provider probe adapters (v2-M1).

Three external API shapes:
  - anthropic: GET /v1/models with `x-api-key` + `anthropic-version` headers
  - openai-compat: GET {base_url}/models with `Authorization: Bearer`
  - ollama (native): GET {base_url}/api/tags (no auth)

Each `probe_*` function returns a list of model id strings or raises ProbeError
with a user-facing message. The HTTP layer surfaces specific failure modes
(DNS/connection/auth/4xx) so the UI can show actionable toasts.
"""
from __future__ import annotations

from dataclasses import dataclass

import httpx

from src.infra.embedding import MODEL_DIMS

ANTHROPIC_VERSION = "2023-06-01"
_TIMEOUT = httpx.Timeout(15.0, connect=8.0)


class ProbeError(Exception):
    """Raised when a provider probe fails. message is safe to show users."""


@dataclass
class EmbeddingProbeResult:
    models: list[str]
    dim: int | None  # only set when caller passed a model AND we live-probed


def _humanize_http_error(provider: str, exc: Exception) -> str:
    if isinstance(exc, httpx.ConnectError):
        return f"{provider}: 无法连接到 {exc.request.url.host} —— 检查 URL 或网络可达性"
    if isinstance(exc, httpx.TimeoutException):
        return f"{provider}: 连接超时 —— 服务可能不可达"
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code in (401, 403):
            return f"{provider}: 认证失败 ({code}) —— API key 不正确"
        if code == 404:
            return f"{provider}: 端点不存在 (404) —— base_url 路径可能错了"
        return f"{provider}: HTTP {code} —— {exc.response.text[:200]}"
    return f"{provider}: {type(exc).__name__}: {exc}"


# ---------------------------------------------------------------------------
# LLM probes
# ---------------------------------------------------------------------------
async def probe_llm_models(provider: str, base_url: str, api_key: str) -> list[str]:
    provider = (provider or "").lower()
    base_url = (base_url or "").rstrip("/")
    if not base_url:
        raise ProbeError(f"{provider}: base_url 不能为空")
    if not api_key:
        raise ProbeError(f"{provider}: api_key 不能为空")

    if provider == "anthropic":
        return await _probe_anthropic_llm(base_url, api_key)
    if provider == "openai-compat":
        return await _probe_openai_compat_models(base_url, api_key)
    raise ProbeError(f"不支持的 LLM provider: {provider}")


async def _probe_anthropic_llm(base_url: str, api_key: str) -> list[str]:
    url = f"{base_url}/v1/models"
    headers = {
        "x-api-key": api_key,
        "anthropic-version": ANTHROPIC_VERSION,
        "content-type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.get(url, headers=headers)
            r.raise_for_status()
            data = r.json().get("data", [])
            ids = [m.get("id") for m in data if m.get("id")]
            ids.sort()
            return ids
    except Exception as e:
        raise ProbeError(_humanize_http_error("anthropic", e)) from e


async def _probe_openai_compat_models(base_url: str, api_key: str) -> list[str]:
    # Many providers expose /v1/models; some lift the /v1 prefix into base_url already.
    candidates = [f"{base_url}/models", f"{base_url}/v1/models"]
    headers = {"Authorization": f"Bearer {api_key}"}
    last_err: Exception | None = None
    for url in candidates:
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
                r = await c.get(url, headers=headers)
                if r.status_code == 404:
                    last_err = httpx.HTTPStatusError("404", request=r.request, response=r)
                    continue
                r.raise_for_status()
                payload = r.json()
                data = payload.get("data") if isinstance(payload, dict) else payload
                if not isinstance(data, list):
                    raise ProbeError(f"openai-compat: 响应格式异常 —— 期望 data 数组")
                ids = [m.get("id") for m in data if isinstance(m, dict) and m.get("id")]
                ids.sort()
                return ids
        except ProbeError:
            raise
        except Exception as e:
            last_err = e
            continue
    raise ProbeError(_humanize_http_error("openai-compat", last_err or RuntimeError("unknown")))


# ---------------------------------------------------------------------------
# Embedding probes
# ---------------------------------------------------------------------------
async def probe_embedding(
    provider: str,
    base_url: str,
    api_key: str,
    model: str | None = None,
) -> EmbeddingProbeResult:
    """List models for the embedding provider; if `model` given, live-probe its dim.

    For openai-compat, the /models list often mixes chat + embedding models — caller
    can't tell which is which. We return everything; UI lets the user pick.
    """
    provider = (provider or "").lower()
    base_url = (base_url or "").rstrip("/")
    if not base_url:
        raise ProbeError(f"{provider}: base_url 不能为空")

    if provider == "openai-compat":
        if not api_key:
            raise ProbeError("openai-compat: api_key 不能为空")
        models = await _probe_openai_compat_models(base_url, api_key)
    elif provider == "ollama":
        models = await _probe_ollama_tags(base_url)
    else:
        raise ProbeError(f"不支持的 embedding provider: {provider}")

    dim: int | None = None
    if model:
        # Prefer table lookup; fall back to a 1-call live probe.
        if model in MODEL_DIMS:
            dim = MODEL_DIMS[model]
        else:
            dim = await _live_probe_dim(provider, base_url, api_key, model)

    return EmbeddingProbeResult(models=models, dim=dim)


async def _probe_ollama_tags(base_url: str) -> list[str]:
    url = f"{base_url}/api/tags"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.get(url)
            r.raise_for_status()
            tags = r.json().get("models", [])
            names = [t.get("name") for t in tags if t.get("name")]
            names.sort()
            return names
    except Exception as e:
        raise ProbeError(_humanize_http_error("ollama", e)) from e


async def _live_probe_dim(
    provider: str,
    base_url: str,
    api_key: str,
    model: str,
) -> int:
    """Embed a tiny string to discover the actual vector dimension."""
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            if provider == "ollama":
                r = await c.post(
                    f"{base_url}/api/embeddings",
                    json={"model": model, "prompt": "ok"},
                )
                r.raise_for_status()
                vec = r.json().get("embedding", [])
            else:
                # openai-compat — try both /embeddings and /v1/embeddings
                headers = {"Authorization": f"Bearer {api_key}"}
                last_err: Exception | None = None
                vec = None
                for url in (f"{base_url}/embeddings", f"{base_url}/v1/embeddings"):
                    try:
                        r = await c.post(url, headers=headers, json={"input": "ok", "model": model})
                        if r.status_code == 404:
                            last_err = httpx.HTTPStatusError("404", request=r.request, response=r)
                            continue
                        r.raise_for_status()
                        vec = r.json()["data"][0]["embedding"]
                        break
                    except Exception as e:
                        last_err = e
                if vec is None:
                    raise last_err or RuntimeError("embedding probe failed")
            if not isinstance(vec, list) or not vec:
                raise ProbeError(f"{provider}: embedding 响应缺少 vector —— 模型不可用?")
            return len(vec)
    except ProbeError:
        raise
    except Exception as e:
        raise ProbeError(_humanize_http_error(provider, e)) from e
