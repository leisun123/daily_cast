"""Strict structured-script, review, metadata, and validation-report DTOs."""

from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, model_validator

from dailycast.llm.outline_schemas import EpisodeOutline, EvidenceDossier

GENERATE_SCRIPT_V1_SCHEMA_VERSION = "generate_script_v1"
REVIEW_SCRIPT_V1_SCHEMA_VERSION = "review_script_v1"
GENERATE_METADATA_V1_SCHEMA_VERSION = "generate_metadata_v1"

_MAX_SCRIPT_SECTION_CHARS = 2400
_MAX_CLAIM_CHARS = 600
_HTML_OR_SSML_PATTERN = re.compile(r"</?[A-Za-z][^>]*>")
_URL_PATTERN = re.compile(r"https?://\S+", re.IGNORECASE)
_METADATA_NUMBER_PATTERN = re.compile(r"(?<![A-Za-z0-9])\d+(?:[.,]\d+)?%?")
_SECRET_OR_TASK_METADATA_PATTERN = re.compile(
    r"\b(?:api[_-]?key|authorization|bearer\s+\S+|task[_-]?(?:run|step)[_-]?id)\b",
    re.IGNORECASE,
)


class ScriptClaim(BaseModel):
    """One spoken claim and the Article identifiers that support it."""

    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=_MAX_CLAIM_CHARS)
    article_ids: tuple[int, ...] = Field(max_length=3)

    @model_validator(mode="after")
    def require_unique_article_ids(self) -> ScriptClaim:
        """Reject repeated source references before the result can enter the Artifact cache."""
        if len(self.article_ids) != len(set(self.article_ids)):
            raise ValueError("script claim must not repeat article IDs")
        if _has_unsafe_spoken_text(self.text):
            raise ValueError("script claim contains unsupported formatting or sensitive metadata")
        return self


class EpisodeScriptSection(BaseModel):
    """One ordered spoken section preserving outline and evidence provenance."""

    model_config = ConfigDict(extra="forbid")

    section_id: str = Field(min_length=1, max_length=80)
    text: str = Field(max_length=_MAX_SCRIPT_SECTION_CHARS)
    event_ids: tuple[int, ...] = Field(max_length=30)
    article_ids: tuple[int, ...] = Field(max_length=9)
    claims: tuple[ScriptClaim, ...] = Field(max_length=16)

    @model_validator(mode="after")
    def require_unique_references(self) -> EpisodeScriptSection:
        """Keep direct references deterministic; the validator reports empty claim sources."""
        if len(self.event_ids) != len(set(self.event_ids)):
            raise ValueError("script section must not repeat event IDs")
        if len(self.article_ids) != len(set(self.article_ids)):
            raise ValueError("script section must not repeat article IDs")
        return self


class PronunciationHint(BaseModel):
    """A bounded optional pronunciation hint for later TTS work, not executable SSML."""

    model_config = ConfigDict(extra="forbid")

    term: str = Field(min_length=1, max_length=120)
    pronunciation: str = Field(min_length=1, max_length=240)


