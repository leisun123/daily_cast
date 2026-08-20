"""Persist validated editorial artifacts as immutable Episode snapshots."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import date

from pydantic import ValidationError
from sqlalchemy.orm import Session, sessionmaker

from dailycast.core.errors import DailyCastError
from dailycast.core.hashes import sha256_text
from dailycast.core.identifiers import UUIDGenerator
from dailycast.core.time import Clock
from dailycast.db.models import Episode, EpisodeStatus, NewsEventStatus, ScriptOrigin, utc_now
from dailycast.db.repositories import (
    EpisodeItemRepository,
    EpisodeRepository,
    NewsEventRepository,
    TaskRunRepository,
)
from dailycast.db.transactions import UnitOfWork
from dailycast.llm.outline_schemas import EpisodeOutline, EvidenceDossier
from dailycast.llm.script_schemas import (
    EpisodeMetadata,
    EpisodeScript,
    ScriptReview,
    ValidationReport,
)


class EpisodeCreationPreconditionError(DailyCastError):
    """Raised when editorial artifacts are not structurally safe to persist as an Episode."""

    def __init__(self) -> None:
        super().__init__(
            code="EPISODE_EDITORIAL_ARTIFACTS_INVALID",
            message="Episode requires structurally valid outline, script, evidence, and metadata",
            status_code=422,
        )


class EpisodeStateTransitionError(DailyCastError):
    """Raised for a lifecycle edge not permitted by the Sprint 5A Episode state machine."""

    def __init__(self, current: EpisodeStatus, target: EpisodeStatus) -> None:
        super().__init__(
            code="EPISODE_STATE_TRANSITION_INVALID",
            message=f"Episode transition {current.value} -> {target.value} is not allowed",
            status_code=409,
        )


class EpisodeRegenerationPreconditionError(DailyCastError):
    """Raised when an explicit regenerate would mutate a published public record."""

    def __init__(self) -> None:
        super().__init__(
            code="EPISODE_REGENERATION_NOT_ALLOWED",
            message="only an unpublished Episode can be regenerated",
            status_code=409,
        )


_ALLOWED_TRANSITIONS: dict[EpisodeStatus, frozenset[EpisodeStatus]] = {
    EpisodeStatus.DRAFT: frozenset({EpisodeStatus.REVIEW_REQUIRED}),
    EpisodeStatus.REVIEW_REQUIRED: frozenset({EpisodeStatus.DRAFT, EpisodeStatus.APPROVED}),
    EpisodeStatus.APPROVED: frozenset({EpisodeStatus.REVIEW_REQUIRED, EpisodeStatus.PUBLISHING}),
    EpisodeStatus.PUBLISHING: frozenset(
        {EpisodeStatus.APPROVED, EpisodeStatus.PUBLISHED, EpisodeStatus.FAILED}
    ),
    EpisodeStatus.FAILED: frozenset({EpisodeStatus.DRAFT, EpisodeStatus.PUBLISHING}),
}


class EpisodeService:
    """Own Episode creation, snapshotting, and explicit lifecycle transitions."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        clock: Clock | None = None,
        uuid_generator: UUIDGenerator | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock or Clock()
        self._uuid_generator = uuid_generator or UUIDGenerator()

    def create_from_editorial_artifacts(
        self,
        *,
        episode_date: date,
        edition: str,
        outline: object,
        script: object,
        validation: object,
        review: object,
        metadata: object,
        selected_event_ids: Sequence[int],
        evidence_dossiers: Sequence[object],
        task_run_id: str | None = None,
        enforce_quality_gate: bool = True,
    ) -> Episode:
        """Create one review-required Episode and frozen EpisodeItems, or reuse its identity."""
        with UnitOfWork(self._session_factory) as unit:
            assert unit.session is not None
            episodes = EpisodeRepository(unit.session)
            existing = episodes.get_by_date_and_edition(episode_date, edition)
            if existing is not None:
                self._attach_task_run(unit.session, task_run_id, existing.id)
                return existing

            artifacts = _validated_artifacts(
                outline=outline,
                script=script,
                validation=validation,
                review=review,
                metadata=metadata,
                selected_event_ids=selected_event_ids,
                evidence_dossiers=evidence_dossiers,
                enforce_quality_gate=enforce_quality_gate,
            )
            events = NewsEventRepository(unit.session)
            selected_events = []
            for event_id in artifacts.selected_event_ids:
                event = events.get(event_id)
                if event is None or event.status is not NewsEventStatus.SELECTED:
                    raise EpisodeCreationPreconditionError()
                selected_events.append(event)

            episode = episodes.create(
                public_id=str(self._uuid_generator.new()),
                episode_date=episode_date,
                edition=edition,
                status=EpisodeStatus.REVIEW_REQUIRED,
                title=artifacts.metadata.title,
                description=artifacts.metadata.description,
                outline_json=_canonical_json(artifacts.outline.model_dump(mode="json")),
                script_json=_canonical_json(artifacts.script.model_dump(mode="json")),
                script_text=artifacts.script_text,
                script_revision=1,
                script_hash=sha256_text(artifacts.script_text),
                script_origin=ScriptOrigin.GENERATED,
                review_json=_canonical_json(
                    {
                        "validation": artifacts.validation.model_dump(mode="json"),
                        "review": artifacts.review.model_dump(mode="json"),
                    }
                ),
                target_duration_seconds=artifacts.outline.target_seconds,
                news_count=len(selected_events),
            )
            item_repository = EpisodeItemRepository(unit.session)
            for position, event in enumerate(selected_events, start=1):
                dossier = artifacts.dossiers_by_event_id[event.id]
                item_repository.create(
                    episode_id=episode.id,
                    news_event_id=event.id,
                    position=position,
                    event_title_snapshot=event.title,
                    selection_reason_snapshot=dossier.selection_reason,
                    score_snapshot_json=_canonical_json(
                        {
                            "confidence_score": dossier.confidence_score,
                            "deterministic_score": event.deterministic_score,
                            "importance_score": dossier.importance_score,
                            "relevance_score": dossier.relevance_score,
                        }
                    ),
                    source_article_ids_json=_canonical_json(
                        [source.article_id for source in dossier.evidence_sources]
                    ),
                    section_id=artifacts.section_ids_by_event_id.get(event.id),
                )
            self._attach_task_run(unit.session, task_run_id, episode.id)
            return episode

    def get_episode(self, episode_id: int) -> Episode | None:
        """Load one Episode by durable integer identifier."""
        with UnitOfWork(self._session_factory) as unit:
            assert unit.session is not None
            return EpisodeRepository(unit.session).get(episode_id)

    def regenerate_from_editorial_artifacts(
        self,
        *,
        episode_date: date,
        edition: str,
        outline: object,
        script: object,
        validation: object,
        review: object,
        metadata: object,
        selected_event_ids: Sequence[int],
        evidence_dossiers: Sequence[object],
        task_run_id: str | None = None,
        enforce_quality_gate: bool = True,
    ) -> Episode:
        """Replace one unpublished same-day draft only for an explicit regenerate command."""
        artifacts = _validated_artifacts(
            outline=outline,
            script=script,
            validation=validation,
            review=review,
            metadata=metadata,
            selected_event_ids=selected_event_ids,
            evidence_dossiers=evidence_dossiers,
            enforce_quality_gate=enforce_quality_gate,
        )
        with UnitOfWork(self._session_factory) as unit:
            assert unit.session is not None
            episodes = EpisodeRepository(unit.session)
            episode = episodes.get_by_date_and_edition(episode_date, edition)
            if episode is None or episode.status in {
                EpisodeStatus.PUBLISHED,
                EpisodeStatus.PUBLISHING,
            }:
                raise EpisodeRegenerationPreconditionError()
            events = NewsEventRepository(unit.session)
            selected_events = []
            for event_id in artifacts.selected_event_ids:
                event = events.get(event_id)
                if event is None or event.status is not NewsEventStatus.SELECTED:
                    raise EpisodeCreationPreconditionError()
                selected_events.append(event)
            episode.title = artifacts.metadata.title
            episode.description = artifacts.metadata.description
            episode.outline_json = _canonical_json(artifacts.outline.model_dump(mode="json"))
            episode.script_json = _canonical_json(artifacts.script.model_dump(mode="json"))
            episode.script_text = artifacts.script_text
            episode.script_revision += 1
            episode.script_hash = sha256_text(artifacts.script_text)
            episode.script_origin = ScriptOrigin.GENERATED
            episode.review_json = _canonical_json(
                {
                    "validation": artifacts.validation.model_dump(mode="json"),
                    "review": artifacts.review.model_dump(mode="json"),
                }
            )
            episode.target_duration_seconds = artifacts.outline.target_seconds
            episode.news_count = len(selected_events)
            episode.actual_duration_ms = None
            episode.audio_version = 0
            episode.audio_manifest_hash = None
            episode.draft_audio_path = None
            episode.draft_audio_sha256 = None
            episode.approved_script_revision = None
            episode.approved_audio_version = None
            episode.approved_at = None
            episode.status = EpisodeStatus.DRAFT
            episode.error_code = None
            episode.error_summary = None
            EpisodeItemRepository(unit.session).delete_by_episode(episode.id)
            items = EpisodeItemRepository(unit.session)
            for position, event in enumerate(selected_events, start=1):
                dossier = artifacts.dossiers_by_event_id[event.id]
                items.create(
                    episode_id=episode.id,
                    news_event_id=event.id,
                    position=position,
                    event_title_snapshot=event.title,
                    selection_reason_snapshot=dossier.selection_reason,
                    score_snapshot_json=_canonical_json(
                        {
                            "confidence_score": dossier.confidence_score,
                            "deterministic_score": event.deterministic_score,
                            "importance_score": dossier.importance_score,
                            "relevance_score": dossier.relevance_score,
                        }
                    ),
                    source_article_ids_json=_canonical_json(
                        [source.article_id for source in dossier.evidence_sources]
                    ),
                    section_id=artifacts.section_ids_by_event_id.get(event.id),
                )
            EpisodeRepository(unit.session).increment_lock_version(episode)
            self._attach_task_run(unit.session, task_run_id, episode.id)
            return episode

    def transition_status(self, episode_id: int, target: EpisodeStatus) -> Episode:
        """Validate and persist one documented Episode lifecycle transition."""
        with UnitOfWork(self._session_factory) as unit:
            assert unit.session is not None
            repository = EpisodeRepository(unit.session)
            episode = repository.get(episode_id)
            if episode is None:
                msg = f"Episode {episode_id} does not exist"
                raise LookupError(msg)
            self._validate_transition(episode.status, target)
            if episode.status is EpisodeStatus.APPROVED and target is EpisodeStatus.REVIEW_REQUIRED:
                episode.approved_script_revision = None
                episode.approved_audio_version = None
                episode.approved_at = None
            return repository.update_status(episode, target)

    def approve(self, episode_id: int) -> Episode:
        """Bind the current script and audio versions to an explicit human approval."""
        with UnitOfWork(self._session_factory) as unit:
            assert unit.session is not None
            repository = EpisodeRepository(unit.session)
            episode = repository.get(episode_id)
            if episode is None:
                msg = f"Episode {episode_id} does not exist"
                raise LookupError(msg)
            self._validate_transition(episode.status, EpisodeStatus.APPROVED)
            episode.approved_script_revision = episode.script_revision
            episode.approved_audio_version = episode.audio_version
            episode.approved_at = self._clock.now()
            return repository.update_status(episode, EpisodeStatus.APPROVED)

    @staticmethod
    def _validate_transition(current: EpisodeStatus, target: EpisodeStatus) -> None:
        """Reject non-documented state edges before modifying a persisted Episode."""
        if target not in _ALLOWED_TRANSITIONS.get(current, frozenset()):
            raise EpisodeStateTransitionError(current, target)

    @staticmethod
    def _attach_task_run(session: Session, task_run_id: str | None, episode_id: int) -> None:
        """Record the Episode produced by a TaskRun without creating a separate workflow row."""
        if task_run_id is None:
            return
        task_run = TaskRunRepository(session).get(task_run_id)
        if task_run is None:
            msg = f"TaskRun {task_run_id} does not exist"
            raise LookupError(msg)
        task_run.episode_id = episode_id
        task_run.updated_at = utc_now()
        session.flush()


