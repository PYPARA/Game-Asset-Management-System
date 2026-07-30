from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from .domain import TaskKind
from .models import ProviderProfile, ProviderRoutingDefaults, utcnow


Modality = Literal["text", "image"]

IMAGE_HINTS = (
    "dall-e",
    "flux",
    "image",
    "imagen",
    "sdxl",
    "stable-diffusion",
)
TEXT_HINTS = (
    "chat",
    "claude",
    "deepseek",
    "gemini",
    "gpt",
    "llama",
    "qwen",
    "text",
)


def required_modality(kind: str | TaskKind) -> Modality:
    return "text" if str(kind) == TaskKind.TEXT.value else "image"


def _metadata_modalities(item: dict[str, Any]) -> list[Modality]:
    values: list[str] = []
    for key in ("modalities", "capabilities", "input_modalities", "output_modalities"):
        raw = item.get(key)
        if isinstance(raw, dict):
            values.extend(str(name) for name, enabled in raw.items() if enabled)
        elif isinstance(raw, list):
            values.extend(str(value) for value in raw)
        elif isinstance(raw, str):
            values.append(raw)
    lowered = " ".join(values).lower()
    modalities: list[Modality] = []
    if any(token in lowered for token in ("text", "chat", "completion", "language")):
        modalities.append("text")
    if any(token in lowered for token in ("image", "vision-generation", "image_generation")):
        modalities.append("image")
    return modalities


def classify_model(item: dict[str, Any]) -> tuple[list[Modality], str]:
    metadata = _metadata_modalities(item)
    if metadata:
        return sorted(set(metadata)), "provider"
    model_id = str(item.get("id", "")).lower()
    if any(token in model_id for token in IMAGE_HINTS):
        return ["image"], "heuristic"
    if any(token in model_id for token in TEXT_HINTS):
        return ["text"], "heuristic"
    return [], "unknown"


def _catalog_item(value: dict[str, Any]) -> dict[str, Any] | None:
    model_id = str(value.get("id", "")).strip()
    if not model_id:
        return None
    modalities = [
        modality
        for modality in value.get("modalities", [])
        if modality in {"text", "image"}
    ]
    classification = str(value.get("classification", "unknown"))
    if classification not in {"provider", "heuristic", "manual", "unknown"}:
        classification = "unknown"
    return {
        "id": model_id,
        "modalities": sorted(set(modalities)),
        "classification": classification,
        "available": bool(value.get("available", True)),
    }


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
        if previous and previous["classification"] == "manual":
            modalities = list(previous["modalities"])
            classification = "manual"
        catalog[model_id] = {
            "id": model_id,
            "modalities": modalities,
            "classification": classification,
            "available": True,
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
        modalities = sorted(
            {
                modality
                for modality in value.get("modalities", [])
                if modality in {"text", "image"}
            }
        )
        catalog[model_id] = {
            "id": model_id,
            "modalities": modalities,
            "classification": "manual",
            "available": bool(previous and previous.get("available")),
        }
    return sorted(catalog.values(), key=lambda item: item["id"].lower())


def ensure_profile_default_models(profile: ProviderProfile) -> None:
    overrides: list[dict[str, Any]] = []
    by_id = {str(item.get("id")): item for item in profile.models_json or []}
    for model_id, modality in (
        (profile.text_model, "text"),
        (profile.image_model, "image"),
    ):
        existing = by_id.get(model_id)
        if existing and modality in existing.get("modalities", []):
            continue
        modalities = set(existing.get("modalities", []) if existing else [])
        modalities.add(modality)
        overrides.append({"id": model_id, "modalities": sorted(modalities)})
    if overrides:
        profile.models_json = apply_model_overrides(profile.models_json or [], overrides)


def model_is_compatible(profile: ProviderProfile, model_id: str, modality: Modality) -> bool:
    for item in profile.models_json or []:
        if str(item.get("id")) != model_id:
            continue
        modalities = [value for value in item.get("modalities", []) if value in {"text", "image"}]
        classification = str(item.get("classification", "unknown"))
        return classification == "unknown" or modality in modalities
    return True


def ensure_routing_defaults(session: Session) -> ProviderRoutingDefaults:
    defaults = session.get(ProviderRoutingDefaults, "global")
    if defaults is not None:
        return defaults
    first = session.scalar(
        select(ProviderProfile)
        .where(ProviderProfile.is_active.is_(True))
        .order_by(ProviderProfile.created_at, ProviderProfile.id)
        .limit(1)
    )
    defaults = ProviderRoutingDefaults(
        id="global",
        text_provider_profile_id=first.id if first else None,
        text_model=first.text_model if first else None,
        image_provider_profile_id=first.id if first else None,
        image_model=first.image_model if first else None,
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
        "pricing": profile.pricing,
        "captured_at": utcnow().isoformat(),
    }
