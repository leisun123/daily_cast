"""Pydantic Settings loader with YAML, .env, and environment overrides."""

import os
from collections.abc import Mapping
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from dotenv import dotenv_values
from pydantic import BaseModel, Field, field_validator
from pydantic.fields import FieldInfo
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

from dailycast.core.errors import ConfigurationError

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "app.example.yaml"
ZEABUR_CONFIG_PATH = PROJECT_ROOT / "config" / "zeabur.yaml"
_yaml_path_context: ContextVar[Path] = ContextVar(
    "dailycast_yaml_path", default=DEFAULT_CONFIG_PATH
)
_env_file_context: ContextVar[Path | None] = ContextVar("dailycast_env_file_path", default=None)

_LLM_ENVIRONMENT_NAMES = {
    "LLM_PROVIDER": "provider",
    "LLM_BASE_URL": "base_url",
    "LLM_MODEL": "model",
    "LLM_API_KEY": "api_key",
}
_CANONICAL_LLM_DOTENV_KEYS = frozenset(
    f"llm_{field_name}" for field_name in _LLM_ENVIRONMENT_NAMES.values()
)


class ServerSettings(BaseModel):
    """HTTP bind settings."""

    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)


class AppSettings(BaseModel):
    """Application identity and local server settings."""

    name: str = "DailyCast"
    environment: str = "development"
    timezone: str = "Asia/Shanghai"
    public_only: bool = False
    server: ServerSettings = Field(default_factory=ServerSettings)

    @field_validator("timezone")
    @classmethod
    def require_iana_timezone(cls, value: str) -> str:
        """Reject an invalid timezone before it can change the daily-edition business date."""
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as error:
            msg = "app.timezone must be a valid IANA timezone"
            raise ValueError(msg) from error
        return value


class DatabaseSettings(BaseModel):
    """SQLite connection configuration."""

    url: str = "sqlite:///./data/dailycast.db"
    echo: bool = False

    @field_validator("url")
    @classmethod
    def require_sqlite(cls, value: str) -> str:
        """Sprint 0 supports only SQLite, as defined by the approved architecture."""
        if not value.startswith("sqlite"):
            msg = "database.url must use a sqlite URL"
            raise ValueError(msg)
        return value


class StorageSettings(BaseModel):
    """Runtime directories kept outside package source code."""

    data_dir: Path = Path("data")
    public_dir: Path = Path("public")


class SourcesSettings(BaseModel):
    """The seed-only YAML file used to create missing Source rows on first startup."""

    config_path: Path = Path("config/sources.example.yaml")


class SchedulerSettings(BaseModel):
    """Local APScheduler submission settings; disabled until a user opts in."""

    enabled: bool = False
    cron_expression: str = "0 8 * * *"


class TaskExecutionSettings(BaseModel):
    """Bound the lifetime of one local pipeline request without adding a worker service."""

    deadline_seconds: int = Field(default=1800, ge=1, le=86_400)


class LLMBudgetSettings(BaseModel):
    """Hard per-task LLM use limits, applied before cache-miss provider calls."""

    max_calls: int = Field(default=12, ge=0)
    max_input_tokens: int = Field(default=60_000, ge=0)
    max_output_tokens: int = Field(default=15_000, ge=0)


class LLMSettings(BaseModel):
    """Direct model-provider settings; the key comes only from .env or environment."""

    provider: str = "openai_compatible"
    base_url: str = "https://api.openai.com/v1"
    api_key: str | None = None
    model: str = "gpt-5.6-terra"
    temperature: float = Field(default=0.1, ge=0.0, le=2.0)
    top_p: float | None = Field(default=None, gt=0.0, le=1.0)
    max_output_tokens: int = Field(default=2000, ge=1)
    timeout_seconds: float = Field(default=30.0, gt=0.0, le=300.0)
    max_retries: int = Field(default=2, ge=0, le=10)
    response_format: str = "json_schema"
    budget: LLMBudgetSettings = Field(default_factory=LLMBudgetSettings)

    @field_validator("provider")
    @classmethod
    def require_supported_provider(cls, value: str) -> str:
        """Fail at configuration load instead of silently selecting an unsupported wire protocol."""
        supported = {"openai_compatible", "openai_responses"}
        if value not in supported:
            msg = f"llm.provider must be one of: {', '.join(sorted(supported))}"
            raise ValueError(msg)
        return value


