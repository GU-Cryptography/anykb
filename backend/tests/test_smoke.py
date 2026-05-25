"""Smoke tests — non-network."""
import pytest

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
    reg = build_default_registry()
    ok, _ = is_tool_allowed("get_weather", reg.names())
    assert ok


def test_tool_guard_blocks_unknown():
    ok, reason = is_tool_allowed("execute_shell", ["get_weather"])
    assert not ok
    assert "危险工具" in (reason or "")


def test_registry_has_three_tools():
    reg = build_default_registry()
    names = reg.names()
    assert "get_weather" in names
    assert "search_restaurant_kb" in names
    assert "amap_search" in names
