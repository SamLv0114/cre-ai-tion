"""Error taxonomy. Retry decisions key off `retryable`, never off message text."""

from __future__ import annotations

from typing import Optional


class PipelineError(Exception):
    retryable: bool = False

    def __init__(self, message: str = "", *, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


# --- LLM transport ----------------------------------------------------------
class LLMError(PipelineError):
    pass


# temporary problems: worth retrying
class LLMTimeout(LLMError):
    retryable = True


class LLMRateLimited(LLMError):
    retryable = True


class LLMServerError(LLMError):
    retryable = True


class LLMAuthError(LLMError):
    """401/403: retrying cannot help; trips the breaker immediately."""

    retryable = False


# our request is wrong (400); retrying the same thing won't help
class LLMBadRequest(LLMError):
    retryable = False


# raised instead of calling a dependency that's known to be down
class CircuitOpenError(PipelineError):
    retryable = False


# --- Structured output ------------------------------------------------------
class StructuredOutputError(PipelineError):
    """All models in the route failed to return schema-valid output after bounded repair."""

    retryable = False

    def __init__(self, message: str, attempts: list[str]):
        super().__init__(message)
        self.attempts = attempts


# --- Tools --------------------------------------------------------------------
class ToolError(PipelineError):
    retryable = True


class ToolTimeout(ToolError):
    retryable = True


# --- Guards ---------------------------------------------------------------------
# variant has spent its token/money allowance
class BudgetExceeded(PipelineError):
    retryable = False