class EditorialSettings(BaseModel):
    """Explicit limits for ranking, bounded evidence, and outline generation."""

    enforce_quality_gate: bool = True
    max_candidates: int = Field(default=30, ge=1, le=30)
    max_selected_events: int = Field(default=8, ge=1, le=30)
    max_sources_per_event: int = Field(default=3, ge=1, le=3)
    max_chars_per_source: int = Field(default=1200, ge=1, le=1200)
    max_total_evidence_chars: int = Field(default=24_000, ge=1, le=240_000)
    min_publishable_events: int = Field(default=1, ge=1, le=30)
    target_duration_seconds: int = Field(default=900, ge=60, le=7200)
    outline_duration_tolerance_seconds: int = Field(default=60, ge=0, le=600)
    max_outline_sections: int = Field(default=12, ge=3, le=12)
    estimated_chars_per_second: float = Field(default=4.0, gt=0.0, le=100.0)
    script_duration_tolerance_ratio: float = Field(default=0.20, ge=0.0, lt=1.0)
    max_script_chars: int = Field(default=12_000, ge=1, le=12_000)
    max_section_chars: int = Field(default=2_400, ge=1, le=2_400)
    max_automatic_script_revisions: int = Field(default=1, ge=0, le=1)


class ProcessingSettings(BaseModel):
    """Deterministic Article-to-NewsEvent processing limits."""

    max_age_hours: int = Field(default=36, ge=1, le=720)
    min_content_length: int = Field(default=300, ge=1, le=100_000)
    similarity_threshold: float = Field(default=0.58, ge=0.0, le=1.0)


class TTSSettings(BaseModel):
    """Provider-neutral draft-audio settings without credentials."""

    provider: str = "edge_tts"
    voice: str = "zh-CN-XiaoxiaoNeural"
    speed: float = Field(default=1.0, gt=0.0, le=2.0)
    format: str = "mp3"
    text_mode: Literal["plain", "enhanced_text"] = "enhanced_text"
    pronunciation_dictionary_path: Path = Path("config/pronunciation.yaml")
    opening_summary_speed: float = Field(default=0.94, gt=0.0, le=2.0)
    closing_summary_speed: float = Field(default=0.94, gt=0.0, le=2.0)
    timeout_seconds: float = Field(default=30.0, gt=0.0, le=300.0)
    max_retries: int = Field(default=2, ge=0, le=10)
    cache_enabled: bool = True


class FFmpegSettings(BaseModel):
    """Output-normalization settings used by the FFmpeg draft-audio merger."""

    sample_rate: int = Field(default=24_000, ge=8_000, le=96_000)
    bitrate: str = "64k"


class PublishingSettings(BaseModel):
    """Self-hosted RSS publication configuration without changing management API exposure."""

    auto_publish: bool = False
    public_base_url: str = "http://127.0.0.1:8000"
    feed_title: str = "DailyCast"
    feed_description: str = "A personal AI news podcast."
    language: str = "zh-CN"
    author: str = "DailyCast"

    @field_validator("public_base_url")
    @classmethod
    def require_http_base_url(cls, value: str) -> str:
        """Require an explicit absolute HTTP(S) origin for RSS enclosure URLs."""
        normalized = value.rstrip("/")
        if not normalized.startswith(("http://", "https://")):
            raise ValueError("publishing.public_base_url must be an absolute HTTP(S) URL")
        return normalized


