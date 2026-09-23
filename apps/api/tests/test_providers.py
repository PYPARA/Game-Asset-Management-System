from __future__ import annotations

from typing import Any

import pytest

from game_assets_api.domain import ErrorCategory
from game_assets_api.providers import (
    CredentialVault,
    ProviderError,
    ProviderRuntimeConfig,
    _extract_model_items,
    build_provider,
    classify_http_error,
    guard_resolved_host,
    validate_base_url,
)


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


def test_resolved_private_or_reserved_address_reports_address_and_requires_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import game_assets_api.providers as providers_module

    monkeypatch.setattr(
        providers_module.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (providers_module.socket.AF_INET, providers_module.socket.SOCK_STREAM, 6, "", ("10.0.0.64", 443)),
        ],
    )

    with pytest.raises(ProviderError) as raised:
        guard_resolved_host("https://api.example.com/v1", allow_private_network=False)

    error = raised.value
    assert error.category == ErrorCategory.VALIDATION
    assert "10.0.0.64" in str(error)
    assert error.hint is not None
    assert "允许访问局域网或私有地址" in error.hint

    # The opt-in is explicit and bypasses the DNS classification only after
    # the user has enabled it for this provider.
    guard_resolved_host("https://api.example.com/v1", allow_private_network=True)


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ([{"id": "alpha"}, {"name": "beta"}], ["alpha", "beta"]),
        ({"data": [{"model": "gamma"}]}, ["gamma"]),
        ({"models": [{"model_id": "delta"}]}, ["delta"]),
        ({"items": ["epsilon"]}, ["epsilon"]),
    ],
)
def test_model_catalog_accepts_common_response_shapes(payload: Any, expected: list[str]) -> None:
    assert [item["id"] for item in _extract_model_items(payload)] == expected


def test_optional_and_none_credentials_build_without_an_api_key() -> None:
    vault = CredentialVault()
    for mode in ("optional", "none"):
        provider = build_provider(
            ProviderRuntimeConfig(
                id=f"local-{mode}",
                kind="openai_compatible",
                base_url="http://127.0.0.1:11434/v1",
                text_model="local-text",
                image_model="local-image",
                quality="high",
                allow_private_network=False,
                credential_mode=mode,
            ),
            vault,
        )
        assert provider.requires_credentials is False
