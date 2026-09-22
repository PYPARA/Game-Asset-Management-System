from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Literal

from sqlalchemy.orm import Session

from .domain import TaskKind
from .models import ProviderProfile, ProviderRoutingDefaults, utcnow


Modality = Literal["text", "image", "video", "audio"]
ModelCapability = Literal["text", "image", "video", "audio"]

MODEL_SYNC_STATES = {"never", "synced", "empty", "manual_required", "error"}

VIDEO_HINTS = (
    "seedance",
    "video",
    "sora",
    "veo",
    "kling",
    "wan",
    "hailuo",
)
AUDIO_HINTS = (
    "audio",
    "tts",
    "speech",
    "voice",
    "music",
    "sound",
)
IMAGE_HINTS = (
    "seedream",
    "gpt-image",
    "image",
    "imagine",
    "dall-e",
    "imagen",
    "flux",
    "sdxl",
    "midjourney",
)


def required_modality(kind: str | TaskKind) -> Modality:
    return "text" if str(kind) == TaskKind.TEXT.value else "image"


def guess_capability(model_id: str) -> ModelCapability:
    lowered = model_id.lower()
    if any(token in lowered for token in VIDEO_HINTS):
        return "video"
    if any(token in lowered for token in AUDIO_HINTS):
        return "audio"
    if any(token in lowered for token in IMAGE_HINTS):
        return "image"
    return "text"


def _normalized_modalities(model_id: str, values: Any) -> list[ModelCapability]:
    valid: list[ModelCapability] = []
    if isinstance(values, list):
        for value in values:
            if value in {"text", "image", "video", "audio"} and value not in valid:
                valid.append(value)
    # The catalog now has one primary model type. Legacy empty/multi-value
    # records are deterministically collapsed from the model id.
    return valid if len(valid) == 1 else [guess_capability(model_id)]


def classify_model(item: dict[str, Any]) -> tuple[list[ModelCapability], str]:
    return [guess_capability(str(item.get("id", "")))], "heuristic"


def _catalog_item(value: dict[str, Any]) -> dict[str, Any] | None:
    model_id = str(value.get("id", "")).strip()
    if not model_id:
        return None
    modalities = _normalized_modalities(model_id, value.get("modalities"))
    classification = str(value.get("classification", "unknown"))
    if classification not in {"provider", "heuristic", "manual", "unknown"}:
        classification = "unknown"
    return {
        "id": model_id,
        "modalities": modalities,
        "classification": classification,
        "available": bool(value.get("available", True)),
        # ``enabled`` was added after the first model-cache format shipped.
        # Treat a missing value as enabled so old caches remain usable.
        "enabled": value.get("enabled") is not False,
    }


def normalize_model_catalog(existing: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        (
            item
            for value in existing
            if (item := _catalog_item(value)) is not None
        ),
        key=lambda item: item["id"].lower(),
    )


def merge_discovered_models(
    existing: Iterable[dict[str, Any]], discovered: Iterable[dict[str, Any]]
) -> list[dict[str, Any]]:
    catalog = {
        item["id"]: item
        for value in existing
        if (item := _catalog_item(value)) is not None
    }
    for item in catalog.values():
        item["available"] = False
    for raw in discovered:
        model_id = str(raw.get("id", "")).strip()
        if not model_id:
            continue
        previous = catalog.get(model_id)
        modalities, classification = classify_model(raw)
        if previous:
            modalities = list(previous["modalities"])
        if previous and previous["classification"] == "manual":
            classification = "manual"
        catalog[model_id] = {
            "id": model_id,
            "modalities": modalities,
            "classification": classification,
            "available": True,
            "enabled": previous.get("enabled", True) if previous else True,
        }
    return sorted(catalog.values(), key=lambda item: item["id"].lower())


