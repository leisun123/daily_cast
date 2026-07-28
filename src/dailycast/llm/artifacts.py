"""Schema-validated, exact-identity LLMArtifact cache integration."""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import cast

from pydantic import BaseModel, ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from dailycast.core.errors import (
    DailyCastError,
    LLMProviderError,
    LLMStructuredOutputUnsupportedError,
)
from dailycast.core.hashes import sha256_text
from dailycast.db.models import LLMArtifact, LLMOperation
from dailycast.db.repositories import LLMArtifactRepository
from dailycast.db.transactions import UnitOfWork
from dailycast.llm.budget import BudgetController, estimate_message_input_tokens
from dailycast.llm.contracts import JSONValue, LLMMessage, LLMProvider, LLMUsage, StructuredResult

logger = logging.getLogger(__name__)


class LLMResponseValidationError(DailyCastError):
    """Raised when a provider response cannot satisfy the local output schema."""

    def __init__(self, *, repairable: bool = False) -> None:
        super().__init__(
            code="AI_RESPONSE_SCHEMA_INVALID",
            message="LLM response did not satisfy the required structured schema",
            status_code=502,
        )
        self.repairable = repairable


class LLMArtifactCorruptError(DailyCastError):
    """Raised if a legacy or manually corrupted cache row no longer validates locally."""

    def __init__(self) -> None:
        super().__init__(
            code="AI_ARTIFACT_INVALID",
            message="cached LLM artifact failed local schema validation",
            status_code=500,
        )


class LLMArtifactPersistenceError(DailyCastError):
    """Raised when a validated result cannot be durably associated with its TaskStep."""

    def __init__(self) -> None:
        super().__init__(
            code="AI_ARTIFACT_PERSISTENCE_FAILED",
            message="validated LLM result could not be saved with its task provenance",
            status_code=500,
        )


@dataclass(frozen=True, slots=True)
class LLMCacheIdentity:
    """The approved seven-field durable identity for a reusable successful result."""

    operation: LLMOperation
    provider: str
    model: str
    prompt_version: str
    schema_version: str
    generation_config_hash: str
    input_hash: str

    def as_repository_values(self) -> dict[str, object]:
        """Return the seven cache columns expected by LLMArtifactRepository."""
        return asdict(self)