class EpisodeScript(BaseModel):
    """A strict, ordered Chinese podcast script linked only to supplied evidence IDs."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1"]
    sections: tuple[EpisodeScriptSection, ...] = Field(min_length=1, max_length=12)
    pronunciation_hints: tuple[PronunciationHint, ...] = Field(default=(), max_length=50)

    @model_validator(mode="after")
    def require_outline_order_and_traceability(self, info: ValidationInfo) -> EpisodeScript:
        """Reject section, event, and Article references outside supplied outline and dossiers."""
        section_ids = [section.section_id for section in self.sections]
        if len(section_ids) != len(set(section_ids)):
            raise ValueError("script contains duplicate section IDs")
        normalized_terms = [_normalized_hint_term(hint.term) for hint in self.pronunciation_hints]
        if len(normalized_terms) != len(set(normalized_terms)):
            raise ValueError("pronunciation hints must be unique by normalized term")
        if any(_has_unsafe_spoken_text(section.text) for section in self.sections):
            raise ValueError("script text contains unsupported formatting or sensitive metadata")
        context = info.context
        if context is None:
            return self
        outline, dossiers = _script_context(context)
        expected_sections = tuple(section.section_id for section in outline.sections)
        if tuple(section_ids) != expected_sections:
            raise ValueError("script sections must exactly match outline order and coverage")
        dossier_article_ids = {
            dossier.event_id: {source.article_id for source in dossier.evidence_sources}
            for dossier in dossiers
        }
        outline_by_id = {section.section_id: section for section in outline.sections}
        for section in self.sections:
            outline_section = outline_by_id[section.section_id]
            allowed_event_ids = set(outline_section.event_ids)
            if not set(section.event_ids).issubset(allowed_event_ids):
                raise ValueError("script section references an unknown event ID")
            allowed_article_ids = {
                article_id
                for event_id in section.event_ids
                for article_id in dossier_article_ids.get(event_id, set())
            }
            if not set(section.article_ids).issubset(allowed_article_ids):
                raise ValueError("script section references an unknown article ID")
            for claim in section.claims:
                if not set(claim.article_ids).issubset(set(section.article_ids)):
                    raise ValueError("script claim references an article outside its section")
            if outline_section.type == "news" and (
                not section.text.strip() or not section.event_ids or not section.article_ids
            ):
                raise ValueError("news script section requires text, event IDs, and article IDs")
        return self


class ValidationIssue(BaseModel):
    """One stable deterministic validation issue for JSON artifacts and UI display."""

    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1, max_length=80)
    severity: Literal["warning", "blocking"]
    section_id: str | None = Field(default=None, max_length=80)
    message: str = Field(min_length=1, max_length=600)
    related_article_ids: tuple[int, ...] = Field(default=(), max_length=9)


class ValidationReport(BaseModel):
    """Deterministic local validation output, distinct from the bounded semantic LLM review."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1"] = "1"
    estimated_duration_seconds: float = Field(ge=0)
    character_count: int = Field(ge=0)
    issues: tuple[ValidationIssue, ...] = Field(default=())

    @property
    def has_blocking_issues(self) -> bool:
        """State whether the script can bypass automatic revision and metadata generation safely."""
        return any(issue.severity == "blocking" for issue in self.issues)


class ScriptReviewIssue(BaseModel):
    """One evidence-bounded semantic review finding returned by the LLM provider."""

    model_config = ConfigDict(extra="forbid")

    severity: Literal["warning", "blocking"]
    type: str = Field(min_length=1, max_length=80)
    section_id: str = Field(min_length=1, max_length=80)
    message: str = Field(min_length=1, max_length=600)
    article_ids: tuple[int, ...] = Field(default=(), max_length=9)


class ScriptReview(BaseModel):
    """Strict bounded LLM review result that never claims external fact-checking work."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1"]
    verdict: Literal["pass", "revise", "human_review"]
    issues: tuple[ScriptReviewIssue, ...] = Field(default=(), max_length=32)
    suggested_changes: tuple[Annotated[str, Field(min_length=1, max_length=600)], ...] = Field(
        default=(), max_length=16
    )

    @model_validator(mode="after")
    def require_valid_review_references(self, info: ValidationInfo) -> ScriptReview:
        """Reject unknown references and an internally inconsistent pass verdict before caching."""
        if self.verdict == "pass" and any(issue.severity == "blocking" for issue in self.issues):
            raise ValueError("pass review verdict cannot include blocking issues")
        context = info.context
        if context is None:
            return self
        script, dossiers = _review_context(context)
        script_sections = {section.section_id: section for section in script.sections}
        evidence_article_ids = {
            source.article_id for dossier in dossiers for source in dossier.evidence_sources
        }
        for issue in self.issues:
            section = script_sections.get(issue.section_id)
            if section is None:
                raise ValueError("review issue references an unknown script section")
            if not set(issue.article_ids).issubset(set(section.article_ids) & evidence_article_ids):
                raise ValueError("review issue references an unknown article ID")
        return self


class EpisodeMetadata(BaseModel):
    """Bounded metadata from selected titles and validated final script text."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1"]
    title: str = Field(min_length=1, max_length=160)
    description: str = Field(min_length=1, max_length=1200)
    keywords: tuple[Annotated[str, Field(min_length=1, max_length=80)], ...] = Field(
        min_length=1, max_length=12
    )

    @model_validator(mode="after")
    def require_safe_unique_keywords(self, info: ValidationInfo) -> EpisodeMetadata:
        """Reject Markdown and links in user-visible metadata before caching or artifact writes."""
        normalized_keywords = [_normalized_hint_term(keyword) for keyword in self.keywords]
        if len(normalized_keywords) != len(set(normalized_keywords)):
            raise ValueError("metadata keywords must be unique by normalized form")
        metadata_values = (self.title, self.description, *self.keywords)
        if any(_has_unsupported_metadata_format(value) for value in metadata_values):
            raise ValueError("metadata must not include Markdown or URLs")
        if info.context is not None:
            script, selected_event_titles = _metadata_context(info.context)
            known_text = " ".join(
                (*selected_event_titles, *(section.text for section in script.sections))
            )
            known_numbers = {
                match.group(0) for match in _METADATA_NUMBER_PATTERN.finditer(known_text)
            }
            metadata_numbers = {
                match.group(0)
                for match in _METADATA_NUMBER_PATTERN.finditer(
                    " ".join((self.title, self.description, *self.keywords))
                )
            }
            if not metadata_numbers.issubset(known_numbers):
                raise ValueError("metadata contains a numeric claim absent from its bounded input")
        return self


