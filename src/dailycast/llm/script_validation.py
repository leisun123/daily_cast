"""Deterministic local validation for structured scripts before and after LLM semantic review."""

from __future__ import annotations

import re
from collections.abc import Sequence

from dailycast.llm.outline_schemas import EpisodeOutline, EvidenceDossier
from dailycast.llm.script_schemas import EpisodeScript, ValidationIssue, ValidationReport

_HTML_OR_SSML_PATTERN = re.compile(r"</?[A-Za-z][^>]*>")
_URL_PATTERN = re.compile(r"https?://\S+", re.IGNORECASE)
_SECRET_OR_TASK_METADATA_PATTERN = re.compile(
    r"\b(?:api[_-]?key|authorization|bearer\s+\S+|task[_-]?(?:run|step)[_-]?id)\b",
    re.IGNORECASE,
)
_NUMBER_PATTERN = re.compile(r"(?<![A-Za-z0-9])\d+(?:[.,]\d+)?%?")
_SENTENCE_SPLIT_PATTERN = re.compile(r"[。！？!?]+")
_STRUCTURAL_CODES = frozenset(
    {
        "UNKNOWN_EVENT_REFERENCE",
        "UNKNOWN_ARTICLE_REFERENCE",
        "MISSING_SECTION",
    }
)


class ScriptValidator:
    """Check traceability, plain-text safety, character budgets, and estimated duration."""

    def __init__(
        self,
        *,
        estimated_chars_per_second: float,
        duration_tolerance_ratio: float,
        max_script_chars: int,
        max_section_chars: int,
    ) -> None:
        if (
            estimated_chars_per_second <= 0
            or not 0 <= duration_tolerance_ratio < 1
            or max_script_chars < 1
            or max_section_chars < 1
        ):
            msg = "script validation configuration is invalid"
            raise ValueError(msg)
        self._estimated_chars_per_second = estimated_chars_per_second
        self._duration_tolerance_ratio = duration_tolerance_ratio
        self._max_script_chars = max_script_chars
        self._max_section_chars = max_section_chars

    def validate(
        self,
        script: EpisodeScript,
        outline: EpisodeOutline,
        evidence_dossiers: Sequence[EvidenceDossier],
    ) -> ValidationReport:
        """Return stable local issues without deciding whether semantic review should revise."""
        dossiers = tuple(EvidenceDossier.model_validate(dossier) for dossier in evidence_dossiers)
        issues: list[ValidationIssue] = []
        expected_sections = tuple(section.section_id for section in outline.sections)
        actual_sections = tuple(section.section_id for section in script.sections)
        if actual_sections != expected_sections:
            issues.append(
                _issue(
                    "MISSING_SECTION",
                    "blocking",
                    None,
                    "Script sections do not exactly cover the validated outline order.",
                )
            )
        outline_by_id = {section.section_id: section for section in outline.sections}
        article_ids_by_event = {
            dossier.event_id: {source.article_id for source in dossier.evidence_sources}
            for dossier in dossiers
        }
        normalized_sections: list[str] = []
        for section in script.sections:
            normalized_text = _normalized_text(section.text)
            normalized_sections.append(normalized_text)
            outline_section = outline_by_id.get(section.section_id)
            if outline_section is None:
                issues.append(
                    _issue(
                        "MISSING_SECTION",
                        "blocking",
                        section.section_id,
                        "Script section is not present in the validated outline.",
                    )
                )
                continue
            allowed_event_ids = set(outline_section.event_ids)
            unknown_events = set(section.event_ids) - allowed_event_ids
            if unknown_events:
                issues.append(
                    _issue(
                        "UNKNOWN_EVENT_REFERENCE",
                        "blocking",
                        section.section_id,
                        "Script section references an event outside its outline section.",
                    )
                )
            allowed_article_ids = {
                article_id
                for event_id in section.event_ids
                for article_id in article_ids_by_event.get(event_id, set())
            }
            unknown_articles = set(section.article_ids) - allowed_article_ids
            if unknown_articles:
                issues.append(
                    _issue(
                        "UNKNOWN_ARTICLE_REFERENCE",
                        "blocking",
                        section.section_id,
                        "Script section references an Article outside its event evidence.",
                        tuple(sorted(unknown_articles)),
                    )
                )
            if not normalized_text:
                issues.append(
                    _issue(
                        "EMPTY_SECTION_TEXT",
                        "blocking",
                        section.section_id,
                        "Script section text is empty after whitespace normalization.",
                    )
                )
            if len(normalized_text) > self._max_section_chars:
                issues.append(
                    _issue(
                        "SCRIPT_TOO_LONG",
                        "blocking",
                        section.section_id,
                        "Script section exceeds the configured character limit.",
                    )
                )
            if _has_unsupported_format(section.text):
                issues.append(
                    _issue(
                        "UNSUPPORTED_FORMAT",
                        "blocking",
                        section.section_id,
                        "Script text contains Markdown, HTML, SSML, or a raw URL.",
                    )
                )
            if _has_duplicate_adjacent_text(section.text):
                issues.append(
                    _issue(
                        "DUPLICATE_TEXT",
                        "warning",
                        section.section_id,
                        "Script contains duplicate adjacent sentences or paragraphs.",
                    )
                )
            for claim in section.claims:
                if not claim.article_ids:
                    issues.append(
                        _issue(
                            "CLAIM_WITHOUT_SOURCE",
                            "blocking",
                            section.section_id,
                            "A script claim has no supporting Article reference.",
                        )
                    )
            evidence_text = " ".join(
                source.text_excerpt
                for dossier in dossiers
                for source in dossier.evidence_sources
                if source.article_id in section.article_ids
            )
            missing_numbers = _missing_number_tokens(section.text, evidence_text)
            if missing_numbers:
                issues.append(
                    _issue(
                        "NUMBER_NOT_FOUND_IN_EVIDENCE",
                        "warning",
                        section.section_id,
                        "Numeric token is not present in the referenced evidence excerpts: "
                        + ", ".join(missing_numbers),
                        section.article_ids,
                    )
                )
        character_count = sum(len(text) for text in normalized_sections)
        estimated_duration_seconds = character_count / self._estimated_chars_per_second
        if character_count > self._max_script_chars:
            issues.append(
                _issue(
                    "SCRIPT_TOO_LONG",
                    "blocking",
                    None,
                    "Script exceeds the configured total character limit.",
                )
            )
        expected_duration = outline.target_seconds
        lower = expected_duration * (1 - self._duration_tolerance_ratio)
        upper = expected_duration * (1 + self._duration_tolerance_ratio)
        if estimated_duration_seconds < lower:
            issues.append(
                _issue(
                    "SCRIPT_TOO_SHORT",
                    "blocking",
                    None,
                    "Estimated spoken duration is below the configured tolerance.",
                )
            )
        elif estimated_duration_seconds > upper:
            issues.append(
                _issue(
                    "SCRIPT_TOO_LONG",
                    "blocking",
                    None,
                    "Estimated spoken duration exceeds the configured tolerance.",
                )
            )
        return ValidationReport(
            estimated_duration_seconds=estimated_duration_seconds,
            character_count=character_count,
            issues=tuple(issues),
        )

    @staticmethod
    def has_structural_blocking_issues(report: ValidationReport) -> bool:
        """Identify reference or section topology that cannot be safely repaired automatically."""
        return any(
            issue.severity == "blocking" and issue.code in _STRUCTURAL_CODES
            for issue in report.issues
        )


