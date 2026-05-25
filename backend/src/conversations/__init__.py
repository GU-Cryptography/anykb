"""Conversation history persistence (v2-M3).

Replaces the v1 localStorage-based conversation store with SQLite-backed
storage so user history syncs across devices and browsers.

Schema lives in `models.py`, HTTP endpoints in `routes.py`. Chat endpoint
(`src/app.py`) stays stateless — the frontend orchestrates persistence by
POSTing user messages before chat and assistant messages on stream completion.
"""
from src.conversations.models import Conversation, Message  # noqa: F401
from src.conversations.routes import router  # noqa: F401
