"""Sprint 5A Episode persistence and lifecycle behavior."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import date

import pytest
from editorial_test_support import (
    FakeLLMProvider,
    build_dossiers,
    build_outline,
    create_selected_event,
    upgraded_session_factory,
    valid_script_payload,
)
from sqlalchemy.orm import Session, sessionmaker

from dailycast.db.models import EpisodeStatus
from dailycast.db.repositories import EpisodeItemRepository, EpisodeRepository
from dailycast.db.transactions import UnitOfWork
from dailycast.episodes.service import (
    EpisodeCreationPreconditionError,
    EpisodeService,
    EpisodeStateTransitionError,
)
from dailycast.llm.editorial_service import AIEditorialService
from dailycast.llm.script_schemas import EpisodeMetadata, EpisodeScript, ScriptReview


@dataclass(frozen=True, slots=True)
class AcceptedEditorialArtifacts:
    """One complete, locally validated editorial result ready for persistence."""

    episode_date: date
    outline: object
    script: EpisodeScript
    validation: object
    review: ScriptReview
    metadata: EpisodeMetadata
    selected_event_ids: tuple[int, ...]
    dossiers: tuple[object, ...]
    article_id: int


def accepted_artifacts(
    factory: sessionmaker[Session], *, key: str = "episode"
) -> AcceptedEditorialArtifacts:
    """Build durable selected evidence together with a pass verdict and metadata."""
    fixture = create_selected_event(factory, key=key, content="可信新闻证据。")
    outline = build_outline(fixture.event_id)
    dossiers = build_dossiers(factory, fixture)
    script = EpisodeScript.model_validate(
        valid_script_payload(outline, fixture),
        context={"outline": outline, "evidence_dossiers": dossiers},
    )
    validation = AIEditorialService(factory, FakeLLMProvider({})).validate_script(
        script, outline, dossiers
    )
    assert not validation.has_blocking_issues
    return AcceptedEditorialArtifacts(
        episode_date=date(2026, 7, 22),
        outline=outline,
        script=script,
        validation=validation,
        review=ScriptReview.model_validate(
            {
                "schema_version": "1",
                "verdict": "pass",
                "issues": [],
                "suggested_changes": [],
            },
            context={"script": script, "evidence_dossiers": dossiers},
        ),
        metadata=EpisodeMetadata.model_validate(
            {
                "schema_version": "1",
                "title": "今日科技新闻",
                "description": "围绕一项经过核验的科技新闻展开。",
                "keywords": ["科技", "新闻"],
            },
            context={"script": script, "selected_event_titles": ("事件 " + key,)},
        ),
        selected_event_ids=(fixture.event_id,),
        dossiers=dossiers,
        article_id=fixture.article_id,
    )


def create_episode(service: EpisodeService, artifacts: AcceptedEditorialArtifacts):
    """Pass the full accepted-artifact contract to the public Episode service."""
    return service.create_from_editorial_artifacts(
        episode_date=artifacts.episode_date,
        edition="daily",
        outline=artifacts.outline,
        script=artifacts.script,
        validation=artifacts.validation,
        review=artifacts.review,
        metadata=artifacts.metadata,
        selected_event_ids=artifacts.selected_event_ids,
        evidence_dossiers=artifacts.dossiers,
    )


def test_accepted_editorial_artifacts_create_review_required_episode(
    app_config_path,
) -> None:
    """Only a validated pass verdict and successful metadata create a persistent Episode."""
    factory: sessionmaker[Session] = upgraded_session_factory(app_config_path)
    try:
        artifacts = accepted_artifacts(factory)
        episode = create_episode(EpisodeService(factory), artifacts)

        assert episode.status is EpisodeStatus.REVIEW_REQUIRED
        assert episode.script_revision == 1
        assert episode.title == artifacts.metadata.title
        assert episode.description == artifacts.metadata.description
        assert episode.approved_script_revision is None
        assert episode.approved_audio_version is None
        assert episode.published_at is None
        assert episode.audio_version == 0
        assert episode.script_text == "\n\n".join(
            section.text for section in artifacts.script.sections
        )
    finally:
        factory.kw["bind"].dispose()


def test_non_pass_review_never_creates_episode(app_config_path) -> None:
    """A revise or human-review verdict cannot leave a partial Episode draft behind."""
    factory: sessionmaker[Session] = upgraded_session_factory(app_config_path)
    try:
        artifacts = accepted_artifacts(factory, key="blocked")
        blocked_review = artifacts.review.model_copy(update={"verdict": "human_review"})

        with pytest.raises(EpisodeCreationPreconditionError):
            EpisodeService(factory).create_from_editorial_artifacts(
                episode_date=artifacts.episode_date,
                edition="daily",
                outline=artifacts.outline,
                script=artifacts.script,
                validation=artifacts.validation,
                review=blocked_review,
                metadata=artifacts.metadata,
                selected_event_ids=artifacts.selected_event_ids,
                evidence_dossiers=artifacts.dossiers,
            )

        with UnitOfWork(factory) as unit:
            assert unit.session is not None
            assert EpisodeRepository(unit.session).list() == []
    finally:
        factory.kw["bind"].dispose()


def test_relaxed_quality_gate_still_rejects_unknown_script_references(app_config_path) -> None:
    """Alpha relaxation never permits a script that escapes the selected evidence topology."""
    factory: sessionmaker[Session] = upgraded_session_factory(app_config_path)
    try:
        artifacts = accepted_artifacts(factory, key="relaxed-structural-invalid")
        payload = artifacts.script.model_dump(mode="json")
        sections = payload["sections"]
        assert isinstance(sections, list)
        news_section = sections[1]
        assert isinstance(news_section, dict)
        news_section["article_ids"] = [999_999]
        claims = news_section["claims"]
        assert isinstance(claims, list)
        claim = claims[0]
        assert isinstance(claim, dict)
        claim["article_ids"] = [999_999]

        with pytest.raises(EpisodeCreationPreconditionError):
            EpisodeService(factory).create_from_editorial_artifacts(
                episode_date=artifacts.episode_date,
                edition="daily",
                outline=artifacts.outline,
                script=payload,
                validation=artifacts.validation,
                review=artifacts.review,
                metadata=artifacts.metadata,
                selected_event_ids=artifacts.selected_event_ids,
                evidence_dossiers=artifacts.dossiers,
                enforce_quality_gate=False,
            )

        with UnitOfWork(factory) as unit:
            assert unit.session is not None
            assert EpisodeRepository(unit.session).list() == []
    finally:
        factory.kw["bind"].dispose()


def test_episode_item_uses_immutable_selected_event_snapshot(app_config_path) -> None:
    """EpisodeItem freezes event title, score, reason, source IDs, and outline position."""
    factory: sessionmaker[Session] = upgraded_session_factory(app_config_path)
    try:
        artifacts = accepted_artifacts(factory, key="snapshot")
        episode = create_episode(EpisodeService(factory), artifacts)

        with UnitOfWork(factory) as unit:
            assert unit.session is not None
            items = EpisodeItemRepository(unit.session).list_by_episode(episode.id)
            assert len(items) == 1
            item = items[0]
            assert item.position == 1
            assert item.news_event_id == artifacts.selected_event_ids[0]
            assert item.event_title_snapshot == "事件 snapshot"
            assert item.selection_reason_snapshot == "重要且相关。"
            assert item.section_id == "news-1"
            assert json.loads(item.score_snapshot_json) == {
                "confidence_score": 70.0,
                "deterministic_score": 90.0,
                "importance_score": 90.0,
                "relevance_score": 80.0,
            }
            assert json.loads(item.source_article_ids_json) == [artifacts.article_id]
    finally:
        factory.kw["bind"].dispose()


def test_documented_episode_state_transitions_and_approval_are_enforced(app_config_path) -> None:
    """The service accepts only Sprint 5A lifecycle edges and binds approval revisions."""
    factory: sessionmaker[Session] = upgraded_session_factory(app_config_path)
    try:
        episode = create_episode(EpisodeService(factory), accepted_artifacts(factory, key="states"))
        service = EpisodeService(factory)

        approved = service.approve(episode.id)
        assert approved.status is EpisodeStatus.APPROVED
        assert approved.approved_script_revision == 1
        assert approved.approved_audio_version == 0

        assert service.transition_status(episode.id, EpisodeStatus.REVIEW_REQUIRED).status is (
            EpisodeStatus.REVIEW_REQUIRED
        )
        assert (
            service.transition_status(episode.id, EpisodeStatus.DRAFT).status is EpisodeStatus.DRAFT
        )
        assert (
            service.transition_status(episode.id, EpisodeStatus.REVIEW_REQUIRED).status
            is EpisodeStatus.REVIEW_REQUIRED
        )
        assert (
            service.transition_status(episode.id, EpisodeStatus.APPROVED).status
            is EpisodeStatus.APPROVED
        )
        assert (
            service.transition_status(episode.id, EpisodeStatus.PUBLISHING).status
            is EpisodeStatus.PUBLISHING
        )
    finally:
        factory.kw["bind"].dispose()


def test_invalid_episode_state_transition_is_rejected(app_config_path) -> None:
    """A review-required Episode cannot skip directly to publishing."""
    factory: sessionmaker[Session] = upgraded_session_factory(app_config_path)
    try:
        episode = create_episode(
            EpisodeService(factory), accepted_artifacts(factory, key="invalid")
        )

        with pytest.raises(EpisodeStateTransitionError):
            EpisodeService(factory).transition_status(episode.id, EpisodeStatus.PUBLISHING)
    finally:
        factory.kw["bind"].dispose()


def test_repeated_editorial_task_reuses_same_episode_and_items(app_config_path) -> None:
    """A retry for the same date and edition does not create duplicate Episode or snapshots."""
    factory: sessionmaker[Session] = upgraded_session_factory(app_config_path)
    try:
        artifacts = accepted_artifacts(factory, key="retry")
        service = EpisodeService(factory)
        first = create_episode(service, artifacts)
        retry = create_episode(service, artifacts)

        assert retry.id == first.id
        with UnitOfWork(factory) as unit:
            assert unit.session is not None
            episodes = EpisodeRepository(unit.session)
            assert episodes.get_by_date_and_edition(artifacts.episode_date, "daily") is not None
            assert len(episodes.list()) == 1
            assert len(EpisodeItemRepository(unit.session).list_by_episode(first.id)) == 1
    finally:
        factory.kw["bind"].dispose()


def test_existing_date_and_edition_are_reused_before_retry_artifact_validation(
    app_config_path,
) -> None:
    """A retry reuses its durable Episode even when a later editorial checkpoint is rejected."""
    factory: sessionmaker[Session] = upgraded_session_factory(app_config_path)
    try:
        artifacts = accepted_artifacts(factory, key="existing-identity")
        service = EpisodeService(factory)
        first = create_episode(service, artifacts)
        rejected_review = artifacts.review.model_copy(update={"verdict": "human_review"})

        retry = service.create_from_editorial_artifacts(
            episode_date=artifacts.episode_date,
            edition="daily",
            outline=artifacts.outline,
            script=artifacts.script,
            validation=artifacts.validation,
            review=rejected_review,
            metadata=artifacts.metadata,
            selected_event_ids=artifacts.selected_event_ids,
            evidence_dossiers=artifacts.dossiers,
        )

        assert retry.id == first.id
    finally:
        factory.kw["bind"].dispose()


def test_explicit_regenerate_replaces_unpublished_episode_and_invalidates_audio(
    app_config_path,
) -> None:
    """A same-day regenerate is explicit; retries still reuse the immutable episode identity."""
    factory: sessionmaker[Session] = upgraded_session_factory(app_config_path)
    try:
        original_artifacts = accepted_artifacts(factory, key="regenerate-original")
        service = EpisodeService(factory)
        original = create_episode(service, original_artifacts)
        with UnitOfWork(factory) as unit:
            assert unit.session is not None
            current = EpisodeRepository(unit.session).get(original.id)
            assert current is not None
            current.audio_version = 3
            current.draft_audio_path = "audio/drafts/1/revision-1.mp3"
            current.draft_audio_sha256 = "a" * 64
            current.approved_script_revision = 1
            current.approved_audio_version = 3
            current.status = EpisodeStatus.APPROVED
        refreshed_artifacts = accepted_artifacts(factory, key="regenerate-new")
        refreshed_artifacts = replace(
            refreshed_artifacts, episode_date=original_artifacts.episode_date
        )

        regenerated = service.regenerate_from_editorial_artifacts(
            episode_date=original_artifacts.episode_date,
            edition="daily",
            outline=refreshed_artifacts.outline,
            script=refreshed_artifacts.script,
            validation=refreshed_artifacts.validation,
            review=refreshed_artifacts.review,
            metadata=refreshed_artifacts.metadata,
            selected_event_ids=refreshed_artifacts.selected_event_ids,
            evidence_dossiers=refreshed_artifacts.dossiers,
        )

        assert regenerated.id == original.id
        assert regenerated.script_revision == 2
        assert regenerated.status is EpisodeStatus.DRAFT
        assert regenerated.audio_version == 0
        assert regenerated.draft_audio_path is None
        assert regenerated.approved_script_revision is None
        assert regenerated.approved_audio_version is None
    finally:
        factory.kw["bind"].dispose()


def test_episode_repository_increments_lock_version(app_config_path) -> None:
    """Repository mutation support increments the optimistic concurrency version explicitly."""
    factory: sessionmaker[Session] = upgraded_session_factory(app_config_path)
    try:
        episode = create_episode(EpisodeService(factory), accepted_artifacts(factory, key="lock"))
        with UnitOfWork(factory) as unit:
            assert unit.session is not None
            changed = EpisodeRepository(unit.session).increment_lock_version(episode)
            assert changed.lock_version == 2
    finally:
        factory.kw["bind"].dispose()