class LoggingSettings(BaseModel):
    """Console logging configuration."""

    level: str = "INFO"


class YamlSettingsSource(PydanticBaseSettingsSource):
    """Load the lowest-precedence settings layer from a validated YAML mapping."""

    def __init__(self, settings_cls: type[BaseSettings], yaml_path: Path) -> None:
        super().__init__(settings_cls)
        self._yaml_path = yaml_path
        self._values = self._load()

    def _load(self) -> dict[str, Any]:
        if not self._yaml_path.is_file():
            msg = f"configuration file does not exist: {self._yaml_path}"
            raise ConfigurationError(msg)
        try:
            loaded = yaml.safe_load(self._yaml_path.read_text(encoding="utf-8"))
        except yaml.YAMLError as error:
            msg = f"configuration YAML is invalid: {self._yaml_path}"
            raise ConfigurationError(msg) from error
        if loaded is None:
            return {}
        if not isinstance(loaded, dict):
            msg = "configuration YAML root must be a mapping"
            raise ConfigurationError(msg)
        return loaded

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:
        """Return a raw YAML value for Pydantic's normal field preparation."""
        del field
        return self._values.get(field_name), field_name, False

    def __call__(self) -> dict[str, Any]:
        """Provide all YAML values as a settings source."""
        return self._values


class DirectStoragePathSettingsSource(PydanticBaseSettingsSource):
    """Map documented DATA_DIR and PUBLIC_DIR values into the nested storage settings."""

    def __init__(self, settings_cls: type[BaseSettings]) -> None:
        super().__init__(settings_cls)
        dotenv_path = _env_file_context.get()
        dotenv_values_map = dotenv_values(dotenv_path) if dotenv_path is not None else {}
        storage: dict[str, str] = {}
        for environment_name, field_name in (
            ("DATA_DIR", "data_dir"),
            ("PUBLIC_DIR", "public_dir"),
        ):
            raw_value = os.environ.get(environment_name, dotenv_values_map.get(environment_name))
            if raw_value is not None:
                storage[field_name] = raw_value
        self._values: dict[str, Any] = {"storage": storage} if storage else {}

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:
        """Return direct storage overrides through Pydantic normal field preparation."""
        del field
        return self._values.get(field_name), field_name, False

    def __call__(self) -> dict[str, Any]:
        """Provide only documented direct storage-path overrides."""
        return self._values


class DailyCastWithoutLLMSettingsSource(PydanticBaseSettingsSource):
    """Preserve DAILYCAST_* settings while reserving LLM configuration for LLM_* names."""

    def __init__(
        self,
        settings_cls: type[BaseSettings],
        delegate: PydanticBaseSettingsSource,
    ) -> None:
        super().__init__(settings_cls)
        self._delegate = delegate

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:
        """Delegate normal Pydantic field loading to the wrapped source."""
        return self._delegate.get_field_value(field, field_name)

    def __call__(self) -> dict[str, Any]:
        """Remove every nested `llm` value from the DAILYCAST_* source."""
        values = self._delegate()
        sanitized_values = dict(values)
        sanitized_values.pop("llm", None)
        for key in _CANONICAL_LLM_DOTENV_KEYS:
            sanitized_values.pop(key, None)
        return sanitized_values


class CanonicalLLMSettingsSource(PydanticBaseSettingsSource):
    """Load the only supported model-provider environment interface: LLM_*."""

    def __init__(
        self,
        settings_cls: type[BaseSettings],
        values: Mapping[str, str | None],
    ) -> None:
        super().__init__(settings_cls)
        llm_values = {
            setting_name: raw_value
            for environment_name, setting_name in _LLM_ENVIRONMENT_NAMES.items()
            if (raw_value := values.get(environment_name)) is not None
        }
        self._values: dict[str, Any] = {"llm": llm_values} if llm_values else {}

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:
        """Return legacy values through Pydantic's normal field preparation."""
        del field
        return self._values.get(field_name), field_name, False

    def __call__(self) -> dict[str, Any]:
        """Provide canonical model settings at normal environment precedence."""
        return self._values


