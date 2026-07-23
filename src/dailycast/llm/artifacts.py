"""Schema-validated, exact-identity LLMArtifact cache integration."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import cast

from pydantic import BaseModel, ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from dailycast.core.errors import DailyCastError
from dailycast.core.hashes import sha256_text
from dailycast.db.models import LLMArtifact, LLMOperation
from dailycast.db.repositories import LLMArtifactRepository
from dailycast.db.transactions import UnitOfWork
from dailycast.llm.budget import BudgetController, estimate_message_input_tokens
from dailycast.llm.contracts import JSONValue, LLMMessage, LLMProvider, LLMUsage, StructuredResult


class LLMResponseValidationError(DailyCastError):
    """Raised when a provider response cannot satisfy the local output schema."""

    def __init__(self) -> None:
        super().__init__(
            code="AI_RESPONSE_SCHEMA_INVALID",
            message="LLM response did not satisfy the required structured schema",
            status_code=502,
        )


class LLMArtifactCorruptError(DailyCastError):
    """Raised if a legacy or manually corrupted cache row no longer validates locally."""

    def __init__(self) -> None:
        super().__init__(
            code="AI_ARTIFACT_INVALID",
            message="cached LLM artifact failed local schema validation",
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
        raise LLMResponseValidationError() from error
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
        identity = LLMCacheIdentity(
            operation=operation,
            provider=self._provider.provider_name,
            model=self._provider.model,
            prompt_version=prompt_version,
            schema_version=schema_version,
            generation_config_hash=self._provider.generation_config_hash(model_options),
            input_hash=_input_hash(messages),
        )
        cached = self._get_by_identity(identity)
        if cached is not None:
            return self._cached_result(
                cached, response_schema, validation_context=validation_context
            )

        self._budget.reserve(
            input_tokens=estimate_message_input_tokens(messages),
            output_tokens=_requested_output_tokens(self._provider, model_options),
        )
        generated = await self._provider.generate_structured(
            operation, messages, response_schema, model_options
        )
        content = _validated_content(
            response_schema, generated.content, validation_context=validation_context
        )
        output_json = _canonical_json(content)
        artifact, concurrent_cache_hit = self._insert_or_reuse(
            identity=identity,
            output_json=output_json,
            output_hash=sha256_text(output_json),
            usage=generated.usage,
            provider_request_id=generated.request_id,
            created_by_task_run_id=created_by_task_run_id,
            created_by_task_step_id=created_by_task_step_id,
        )
        return StructuredResult(
            content=content,
            model=artifact.model,
            usage=generated.usage,
            request_id=generated.request_id,
            cache_hit=concurrent_cache_hit,
            artifact_id=artifact.id,
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
        except IntegrityError:
            cached = self._get_by_identity(identity)
            if cached is not None:
                return cached, True
            raise


def _requested_output_tokens(provider: LLMProvider, model_options: Mapping[str, JSONValue]) -> int:
    """Read an optional semantic token cap without treating transport settings as cache inputs."""
    configured = model_options.get("max_output_tokens", provider.max_output_tokens)
    if isinstance(configured, bool) or not isinstance(configured, int) or configured < 0:
        msg = "max_output_tokens must be a non-negative integer"
        raise ValueError(msg)
    return configured
