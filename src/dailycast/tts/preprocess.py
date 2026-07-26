"""Deterministic spoken-text preparation kept separate from the stored Episode script."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Literal

import yaml

from dailycast.core.hashes import sha256_text

TextMode = Literal["plain", "enhanced_text"]
SectionRole = Literal["opening", "body", "closing"]

_HAN_DIGITS = "零一二三四五六七八九"
_HAN_NUMBERS = set(_HAN_DIGITS[1:])
_FINANCIAL_QUANTITY = re.compile(
    r"(?<![A-Za-z0-9])(?P<number>\d+(?:\.\d+)?)(?P<unit>万亿|亿|万)(?P<currency>美元|人民币)?(?![A-Za-z0-9])"
)
_PERCENT = re.compile(r"(?<![A-Za-z0-9])(?P<number>\d+(?:\.\d+)?)%(?![A-Za-z0-9])")
_PARAGRAPH_BREAK = re.compile(r"\n\s*\n+")
_INVISIBLE_SPOKEN_CHARACTERS = re.compile(r"[\u200B\u200C\u200D\u2060\uFEFF]")
_HAN_CHARACTER = r"[\u3400-\u4DBF\u4E00-\u9FFF\uF900-\uFAFF]"
_CHINESE_INTERNAL_WHITESPACE = re.compile(
    rf"(?<={_HAN_CHARACTER})(?:[^\S\r\n]+|[^\S\r\n]*(?:\r\n|[\r\n])[^\S\r\n]*)(?={_HAN_CHARACTER})"
)


@dataclass(frozen=True, slots=True)
class PronunciationDictionary:
    """A versionable, non-secret pronunciation policy loaded from YAML."""

    entries: tuple[tuple[str, str], ...]
    semantic_hash: str

    @classmethod
    def from_yaml(cls, path: Path) -> PronunciationDictionary:
        """Load the small human-maintained dictionary without accepting executable YAML."""
        try:
            loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        except OSError as error:
            raise ValueError(f"pronunciation dictionary is unreadable: {path}") from error
        except yaml.YAMLError as error:
            raise ValueError(f"pronunciation dictionary YAML is invalid: {path}") from error
        if loaded is None:
            loaded = {}
        if not isinstance(loaded, Mapping):
            raise ValueError("pronunciation dictionary root must be a mapping")
        return cls.from_mapping(loaded)

    @classmethod
    def from_mapping(cls, mapping: Mapping[object, object]) -> PronunciationDictionary:
        """Validate replacements so configuration changes invalidate audio cache reuse."""
        entries: list[tuple[str, str]] = []
        for raw_term, raw_value in mapping.items():
            if not isinstance(raw_term, str) or not raw_term.strip():
                raise ValueError("pronunciation dictionary term must be a non-empty string")
            if not isinstance(raw_value, Mapping):
                raise ValueError("pronunciation dictionary entry must be a mapping")
            replacement = raw_value.get("replacement")
            if not isinstance(replacement, str) or not replacement.strip():
                raise ValueError("pronunciation dictionary replacement must be a non-empty string")
            entries.append((raw_term.strip(), replacement.strip()))
        ordered = tuple(sorted(entries, key=lambda item: (-len(item[0]), item[0])))
        canonical = json.dumps(
            [{"replacement": replacement, "term": term} for term, replacement in ordered],
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return cls(entries=ordered, semantic_hash=sha256_text(canonical))


@dataclass(frozen=True, slots=True)
class PreparedSpeech:
    """One provider-only representation; the original script remains untouched."""

    text: str
    text_mode: TextMode
    semantic_hash: str
    spoken_character_count: int


class FinancialNumberNormalizer:
    """Render only bounded money and percentage expressions for spoken Chinese delivery."""

    def normalize(self, text: str) -> str:
        """Leave technical identifiers untouched: global bare-number replacement is forbidden."""
        normalized = _FINANCIAL_QUANTITY.sub(_replace_financial_quantity, text)
        return _PERCENT.sub(
            lambda match: f"百分之{_spoken_decimal(match.group('number'))}", normalized
        )


def normalize_spoken_whitespace(text: str) -> str:
    """Remove invisible marks and whitespace that splits adjacent Chinese characters."""
    without_invisible_characters = _INVISIBLE_SPOKEN_CHARACTERS.sub("", text)
    return _CHINESE_INTERNAL_WHITESPACE.sub("", without_invisible_characters)


class TTSPreprocessor:
    """Normalize a bounded spoken form and optionally add conservative plain-text pauses."""

    def __init__(
        self, *, dictionary: PronunciationDictionary | None = None, text_mode: TextMode = "plain"
    ) -> None:
        if text_mode not in {"plain", "enhanced_text"}:
            raise ValueError("text_mode must be plain or enhanced_text")
        self._dictionary = dictionary or PronunciationDictionary.from_mapping({})
        self._text_mode = text_mode

    @property
    def semantic_hash(self) -> str:
        """Return the cache-visible identity of the non-secret preparation policy."""
        return sha256_text(
            json.dumps(
                {
                    "dictionary": self._dictionary.semantic_hash,
                    "implementation": "tts-preprocess-v3-financial-enhanced-text",
                    "text_mode": self._text_mode,
                },
                separators=(",", ":"),
                sort_keys=True,
            )
        )

    def prepare(
        self,
        text: str,
        *,
        section_role: SectionRole,
        pronunciation_hints: Sequence[tuple[str, str]] = (),
    ) -> PreparedSpeech:
        """Return provider input while preserving the persisted Episode script text verbatim."""
        spoken = normalize_spoken_whitespace(text.strip())
        spoken = FinancialNumberNormalizer().normalize(spoken)
        spoken = _apply_replacements(spoken, pronunciation_hints)
        spoken = _apply_replacements(spoken, self._dictionary.entries)
        spoken = normalize_spoken_whitespace(spoken)
        provider_text = (
            _to_enhanced_text(spoken, section_role=section_role)
            if self._text_mode == "enhanced_text"
            else spoken
        )
        return PreparedSpeech(
            text=provider_text,
            text_mode=self._text_mode,
            semantic_hash=self.semantic_hash,
            spoken_character_count=len(spoken),
        )


def _to_enhanced_text(text: str, *, section_role: SectionRole) -> str:
    """Add one conservative paragraph pause without pretending the public SDK accepts SSML."""
    paragraphs = tuple(
        paragraph.strip() for paragraph in _PARAGRAPH_BREAK.split(text) if paragraph.strip()
    )
    pause_ms = 500 if section_role in {"opening", "closing"} else 350
    fragments: list[str] = []
    for index, paragraph in enumerate(paragraphs):
        fragments.append(re.sub(r"\s*\n\s*", " ", paragraph))
        if index < len(paragraphs) - 1 or section_role != "closing":
            fragments.append("\n\n" if pause_ms >= 250 else "，")
    return "".join(fragments)


def _replace_financial_quantity(match: re.Match[str]) -> str:
    """Render supported monetary units while preserving the explicit currency suffix."""
    unit = match.group("unit")
    currency = match.group("currency") or ""
    if unit != "万亿":
        return f"{_spoken_decimal(match.group('number'))}{unit}{currency}"
    try:
        scaled = Decimal(match.group("number")) * Decimal(10_000)
    except InvalidOperation:
        return match.group(0)
    if scaled != scaled.to_integral_value():
        return f"{_spoken_decimal(match.group('number'))}万亿{currency}"
    return f"{_spoken_integer(int(scaled))}亿{currency}"


def _spoken_decimal(value: str) -> str:
    """Convert decimal digits to a compact natural Chinese reading."""
    integer, separator, fraction = value.partition(".")
    rendered = _spoken_integer(int(integer))
    if not separator:
        return rendered
    return f"{rendered}点{''.join(_HAN_DIGITS[int(digit)] for digit in fraction)}"


def _spoken_integer(value: int) -> str:
    """Render non-negative integers with units sufficient for common news figures."""
    if value == 0:
        return _HAN_DIGITS[0]
    if value < 0:
        return f"负{_spoken_integer(-value)}"
    if value >= 10_000:
        high, low = divmod(value, 10_000)
        suffix = "" if low == 0 else (_HAN_DIGITS[0] if low < 1_000 else "") + _spoken_integer(low)
        return f"{_spoken_integer(high)}万{suffix}"
    pieces: list[str] = []
    pending_zero = False
    for divisor, unit in ((1_000, "千"), (100, "百"), (10, "十"), (1, "")):
        digit, value = divmod(value, divisor)
        if digit:
            if pending_zero:
                pieces.append(_HAN_DIGITS[0])
                pending_zero = False
            if not (divisor == 10 and digit == 1 and not pieces):
                pieces.append(_HAN_DIGITS[digit])
            pieces.append(unit)
        elif pieces and value:
            pending_zero = True
    return "".join(pieces)


def _apply_replacements(text: str, replacements: Sequence[tuple[str, str]]) -> str:
    """Apply longer terms first while avoiding accidental substitutions inside English words."""
    rendered = text
    for term, replacement in sorted(replacements, key=lambda item: (-len(item[0]), item[0])):
        if re.fullmatch(r"[A-Za-z0-9]+", term):
            suffix = r"(?![A-Za-z0-9])"
            if term == "GPT":
                suffix = rf"(?![A-Za-z0-9]|-\d|\s*[{''.join(_HAN_NUMBERS)}])"
            rendered = re.sub(rf"(?<![A-Za-z0-9]){re.escape(term)}{suffix}", replacement, rendered)
        else:
            rendered = rendered.replace(term, replacement)
    return rendered
