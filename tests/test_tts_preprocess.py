"""Spoken-text preparation stays deterministic and separate from the stored script."""

from __future__ import annotations

from pathlib import Path

import pytest

from dailycast.tts import preprocess
from dailycast.tts.preprocess import PronunciationDictionary, TTSPreprocessor


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("日\n常运维", "日常运维"),
        ("日 常运维", "日常运维"),
        ("日\t常运维", "日常运维"),
        ("日\u200b常运维", "日常运维"),
        ("日\u200c常运维", "日常运维"),
        ("日\u200d常运维", "日常运维"),
        ("日\u2060常运维", "日常运维"),
        ("日\ufeff常运维", "日常运维"),
        ("GPT 5", "GPT 5"),
        ("Python 3.12", "Python 3.12"),
        ("第一段。\n\n第二段。", "第一段。\n\n第二段。"),
    ],
)
def test_normalize_spoken_whitespace_removes_chinese_internal_boundaries_only(
    source: str, expected: str
) -> None:
    """Chinese word internals cannot carry provider-visible whitespace or zero-width marks."""
    assert preprocess.normalize_spoken_whitespace(source) == expected


def test_preprocessor_re_normalizes_pronunciation_replacement_text() -> None:
    """Hints and dictionary replacements cannot reintroduce Chinese-internal breaks."""
    dictionary = PronunciationDictionary.from_mapping({"术语甲": {"replacement": "日\u200b常运维"}})

    prepared = TTSPreprocessor(dictionary=dictionary, text_mode="enhanced_text").prepare(
        "术语甲，术语乙",
        section_role="body",
        pronunciation_hints=(("术语乙", "日\n常运维"),),
    )

    assert prepared.text == "日常运维，日常运维\n\n"


def test_preprocessor_normalizes_financial_numbers_without_damaging_technical_identifiers() -> None:
    """TTS only rewrites bounded financial readings, never model or product identifiers."""
    dictionary = PronunciationDictionary.from_mapping(
        {
            "GPT": {"replacement": "GPT 五"},
            "AI": {"replacement": "人工智能"},
            "LLM": {"replacement": "大语言模型"},
        }
    )
    original = (
        "GPT-5 支持 5G，iPhone16 搭配 RTX4090，Python 3.12 可用；"
        "规模达到1.65万亿；AI 和 LLM 都受影响。"
    )

    prepared = TTSPreprocessor(dictionary=dictionary, text_mode="plain").prepare(
        original, section_role="body"
    )

    assert (
        original == "GPT-5 支持 5G，iPhone16 搭配 RTX4090，Python 3.12 可用；"
        "规模达到1.65万亿；AI 和 LLM 都受影响。"
    )
    assert (
        prepared.text == "GPT-5 支持 5G，iPhone16 搭配 RTX4090，Python 3.12 可用；"
        "规模达到一万六千五百亿；人工智能和大语言模型都受影响。"
    )
    assert prepared.text_mode == "plain"


def test_financial_number_normalizer_converts_only_supported_money_and_percent_forms() -> None:
    """Financial quantities receive a natural spoken form without globally rewriting digits."""
    normalizer = preprocess.FinancialNumberNormalizer()

    assert (
        normalizer.normalize(
            "OpenAI发布GPT-5，融资1.65万亿美元，成本下降30%，另获5亿美元和2万人民币。"
        )
        == "OpenAI发布GPT-5，融资一万六千五百亿美元，成本下降百分之三十，另获五亿美元和二万人民币。"
    )


def test_enhanced_text_preparation_adds_plain_pause_boundaries_without_modifying_script() -> None:
    """The public Edge SDK receives enhanced plain text, never misleading custom SSML."""
    original = "大家好，欢迎收听DailyCast。\n\n今天有三个重要消息。"

    prepared = TTSPreprocessor(text_mode="enhanced_text").prepare(original, section_role="opening")

    assert original == "大家好，欢迎收听DailyCast。\n\n今天有三个重要消息。"
    assert prepared.text_mode == "enhanced_text"
    assert prepared.text == "大家好，欢迎收听DailyCast。\n\n今天有三个重要消息。\n\n"
    assert "<speak" not in prepared.text


def test_preprocessor_rejects_misleading_ssml_text_mode() -> None:
    """A mode name must not promise native SSML when the chosen SDK cannot accept it."""
    with pytest.raises(ValueError, match="enhanced_text"):
        TTSPreprocessor(text_mode="ssml")  # type: ignore[arg-type]


def test_pronunciation_dictionary_identity_changes_when_replacements_change() -> None:
    """A pronunciation-policy change must be visible to the semantic TTS cache identity."""
    first = PronunciationDictionary.from_mapping({"AI": {"replacement": "人工智能"}})
    changed = PronunciationDictionary.from_mapping({"AI": {"replacement": "A I"}})

    assert first.semantic_hash != changed.semantic_hash


def test_shipped_dictionary_handles_product_names_and_compound_terms() -> None:
    """Production pronunciation policy keeps common names and Chinese compounds speakable."""
    dictionary_path = Path(__file__).resolve().parents[1] / "config" / "pronunciation.yaml"
    dictionary = PronunciationDictionary.from_yaml(dictionary_path)

    prepared = TTSPreprocessor(dictionary=dictionary, text_mode="plain").prepare(
        "claude opus 改进了日常运维。", section_role="body"
    )

    assert prepared.text == "克劳德，欧普斯改进了日常的运维。"