class _ValidatedArtifacts:
    """Internal normalized editorial values that passed the Episode creation gate."""

    def __init__(
        self,
        *,
        outline: EpisodeOutline,
        script: EpisodeScript,
        validation: ValidationReport,
        review: ScriptReview,
        metadata: EpisodeMetadata,
        selected_event_ids: tuple[int, ...],
        dossiers_by_event_id: dict[int, EvidenceDossier],
        section_ids_by_event_id: dict[int, str],
    ) -> None:
        self.outline = outline
        self.script = script
        self.validation = validation
        self.review = review
        self.metadata = metadata
        self.selected_event_ids = selected_event_ids
        self.dossiers_by_event_id = dossiers_by_event_id
        self.section_ids_by_event_id = section_ids_by_event_id

    @property
    def script_text(self) -> str:
        """Render the one plain-text TTS projection from the validated structured script."""
        return "\n\n".join(section.text for section in self.script.sections)


def _validated_artifacts(
    *,
    outline: object,
    script: object,
    validation: object,
    review: object,
    metadata: object,
    selected_event_ids: Sequence[int],
    evidence_dossiers: Sequence[object],
    enforce_quality_gate: bool,
) -> _ValidatedArtifacts:
    """Validate every input crossing the editorial-to-persistence boundary exactly once."""
    try:
        validated_outline = EpisodeOutline.model_validate(outline)
        validated_validation = ValidationReport.model_validate(validation)
        validated_metadata = EpisodeMetadata.model_validate(metadata)
        event_ids = tuple(selected_event_ids)
        dossiers = tuple(EvidenceDossier.model_validate(dossier) for dossier in evidence_dossiers)
        validated_script = EpisodeScript.model_validate(
            script,
            context={"outline": validated_outline, "evidence_dossiers": dossiers},
        )
        validated_review = ScriptReview.model_validate(
            review,
            context={"script": validated_script, "evidence_dossiers": dossiers},
        )
    except ValidationError as error:
        raise EpisodeCreationPreconditionError() from error
    if (
        not event_ids
        or len(event_ids) != len(set(event_ids))
        or not all(isinstance(event_id, int) and event_id > 0 for event_id in event_ids)
        or (
            validated_validation.has_blocking_issues
            or (
                enforce_quality_gate
                and (
                    validated_review.verdict != "pass"
                    or any(issue.severity == "blocking" for issue in validated_review.issues)
                )
            )
        )
    ):
        raise EpisodeCreationPreconditionError()
    dossiers_by_event_id = {dossier.event_id: dossier for dossier in dossiers}
    if len(dossiers_by_event_id) != len(dossiers) or set(dossiers_by_event_id) != set(event_ids):
        raise EpisodeCreationPreconditionError()
    section_ids_by_event_id: dict[int, str] = {}
    for section in validated_outline.sections:
        for event_id in section.event_ids:
            section_ids_by_event_id.setdefault(event_id, section.section_id)
    if set(section_ids_by_event_id) != set(event_ids):
        raise EpisodeCreationPreconditionError()
    return _ValidatedArtifacts(
        outline=validated_outline,
        script=validated_script,
        validation=validated_validation,
        review=validated_review,
        metadata=validated_metadata,
        selected_event_ids=event_ids,
        dossiers_by_event_id=dossiers_by_event_id,
        section_ids_by_event_id=section_ids_by_event_id,
    )


def _canonical_json(value: object) -> str:
    """Encode snapshot JSON deterministically for SQLite JSON checks and reproducible hashes."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
