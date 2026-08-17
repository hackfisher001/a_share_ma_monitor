"""DeepSeek client tests without network."""

from src.llm import _clean_answer, deepseek_enabled


def test_deepseek_disabled_without_key(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    assert deepseek_enabled() is False


def test_deepseek_enabled_with_key(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    assert deepseek_enabled() is True


def test_clean_answer_strips_meta_tail():
    raw = (
        "一月分化明显，比特币回撤较深。\n"
        "免责声明：不构成投资建议。\n"
        "这个约350字，符合。可以更详细一点但要保持500字内。"
        "需要确认“不编造基本面”。"
    )
    cleaned = _clean_answer(raw)
    assert "免责声明" in cleaned
    assert "这个约350字" not in cleaned
    assert "需要确认" not in cleaned
