from __future__ import annotations

import asyncio
import base64
import io
import ipaddress
import json
import re
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
        endpoint: str | None = None,
        hint: str | None = None,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.status_code = status_code
        self.retry_after = retry_after
        self.request_id = request_id
        self.endpoint = endpoint
        self.hint = hint

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


def _redact_provider_text(
    value: str,
    *,
    secrets: tuple[str, ...] = (),
    limit: int = 320,
) -> str:
    redacted = value
    for secret in secrets:
        if secret:
            redacted = redacted.replace(secret, "[redacted]")
    redacted = re.sub(r"Bearer\s+[^\s,;]+", "Bearer [redacted]", redacted, flags=re.IGNORECASE)
    redacted = re.sub(
        r"(?:sk|key|token)[-_][A-Za-z0-9._-]{8,}",
        "[redacted]",
        redacted,
        flags=re.IGNORECASE,
    )
    return redacted[:limit].strip()


def _response_hint(response: httpx.Response, *, secrets: tuple[str, ...] = ()) -> str | None:
    try:
        payload = response.json()
    except ValueError:
        payload = None
    candidates: list[Any] = []
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            candidates.extend(error.get(key) for key in ("message", "detail", "code"))
        candidates.extend(payload.get(key) for key in ("message", "detail"))
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            return _redact_provider_text(candidate, secrets=secrets)
    # Do not echo an arbitrary gateway response body. It can contain a prompt,
    # credentials, or other provider-private data even when the body is short.
    return None


