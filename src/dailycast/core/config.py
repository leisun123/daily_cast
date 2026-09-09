"""Pydantic Settings loader with YAML, .env, and environment overrides."""

import logging
import os
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from dotenv import dotenv_values
from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic.fields import FieldInfo
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

from dailycast.briefing.webhook import WebhookFormat
from dailycast.core.errors import ConfigurationError
from dailycast.news.source_windows import DEFAULT_SOURCE_MAX_AGE_HOURS

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "app.example.yaml"
ZEABUR_CONFIG_PATH = PROJECT_ROOT / "config" / "zeabur.yaml"
logger = logging.getLogger(__name__)
_yaml_path_context: ContextVar[Path] = ContextVar(
    "dailycast_yaml_path", default=DEFAULT_CONFIG_PATH
)
_env_file_context: ContextVar[Path | None] = ContextVar("dailycast_env_file_path", default=None)


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
    manual_trigger_token: str | None = Field(default=None, min_length=32, repr=False)
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


class BriefingSettings(BaseModel):
    """Independent daily text-briefing settings; disabled until a deployment opts in."""

    enabled: bool = False
    sources_config_path: Path = Path("config/briefing.sources.yaml")
    selection_policy_path: Path = Path("config/briefing.selection.yaml")
    # Generation finishes before the user-facing delivery time. The retry is
    # intentionally earlier than 08:30 so delivery never begins collection.
    preparation_cron_expression: str = "55 7 * * mon-fri"
    preparation_retry_cron_expression: str = "15 8 * * mon-fri"
    cron_expression: str = "30 8 * * mon-fri"
    window_hours: int = Field(default=24, ge=1, le=168)
    max_items_per_category: int = Field(default=6, ge=1, le=6)
    max_evidence_chars_per_article: int = Field(default=800, ge=1, le=8000)
    # `rsshub://` source routes are resolved through this deployment-controlled
    # HTTP(S) endpoint. Keep it unset unless the deployment has a known-good
    # RSSHub instance; regular RSS sources do not depend on it.
    rsshub_base_url: str | None = None
    # Instance-level RSSHub ACCESS_KEY appended to every resolved route. Keep it
    # in the deployment environment so the committed source seeds stay secret-free.
    rsshub_access_key: str | None = None
    webhook_enabled: bool = False
    webhook_url: str | None = None
    webhook_format: WebhookFormat = "wecom_markdown"

    @model_validator(mode="after")
    def require_webhook_url_when_webhook_enabled(self) -> "BriefingSettings":
        """A webhook push target without a URL must fail at configuration load."""
        if self.webhook_enabled and not self.webhook_url:
            msg = "briefing.webhook_url is required when briefing.webhook_enabled=true"
            raise ValueError(msg)
        return self


class MonitoringSettings(BaseModel):
    """Independent operational-alert delivery settings."""

    webhook_url: str | None = None
    webhook_format: WebhookFormat = "wecom_markdown_v2"


class WebResearchSettings(BaseModel):
    """Bound native web-search discovery for briefing-only sources."""

    enabled: bool = False
    # primary_responses reuses the Responses web_search tool when the primary
    # provider supports it; zhipu drives BigModel's verbatim-URL /web_search API
    # and filters the results with the primary GLM chat model.
    provider: Literal["primary_responses", "zhipu"] = "primary_responses"
    max_candidates_per_source: int = Field(default=20, ge=1, le=80)
    max_search_calls_per_source: int = Field(default=1, ge=1, le=4)
    search_context_size: Literal["low", "medium", "high"] = "medium"
    # Zhipu-style discovery only: upstream recency window for the search API.
    # The briefing window itself stays enforced by local date verification.
    search_recency_filter: Literal["oneDay", "oneWeek", "oneMonth", "oneYear", "noLimit"] = (
        "oneWeek"
    )
    max_article_chars: int = Field(default=12_000, ge=1_000, le=50_000)


class TaskExecutionSettings(BaseModel):
    """Bound the lifetime of one local pipeline request without adding a worker service."""

    deadline_seconds: int = Field(default=1800, ge=1, le=86_400)


class LLMBudgetSettings(BaseModel):
    """Hard per-task request and input limits, applied before cache-miss provider calls."""

    max_calls: int = Field(default=12, ge=0)
    max_input_tokens: int = Field(default=60_000, ge=0)
    max_output_tokens: int = Field(default=15_000, ge=0)


