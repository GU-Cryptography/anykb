"""User model.

Single table for v1. KB / Conversation tables will be added in M2-M3 (separate
files, all sharing `Base` from infra/database.py).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import Boolean, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from src.infra.database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    # v2-M1: per-user LLM / embedding self-config. All nullable — NULL means
    # "not configured by this user, fall back to env Settings".
    llm_provider: Mapped[Optional[str]] = mapped_column(String(32), nullable=True, default=None)
    llm_base_url: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, default=None)
    llm_api_key_enc: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True, default=None)
    llm_default_model: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, default=None)
    llm_complex_model: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, default=None)

    embedding_provider: Mapped[Optional[str]] = mapped_column(String(32), nullable=True, default=None)
    embedding_base_url: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, default=None)
    embedding_api_key_enc: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True, default=None)
    embedding_model: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, default=None)
    embedding_dim: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, default=None)

    # v3-M4: per-user cross-encoder reranker (opt-in, default off). When
    # `reranker_enabled` is True AND the four config cols are populated,
    # `resolve_user_reranker(user)` returns a populated UserRerankerConfig and
    # KBSearchTool will reorder top-N hits via the configured /rerank endpoint.
    reranker_provider: Mapped[Optional[str]] = mapped_column(String(32), nullable=True, default=None)
    reranker_base_url: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, default=None)
    reranker_api_key_enc: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True, default=None)
    reranker_model: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, default=None)
    reranker_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )

    # v2-M6: per-user KB options. Currently just web_search opt-in for KB mode.
    # Default False — keep KB answers strictly grounded in chunks unless user
    # explicitly opts in to web fallback. See docs/roadmap.md v2-M6 for rationale.
    kb_web_search_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )

    # 06-01 admin-dashboard: platform-level role + active flag.
    # - is_admin gates /api/admin/* (see auth.middleware.require_admin) and is
    #   seeded from settings.admin_emails on startup; togglable at runtime.
    # - is_active False = banned: current_user + login reject the user
    #   everywhere. Defaults keep every existing/new account a normal, active user.
    is_admin: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="1"
    )

    def to_public_dict(self) -> dict:
        return {
            "id": self.id,
            "email": self.email,
            "display_name": self.display_name or self.email.split("@")[0],
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "is_admin": self.is_admin,
            "is_active": self.is_active,
        }
