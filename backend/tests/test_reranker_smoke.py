"""Smoke test for reranker abstraction (v3-M4).

Pure unit — no network, no DB. Mocks the module-level httpx client so we can
assert rerank() respects the Cohere response shape and preserves cosine
semantics in the calling-side contract (passthrough when cfg is None).

Also covers the resolve_user_reranker toggle gate: even with all five cols
populated, a False `reranker_enabled` must return None so the chat path
cleanly skips reranking.
"""
from __future__ import annotations

import pytest


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


class _FakeClient:
    """Minimal stand-in for httpx.AsyncClient — records the last call so a
    test can assert request shape if needed.
    """

    def __init__(self, payload: dict) -> None:
        self._payload = payload
        self.last_url: str | None = None
        self.last_headers: dict | None = None
        self.last_json: dict | None = None
        self.is_closed = False

    async def post(self, url, headers=None, json=None, **_kw):
        self.last_url = url
        self.last_headers = headers
        self.last_json = json
        return _FakeResponse(self._payload)

    async def aclose(self) -> None:
        self.is_closed = True


@pytest.mark.asyncio
async def test_rerank_reorders_by_relevance_score(monkeypatch):
    from src.infra import reranker
    from src.settings_user.models import UserRerankerConfig

    cfg = UserRerankerConfig(
        provider="siliconflow",
        base_url="https://api.siliconflow.cn/v1",
        api_key="fake",
        model="BAAI/bge-reranker-v2-m3",
    )

    # Mock SiliconFlow response — chunk index 2 wins, then 0, then 1.
    fake = _FakeClient(
        payload={
            "results": [
                {"index": 2, "relevance_score": 0.95},
                {"index": 0, "relevance_score": 0.42},
                {"index": 1, "relevance_score": 0.03},
            ]
        }
    )
    monkeypatch.setattr(reranker, "_get_client", lambda: fake)

    result = await reranker.rerank(
        "Redis Stream", ["RabbitMQ doc", "Kafka doc", "Redis Stream doc"], top_n=3, cfg=cfg
    )

    assert result == [(2, 0.95), (0, 0.42), (1, 0.03)]
    # Request shape sanity check — Cohere-compat format.
    assert fake.last_url == "https://api.siliconflow.cn/v1/rerank"
    assert fake.last_headers and fake.last_headers.get("Authorization") == "Bearer fake"
    assert fake.last_json == {
        "model": "BAAI/bge-reranker-v2-m3",
        "query": "Redis Stream",
        "documents": ["RabbitMQ doc", "Kafka doc", "Redis Stream doc"],
        "top_n": 3,
    }


@pytest.mark.asyncio
async def test_rerank_passthrough_when_cfg_none(monkeypatch):
    from src.infra import reranker

    # Even if a client exists, no call should be made.
    fake = _FakeClient(payload={"results": []})
    monkeypatch.setattr(reranker, "_get_client", lambda: fake)

    result = await reranker.rerank("q", ["a", "b", "c"], top_n=2, cfg=None)

    assert result == [(0, 0.0), (1, 0.0)]
    assert fake.last_url is None  # didn't hit network


@pytest.mark.asyncio
async def test_rerank_empty_documents_returns_empty(monkeypatch):
    from src.infra import reranker
    from src.settings_user.models import UserRerankerConfig

    cfg = UserRerankerConfig(
        provider="siliconflow",
        base_url="https://api.siliconflow.cn/v1",
        api_key="k",
        model="BAAI/bge-reranker-v2-m3",
    )
    fake = _FakeClient(payload={"results": []})
    monkeypatch.setattr(reranker, "_get_client", lambda: fake)

    result = await reranker.rerank("q", [], top_n=5, cfg=cfg)
    assert result == []
    assert fake.last_url is None  # short-circuit before HTTP


@pytest.mark.asyncio
async def test_rerank_filters_out_of_range_indices(monkeypatch):
    from src.infra import reranker
    from src.settings_user.models import UserRerankerConfig

    cfg = UserRerankerConfig(
        provider="siliconflow",
        base_url="https://api.siliconflow.cn/v1",
        api_key="k",
        model="BAAI/bge-reranker-v2-m3",
    )
    fake = _FakeClient(
        payload={
            "results": [
                {"index": 1, "relevance_score": 0.9},
                {"index": 99, "relevance_score": 0.5},  # bogus
                {"index": 0, "relevance_score": 0.1},
            ]
        }
    )
    monkeypatch.setattr(reranker, "_get_client", lambda: fake)

    result = await reranker.rerank("q", ["a", "b"], top_n=3, cfg=cfg)
    # idx=99 dropped; idx=0,1 kept in their server-returned order
    assert result == [(1, 0.9), (0, 0.1)]


def test_resolve_user_reranker_returns_none_when_toggle_off():
    from src.settings_user.models import resolve_user_reranker

    class FakeUser:
        reranker_enabled = False
        reranker_provider = "siliconflow"
        reranker_base_url = "https://x"
        reranker_api_key_enc = "enc"
        reranker_model = "BAAI/bge-reranker-v2-m3"

    # Toggle off → None even though all five cols are populated.
    assert resolve_user_reranker(FakeUser()) is None


def test_resolve_user_reranker_returns_none_when_unconfigured():
    from src.settings_user.models import resolve_user_reranker

    class FakeUser:
        reranker_enabled = True   # enabled but missing fields
        reranker_provider = None
        reranker_base_url = None
        reranker_api_key_enc = None
        reranker_model = None

    assert resolve_user_reranker(FakeUser()) is None


def test_resolve_user_reranker_returns_config_when_enabled_and_configured(monkeypatch):
    from src.settings_user import models as su_models

    # Bypass Fernet decryption (it requires a real settings.jwt_secret).
    monkeypatch.setattr(su_models, "decrypt", lambda token: f"decrypted:{token}")

    class FakeUser:
        reranker_enabled = True
        reranker_provider = "siliconflow"
        reranker_base_url = "https://api.siliconflow.cn/v1/"
        reranker_api_key_enc = "ENC"
        reranker_model = "BAAI/bge-reranker-v2-m3"

    cfg = su_models.resolve_user_reranker(FakeUser())
    assert cfg is not None
    assert cfg.provider == "siliconflow"
    assert cfg.base_url == "https://api.siliconflow.cn/v1"  # trailing / stripped
    assert cfg.api_key == "decrypted:ENC"
    assert cfg.model == "BAAI/bge-reranker-v2-m3"


def test_resolve_user_reranker_handles_empty_api_key(monkeypatch):
    """Self-hosted openai-compat endpoints may have no api_key. Resolver
    must not call decrypt on empty/None and must still return a config.
    """
    from src.settings_user import models as su_models

    # decrypt should NOT be called when api_key_enc is empty — fail loudly if it is
    monkeypatch.setattr(su_models, "decrypt", lambda token: (_ for _ in ()).throw(
        AssertionError("decrypt should not run for empty api_key_enc")
    ))

    class FakeUser:
        reranker_enabled = True
        reranker_provider = "openai-compat"
        reranker_base_url = "http://localhost:8080"
        reranker_api_key_enc = None
        reranker_model = "bge-reranker-v2-m3"

    cfg = su_models.resolve_user_reranker(FakeUser())
    assert cfg is not None
    assert cfg.api_key == ""