class LLMProviderSettings(BaseModel):
    """One direct model endpoint; credentials come only from .env or environment."""

    provider: str = "openai_compatible"
    base_url: str = "https://api.openai.com/v1"
    api_key: str | None = None
    model: str = "gpt-5.6-terra"
    temperature: float = Field(default=0.1, ge=0.0, le=2.0)
    top_p: float | None = Field(default=None, gt=0.0, le=1.0)
    timeout_seconds: float = Field(default=30.0, gt=0.0, le=300.0)
    max_retries: int = Field(default=2, ge=0, le=10)
    # The output budget belongs to the model: null (the default) sends no
    # cap, so reasoning models can split their allowance between thinking
    # and content. An external cap only truncates them to an empty answer.
    max_output_tokens: int | None = Field(default=None, ge=1)
    response_format: str = "json_schema"
    # Optional Zhipu-style reasoning switch; null sends nothing and keeps the
    # upstream default. "disabled" suits bounded editorial JSON steps where
    # deep reasoning only adds latency and token burn, not output quality.
    thinking: Literal["enabled", "disabled"] | None = None

    @field_validator("provider")
    @classmethod
    def require_supported_provider(cls, value: str) -> str:
        """Fail at configuration load instead of silently selecting an unsupported wire protocol."""
        supported = {"openai_compatible", "openai_responses"}
        if value not in supported:
            msg = f"llm.provider must be one of: {', '.join(sorted(supported))}"
            raise ValueError(msg)
        return value


class LLMSettings(LLMProviderSettings):
    """Preferred model endpoint plus an ordered multi-provider fallback chain."""

    provider: str = "openai_responses"
    model: str = "gpt-5.6-terra"
    # Tried in order after each provider-level failure of the providers before
    # it; every entry keeps its own wire protocol, response mode, and key.
    fallbacks: list[LLMProviderSettings] = []
    budget: LLMBudgetSettings = Field(default_factory=LLMBudgetSettings)

    @field_validator("fallbacks", mode="before")
    @classmethod
    def coerce_indexed_fallback_env_vars(cls, value: object) -> object:
        """Turn the numeric-key dict from indexed env vars into an ordered list.

        Environment sources collect DAILYCAST_LLM__FALLBACKS__<index>__FIELD
        pairs as a dict; YAML already provides a real list and passes through.
        """
        if (
            isinstance(value, dict)
            and value
            and all(isinstance(key, str) and key.isdigit() for key in value)
        ):
            return [value[key] for key in sorted(value, key=str)]
        return value


class EditorialSettings(BaseModel):
    """Explicit limits for ranking, bounded evidence, and outline generation."""

    enforce_quality_gate: bool = True
    max_candidates: int = Field(default=30, ge=1, le=30)
    max_selected_events: int = Field(default=8, ge=1, le=30)
    max_ai_events: int = Field(default=3, ge=1, le=30)
    min_domestic_events_when_available: int = Field(default=2, ge=0, le=30)
    min_recruitment_events_when_available: int = Field(default=1, ge=0, le=30)
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
    source_max_age_hours: dict[str, int] = Field(
        default_factory=lambda: dict(DEFAULT_SOURCE_MAX_AGE_HOURS)
    )
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


class RSSPublishingSettings(BaseModel):
    """Settings for the durable self-hosted RSS target."""

    enabled: bool = True


class NetEasePublishingSettings(BaseModel):
    """Settings for the optional persistent-browser NetEase distribution target."""

    enabled: bool = False
    profile_dir: Path = Path("netease/profile")
    headless: bool = True
    creator_url: str = "https://music.163.com/creatorcenter"
    cover_path: Path | None = None
    category: str = "资讯"


class XiaoyuzhouPublishingSettings(BaseModel):
    """Reserved configuration for a future RSS-based Xiaoyuzhou handoff."""

    enabled: bool = False