class Settings(BaseSettings):
    """Immutable application configuration after source precedence is resolved."""

    model_config = SettingsConfigDict(
        env_prefix="DAILYCAST_",
        env_nested_delimiter="__",
        env_file=".env",
        extra="forbid",
        frozen=True,
    )

    app: AppSettings = Field(default_factory=AppSettings)
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    storage: StorageSettings = Field(default_factory=StorageSettings)
    sources: SourcesSettings = Field(default_factory=SourcesSettings)
    scheduler: SchedulerSettings = Field(default_factory=SchedulerSettings)
    task_execution: TaskExecutionSettings = Field(default_factory=TaskExecutionSettings)
    llm: LLMSettings = Field(default_factory=LLMSettings)
    editorial: EditorialSettings = Field(default_factory=EditorialSettings)
    processing: ProcessingSettings = Field(default_factory=ProcessingSettings)
    tts: TTSSettings = Field(default_factory=TTSSettings)
    ffmpeg: FFmpegSettings = Field(default_factory=FFmpegSettings)
    publishing: PublishingSettings = Field(default_factory=PublishingSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Apply explicit values, environment, .env, then YAML defaults in that order."""
        dotenv_path = _env_file_context.get()
        dotenv_values_map = dotenv_values(dotenv_path) if dotenv_path is not None else {}
        return (
            init_settings,
            DailyCastWithoutLLMSettingsSource(settings_cls, env_settings),
            CanonicalLLMSettingsSource(settings_cls, os.environ),
            DirectStoragePathSettingsSource(settings_cls),
            DailyCastWithoutLLMSettingsSource(settings_cls, dotenv_settings),
            CanonicalLLMSettingsSource(settings_cls, dotenv_values_map),
            YamlSettingsSource(settings_cls, _yaml_path_context.get()),
            file_secret_settings,
        )

    def resolve_path(self, path: Path) -> Path:
        """Resolve a configured path relative to the process working directory."""
        return path if path.is_absolute() else (Path.cwd() / path).resolve()

    @property
    def data_dir(self) -> Path:
        """Return the resolved private runtime data directory."""
        return self.resolve_path(self.storage.data_dir)

    @property
    def public_dir(self) -> Path:
        """Return the resolved public runtime directory."""
        return self.resolve_path(self.storage.public_dir)


def _resolve_yaml_path(config_path: Path | None, env_file: Path | None) -> Path:
    if config_path is not None:
        return config_path.resolve()
    dotenv_path = env_file or (Path.cwd() / ".env")
    dotenv_config_path = dotenv_values(dotenv_path).get("DAILYCAST_CONFIG_PATH")
    configured_path = os.environ.get("DAILYCAST_CONFIG_PATH", dotenv_config_path)
    if os.environ.get("ZEABUR_WEB_URL") and configured_path in {
        None,
        "/app/config/app.example.yaml",
    }:
        return ZEABUR_CONFIG_PATH
    if configured_path is None:
        return DEFAULT_CONFIG_PATH
    path = Path(configured_path)
    return path if path.is_absolute() else (Path.cwd() / path).resolve()


def load_settings(*, config_path: Path | None = None, env_file: Path | None = None) -> Settings:
    """Load settings with environment values overriding .env and YAML values."""
    yaml_path = _resolve_yaml_path(config_path, env_file)
    token = _yaml_path_context.set(yaml_path)
    resolved_env_file = env_file or (Path.cwd() / ".env")
    env_token = _env_file_context.set(resolved_env_file)
    try:
        return Settings(_env_file=resolved_env_file)
    except ConfigurationError:
        raise
    except ValueError as error:
        msg = "configuration values are invalid"
        raise ConfigurationError(msg) from error
    finally:
        _env_file_context.reset(env_token)
        _yaml_path_context.reset(token)
