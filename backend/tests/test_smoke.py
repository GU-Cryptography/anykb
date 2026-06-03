"""Smoke tests — non-network."""

from src.safety.input_filter import sanitize_user_input
from src.safety.output_filter import redact_pii
from src.safety.tool_guard import is_tool_allowed
from src.tools.base import build_default_registry


def test_input_sanitize_passes_normal():
    text, blocked = sanitize_user_input("5月12号上海酸菜鱼")
    assert text == "5月12号上海酸菜鱼"
    assert blocked is None


def test_input_sanitize_blocks_dangerous():
    _, blocked = sanitize_user_input("rm -rf / && eat food")
    assert blocked is not None


def test_output_redact_phone():
    out = redact_pii("contact: 13800138000 thanks")
    assert "13800138000" not in out
    assert "已隐藏" in out


def test_tool_guard_allows_registered():
    # General chat mode (kb=None) registers only web_search since v2-M5; a
    # registered tool must pass the guard.
    reg = build_default_registry()
    ok, _ = is_tool_allowed("web_search", reg.names())
    assert ok


def test_tool_guard_blocks_unknown():
    ok, reason = is_tool_allowed("execute_shell", ["get_weather"])
    assert not ok
    assert "危险工具" in (reason or "")


def test_default_registry_is_web_search_only():
    # kb=None → general chat mode mounts only web_search (v2-M5). The travel
    # tools moved behind explicit selection of the built-in demo KB.
    assert build_default_registry().names() == ["web_search"]


def test_travel_kb_registry_has_travel_tools():
    # Selecting the built-in travel demo KB restores the v1 tool kit
    # (generate_travel_report is a skill wired in graph.py, not the registry).
    from types import SimpleNamespace

    from src.kb.models import SYSTEM_TRAVEL_KB_ID

    names = build_default_registry(SimpleNamespace(id=SYSTEM_TRAVEL_KB_ID)).names()
    assert "get_weather" in names
    assert "search_restaurant_kb" in names
    assert "amap_search" in names
