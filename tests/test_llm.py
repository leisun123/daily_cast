"""Sprint 4A LLM provider, cache, budget, and prompt behavior tests."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from alembic import command
from pydantic import BaseModel, model_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from dailycast.core.config import LLMSettings, load_settings
from dailycast.core.errors import (
    AIBudgetExceededError,
    LLMProviderAuthenticationError,
    LLMProviderResponseError,
)
from dailycast.core.hashes import sha256_text
from dailycast.core.lifespan import build_llm_provider
from dailycast.db.models import LLMArtifact, LLMOperation, TaskRunStatus, TaskType, TriggerType
from dailycast.db.repositories import TaskRunRepository, TaskStepRepository
from dailycast.db.revision import build_alembic_config
from dailycast.db.session import create_session_factory, create_sqlite_engine
from dailycast.db.transactions import UnitOfWork
from dailycast.llm.artifacts import LLMArtifactService, LLMResponseValidationError
from dailycast.llm.budget import BudgetController
from dailycast.llm.contracts import LLMMessage, LLMProviderTimeoutError, LLMUsage, StructuredResult
from dailycast.llm.prompts.score_events_v1 import SCORE_EVENTS_V1
from dailycast.llm.providers.openai_compatible import OpenAICompatibleLLMProvider
from dailycast.llm.providers.openai_responses import OpenAIResponsesLLMProvider


class ScoreOutput(BaseModel):
    """Small schema used to prove local structured-output validation."""

    score: int


class SemanticallyBoundedScoreOutput(BaseModel):
    """Schema whose post-parse rule represents a non-repairable domain decision."""

    score: int

    @model_validator(mode="after")
    def score_must_be_in_editorial_range(self) -> SemanticallyBoundedScoreOutput:
        """Reject structurally valid but semantically unsafe scores."""
        if self.score > 10:
            raise ValueError("score exceeds the allowed editorial range")
        return self


class FakeLLMProvider:
    """In-memory provider with a controllable cache-identity marker."""

    provider_name = "fake"
    model = "fake-model"
    max_output_tokens = 10

    def __init__(
        self,
        output: Mapping[str, object],
        *,
        generation_marker: str = "v1",
    ) -> None:
        self._output = output
        self._generation_marker = generation_marker
        self.calls = 0

    def generation_config_hash(self, model_options: Mapping[str, object]) -> str:
        """Return a deterministic non-secret semantic configuration identity."""
        return sha256_text(f"{self._generation_marker}:{sorted(model_options.items())}")

    async def generate_structured(
        self,
        operation: LLMOperation,
        messages: tuple[LLMMessage, ...],
        response_schema: type[BaseModel],
        model_options: Mapping[str, object],
    ) -> StructuredResult:
        """Return a synthetic provider response without external I/O."""
        del operation, messages, response_schema, model_options
        self.calls += 1
        return StructuredResult(
            content=dict(self._output),
            model=self.model,
            usage=LLMUsage(input_tokens=2, output_tokens=3),
            request_id=f"fake-{self.calls}",
        )


class SequencedFakeLLMProvider:
    """Return controlled attempts so schema-repair behavior can be tested without a network."""

    provider_name = "fake"
    model = "fake-model"
    max_output_tokens = 10

    def __init__(self, outputs: Sequence[Mapping[str, object]]) -> None:
        self._outputs = list(outputs)
        self.calls = 0
        self.messages: list[tuple[LLMMessage, ...]] = []

    def generation_config_hash(self, model_options: Mapping[str, object]) -> str:
        """Return the same stable identity for both attempts of one semantic request."""
        del model_options
        return sha256_text("sequenced-fake-v1")

    async def generate_structured(
        self,
        operation: LLMOperation,
        messages: tuple[LLMMessage, ...],
        response_schema: type[BaseModel],
        model_options: Mapping[str, object],
    ) -> StructuredResult:
        """Return one configured response per call and retain messages for repair assertions."""
        del operation, response_schema, model_options
        self.calls += 1
        self.messages.append(messages)
        return StructuredResult(
            content=dict(self._outputs.pop(0)),
            model=self.model,
            usage=LLMUsage(input_tokens=2, output_tokens=3),
            request_id=f"sequenced-fake-{self.calls}",
        )


@pytest.fixture
def migrated_session_factory(app_config_path: Path) -> sessionmaker[Session]:
    """Build an isolated full V1 schema through the application migration path."""
    settings = load_settings(
        config_path=app_config_path,
        env_file=app_config_path.with_suffix(".env"),
    )
    command.upgrade(
        build_alembic_config(
            ini_path=Path(__file__).resolve().parents[1] / "alembic.ini",
            database_url=settings.database.url,
        ),
        "head",
    )
    engine = create_sqlite_engine(settings.database)
    factory = create_session_factory(engine)
    try:
        yield factory
    finally:
        engine.dispose()


def create_task_provenance(factory: sessionmaker[Session]) -> tuple[str, int]:
    """Create the non-null TaskRun and TaskStep provenance required by an artifact."""
    task_run_id = str(uuid4())
    with UnitOfWork(factory) as unit:
        assert unit.session is not None
        task_run = TaskRunRepository(unit.session).create(
            id=task_run_id,
            task_type=TaskType.DAILY_GENERATE,
            business_key=f"llm-test:{task_run_id}",
            idempotency_key=f"llm-test:{task_run_id}",
            trigger_type=TriggerType.MANUAL,
            status=TaskRunStatus.RUNNING,
            pipeline_version="test-v1",
            config_fingerprint="a" * 64,
            config_snapshot_json="{}",
            request_json="{}",
        )
        step = TaskStepRepository(unit.session).create(
            task_run_id=task_run.id,
            step_name="test_llm",
            step_order=1,
            attempt=1,
            status="running",
            details_json="{}",
        )
        return task_run.id, step.id


def artifact_count(factory: sessionmaker[Session]) -> int:
    """Read the number of durable successful structured results."""
    with UnitOfWork(factory) as unit:
        assert unit.session is not None
        count = unit.session.scalar(select(func.count()).select_from(LLMArtifact))
        assert count is not None
        return count


def request_kwargs(task_run_id: str, task_step_id: int) -> dict[str, object]:
    """Return a valid complete cache request for the test schema."""
    return {
        "operation": LLMOperation.SCORE_EVENTS,
        "messages": (LLMMessage(role="system", content="Return a score."),),
        "response_schema": ScoreOutput,
        "prompt_version": SCORE_EVENTS_V1.version,
        "schema_version": "score-output-v1",
        "model_options": {},
        "created_by_task_run_id": task_run_id,
        "created_by_task_step_id": task_step_id,
    }


def test_llm_settings_have_explicit_safe_defaults(app_config_path: Path) -> None:
    """LLM semantics and daily budget are configuration, never hidden constants."""
    settings = load_settings(
        config_path=app_config_path,
        env_file=app_config_path.with_suffix(".env"),
    )

    assert settings.llm.provider == "openai_compatible"
    assert settings.llm.temperature == 0.1
    assert settings.llm.max_output_tokens == 2000
    assert settings.llm.budget.max_calls == 12
    assert settings.llm.budget.max_input_tokens == 60_000
    assert settings.llm.budget.max_output_tokens == 15_000


def test_llm_settings_allow_only_supported_wire_protocols() -> None:
    """Provider selection is explicit because the supported wire protocols differ."""
    assert LLMSettings(provider="openai_responses").provider == "openai_responses"
    with pytest.raises(ValueError, match="llm.provider"):
        LLMSettings(provider="unknown")


def test_runtime_selects_responses_provider(app_config_path: Path) -> None:
    """Lifespan wiring selects the Responses adapter without changing editorial code."""

    async def scenario() -> None:
        settings = load_settings(config_path=app_config_path)
        settings.llm.provider = "openai_responses"
        async with httpx.AsyncClient() as client:
            provider = build_llm_provider(settings, http_client=client)
        assert isinstance(provider, OpenAIResponsesLLMProvider)

    asyncio.run(scenario())


def test_artifact_service_validates_and_persists_success(
    migrated_session_factory: sessionmaker[Session],
) -> None:
    """Only a locally schema-validated provider response becomes an LLMArtifact."""
    task_run_id, task_step_id = create_task_provenance(migrated_session_factory)
    provider = FakeLLMProvider({"score": 7})
    service = LLMArtifactService(migrated_session_factory, provider, BudgetController())

    result = asyncio.run(service.generate_structured(**request_kwargs(task_run_id, task_step_id)))

    assert result.content == {"score": 7}
    assert result.cache_hit is False
    assert provider.calls == 1
    assert artifact_count(migrated_session_factory) == 1


def test_schema_validation_failure_is_not_cached(
    migrated_session_factory: sessionmaker[Session],
) -> None:
    """An invalid structured response is returned as a safe error and leaves no cache row."""
    task_run_id, task_step_id = create_task_provenance(migrated_session_factory)
    service = LLMArtifactService(
        migrated_session_factory,
        FakeLLMProvider({"score": "not-an-integer"}),
        BudgetController(),
    )

    with pytest.raises(LLMResponseValidationError):
        asyncio.run(service.generate_structured(**request_kwargs(task_run_id, task_step_id)))

    assert artifact_count(migrated_session_factory) == 0


def test_schema_validation_failure_is_repaired_once_before_artifact_persistence(
    migrated_session_factory: sessionmaker[Session],
) -> None:
    """One invalid model object receives a bounded corrective retry before the task fails."""
    task_run_id, task_step_id = create_task_provenance(migrated_session_factory)
    provider = SequencedFakeLLMProvider(({"score": "not-an-integer"}, {"score": 8}))
    service = LLMArtifactService(migrated_session_factory, provider, BudgetController())

    result = asyncio.run(service.generate_structured(**request_kwargs(task_run_id, task_step_id)))

    assert result.content == {"score": 8}
    assert provider.calls == 2
    assert "corrected replacement JSON object" in provider.messages[1][-1].content
    assert artifact_count(migrated_session_factory) == 1


def test_semantic_schema_validation_failure_is_not_retried(
    migrated_session_factory: sessionmaker[Session],
) -> None:
    """A local domain-safety rejection must not prompt the provider to reinterpret facts."""
    task_run_id, task_step_id = create_task_provenance(migrated_session_factory)
    provider = SequencedFakeLLMProvider(({"score": 99}, {"score": 8}))
    service = LLMArtifactService(migrated_session_factory, provider, BudgetController())
    request = request_kwargs(task_run_id, task_step_id)
    request["response_schema"] = SemanticallyBoundedScoreOutput

    with pytest.raises(LLMResponseValidationError):
        asyncio.run(service.generate_structured(**request))

    assert provider.calls == 1
    assert artifact_count(migrated_session_factory) == 0


def test_schema_repair_retry_respects_remaining_budget(
    migrated_session_factory: sessionmaker[Session],
) -> None:
    """A corrective retry cannot exceed the same per-task LLM budget guardrail."""
    task_run_id, task_step_id = create_task_provenance(migrated_session_factory)
    provider = SequencedFakeLLMProvider(({"score": "not-an-integer"}, {"score": 8}))
    service = LLMArtifactService(
        migrated_session_factory,
        provider,
        BudgetController(max_calls=1, max_input_tokens=100, max_output_tokens=100),
    )

    with pytest.raises(AIBudgetExceededError):
        asyncio.run(service.generate_structured(**request_kwargs(task_run_id, task_step_id)))

    assert provider.calls == 1
    assert artifact_count(migrated_session_factory) == 0


def test_exact_cache_identity_returns_artifact_without_second_provider_call(
    migrated_session_factory: sessionmaker[Session],
) -> None:
    """A cache hit reuses a previous successful result across TaskRun provenance."""
    first_run_id, first_step_id = create_task_provenance(migrated_session_factory)
    second_run_id, second_step_id = create_task_provenance(migrated_session_factory)
    provider = FakeLLMProvider({"score": 7})
    service = LLMArtifactService(migrated_session_factory, provider, BudgetController())

    asyncio.run(service.generate_structured(**request_kwargs(first_run_id, first_step_id)))
    cached = asyncio.run(
        service.generate_structured(**request_kwargs(second_run_id, second_step_id))
    )

    assert cached.content == {"score": 7}
    assert cached.cache_hit is True
    assert provider.calls == 1
    assert artifact_count(migrated_session_factory) == 1


def test_prompt_or_generation_config_change_causes_cache_miss(
    migrated_session_factory: sessionmaker[Session],
) -> None:
    """Prompt versions and semantic model settings both belong to the cache identity."""
    task_run_id, task_step_id = create_task_provenance(migrated_session_factory)
    provider = FakeLLMProvider({"score": 7})
    service = LLMArtifactService(migrated_session_factory, provider, BudgetController())
    request = request_kwargs(task_run_id, task_step_id)

    asyncio.run(service.generate_structured(**request))
    asyncio.run(service.generate_structured(**{**request, "prompt_version": "score_events_v2"}))
    provider._generation_marker = "v2"
    asyncio.run(service.generate_structured(**request))

    assert provider.calls == 3
    assert artifact_count(migrated_session_factory) == 3


def test_budget_is_reserved_before_a_provider_call(
    migrated_session_factory: sessionmaker[Session],
) -> None:
    """A cache miss cannot invoke the provider after the configured call budget is spent."""
    first_run_id, first_step_id = create_task_provenance(migrated_session_factory)
    second_run_id, second_step_id = create_task_provenance(migrated_session_factory)
    provider = FakeLLMProvider({"score": 7})
    service = LLMArtifactService(
        migrated_session_factory,
        provider,
        BudgetController(max_calls=1, max_input_tokens=100, max_output_tokens=100),
    )

    asyncio.run(service.generate_structured(**request_kwargs(first_run_id, first_step_id)))
    second_request = request_kwargs(second_run_id, second_step_id)
    second_request["messages"] = (LLMMessage(role="system", content="Different input."),)
    with pytest.raises(AIBudgetExceededError):
        asyncio.run(service.generate_structured(**second_request))

    assert provider.calls == 1


def test_openai_compatible_provider_requests_json_schema_output() -> None:
    """The direct provider posts a strict JSON schema and maps its success response."""
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["request"] = request
        return httpx.Response(
            200,
            json={
                "id": "provider-request",
                "choices": [{"message": {"content": '{"score":7}'}}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2},
            },
        )

    async def scenario() -> StructuredResult:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = OpenAICompatibleLLMProvider(
                base_url="https://models.example/v1",
                api_key="test-key",
                model="test-model",
                timeout_seconds=2,
                temperature=0.1,
                max_output_tokens=30,
                http_client=client,
            )
            return await provider.generate_structured(
                LLMOperation.SCORE_EVENTS,
                (LLMMessage(role="user", content="Score this."),),
                ScoreOutput,
                {},
            )

    result = asyncio.run(scenario())
    request = captured["request"]
    assert isinstance(request, httpx.Request)
    assert request.url == "https://models.example/v1/chat/completions"
    assert json.loads(request.content)["response_format"]["type"] == "json_schema"
    assert result.content == {"score": 7}
    assert result.usage == LLMUsage(input_tokens=3, output_tokens=2)
    assert result.request_id == "provider-request"


def test_openai_responses_provider_requests_json_schema_and_reads_output_text() -> None:
    """Responses requests use input/text.format and ignore non-message output items."""
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["request"] = request
        return httpx.Response(
            200,
            json={
                "id": "response-request",
                "output": [
                    {"type": "reasoning", "content": []},
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": '{"score":7}'}],
                    },
                ],
                "usage": {"input_tokens": 4, "output_tokens": 2},
            },
        )

    async def scenario() -> StructuredResult:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = OpenAIResponsesLLMProvider(
                base_url="https://models.example/v1",
                api_key="test-key",
                model="test-model",
                timeout_seconds=2,
                temperature=0.1,
                max_output_tokens=30,
                http_client=client,
            )
            return await provider.generate_structured(
                LLMOperation.SCORE_EVENTS,
                (
                    LLMMessage(role="system", content="Return JSON."),
                    LLMMessage(role="user", content="Score this."),
                ),
                ScoreOutput,
                {},
            )

    result = asyncio.run(scenario())
    request = captured["request"]
    assert isinstance(request, httpx.Request)
    assert request.url == "https://models.example/v1/responses"
    payload = json.loads(request.content)
    assert payload["input"][0]["role"] == "developer"
    assert payload["input"][0]["content"] == [{"type": "input_text", "text": "Return JSON."}]
    assert payload["text"]["format"]["type"] == "json_schema"
    assert result.content == {"score": 7}
    assert result.usage == LLMUsage(input_tokens=4, output_tokens=2)
    assert result.request_id == "response-request"


def test_openai_responses_provider_rejects_missing_output_text() -> None:
    """A nominal success response cannot bypass local structured-output validation."""

    async def scenario() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"output": []}))
        ) as client:
            provider = OpenAIResponsesLLMProvider(
                base_url="https://models.example",
                api_key="test-key",
                model="test-model",
                timeout_seconds=2,
                temperature=0.1,
                max_output_tokens=30,
                http_client=client,
            )
            with pytest.raises(LLMProviderResponseError):
                await provider.generate_structured(
                    LLMOperation.SCORE_EVENTS,
                    (LLMMessage(role="user", content="Score this."),),
                    ScoreOutput,
                    {},
                )

    asyncio.run(scenario())


def test_openai_responses_json_object_embeds_schema_contract_and_changes_cache_identity() -> None:
    """A JSON-only Responses gateway receives the local schema as a developer instruction."""
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["request"] = request
        return httpx.Response(
            200,
            json={
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": '{"score":7}'}],
                    }
                ]
            },
        )

    async def scenario() -> tuple[StructuredResult, str, str]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = OpenAIResponsesLLMProvider(
                base_url="https://models.example",
                api_key="test-key",
                model="test-model",
                timeout_seconds=2,
                temperature=0.1,
                max_output_tokens=30,
                response_format="json_object",
                http_client=client,
            )
            result = await provider.generate_structured(
                LLMOperation.SCORE_EVENTS,
                (LLMMessage(role="user", content="Score this."),),
                ScoreOutput,
                {},
            )
            return (
                result,
                provider.generation_config_hash({}),
                provider.generation_config_hash({"response_format": "json_schema"}),
            )

    result, json_object_hash, json_schema_hash = asyncio.run(scenario())

    request = captured["request"]
    assert isinstance(request, httpx.Request)
    payload = json.loads(request.content)
    contract = payload["input"][0]["content"][0]["text"]
    assert "Structured output contract" in contract
    assert '"score"' in contract
    assert json_object_hash != json_schema_hash
    assert result.content == {"score": 7}


def test_openai_responses_falls_back_to_json_object_when_native_schema_fails() -> None:
    """A flaky gateway cannot prevent a compatible JSON-object request from completing."""
    formats: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        response_format = payload["text"]["format"]["type"]
        formats.append(response_format)
        if response_format == "json_schema":
            return httpx.Response(502)
        contract = payload["input"][0]["content"][0]["text"]
        assert "Structured output contract" in contract
        return httpx.Response(
            200,
            json={
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": '{"score":7}'}],
                    }
                ]
            },
        )

    async def scenario() -> StructuredResult:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = OpenAIResponsesLLMProvider(
                base_url="https://models.example",
                api_key="test-key",
                model="test-model",
                timeout_seconds=2,
                temperature=0.1,
                max_output_tokens=30,
                max_retries=0,
                http_client=client,
            )
            return await provider.generate_structured(
                LLMOperation.SCORE_EVENTS,
                (LLMMessage(role="system", content="Return JSON."),),
                ScoreOutput,
                {},
            )

    result = asyncio.run(scenario())

    assert formats == ["json_schema", "json_object"]
    assert result.content == {"score": 7}


def test_openai_provider_timeout_and_authentication_error_are_mapped() -> None:
    """Transport failures map to stable safe error classes without provider response bodies."""

    async def timeout_scenario() -> None:
        def timeout_handler(request: httpx.Request) -> httpx.Response:
            del request
            raise httpx.ReadTimeout("socket timed out")

        async with httpx.AsyncClient(transport=httpx.MockTransport(timeout_handler)) as client:
            provider = OpenAICompatibleLLMProvider(
                base_url="https://models.example/v1",
                api_key="test-key",
                model="test-model",
                timeout_seconds=2,
                temperature=0.1,
                max_output_tokens=30,
                max_retries=0,
                http_client=client,
            )
            with pytest.raises(LLMProviderTimeoutError):
                await provider.generate_structured(
                    LLMOperation.SCORE_EVENTS,
                    (LLMMessage(role="user", content="Score this."),),
                    ScoreOutput,
                    {},
                )

    async def auth_scenario() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(401))
        ) as client:
            provider = OpenAICompatibleLLMProvider(
                base_url="https://models.example/v1",
                api_key="test-key",
                model="test-model",
                timeout_seconds=2,
                temperature=0.1,
                max_output_tokens=30,
                max_retries=0,
                http_client=client,
            )
            with pytest.raises(LLMProviderAuthenticationError) as error:
                await provider.generate_structured(
                    LLMOperation.SCORE_EVENTS,
                    (LLMMessage(role="user", content="Score this."),),
                    ScoreOutput,
                    {},
                )
        assert error.value.code == "AI_PROVIDER_AUTHENTICATION_FAILED"

    asyncio.run(timeout_scenario())
    asyncio.run(auth_scenario())


def test_provider_generation_identity_uses_only_semantic_non_secret_settings() -> None:
    """Prove identity changes for semantics but not keys or retry transport settings."""

    async def scenario() -> None:
        async with httpx.AsyncClient() as client:
            baseline = OpenAICompatibleLLMProvider(
                base_url="https://models.example/v1",
                api_key="first-key",
                model="test-model",
                timeout_seconds=2,
                temperature=0.1,
                max_output_tokens=30,
                max_retries=0,
                http_client=client,
            )
            transport_only_change = OpenAICompatibleLLMProvider(
                base_url="https://models.example/v1",
                api_key="second-key",
                model="test-model",
                timeout_seconds=9,
                temperature=0.1,
                max_output_tokens=30,
                max_retries=4,
                http_client=client,
            )
            endpoint_change = OpenAICompatibleLLMProvider(
                base_url="https://models.example/v2",
                api_key="first-key",
                model="test-model",
                timeout_seconds=2,
                temperature=0.1,
                max_output_tokens=30,
                max_retries=0,
                http_client=client,
            )
            base_identity = baseline.generation_config_hash({})
            assert base_identity == transport_only_change.generation_config_hash({})
            assert base_identity != endpoint_change.generation_config_hash({})
            assert base_identity != baseline.generation_config_hash({"temperature": 0.2})
            assert base_identity != baseline.generation_config_hash({"top_p": 0.4})
            assert base_identity != baseline.generation_config_hash({"max_output_tokens": 40})
            assert base_identity != baseline.generation_config_hash(
                {"response_format": "json_object"}
            )

    asyncio.run(scenario())