def _metadata_context(context: object) -> tuple[EpisodeScript, tuple[str, ...]]:
    """Read the bounded final script and selected titles allowed to support metadata."""
    if not isinstance(context, dict):
        raise ValueError("metadata validation context must be a mapping")
    script = context.get("script")
    selected_event_titles = context.get("selected_event_titles")
    if not isinstance(script, EpisodeScript) or not isinstance(selected_event_titles, tuple):
        raise ValueError("metadata validation context is invalid")
    if not all(isinstance(title, str) and title.strip() for title in selected_event_titles):
        raise ValueError("metadata validation titles are invalid")
    return script, selected_event_titles


def _has_unsafe_spoken_text(value: str) -> bool:
    """Reject non-spoken formatting and operational data before an LLM result can be cached."""
    return (
        bool(_HTML_OR_SSML_PATTERN.search(value))
        or bool(_URL_PATTERN.search(value))
        or bool(_SECRET_OR_TASK_METADATA_PATTERN.search(value))
        or "|" in value
        or any(line.lstrip().startswith("#") for line in value.splitlines())
    )


def _script_context(context: object) -> tuple[EpisodeOutline, tuple[EvidenceDossier, ...]]:
    """Read the exact validated outline and dossiers supplied by the editorial service."""
    if not isinstance(context, dict):
        raise ValueError("script validation context must be a mapping")
    outline = context.get("outline")
    dossiers = context.get("evidence_dossiers")
    if not isinstance(outline, EpisodeOutline) or not isinstance(dossiers, tuple):
        raise ValueError("script validation context is invalid")
    if not all(isinstance(dossier, EvidenceDossier) for dossier in dossiers):
        raise ValueError("script validation dossiers are invalid")
    return outline, dossiers


def _review_context(context: object) -> tuple[EpisodeScript, tuple[EvidenceDossier, ...]]:
    """Read the exact validated script and dossiers allowed in an LLM review response."""
    if not isinstance(context, dict):
        raise ValueError("review validation context must be a mapping")
    script = context.get("script")
    dossiers = context.get("evidence_dossiers")
    if not isinstance(script, EpisodeScript) or not isinstance(dossiers, tuple):
        raise ValueError("review validation context is invalid")
    if not all(isinstance(dossier, EvidenceDossier) for dossier in dossiers):
        raise ValueError("review validation dossiers are invalid")
    return script, dossiers


def _normalized_hint_term(value: str) -> str:
    """Canonicalize a bounded user-facing keyword or pronunciation term for uniqueness checks."""
    return " ".join(value.split()).casefold()


def _has_unsupported_metadata_format(value: str) -> bool:
    """Keep generated display metadata plain text and free from URLs or Markdown constructs."""
    lowered = value.casefold()
    return "http://" in lowered or "https://" in lowered or "|" in value or "#" in value
