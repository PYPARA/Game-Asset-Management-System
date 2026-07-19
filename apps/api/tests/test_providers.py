from __future__ import annotations

import pytest

from game_assets_api.domain import ErrorCategory
from game_assets_api.providers import ProviderError, classify_http_error, validate_base_url


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (401, "invalid key", ErrorCategory.AUTH),
        (402, "payment required", ErrorCategory.BILLING),
        (429, "quota exceeded", ErrorCategory.QUOTA),
        (429, "rate limited", ErrorCategory.RATE_LIMIT),
        (500, "oops", ErrorCategory.SERVER),
        (400, "moderation_blocked", ErrorCategory.CONTENT_POLICY),
    ],
)
def test_provider_error_classification(status: int, body: str, expected: ErrorCategory) -> None:
    assert classify_http_error(status, body) == expected


def test_retry_policy_never_retries_auth_billing_or_content_policy() -> None:
    assert ProviderError("rate", ErrorCategory.RATE_LIMIT).retryable is True
    assert ProviderError("server", ErrorCategory.SERVER).retryable is True
    assert ProviderError("auth", ErrorCategory.AUTH).retryable is False
    assert ProviderError("billing", ErrorCategory.BILLING).retryable is False
    assert ProviderError("moderated", ErrorCategory.CONTENT_POLICY).retryable is False


def test_provider_url_policy() -> None:
    assert validate_base_url("https://api.example.com/v1", allow_private_network=False) == "https://api.example.com/v1"
    assert validate_base_url("http://127.0.0.1:8080/v1", allow_private_network=False).startswith("http://")
    with pytest.raises(ValueError):
        validate_base_url("http://example.com/v1", allow_private_network=False)
    with pytest.raises(ValueError):
        validate_base_url("https://user:secret@example.com/v1", allow_private_network=False)
