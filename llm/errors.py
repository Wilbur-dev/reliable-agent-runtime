from __future__ import annotations


class LLMError(RuntimeError):
    error_type = "llm_error"
    retryable = False


class LLMConfigurationError(LLMError):
    error_type = "llm_configuration_error"


class LLMAuthenticationError(LLMError):
    error_type = "llm_authentication_error"


class LLMRateLimitError(LLMError):
    error_type = "llm_rate_limit_error"
    retryable = True


class LLMTimeoutError(LLMError):
    error_type = "llm_timeout_error"
    retryable = True


class LLMServiceError(LLMError):
    error_type = "llm_service_error"
    retryable = True


class LLMResponseError(LLMError):
    error_type = "llm_response_error"
