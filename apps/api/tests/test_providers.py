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


def test_content_policy_does_not_trigger_json_mode_fallback() -> None:
    """A moderation response is a terminal human-handled decision.

    The structured-text adapter may fall back from ``json_schema`` to
    ``json_object`` for providers that reject the response-format feature, but
    it must not send a second paid request after a content-policy rejection.
    """

    from game_assets_api.providers import OpenAICompatibleProvider, ProviderRuntimeConfig

    class _ContentPolicyProvider(OpenAICompatibleProvider):
        def __init__(self) -> None:
            self.profile = ProviderRuntimeConfig(
                id="content-policy-test",
                kind="openai_compatible",
                base_url="https://example.invalid/v1",
                text_model="text",
                image_model="image",
                quality="standard",
                allow_private_network=False,
            )
            self.calls = 0

        async def _chat(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            self.calls += 1
            raise ProviderError("blocked", ErrorCategory.CONTENT_POLICY, status_code=400)

    import asyncio

    provider = _ContentPolicyProvider()
    with pytest.raises(ProviderError) as raised:
        asyncio.run(
            provider.structured_text(
                prompt="blocked",
                schema={"type": "object"},
                model="text",
            )
        )
    assert raised.value.category == ErrorCategory.CONTENT_POLICY
    assert provider.calls == 1


def test_provider_url_policy() -> None:
    assert validate_base_url("https://api.example.com/v1", allow_private_network=False) == "https://api.example.com/v1"
    assert validate_base_url("http://127.0.0.1:8080/v1", allow_private_network=False).startswith("http://")
    with pytest.raises(ValueError):
        validate_base_url("http://example.com/v1", allow_private_network=False)
    with pytest.raises(ValueError):
        validate_base_url("https://user:secret@example.com/v1", allow_private_network=False)
