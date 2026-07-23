"""Idempotent first-run seeding of configured Sources without overwriting database edits."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy.orm import Session, sessionmaker

from dailycast.core.errors import ConfigurationError
from dailycast.db.models import SourceKind
from dailycast.db.repositories import SourceRepository
from dailycast.db.transactions import UnitOfWork
from dailycast.news.normalization import normalize_url


class SourceDefinition(BaseModel):
    """One declarative source seed with the same safe bounds as the persisted Source row."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=200)
    name: str = Field(min_length=1, max_length=500)
    kind: SourceKind
    entry_url: str = Field(min_length=1)
    enabled: bool = True
    priority: int = Field(default=50, ge=0, le=100)
    language: str | None = Field(default=None, max_length=64)
    request_timeout_seconds: int = Field(default=20, ge=1, le=120)
    max_items_per_run: int = Field(default=50, ge=1, le=500)
    config: dict[str, Any] = Field(default_factory=dict)


class SourceConfiguration(BaseModel):
    """The complete, seed-only source configuration file."""

    model_config = ConfigDict(extra="forbid")

    sources: list[SourceDefinition] = Field(min_length=1)

    @field_validator("sources")
    @classmethod
    def require_unique_ids(cls, sources: list[SourceDefinition]) -> list[SourceDefinition]:
        """Fail before any database mutation when two definitions reuse one source identity."""
        if len({source.id for source in sources}) != len(sources):
            raise ValueError("source IDs must be unique")
        return sources


def seed_missing_sources(session_factory: sessionmaker[Session], source_config_path: Path) -> int:
    """Create only missing source IDs from YAML; existing database rows remain authoritative."""
    configuration = _load_source_configuration(source_config_path)
    normalized_by_id = {
        source.id: _normalized_entry_url(source.entry_url) for source in configuration.sources
    }
    if len(set(normalized_by_id.values())) != len(normalized_by_id):
        raise ConfigurationError("source configuration contains duplicate normalized entry URLs")

    created_count = 0
    with UnitOfWork(session_factory) as unit:
        assert unit.session is not None
        repository = SourceRepository(unit.session)
        configured_urls = {source.normalized_entry_url: source.id for source in repository.list()}
        for source in configuration.sources:
            if repository.get(source.id) is not None:
                continue
            normalized_entry_url = normalized_by_id[source.id]
            owner = configured_urls.get(normalized_entry_url)
            if owner is not None:
                raise ConfigurationError(
                    "source configuration entry URL is already owned by configured source "
                    f"{owner!r}"
                )
            repository.create(
                id=source.id,
                name=source.name,
                kind=source.kind,
                entry_url=source.entry_url,
                normalized_entry_url=normalized_entry_url,
                enabled=source.enabled,
                priority=source.priority,
                language=source.language,
                config_json=_canonical_json(source.config),
                request_timeout_seconds=source.request_timeout_seconds,
                max_items_per_run=source.max_items_per_run,
            )
            configured_urls[normalized_entry_url] = source.id
            created_count += 1
    return created_count


def _load_source_configuration(path: Path) -> SourceConfiguration:
    """Read and validate a declared YAML source seed file without accepting arbitrary shape."""
    if not path.is_file():
        raise ConfigurationError(f"source configuration file does not exist: {path}")
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        return SourceConfiguration.model_validate(loaded)
    except (OSError, yaml.YAMLError, ValidationError, ValueError) as error:
        raise ConfigurationError(f"source configuration is invalid: {path}") from error


def _normalized_entry_url(entry_url: str) -> str:
    """Turn an invalid source URL into a startup configuration failure before persistence."""
    try:
        return normalize_url(entry_url)
    except ValueError as error:
        raise ConfigurationError("source configuration contains an invalid entry URL") from error


def _canonical_json(value: dict[str, Any]) -> str:
    """Store source collector options as valid deterministic JSON required by SQLite checks."""
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    except (TypeError, ValueError) as error:
        raise ConfigurationError(
            "source configuration contains non-JSON collector options"
        ) from error
