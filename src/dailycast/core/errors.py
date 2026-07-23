"""Application-level errors that can be safely returned by the API."""


class DailyCastError(Exception):
    """Base error with a stable public error code."""

    def __init__(self, *, code: str, message: str, status_code: int = 500) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


class ConfigurationError(DailyCastError):
    """Raised when local configuration cannot be loaded or validated."""

    def __init__(self, message: str) -> None:
        super().__init__(code="CONFIGURATION_INVALID", message=message, status_code=500)


class InfrastructureError(DailyCastError):
    """Raised when a required local runtime dependency is unavailable."""

    def __init__(self, message: str) -> None:
        super().__init__(code="INFRASTRUCTURE_UNAVAILABLE", message=message, status_code=503)


class AIError(DailyCastError):
    """Base class for safe, provider-independent AI infrastructure failures."""


class AIBudgetExceededError(AIError):
    """Raised before an LLM cache miss would exceed the configured per-task budget."""

    def __init__(self) -> None:
        super().__init__(
            code="AI_BUDGET_EXCEEDED",
            message="LLM budget would be exceeded before this model call",
            status_code=422,
        )


class LLMProviderError(AIError):
    """Raised for a non-authentication OpenAI-compatible provider failure."""

    def __init__(self) -> None:
        super().__init__(
            code="AI_PROVIDER_ERROR",
            message="LLM provider request failed",
            status_code=502,
        )


class LLMProviderTimeoutError(LLMProviderError):
    """Raised after bounded retry cannot complete a provider request before its timeout."""

    def __init__(self) -> None:
        DailyCastError.__init__(
            self,
            code="AI_PROVIDER_TIMEOUT",
            message="LLM provider request timed out",
            status_code=504,
        )


class LLMProviderAuthenticationError(LLMProviderError):
    """Raised for missing, rejected, or unauthorized provider credentials without revealing them."""

    def __init__(self) -> None:
        DailyCastError.__init__(
            self,
            code="AI_PROVIDER_AUTHENTICATION_FAILED",
            message="LLM provider authentication failed",
            status_code=502,
        )


class LLMProviderResponseError(LLMProviderError):
    """Raised when a nominally successful response does not contain a JSON object result."""

    def __init__(self) -> None:
        DailyCastError.__init__(
            self,
            code="AI_PROVIDER_RESPONSE_INVALID",
            message="LLM provider returned an invalid structured response",
            status_code=502,
        )
