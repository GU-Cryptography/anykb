"""Tests for v3-M3 keyword triggers (conversations/keyword_extractor.py).

Pure-unit: no DB / Redis / LLM. Asserts the PATTERNS hit matrix — five positive
categories plus negatives (ordinary questions must NOT trigger) — and that
``task_boundary`` is detected but never counted as storable.
"""
from __future__ import annotations

import pytest

from src.conversations.keyword_extractor import (
    STORABLE_CATEGORIES,
    detect,
    storable_categories,
)


@pytest.mark.parametrize(
    "text,expected",
    [
        # --- five positive categories ---
        ("我是后端工程师", "profile"),
        ("我从事算法相关工作", "profile"),
        ("我平时用 Python 和 Go", "preference"),
        ("我喜欢用 PostgreSQL", "preference"),
        ("我们公司用 K8s 部署服务", "fact"),
        ("我们团队基于 FastAPI 开发", "fact"),
        ("帮我记住我的生日是 6 月", "explicit"),
        ("请记住这个配置", "explicit"),
        # --- task boundary (detected, non-storable) ---
        ("好了，下一步", "task_boundary"),
        ("这个搞定了", "task_boundary"),
    ],
)
def test_pattern_positive_hits(text, expected):
    assert expected in detect(text)


@pytest.mark.parametrize(
    "text",
    [
        "今天天气怎么样？",
        "帮我写一个快速排序",
        "1 + 1 等于几",
        "解释一下什么是闭包",
        "",
        "   ",
    ],
)
def test_pattern_negatives_do_not_trigger(text):
    assert detect(text) == []
    assert storable_categories(text) == []


def test_task_boundary_is_detected_but_not_storable():
    text = "好了，下一步我们看看部署"
    assert "task_boundary" in detect(text)
    # task_boundary must never drive an extraction (PRD §5.4 "不存记忆").
    assert "task_boundary" not in storable_categories(text)
    assert "task_boundary" not in STORABLE_CATEGORIES


def test_multiple_categories_in_one_message():
    text = "我是工程师，我喜欢 Rust"
    hits = detect(text)
    assert "profile" in hits and "preference" in hits
    # storable keeps both (order follows PATTERNS declaration).
    assert storable_categories(text) == [c for c in hits if c in STORABLE_CATEGORIES]


def test_detect_dedupes_categories():
    # Two preference cues in one message → the category appears once.
    hits = detect("我喜欢 Python，我也偏好类型注解")
    assert hits.count("preference") == 1