def _model_id(item: dict[str, Any]) -> str:
    for key in ("id", "name", "model", "model_id"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _extract_model_items(payload: Any) -> list[dict[str, Any]]:
    """Normalize the common OpenAI-compatible model-list envelopes."""

    raw_items: list[Any] | None = None
    if isinstance(payload, list):
        raw_items = payload
    elif isinstance(payload, dict):
        for key in ("data", "models", "items"):
            value = payload.get(key)
            if isinstance(value, list):
                raw_items = value
                break
    if raw_items is None:
        raise TypeError("models payload did not contain a supported list")

    normalized: list[dict[str, Any]] = []
    for raw in raw_items:
        if isinstance(raw, str) and raw.strip():
            normalized.append({"id": raw.strip()})
            continue
        if not isinstance(raw, dict):
            continue
        model_id = _model_id(raw)
        if not model_id:
            continue
        item = dict(raw)
        item["id"] = model_id
        normalized.append(item)
    if raw_items and not normalized:
        raise TypeError("models payload contained no identifiable model IDs")
    return normalized


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


def validate_models_path(models_path: str | None) -> str:
    """Validate a relative model-list path without allowing a second host."""

    value = (models_path or "models").strip()
    if not value:
        return "models"
    if (
        value.startswith(("/", "\\"))
        or "\\" in value
        or "://" in value
        or "?" in value
        or "#" in value
    ):
        raise ValueError("models path must be relative and must not contain a query or fragment")
    parsed = urlparse(value)
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
        raise ValueError("models path must be relative and must not contain a query or fragment")
    parts = [part for part in parsed.path.strip("/").split("/") if part]
    if not parts or any(part in {".", ".."} for part in parts):
        raise ValueError("models path must contain a valid relative path")
    return "/".join(parts)


def guard_resolved_host(base_url: str, *, allow_private_network: bool) -> None:
    if allow_private_network:
        return
    hostname = urlparse(base_url).hostname
    if not hostname or hostname in {"localhost", "127.0.0.1", "::1"}:
        return
    try:
        addresses = {entry[4][0] for entry in socket.getaddrinfo(hostname, None)}
    except socket.gaierror as exc:
        raise ProviderError(
            f"provider hostname could not be resolved: {hostname}",
            ErrorCategory.NETWORK,
            hint="检查供应商域名、DNS 或本机代理配置。",
        ) from exc

    blocked = sorted(
        value for value in addresses if not ipaddress.ip_address(value).is_global
    )
    if blocked:
        displayed = ", ".join(blocked[:4])
        if len(blocked) > 4:
            displayed += ", …"
        raise ProviderError(
            f"provider hostname resolves to a private or reserved address ({displayed}); "
            "enable private network access explicitly",
            ErrorCategory.VALIDATION,
            hint=(
                f"供应商域名 {hostname} 解析到了非公网地址 {displayed}。请确认域名和本机 DNS/代理配置；"
                "如果这是你信任的局域网或本机服务，请在供应商渠道的“高级设置”中开启“允许访问局域网或私有地址”，"
                "保存后再拉取模型。"
            ),
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
    credential_mode: str = "required"
    model_discovery_mode: str = "auto"
    models_path: str | None = None


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
        credential_mode=str(frozen.get("credential_mode") or getattr(profile, "credential_mode", "required")),
        model_discovery_mode=str(
            frozen.get("model_discovery_mode")
            or getattr(profile, "model_discovery_mode", "auto")
        ),
        models_path=str(frozen.get("models_path") or getattr(profile, "models_path", "") or "") or None,
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
        models: list[dict[str, Any]] = []
        if self.profile.text_model:
            models.append({"id": self.profile.text_model, "modalities": ["text"]})
        if self.profile.image_model:
            models.append({"id": self.profile.image_model, "modalities": ["image"]})
        return models

    async def test_connection(self) -> list[str]:
        return [str(item["id"]) for item in await self.discover_models()]


class OpenAICompatibleProvider:
    requires_credentials = True

    def __init__(self, profile: ProviderProfile | ProviderRuntimeConfig, api_key: str | None):
        self.profile = profile
        self.requires_credentials = (
            getattr(profile, "credential_mode", "required") == "required"
        )
        self.base_url = validate_base_url(
            profile.base_url, allow_private_network=profile.allow_private_network
        )
        self._headers = {"Content-Type": "application/json"}
        if api_key and api_key.strip():
            self._headers["Authorization"] = f"Bearer {api_key.strip()}"

    async def _request(
        self,
        method: str,
        path: str,
        *,
        idempotency_key: str | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        endpoint = f"{self.base_url}/{path.lstrip('/')}"
        headers = dict(self._headers)
        headers.update(kwargs.pop("headers", {}) or {})
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        try:
            guard_resolved_host(
                self.base_url,
                allow_private_network=self.profile.allow_private_network,
            )
            async with httpx.AsyncClient(timeout=90.0, follow_redirects=False) as client:
                response = await client.request(
                    method, endpoint, headers=headers, **kwargs
                )
        except ProviderError as exc:
            if exc.endpoint:
                raise
            raise ProviderError(
                str(exc),
                exc.category,
                status_code=exc.status_code,
                retry_after=exc.retry_after,
                request_id=exc.request_id,
                endpoint=endpoint,
                hint=exc.hint or "检查 Base URL、模型列表路径和本机网络连通性。",
            ) from exc
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise ProviderError(
                "provider network request failed",
                ErrorCategory.NETWORK,
                endpoint=endpoint,
                hint="检查 Base URL、模型列表路径和本机网络连通性。",
            ) from exc
        if response.status_code >= 400:
            response_body = response.text[:1000]
            category = classify_http_error(response.status_code, response_body)
            request_id = (
                response.headers.get("x-request-id")
                or response.headers.get("request-id")
                or response.headers.get("cf-ray")
            )
            secret = self._headers.get("Authorization", "").removeprefix("Bearer ")
            hint = _response_hint(response, secrets=(secret,))
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
                endpoint=endpoint,
                hint=hint,
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
                or exc.category
                in {
                    ErrorCategory.AUTH,
                    ErrorCategory.BILLING,
                    ErrorCategory.QUOTA,
                    ErrorCategory.RATE_LIMIT,
                    ErrorCategory.SERVER,
                    ErrorCategory.NETWORK,
                    ErrorCategory.CONTENT_POLICY,
                }
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
            endpoint = f"{self.base_url}/images/edits"
            try:
                guard_resolved_host(
                    self.base_url,
                    allow_private_network=self.profile.allow_private_network,
                )
                async with httpx.AsyncClient(timeout=180.0, follow_redirects=False) as client:
                    response = await client.post(
                        endpoint,
                        headers={
                            **{
                                key: value
                                for key, value in self._headers.items()
                                if key.lower() != "content-type"
                            },
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
            except ProviderError as exc:
                if exc.endpoint:
                    raise
                raise ProviderError(
                    str(exc),
                    exc.category,
                    status_code=exc.status_code,
                    retry_after=exc.retry_after,
                    request_id=exc.request_id,
                    endpoint=endpoint,
                    hint=exc.hint or "检查 Base URL、本机网络连通性和局域网访问开关。",
                ) from exc
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                raise ProviderError(
                    "provider network request failed",
                    ErrorCategory.NETWORK,
                    endpoint=endpoint,
                    hint="检查 Base URL、模型列表路径和本机网络连通性。",
                ) from exc
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
                    request_id=(
                        response.headers.get("x-request-id")
                        or response.headers.get("request-id")
                        or response.headers.get("cf-ray")
                    ),
                    endpoint=endpoint,
                    hint=_response_hint(
                        response,
                        secrets=(self._headers.get("Authorization", "").removeprefix("Bearer "),),
                    ),
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
        models_path = validate_models_path(getattr(self.profile, "models_path", None))
        response = await self._request("GET", models_path)
        try:
            payload = response.json()
            items = _extract_model_items(payload)
            return items
        except (ValueError, TypeError, KeyError) as exc:
            raise ProviderError(
                "provider returned an invalid models response",
                ErrorCategory.INVALID_RESPONSE,
                endpoint=f"{self.base_url}/{models_path}",
                hint="模型列表需要返回数组，或包含 data/models/items 数组。",
            ) from exc

    async def test_connection(self) -> list[str]:
        return [str(item["id"]) for item in await self.discover_models()]


def build_provider(
    profile: ProviderProfile | ProviderRuntimeConfig, vault: CredentialVault
) -> GenerationProvider:
    if profile.kind == ProviderKind.FAKE.value:
        return FakeProvider(profile)
    key = vault.get(profile.id)
    credential_mode = getattr(profile, "credential_mode", "required")
    if not key and credential_mode == "required":
        raise ProviderError(
            "provider credentials are locked",
            ErrorCategory.AUTH,
            hint="保存 API Key 后重新验证连接；本地服务可将凭据模式设为无凭据。",
        )
    return OpenAICompatibleProvider(profile, key)
