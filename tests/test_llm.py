"""DeepSeek client smoke tests (no network by default)."""

import os

from src.llm import deepseek_enabled


def test_deepseek_disabled_without_key(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    assert deepseek_enabled() is False


def test_deepseek_enabled_with_key(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    assert deepseek_enabled() is True