def apply_model_overrides(
    existing: Iterable[dict[str, Any]], overrides: Iterable[dict[str, Any]]
) -> list[dict[str, Any]]:
    catalog = {
        item["id"]: item
        for value in existing
        if (item := _catalog_item(value)) is not None
    }
    for value in overrides:
        model_id = str(value.get("id", "")).strip()
        if not model_id:
            continue
        previous = catalog.get(model_id)
        modalities = (
            _normalized_modalities(model_id, value.get("modalities"))
            if "modalities" in value
            else list(previous["modalities"])
            if previous
            else [guess_capability(model_id)]
        )
        enabled = (
            bool(value["enabled"])
            if value.get("enabled") is not None
            else bool(previous.get("enabled", True)) if previous else True
        )
        classification = value.get("classification")
        if classification not in {"provider", "heuristic", "manual", "unknown"}:
            classification = previous.get("classification", "manual") if previous else "manual"
        available = (
            bool(value["available"])
            if value.get("available") is not None
            else bool(previous and previous.get("available"))
        )
        catalog[model_id] = {
            "id": model_id,
            "modalities": modalities,
            # Browser-direct discovery sends classification/availability when
            # the user confirms the picker. Ordinary manual overrides omit
            # them and preserve the existing catalog state.
            "classification": classification,
            "available": available,
            "enabled": enabled,
        }
    return sorted(catalog.values(), key=lambda item: item["id"].lower())


def model_is_compatible(profile: ProviderProfile, model_id: str, modality: Modality) -> bool:
    for value in profile.models_json or []:
        item = _catalog_item(value)
        if item is None or item["id"] != model_id:
            continue
        if item.get("enabled", True) is False:
            return False
        return item["modalities"] == [modality]
    return False


def ensure_routing_defaults(session: Session) -> ProviderRoutingDefaults:
    defaults = session.get(ProviderRoutingDefaults, "global")
    if defaults is not None:
        return defaults
    defaults = ProviderRoutingDefaults(
        id="global",
        text_provider_profile_id=None,
        text_model=None,
        image_provider_profile_id=None,
        image_model=None,
        video_provider_profile_id=None,
        video_model=None,
        audio_provider_profile_id=None,
        audio_model=None,
        max_concurrency=3,
        max_transport_retries=2,
        updated_at=utcnow(),
    )
    session.add(defaults)
    session.flush()
    return defaults


def default_route(
    session: Session, kind: str | TaskKind
) -> tuple[str | None, str | None]:
    defaults = ensure_routing_defaults(session)
    if required_modality(kind) == "text":
        return defaults.text_provider_profile_id, defaults.text_model
    return defaults.image_provider_profile_id, defaults.image_model


def provider_snapshot(profile: ProviderProfile, *, model: str) -> dict[str, Any]:
    return {
        "runtime_policy_version": 2,
        "profile_id": profile.id,
        "name": profile.name,
        "kind": profile.kind,
        "base_url": profile.base_url,
        "text_model": profile.text_model,
        "image_model": profile.image_model,
        "model": model,
        "quality": profile.quality,
        "concurrency": profile.concurrency,
        "max_retries": profile.max_retries,
        "allow_private_network": profile.allow_private_network,
        "credential_mode": profile.credential_mode,
        "model_discovery_mode": profile.model_discovery_mode,
        "models_path": profile.models_path or "models",
        # Channel-level prices are retained in the compatibility model but are
        # intentionally excluded from new frozen jobs until model pricing has
        # a dedicated schema.
        "pricing": None,
        "captured_at": utcnow().isoformat(),
    }


def provider_credentials_ready(profile: ProviderProfile, *, unlocked: bool) -> bool:
    return (
        profile.kind == "fake"
        or profile.credential_mode in {"optional", "none"}
        or unlocked
    )


def provider_models_sync(profile: ProviderProfile) -> dict[str, Any]:
    state = str(profile.models_sync_state or "never")
    if state not in MODEL_SYNC_STATES:
        state = "never"
    diagnostic = profile.models_sync_diagnostic if isinstance(profile.models_sync_diagnostic, dict) else {}
    return {
        "state": state,
        "checked_at": profile.models_sync_checked_at,
        "endpoint": diagnostic.get("endpoint"),
        "status_code": diagnostic.get("status_code"),
        "message": diagnostic.get("message"),
        "hint": diagnostic.get("hint"),
        "request_id": diagnostic.get("request_id"),
    }


def update_models_sync(
    profile: ProviderProfile,
    *,
    state: str,
    endpoint: str | None = None,
    status_code: int | None = None,
    message: str | None = None,
    hint: str | None = None,
    request_id: str | None = None,
) -> None:
    profile.models_sync_state = state if state in MODEL_SYNC_STATES else "error"
    profile.models_sync_checked_at = utcnow()
    profile.models_sync_diagnostic = {
        key: value
        for key, value in {
            "endpoint": endpoint,
            "status_code": status_code,
            "message": message,
            "hint": hint,
            "request_id": request_id,
        }.items()
        if value is not None
    }
