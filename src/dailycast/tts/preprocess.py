"""Deterministic spoken-text preparation kept separate from the stored Episode script."""

from __future__ import annotations

import html
import json
import re
import xml.etree.ElementTree as element_tree
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Literal

import yaml

from dailycast.core.hashes import sha256_text

TextMode = Literal["plain", "ssml"]
SectionRole = Literal["opening", "body", "closing"]

_HAN_DIGITS = "零一二三四五六七八九"
_HAN_NUMBERS = set(_HAN_DIGITS[1:])
_COMPOUND_WANYI = re.compile(r"(?<![A-Za-z0-9])(?P<number>\d+(?:\.\d+)?)万亿(?![A-Za-z0-9])")
_PERCENT = re.compile(r"(?<![A-Za-z0-9])(?P<number>\d+(?:\.\d+)?)%(?![A-Za-z0-9])")
_G_NETWORK = re.compile(r"(?<![A-Za-z0-9])(?P<number>\d+)G(?![A-Za-z0-9])")
_GPT_VERSION = re.compile(r"(?<![A-Za-z0-9])GPT-(?P<number>\d+)(?![A-Za-z0-9])")
_NUMBER = re.compile(r"(?<![A-Za-z0-9])(?P<number>\d+(?:\.\d+)?)(?![A-Za-z0-9])")
_PARAGRAPH_BREAK = re.compile(r"\n\s*\n+")


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


class TTSPreprocessor:
    """Normalize a bounded spoken form and optionally add conservative SSML pause markers."""

    def __init__(
        self, *, dictionary: PronunciationDictionary | None = None, text_mode: TextMode = "plain"
    ) -> None:
        if text_mode not in {"plain", "ssml"}:
            raise ValueError("text_mode must be plain or ssml")
        self._dictionary = dictionary or PronunciationDictionary.from_mapping({})
        self._text_mode = text_mode

    @property
    def semantic_hash(self) -> str:
        """Return the cache-visible identity of the non-secret preparation policy."""
        return sha256_text(
            json.dumps(
                {
                    "dictionary": self._dictionary.semantic_hash,
                    "implementation": "tts-preprocess-v1",
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
        spoken = _normalize_spoken_text(text)
        spoken = _apply_replacements(spoken, pronunciation_hints)
        spoken = _apply_replacements(spoken, self._dictionary.entries)
        provider_text = (
            _to_ssml(spoken, section_role=section_role) if self._text_mode == "ssml" else spoken
        )
        return PreparedSpeech(
            text=provider_text,
            text_mode=self._text_mode,
            semantic_hash=self.semantic_hash,
            spoken_character_count=len(spoken),
        )


def ssml_to_edge_text(text: str) -> str:
    """Lower DailyCast's small SSML subset before the public edge-tts SDK escapes raw text."""
    try:
        root = element_tree.fromstring(text)
    except element_tree.ParseError as error:
        raise ValueError("invalid TTS SSML") from error
    if _local_tag(root.tag) != "speak":
        raise ValueError("TTS SSML must use a speak root")
    parts: list[str] = []
    _append_node_text(root, parts)
    return "".join(parts).lstrip()


def _append_node_text(node: element_tree.Element, parts: list[str]) -> None:
    """Append permitted XML text recursively and turn explicit breaks into natural punctuation."""
    if node.text:
        parts.append(node.text)
    for child in node:
        tag = _local_tag(child.tag)
        if tag == "break":
            _append_pause(parts, _break_milliseconds(child.attrib.get("time")))
        elif tag in {"p", "prosody", "s"}:
            _append_node_text(child, parts)
        else:
            raise ValueError(f"unsupported TTS SSML tag: {tag}")
        if child.tail:
            parts.append(child.tail)


def _append_pause(parts: list[str], milliseconds: int) -> None:
    """Use punctuation/newlines because edge-tts.Communicate accepts text, not caller SSML."""
    if milliseconds < 250:
        parts.append("，")
        return
    current = "".join(parts).rstrip()
    if current.endswith(("。", "！", "？", "!", "?")):
        parts.append("\n\n")
    else:
        parts.append("。\n\n")


def _break_milliseconds(value: str | None) -> int:
    """Parse only fixed millisecond pauses emitted by this module."""
    if value is None or not value.endswith("ms"):
        raise ValueError("TTS SSML break time must use milliseconds")
    try:
        milliseconds = int(value[:-2])
    except ValueError as error:
        raise ValueError("TTS SSML break time is invalid") from error
    if milliseconds < 0 or milliseconds > 2_000:
        raise ValueError("TTS SSML break time is outside the allowed range")
    return milliseconds


def _local_tag(tag: str) -> str:
    """Strip a parser namespace marker without accepting a different tag vocabulary."""
    return tag.rsplit("}", 1)[-1]


def _to_ssml(text: str, *, section_role: SectionRole) -> str:
    """Add one pause per paragraph and section boundary without over-marking every sentence."""
    paragraphs = tuple(
        paragraph.strip() for paragraph in _PARAGRAPH_BREAK.split(text) if paragraph.strip()
    )
    pause_ms = 500 if section_role in {"opening", "closing"} else 350
    fragments = ['<speak version="1.0" xml:lang="zh-CN">']
    for index, paragraph in enumerate(paragraphs):
        fragments.append("<p>")
        fragments.append(html.escape(re.sub(r"\s*\n\s*", " ", paragraph)))
        if index < len(paragraphs) - 1 or section_role != "closing":
            fragments.append(f'<break time="{pause_ms}ms"/>')
        fragments.append("</p>")
    fragments.append("</speak>")
    return "".join(fragments)


def _normalize_spoken_text(text: str) -> str:
    """Convert only bounded common numerical forms; source script storage is never changed."""
    normalized = text.strip()
    normalized = _COMPOUND_WANYI.sub(_replace_wanyi, normalized)
    normalized = _PERCENT.sub(
        lambda match: f"百分之{_spoken_decimal(match.group('number'))}", normalized
    )
    normalized = _GPT_VERSION.sub(
        lambda match: f"GPT {_spoken_decimal(match.group('number'))}", normalized
    )
    normalized = _G_NETWORK.sub(
        lambda match: f"{_spoken_decimal(match.group('number'))}G", normalized
    )
    return _NUMBER.sub(lambda match: _spoken_decimal(match.group("number")), normalized)


def _replace_wanyi(match: re.Match[str]) -> str:
    """Render 1.65万亿 as 一万六千五百亿 instead of making a model spell punctuation."""
    try:
        scaled = Decimal(match.group("number")) * Decimal(10_000)
    except InvalidOperation:
        return match.group(0)
    if scaled != scaled.to_integral_value():
        return f"{_spoken_decimal(match.group('number'))}万亿"
    return f"{_spoken_integer(int(scaled))}亿"


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
                suffix = rf"(?![A-Za-z0-9]|\s*[{''.join(_HAN_NUMBERS)}])"
            rendered = re.sub(rf"(?<![A-Za-z0-9]){re.escape(term)}{suffix}", replacement, rendered)
        else:
            rendered = rendered.replace(term, replacement)
    return rendered
