"""BYOK (bring-your-own-key) enforcement helpers (v2-M2).

When `settings.byok_required = True`, chat / KB-create / KB-ingest paths must
verify the calling user has configured their own LLM / embedding via
`/api/settings/*`. Otherwise the request is rejected with HTTP 422 and a
structured detail telling the UI where to send the user.

Default `byok_required = False` keeps env fallback for dev / first-run.
Flip to True in production so the project owner's API keys aren't shared with
every registered user.
"""
from __future__ import annotations

from fastapi import HTTPException, status

from src.auth.models import User
from src.settings import get_settings

from .models import resolve_user_embedding, resolve_user_llm


def _gate(code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail={
            "code": code,
            "message": message,
            "settings_url": "/settings",
        },
    )


def require_user_llm(user: User) -> None:
    """Raise 422 if BYOK is on and the user has no LLM cfg."""
    if not get_settings().byok_required:
        return
    if resolve_user_llm(user) is None:
        raise _gate(
            "llm_not_configured",
            "请先到「设置」配置你的 LLM 提供商（base_url + api_key + 默认模型）才能开始对话。",
        )


def require_user_embedding(user: User) -> None:
    """Raise 422 if BYOK is on and the user has no embedding cfg.

    Used for create_kb / upload_document / KB-mode chat (where query embedding
    runs against the user's chosen provider).
    """
    if not get_settings().byok_required:
        return
    if resolve_user_embedding(user) is None:
        raise _gate(
            "embedding_not_configured",
            "请先到「设置」配置你的 Embedding 提供商（base_url + api_key + 模型 + 维度）才能创建 KB / 上传文档 / 在 KB 中提问。",
        )
