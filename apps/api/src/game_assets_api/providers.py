from __future__ import annotations

import asyncio
import base64
import io
import ipaddress
import json
import socket
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlparse

import httpx
import jsonschema
from PIL import Image, ImageDraw

from .domain import ErrorCategory, ProviderKind
from .models import ProviderProfile


class ProviderError(RuntimeError):
    def __init__(
        self,
        message: str,
        category: ErrorCategory = ErrorCategory.UNKNOWN,
        *,
        status_code: int | None = None,
        retry_after: float | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.status_code = status_code
        self.retry_after = retry_after
        self.request_id = request_id

    @property
    def retryable(self) -> bool:
        return self.category in {
            ErrorCategory.RATE_LIMIT,
            ErrorCategory.SERVER,
            ErrorCategory.NETWORK,
            ErrorCategory.EMPTY,
        }


def classify_http_error(status: int, body: str) -> ErrorCategory:
    lowered = body.lower()
    if "moderation_blocked" in lowered or "content_policy" in lowered or "safety system" in lowered:
        return ErrorCategory.CONTENT_POLICY
    if status in {401, 403}:
        return ErrorCategory.AUTH
    if status == 429:
        if "billing" in lowered or "credit" in lowered or "payment" in lowered:
            return ErrorCategory.BILLING
        if "quota" in lowered:
            return ErrorCategory.QUOTA
        return ErrorCategory.RATE_LIMIT
    if status in {402}:
        return ErrorCategory.BILLING
    if status in {408, 409, 425}:
        return ErrorCategory.NETWORK
    if status >= 500:
        return ErrorCategory.SERVER
    if status in {400, 404, 405, 415, 422}:
        return ErrorCategory.VALIDATION
    return ErrorCategory.UNKNOWN


def _retry_after(headers: httpx.Headers) -> float | None:
    value = headers.get("retry-after")
    if not value:
        return None
    try:
        return max(0.0, min(float(value), 120.0))
    except ValueError:
        return None


def validate_base_url(base_url: str, *, allow_private_network: bool) -> str:
    parsed = urlparse(base_url)
    if parsed.username or parsed.password or not parsed.hostname:
        raise ValueError("base URL must not contain credentials and must include a hostname")
    hostname = parsed.hostname.lower()
    localhost = hostname in {"localhost", "127.0.0.1", "::1"}
    if parsed.scheme != "https" and not (parsed.scheme == "http" and localhost):
        raise ValueError("base URL must use HTTPS; only loopback hosts may use HTTP")
    if parsed.query or parsed.fragment:
        raise ValueError("base URL must not contain a query or fragment")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        address = None
    if address and not address.is_global and not address.is_loopback and not allow_private_network:
        raise ValueError("private network provider URLs require explicit opt-in")
    return base_url.rstrip("/")


def guard_resolved_host(base_url: str, *, allow_private_network: bool) -> None:
    if allow_private_network:
        return
    hostname = urlparse(base_url).hostname
    if not hostname or hostname in {"localhost", "127.0.0.1", "::1"}:
        return
    try:
        addresses = {entry[4][0] for entry in socket.getaddrinfo(hostname, None)}
    except socket.gaierror as exc:
        raise ProviderError("provider hostname could not be resolved", ErrorCategory.NETWORK) from exc
    for value in addresses:
        address = ipaddress.ip_address(value)
        if not address.is_global:
            raise ProviderError(
                "provider hostname resolves to a private address; enable private network access explicitly",
                ErrorCategory.VALIDATION,
            )


class CredentialVault:
    """Process-only provider credentials. Values are deliberately never serializable."""

    def __init__(self) -> None:
        self._keys: dict[str, str] = {}

    def unlock(self, provider_id: str, api_key: str) -> None:
        self._keys[provider_id] = api_key

    def lock(self, provider_id: str) -> None:
        self._keys.pop(provider_id, None)

    def get(self, provider_id: str) -> str | None:
        return self._keys.get(provider_id)

    def is_unlocked(self, provider_id: str) -> bool:
        return provider_id in self._keys

    def clear(self) -> None:
        self._keys.clear()


@dataclass(slots=True)
class ProviderResult:
    value: Any
    request_id: str | None = None


@dataclass(slots=True, frozen=True)
class ProviderRuntimeConfig:
    id: str
    kind: str
    base_url: str
    text_model: str
    image_model: str
    quality: str
    allow_private_network: bool


def provider_runtime_config(
    profile: ProviderProfile, snapshot: dict[str, Any] | None = None
) -> ProviderRuntimeConfig:
    frozen = snapshot or {}
    return ProviderRuntimeConfig(
        id=str(frozen.get("profile_id") or profile.id),
        kind=str(frozen.get("kind") or profile.kind),
        base_url=str(frozen.get("base_url") or profile.base_url),
        text_model=str(frozen.get("text_model") or profile.text_model),
        image_model=str(frozen.get("image_model") or profile.image_model),
        quality=str(frozen.get("quality") or profile.quality),
        allow_private_network=bool(
            frozen.get("allow_private_network", profile.allow_private_network)
        ),
    )


class GenerationProvider(Protocol):
    requires_credentials: bool

    async def structured_text(
        self,
        *,
        prompt: str,
        schema: dict[str, Any],
        model: str,
        idempotency_key: str | None = None,
    ) -> ProviderResult: ...

    async def image(
        self,
        *,
        prompt: str,
        width: int | None,
        height: int | None,
        model: str,
        reference: bytes | None = None,
        idempotency_key: str | None = None,
    ) -> ProviderResult: ...

    async def discover_models(self) -> list[dict[str, Any]]: ...

    async def test_connection(self) -> list[str]: ...


def _fake_value(schema: dict[str, Any], name: str = "value") -> Any:
    if "const" in schema:
        return schema["const"]
    if schema.get("enum"):
        return schema["enum"][0]
    schema_type = schema.get("type")
    if isinstance(schema_type, list):
        schema_type = next((item for item in schema_type if item != "null"), "null")
    if schema_type == "object" or "properties" in schema:
        properties = schema.get("properties", {})
        required = set(schema.get("required", properties.keys()))
        return {key: _fake_value(value, key) for key, value in properties.items() if key in required}
    if schema_type == "array":
        return [_fake_value(schema.get("items", {}), name)] if schema.get("minItems", 0) else []
    if schema_type == "integer":
        return int(schema.get("minimum", 1))
    if schema_type == "number":
        return float(schema.get("minimum", 1.0))
    if schema_type == "boolean":
        return True
    if schema_type == "null":
        return None
    return f"generated-{name}"


class FakeProvider:
    requires_credentials = False

    def __init__(self, profile: ProviderProfile | ProviderRuntimeConfig):
        self.profile = profile

    async def structured_text(
        self,
        *,
        prompt: str,
        schema: dict[str, Any],
        model: str,
        idempotency_key: str | None = None,
    ) -> ProviderResult:
        await asyncio.sleep(0)
        value = _fake_value(schema)
        if isinstance(value, dict) and "prompt" in schema.get("properties", {}):
            value["prompt"] = prompt
        jsonschema.validate(value, schema)
        return ProviderResult(value=value, request_id="fake-text-request")

    async def image(
        self,
        *,
        prompt: str,
        width: int | None,
        height: int | None,
        model: str,
        reference: bytes | None = None,
        idempotency_key: str | None = None,
    ) -> ProviderResult:
        await asyncio.sleep(0)
        size = (width or 256, height or 256)
        image = Image.new("RGBA", size, (35, 43, 66, 0))
        draw = ImageDraw.Draw(image)
        margin = max(4, min(size) // 8)
        draw.rounded_rectangle(
            (margin, margin, size[0] - margin, size[1] - margin),
            radius=max(2, margin // 2),
            fill=(91, 143, 249, 255),
        )
        draw.text((margin + 4, margin + 4), prompt[:16], fill=(255, 255, 255, 255))
        output = io.BytesIO()
        image.save(output, "PNG")
        return ProviderResult(value=output.getvalue(), request_id="fake-image-request")

    async def discover_models(self) -> list[dict[str, Any]]:
        return [
            {"id": self.profile.text_model, "modalities": ["text"]},
            {"id": self.profile.image_model, "modalities": ["image"]},
        ]

    async def test_connection(self) -> list[str]:
        return [str(item["id"]) for item in await self.discover_models()]


class OpenAICompatibleProvider:
    requires_credentials = True

    def __init__(self, profile: ProviderProfile | ProviderRuntimeConfig, api_key: str):
        self.profile = profile
        self.base_url = validate_base_url(
            profile.base_url, allow_private_network=profile.allow_private_network
        )
        self._headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    async def _request(
        self,
        method: str,
        path: str,
        *,
        idempotency_key: str | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        guard_resolved_host(self.base_url, allow_private_network=self.profile.allow_private_network)
        headers = dict(kwargs.pop("headers", self._headers))
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        try:
            async with httpx.AsyncClient(timeout=90.0, follow_redirects=False) as client:
                response = await client.request(
                    method, f"{self.base_url}/{path.lstrip('/')}", headers=headers, **kwargs
                )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise ProviderError("provider network request failed", ErrorCategory.NETWORK) from exc
        if response.status_code >= 400:
            response_body = response.text[:1000]
            category = classify_http_error(response.status_code, response_body)
            request_id = response.headers.get("x-request-id")
            message = (
                "provider model is unavailable"
                if category == ErrorCategory.VALIDATION and "model" in response_body.lower()
                else f"provider returned HTTP {response.status_code}"
            )
            raise ProviderError(
                message,
                category,
                status_code=response.status_code,
                retry_after=_retry_after(response.headers),
                request_id=request_id,
            )
        return response

    @staticmethod
    def _content(response: httpx.Response) -> tuple[str, str | None]:
        try:
            payload = response.json()
            content = payload["choices"][0]["message"]["content"]
            if isinstance(content, list):
                content = "".join(
                    str(item.get("text", "")) for item in content if isinstance(item, dict)
                )
            if not isinstance(content, str) or not content.strip():
                raise KeyError("empty content")
            return content, response.headers.get("x-request-id") or payload.get("id")
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ProviderError("provider returned an empty or invalid text response", ErrorCategory.EMPTY) from exc

    async def _chat(
        self,
        messages: list[dict[str, str]],
        response_format: dict[str, Any],
        *,
        model: str,
        idempotency_key: str | None = None,
    ) -> ProviderResult:
        response = await self._request(
            "POST",
            "chat/completions",
            idempotency_key=idempotency_key,
            json={
                "model": model,
                "messages": messages,
                "response_format": response_format,
            },
        )
        content, request_id = self._content(response)
        try:
            return ProviderResult(json.loads(content), request_id)
        except json.JSONDecodeError as exc:
            raise ProviderError("provider text was not valid JSON", ErrorCategory.INVALID_RESPONSE) from exc

    async def structured_text(
        self,
        *,
        prompt: str,
        schema: dict[str, Any],
        model: str,
        idempotency_key: str | None = None,
    ) -> ProviderResult:
        messages = [
            {"role": "system", "content": "Return only JSON that satisfies the supplied schema."},
            {"role": "user", "content": prompt},
        ]
        try:
            result = await self._chat(
                messages,
                {
                    "type": "json_schema",
                    "json_schema": {"name": "asset_candidate", "strict": True, "schema": schema},
                },
                model=model,
                idempotency_key=f"{idempotency_key}:schema" if idempotency_key else None,
            )
        except ProviderError as exc:
            if (
                exc.status_code not in {400, 404, 422}
                or str(exc) == "provider model is unavailable"
            ):
                raise
            result = await self._chat(
                messages,
                {"type": "json_object"},
                model=model,
                idempotency_key=f"{idempotency_key}:fallback" if idempotency_key else None,
            )

        try:
            jsonschema.validate(result.value, schema)
            return result
        except jsonschema.ValidationError:
            repair_messages = messages + [
                {"role": "assistant", "content": json.dumps(result.value, ensure_ascii=False)},
                {
                    "role": "user",
                    "content": "Repair the previous JSON so it strictly satisfies this schema:\n"
                    + json.dumps(schema, ensure_ascii=False),
                },
            ]
            repaired = await self._chat(
                repair_messages,
                {"type": "json_object"},
                model=model,
                idempotency_key=f"{idempotency_key}:repair" if idempotency_key else None,
            )
            try:
                jsonschema.validate(repaired.value, schema)
            except jsonschema.ValidationError as exc:
                raise ProviderError(
                    "provider JSON did not satisfy the schema after one repair",
                    ErrorCategory.VALIDATION,
                ) from exc
            return repaired

    async def image(
        self,
        *,
        prompt: str,
        width: int | None,
        height: int | None,
        model: str,
        reference: bytes | None = None,
        idempotency_key: str | None = None,
    ) -> ProviderResult:
        if reference is not None:
            guard_resolved_host(self.base_url, allow_private_network=self.profile.allow_private_network)
            try:
                async with httpx.AsyncClient(timeout=180.0, follow_redirects=False) as client:
                    response = await client.post(
                        f"{self.base_url}/images/edits",
                        headers={
                            "Authorization": self._headers["Authorization"],
                            **({"Idempotency-Key": idempotency_key} if idempotency_key else {}),
                        },
                        data={
                            "model": model,
                            "prompt": prompt,
                            "quality": self.profile.quality,
                            "size": f"{width}x{height}" if width and height else "auto",
                        },
                        files={"image": ("reference.png", reference, "image/png")},
                    )
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                raise ProviderError("provider network request failed", ErrorCategory.NETWORK) from exc
            if response.status_code >= 400:
                response_body = response.text[:1000]
                category = classify_http_error(response.status_code, response_body)
                raise ProviderError(
                    "provider model is unavailable"
                    if category == ErrorCategory.VALIDATION and "model" in response_body.lower()
                    else f"provider returned HTTP {response.status_code}",
                    category,
                    status_code=response.status_code,
                    retry_after=_retry_after(response.headers),
                    request_id=response.headers.get("x-request-id"),
                )
        else:
            response = await self._request(
                "POST",
                "images/generations",
                idempotency_key=idempotency_key,
                json={
                    "model": model,
                    "prompt": prompt,
                    "quality": self.profile.quality,
                    "size": f"{width}x{height}" if width and height else "auto",
                },
            )
        try:
            payload = response.json()
            item = payload["data"][0]
            request_id = response.headers.get("x-request-id") or payload.get("id")
            if item.get("b64_json"):
                return ProviderResult(base64.b64decode(item["b64_json"], validate=True), request_id)
            raise KeyError("b64_json")
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ProviderError(
                "provider image response must contain base64 image data",
                ErrorCategory.INVALID_RESPONSE,
            ) from exc

    async def discover_models(self) -> list[dict[str, Any]]:
        response = await self._request("GET", "models")
        try:
            data = response.json().get("data", [])
            if not isinstance(data, list):
                raise TypeError("models data is not a list")
            return [dict(item) for item in data if isinstance(item, dict) and item.get("id")]
        except (ValueError, TypeError) as exc:
            raise ProviderError("provider returned an invalid models response", ErrorCategory.INVALID_RESPONSE) from exc

    async def test_connection(self) -> list[str]:
        return [str(item["id"]) for item in await self.discover_models()]


def build_provider(
    profile: ProviderProfile | ProviderRuntimeConfig, vault: CredentialVault
) -> GenerationProvider:
    if profile.kind == ProviderKind.FAKE.value:
        return FakeProvider(profile)
    key = vault.get(profile.id)
    if not key:
        raise ProviderError("provider credentials are locked", ErrorCategory.AUTH)
    return OpenAICompatibleProvider(profile, key)
