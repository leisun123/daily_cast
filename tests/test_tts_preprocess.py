"""Spoken-text preparation stays deterministic and separate from the stored script."""

from __future__ import annotations

from dailycast.tts.preprocess import PronunciationDictionary, TTSPreprocessor, ssml_to_edge_text


def test_preprocessor_normalizes_spoken_numbers_and_configured_terms() -> None:
    """Large Chinese quantities and common AI abbreviations become natural speech input."""
    dictionary = PronunciationDictionary.from_mapping(
        {
            "GPT": {"replacement": "GPT 五"},
            "AI": {"replacement": "人工智能"},
            "LLM": {"replacement": "大语言模型"},
        }
    )
    original = "GPT-5 支持 5G，规模达到1.65万亿；AI 和 LLM 都受影响。"

    prepared = TTSPreprocessor(dictionary=dictionary, text_mode="plain").prepare(
        original, section_role="body"
    )

    assert original == "GPT-5 支持 5G，规模达到1.65万亿；AI 和 LLM 都受影响。"
    assert (
        prepared.text
        == "GPT 五 支持 五G，规模达到一万六千五百亿；人工智能 和 大语言模型 都受影响。"
    )
    assert prepared.text_mode == "plain"


def test_ssml_preparation_uses_escaped_paragraph_pauses_without_modifying_script() -> None:
    """SSML is generated only for the provider input and adds pauses at natural boundaries."""
    original = "大家好，欢迎收听DailyCast。\n\n今天有三个重要消息。"

    prepared = TTSPreprocessor(text_mode="ssml").prepare(original, section_role="opening")

    assert original == "大家好，欢迎收听DailyCast。\n\n今天有三个重要消息。"
    assert prepared.text_mode == "ssml"
    assert prepared.text.startswith('<speak version="1.0" xml:lang="zh-CN">')
    assert '<break time="500ms"/>' in prepared.text
    assert (
        ssml_to_edge_text(prepared.text)
        == "大家好，欢迎收听DailyCast。\n\n今天有三个重要消息。\n\n"
    )


def test_pronunciation_dictionary_identity_changes_when_replacements_change() -> None:
    """A pronunciation-policy change must be visible to the semantic TTS cache identity."""
    first = PronunciationDictionary.from_mapping({"AI": {"replacement": "人工智能"}})
    changed = PronunciationDictionary.from_mapping({"AI": {"replacement": "A I"}})

    assert first.semantic_hash != changed.semantic_hash