def _issue(
    code: str,
    severity: str,
    section_id: str | None,
    message: str,
    related_article_ids: tuple[int, ...] = (),
) -> ValidationIssue:
    """Build one stable typed issue using the documented severity vocabulary."""
    return ValidationIssue(
        code=code,
        severity=severity,
        section_id=section_id,
        message=message,
        related_article_ids=related_article_ids,
    )


def _normalized_text(value: str) -> str:
    """Count spoken characters after whitespace normalization rather than raw formatting bytes."""
    return "".join(value.split())


def _has_unsupported_format(value: str) -> bool:
    """Detect explicitly unsupported spoken-text constructs without interpreting their meaning."""
    return (
        bool(_HTML_OR_SSML_PATTERN.search(value))
        or bool(_URL_PATTERN.search(value))
        or bool(_SECRET_OR_TASK_METADATA_PATTERN.search(value))
        or "|" in value
        or any(line.lstrip().startswith("#") for line in value.splitlines())
    )


def _has_duplicate_adjacent_text(value: str) -> bool:
    """Find immediately repeated normalized paragraphs or sentences deterministically."""
    paragraphs = [" ".join(part.split()) for part in value.split("\n\n") if part.strip()]
    if any(left == right for left, right in zip(paragraphs, paragraphs[1:], strict=False)):
        return True
    sentences = [
        " ".join(part.split()) for part in _SENTENCE_SPLIT_PATTERN.split(value) if part.strip()
    ]
    return any(left == right for left, right in zip(sentences, sentences[1:], strict=False))


def _missing_number_tokens(text: str, evidence_text: str) -> tuple[str, ...]:
    """Warn about numeric tokens absent from cited excerpts; never label a claim false."""
    evidence_tokens = {match.group(0) for match in _NUMBER_PATTERN.finditer(evidence_text)}
    return tuple(
        token
        for token in dict.fromkeys(match.group(0) for match in _NUMBER_PATTERN.finditer(text))
        if token not in evidence_tokens
    )