class PublishingSettings(BaseModel):
    """Self-hosted RSS plus independently enabled external distribution targets."""

    auto_publish: bool = False
    public_base_url: str = "http://127.0.0.1:8000"
    feed_title: str = "DailyCast"
    feed_description: str = "A personal AI news podcast."
    language: str = "zh-CN"
    author: str = "DailyCast"
    rss: RSSPublishingSettings = Field(default_factory=RSSPublishingSettings)
    netease: NetEasePublishingSettings = Field(default_factory=NetEasePublishingSettings)
    xiaoyuzhou: XiaoyuzhouPublishingSettings = Field(default_factory=XiaoyuzhouPublishingSettings)

    @model_validator(mode="after")
    def require_rss_for_external_distribution(self) -> "PublishingSettings":
        """External targets always upload the immutable asset first promoted by RSS."""
        if not self.rss.enabled and (self.netease.enabled or self.xiaoyuzhou.enabled):
            raise ValueError(
                "external distribution requires publishing.rss.enabled=true as the source of truth"
            )
        return self

    @field_validator("public_base_url")
    @classmethod
    def require_http_base_url(cls, value: str) -> str:
        """Require an explicit absolute HTTP(S) origin for RSS enclosure URLs."""
        normalized = value.rstrip("/")
        if not normalized.startswith(("http://", "https://")):
            raise ValueError("publishing.public_base_url must be an absolute HTTP(S) URL")
        return normalized


class LoggingSettings(BaseModel):
    """Console and optional rotating-file logging configuration."""

    level: str = "INFO"
    # Optional rotating JSON log file for deployments whose platform log-query
    # API is unavailable: the file survives restarts and is readable through
    # container exec. Relative paths resolve below DATA_DIR.
    file_path: Path | None = None
    max_bytes: int = Field(default=10 * 1024 * 1024, ge=1024)
    backup_count: int = Field(default=3, ge=0, le=20)


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


class Settings(BaseSettings):
    """Immutable application configuration after source precedence is resolved."""

    model_config = SettingsConfigDict(
        env_prefix="DAILYCAST_",
        env_nested_delimiter="__",
        env_file=".env",
        extra="forbid",
        frozen=True,
    )

    # DAILYCAST_CONFIG_PATH points the loader at the YAML file and is consumed before
    # Settings exists; this field only absorbs the variable so the dotenv and
    # environment sources do not fail the extra="forbid" validation.
    config_path: Path | None = None

    app: AppSettings = Field(default_factory=AppSettings)
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    storage: StorageSettings = Field(default_factory=StorageSettings)
    sources: SourcesSettings = Field(default_factory=SourcesSettings)
    scheduler: SchedulerSettings = Field(default_factory=SchedulerSettings)
    briefing: BriefingSettings = Field(default_factory=BriefingSettings)
    monitoring: MonitoringSettings = Field(default_factory=MonitoringSettings)
    web_research: WebResearchSettings = Field(default_factory=WebResearchSettings)
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
        return (
            init_settings,
            env_settings,
            DirectStoragePathSettingsSource(settings_cls),
            dotenv_settings,
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


_DEPRECATED_FALLBACK_ENV_VARS = (
    "DAILYCAST_LLM__FALLBACK__PROVIDER",
    "DAILYCAST_LLM__FALLBACK__BASE_URL",
    "DAILYCAST_LLM__FALLBACK__MODEL",
    "DAILYCAST_LLM__FALLBACK__API_KEY",
    "DAILYCAST_LLM__FALLBACK__RESPONSE_FORMAT",
)


def _warn_deprecated_fallback_env_vars(logger: Any) -> None:
    """Surface the singular-fallback variable rename instead of silently dropping keys.

    Deployments upgraded from the old interface keep their real provider key in
    DAILYCAST_LLM__FALLBACK__API_KEY; without this notice the fallback chain
    loses the endpoint with no error anywhere.
    """
    found = sorted(name for name in _DEPRECATED_FALLBACK_ENV_VARS if name in os.environ)
    if not found:
        return
    logger.warning(
        "deprecated LLM fallback environment variables detected and ignored: %s. "
        "Rename them to the indexed interface, e.g. DAILYCAST_LLM__FALLBACKS__0__API_KEY "
        "(see README 'ordered LLM configuration').",
        ", ".join(found),
    )


def load_settings(*, config_path: Path | None = None, env_file: Path | None = None) -> Settings:
    """Load settings with environment values overriding .env and YAML values."""
    yaml_path = _resolve_yaml_path(config_path, env_file)
    token = _yaml_path_context.set(yaml_path)
    resolved_env_file = env_file or (Path.cwd() / ".env")
    env_token = _env_file_context.set(resolved_env_file)
    try:
        settings = Settings(_env_file=resolved_env_file)
    except ConfigurationError:
        raise
    except ValueError as error:
        msg = "configuration values are invalid"
        raise ConfigurationError(msg) from error
    finally:
        _env_file_context.reset(env_token)
        _yaml_path_context.reset(token)
    _warn_deprecated_fallback_env_vars(logger)
    return settings