def _canonical_json(value: object) -> str:
    """Encode bounded structured data with a stable representation before hashing or storage."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _input_hash(messages: Sequence[LLMMessage]) -> str:
    """Hash message identity without retaining prompt text in the Artifact table."""
    normalized_messages = [
        {"content": message.content, "role": message.role} for message in messages
    ]
    return sha256_text(_canonical_json(normalized_messages))


def _schema_repair_messages(messages: Sequence[LLMMessage]) -> tuple[LLMMessage, ...]:
    """Request one corrected replacement without exposing validation internals or prior output."""
    return (
        *messages,
        LLMMessage(
            role="user",
            content=(
                "Your previous output did not satisfy the required structured schema. Return one "
                "corrected replacement JSON object only, with every required field valid. Do not "
                "include Markdown, explanations, or additional top-level keys."
            ),
        ),
    )


def _json_object_fallback_messages(
    messages: Sequence[LLMMessage], response_schema: type[BaseModel]
) -> tuple[LLMMessage, ...]:
    """Mirror the Responses JSON-object contract so fallback budget reservation is conservative."""
    schema_json = _canonical_json(response_schema.model_json_schema())
    contract = (
        "Structured output contract: return exactly one JSON object that conforms to this JSON "
        "Schema. Do not use Markdown, explanations, or additional top-level keys. JSON Schema: "
        f"{schema_json}"
    )
    enriched: list[LLMMessage] = []
    attached = False
    for message in messages:
        if message.role == "system" and not attached:
            enriched.append(LLMMessage(role="system", content=f"{message.content}\n\n{contract}"))
            attached = True
        else:
            enriched.append(message)
    if not attached:
        enriched.insert(0, LLMMessage(role="system", content=contract))
    return tuple(enriched)


def _is_repairable_schema_error(error: ValidationError) -> bool:
    """Allow one retry only for JSON-shape errors, never for local semantic safeguards."""
    semantic_error_types = {"assertion_error", "value_error"}
    return any(str(item.get("type", "")) not in semantic_error_types for item in error.errors())


def _validated_content(
    response_schema: type[BaseModel],
    content: Mapping[str, JSONValue],
    *,
    validation_context: Mapping[str, object] | None = None,
) -> dict[str, JSONValue]:
    """Validate one payload locally and return only its JSON-mode normalized representation."""
    try:
        parsed = response_schema.model_validate(content, context=validation_context)
    except ValidationError as error:
        raise LLMResponseValidationError(repairable=_is_repairable_schema_error(error)) from error
    dumped = parsed.model_dump(mode="json")
    if not isinstance(dumped, dict):
        raise LLMResponseValidationError()
    return cast(dict[str, JSONValue], dumped)


class LLMArtifactService:
    """Read exact cache hits or safely create one durable validated structured artifact."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        provider: LLMProvider,
        budget: BudgetController,
    ) -> None:
        self._session_factory = session_factory
        self._provider = provider
        self._budget = budget

    async def generate_structured(
        self,
        *,
        operation: LLMOperation,
        messages: Sequence[LLMMessage],
        response_schema: type[BaseModel],
        prompt_version: str,
        schema_version: str,
        model_options: Mapping[str, JSONValue],
        created_by_task_run_id: str,
        created_by_task_step_id: int,
        validation_context: Mapping[str, object] | None = None,
    ) -> StructuredResult:
        """Reuse a prior exact successful result or make and persist one bounded model call."""
        for candidate_provider in self._ordered_providers():
            candidate_identity = self._cache_identity(
                candidate_provider,
                operation=operation,
                messages=messages,
                prompt_version=prompt_version,
                schema_version=schema_version,
                model_options=model_options,
            )
            cached = self._get_by_identity(candidate_identity)
            if cached is not None:
                return self._cached_result(
                    cached, response_schema, validation_context=validation_context
                )

        effective_options = dict(model_options)
        generated, usage, provider_call_count, selected_provider = (
            await self._generate_with_fallback(
                operation=operation,
                messages=messages,
                response_schema=response_schema,
                model_options=effective_options,
            )
        )
        try:
            content = _validated_content(
                response_schema, generated.content, validation_context=validation_context
            )
        except LLMResponseValidationError as error:
            if not error.repairable:
                raise
            repair_messages = _schema_repair_messages(messages)
            generated, repair_usage, repair_calls, selected_provider = (
                await self._generate_with_fallback(
                    operation=operation,
                    messages=repair_messages,
                    response_schema=response_schema,
                    model_options=effective_options,
                )
            )
            usage = _add_usage(usage, repair_usage)
            provider_call_count += repair_calls
            content = _validated_content(
                response_schema, generated.content, validation_context=validation_context
            )
        identity = self._cache_identity(
            selected_provider,
            operation=operation,
            messages=messages,
            prompt_version=prompt_version,
            schema_version=schema_version,
            model_options=effective_options,
        )
        output_json = _canonical_json(content)
        artifact, concurrent_cache_hit = self._insert_or_reuse(
            identity=identity,
            output_json=output_json,
            output_hash=sha256_text(output_json),
            usage=usage,
            provider_request_id=generated.request_id,
            created_by_task_run_id=created_by_task_run_id,
            created_by_task_step_id=created_by_task_step_id,
        )
        return StructuredResult(
            content=content,
            model=artifact.model,
            usage=usage,
            request_id=generated.request_id,
            cache_hit=concurrent_cache_hit,
            artifact_id=artifact.id,
            provider_call_count=provider_call_count if not concurrent_cache_hit else 0,
        )

    async def _generate_with_fallback(
        self,
        *,
        operation: LLMOperation,
        messages: Sequence[LLMMessage],
        response_schema: type[BaseModel],
        model_options: Mapping[str, JSONValue],
    ) -> tuple[StructuredResult, LLMUsage, int, LLMProvider]:
        """Budget every attempt, preferring one provider before trying its configured fallback."""
        provider_call_count = 0
        last_provider_error: LLMProviderError | None = None
        for provider in self._ordered_providers():
            try:
                self._reserve_for_call(provider, messages, model_options)
                provider_call_count += 1
                try:
                    generated = await provider.generate_structured(
                        operation, messages, response_schema, model_options
                    )
                except LLMStructuredOutputUnsupportedError:
                    requested_format = model_options.get("response_format", "json_schema")
                    if requested_format != "json_schema" or not getattr(
                        provider, "supports_json_object_fallback", False
                    ):
                        raise
                    fallback_options = {**model_options, "response_format": "json_object"}
                    fallback_messages = _json_object_fallback_messages(
                        messages, response_schema
                    )
                    self._reserve_for_call(provider, fallback_messages, fallback_options)
                    provider_call_count += 1
                    generated = await provider.generate_structured(
                        operation, messages, response_schema, fallback_options
                    )
                provider_call_count += max(0, generated.provider_call_count - 1)
                return generated, generated.usage, provider_call_count, provider
            except LLMProviderError as error:
                last_provider_error = error
        assert last_provider_error is not None
        raise last_provider_error

    def _reserve_for_call(
        self,
        provider: LLMProvider,
        messages: Sequence[LLMMessage],
        model_options: Mapping[str, JSONValue],
    ) -> None:
        """Reserve the complete budget before every real provider invocation."""
        self._budget.reserve(
            input_tokens=estimate_message_input_tokens(messages),
            output_tokens=_requested_output_tokens(provider, model_options),
        )

    def _ordered_providers(self) -> tuple[LLMProvider, ...]:
        """Return the preferred provider followed by any explicit runtime fallback."""
        configured = getattr(self._provider, "providers", None)
        if isinstance(configured, tuple) and configured:
            return cast(tuple[LLMProvider, ...], configured)
        return (self._provider,)

    @staticmethod
    def _cache_identity(
        provider: LLMProvider,
        *,
        operation: LLMOperation,
        messages: Sequence[LLMMessage],
        prompt_version: str,
        schema_version: str,
        model_options: Mapping[str, JSONValue],
    ) -> LLMCacheIdentity:
        """Build cache provenance for the provider that actually produced the final output."""
        return LLMCacheIdentity(
            operation=operation,
            provider=provider.provider_name,
            model=provider.model,
            prompt_version=prompt_version,
            schema_version=schema_version,
            generation_config_hash=provider.generation_config_hash(model_options),
            input_hash=_input_hash(messages),
        )

    def _get_by_identity(self, identity: LLMCacheIdentity) -> LLMArtifact | None:
        with UnitOfWork(self._session_factory) as unit:
            assert unit.session is not None
            return LLMArtifactRepository(unit.session).get_by_cache_identity(
                operation=identity.operation,
                provider=identity.provider,
                model=identity.model,
                prompt_version=identity.prompt_version,
                schema_version=identity.schema_version,
                generation_config_hash=identity.generation_config_hash,
                input_hash=identity.input_hash,
            )

    def _cached_result(
        self,
        artifact: LLMArtifact,
        response_schema: type[BaseModel],
        *,
        validation_context: Mapping[str, object] | None,
    ) -> StructuredResult:
        try:
            decoded = json.loads(artifact.output_json)
        except json.JSONDecodeError as error:
            raise LLMArtifactCorruptError() from error
        if not isinstance(decoded, dict):
            raise LLMArtifactCorruptError()
        try:
            content = _validated_content(
                response_schema,
                cast(dict[str, JSONValue], decoded),
                validation_context=validation_context,
            )
        except LLMResponseValidationError as error:
            raise LLMArtifactCorruptError() from error
        return StructuredResult(
            content=content,
            model=artifact.model,
            usage=LLMUsage(
                input_tokens=artifact.input_tokens,
                output_tokens=artifact.output_tokens,
            ),
            request_id=artifact.provider_request_id,
            cache_hit=True,
            artifact_id=artifact.id,
            provider_call_count=0,
        )

    def _insert_or_reuse(
        self,
        *,
        identity: LLMCacheIdentity,
        output_json: str,
        output_hash: str,
        usage: LLMUsage,
        provider_request_id: str | None,
        created_by_task_run_id: str,
        created_by_task_step_id: int,
    ) -> tuple[LLMArtifact, bool]:
        """Handle a unique-key race by returning the already validated successful artifact."""
        try:
            with UnitOfWork(self._session_factory) as unit:
                assert unit.session is not None
                artifact = LLMArtifactRepository(unit.session).insert_validated(
                    **identity.as_repository_values(),
                    output_json=output_json,
                    output_hash=output_hash,
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    provider_request_id=provider_request_id,
                    created_by_task_run_id=created_by_task_run_id,
                    created_by_task_step_id=created_by_task_step_id,
                )
                return artifact, False
        except IntegrityError as error:
            logger.warning(
                "llm_artifact_insert_integrity_error operation=%s",
                identity.operation.value,
                extra={
                    "task_id": created_by_task_run_id,
                    "task_step": created_by_task_step_id,
                },
            )
            cached = self._get_by_identity(identity)
            if cached is not None:
                return cached, True
            raise LLMArtifactPersistenceError() from error


def _requested_output_tokens(provider: LLMProvider, model_options: Mapping[str, JSONValue]) -> int:
    """Read an optional semantic token cap without treating transport settings as cache inputs."""
    configured = model_options.get("max_output_tokens", provider.max_output_tokens)
    if isinstance(configured, bool) or not isinstance(configured, int) or configured < 0:
        msg = "max_output_tokens must be a non-negative integer"
        raise ValueError(msg)
    return configured


def _add_usage(first: LLMUsage, second: LLMUsage) -> LLMUsage:
    """Aggregate actual provider-reported usage from bounded repair/fallback calls."""
    return LLMUsage(
        input_tokens=first.input_tokens + second.input_tokens,
        output_tokens=first.output_tokens + second.output_tokens,
    )
