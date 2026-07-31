"""Deterministic Release v2 export and game-checkout Delivery transactions.

The Project owns Release manifests and Delivery receipts.  A checkout is an
explicit target only; this module never runs Git commands and never deletes a
file that is not listed by the previous ``gams-lock.json``.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Delivery, Project, Release, new_id, utcnow
from .storage import (
    ProjectStore,
    StorageError,
    atomic_write_bytes,
    canonical_json,
    safe_join,
    sha256_bytes,
    sha256_file,
)


class DeliveryError(RuntimeError):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code


def _qualified(value: str) -> str:
    return value if value.startswith("sha256:") else f"sha256:{value}"


def _manifest_snapshot_payload(manifest: dict[str, Any]) -> dict[str, Any]:
    # Display names, timestamps and release IDs must not affect the content
    # snapshot.  The asset array is already normalized by Release creation.
    return {
        "format_version": 2,
        "project_id": str(manifest.get("project_id", "")),
        "assets": manifest.get("assets", []),
    }


def manifest_snapshot_hash(manifest: dict[str, Any]) -> str:
    return _qualified(sha256_bytes(canonical_json(_manifest_snapshot_payload(manifest))))


def _project(session: Session, project_id: str) -> Project:
    project = session.get(Project, project_id)
    if project is None:
        raise DeliveryError(404, f"project not found: {project_id}")
    return project


def _release(session: Session, project_id: str, release_id: str) -> Release:
    release = session.get(Release, release_id)
    if release is None or release.project_id != project_id:
        raise DeliveryError(404, f"release not found: {release_id}")
    return release


def _read_release_manifest(project: Project, release: Release) -> tuple[ProjectStore, dict[str, Any], str]:
    store = ProjectStore(project.root_path)
    try:
        manifest_path = safe_join(store.root, release.manifest_path)
        manifest = store.read_json(manifest_path)
        actual_hash = sha256_file(manifest_path)
    except (OSError, StorageError) as exc:
        raise DeliveryError(409, f"release manifest cannot be read: {exc}") from exc
    if actual_hash != release.manifest_hash:
        raise DeliveryError(409, "release manifest hash no longer matches the Release index")
    if manifest.get("format_version") != 2:
        raise DeliveryError(409, "external Delivery requires a Release Manifest v2")
    if str(manifest.get("project_id")) != project.id:
        raise DeliveryError(409, "release manifest belongs to another Project")
    expected_snapshot = manifest_snapshot_hash(manifest)
    if manifest.get("snapshot_hash") != expected_snapshot:
        raise DeliveryError(409, "release snapshot hash is invalid")
    if release.snapshot_hash and release.snapshot_hash != expected_snapshot:
        raise DeliveryError(409, "release snapshot hash does not match the Release index")
    return store, manifest, actual_hash


def _validate_relative(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DeliveryError(422, f"{label} must be a non-empty relative path")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise DeliveryError(422, f"{label} escapes the checkout")
    return path.as_posix()


def export_config(store: ProjectStore) -> dict[str, Any]:
    try:
        configured = store.export_contract()
    except StorageError as exc:
        raise DeliveryError(422, str(exc)) from exc
    paths = {
        key: _validate_relative(configured.get(key), f"export.{key}")
        for key in ("content_path", "manifest_path", "assets_path", "lock_path")
    }
    commands: list[dict[str, Any]] = []
    for index, command in enumerate(configured.get("validation_commands", [])):
        if not isinstance(command, dict) or not isinstance(command.get("argv"), list):
            raise DeliveryError(422, f"export.validation_commands[{index}] must contain argv")
        argv = command["argv"]
        if not argv or any(not isinstance(value, str) or not value for value in argv):
            raise DeliveryError(422, f"export.validation_commands[{index}].argv is invalid")
        raw_env = command.get("env", {})
        if raw_env is None:
            raw_env = {}
        if not isinstance(raw_env, dict):
            raise DeliveryError(422, f"export.validation_commands[{index}].env must be an object")
        env: dict[str, str] = {}
        for name, value in raw_env.items():
            if (
                not isinstance(name, str)
                or not re.fullmatch(r"(?:CI|PNPM_[A-Z0-9_]+|npm_config_[a-z0-9_]+)", name)
                or not isinstance(value, str)
                or "\x00" in value
            ):
                raise DeliveryError(
                    422,
                    f"export.validation_commands[{index}].env contains an unsupported entry",
                )
            env[name] = value
        commands.append(
            {
                "argv": list(argv),
                "label": str(command.get("label") or " ".join(argv)),
                "env": env,
            }
        )
    try:
        format_version = int(configured.get("format_version", 1))
    except (TypeError, ValueError) as exc:
        raise DeliveryError(422, "export.format_version must be an integer") from exc
    return {"format_version": format_version, **paths, "validation_commands": commands}


def _checkout_root(store: ProjectStore, override: str | None, *, require_exists: bool) -> Path:
    try:
        configured = store.local_export_config().get("game_root")
    except StorageError as exc:
        raise DeliveryError(422, f"game checkout binding cannot be read: {exc}") from exc
    raw = override or configured
    if not isinstance(raw, str) or not raw.strip():
        raise DeliveryError(422, "game checkout is not configured; set project.local.yaml game_root")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise DeliveryError(422, "game_root must be an absolute path")
    resolved = path.resolve(strict=False)
    if require_exists and not resolved.is_dir():
        raise DeliveryError(422, f"game checkout does not exist: {resolved}")
    return resolved


def _checkout_fingerprint(root: Path) -> str:
    try:
        stat = root.stat()
        identity = {"name": root.name, "device": stat.st_dev, "inode": stat.st_ino}
    except OSError:
        identity = {"name": root.name, "path_hint": root.as_posix()}
    return _qualified(sha256_bytes(canonical_json(identity)))


def _target_path(asset: dict[str, Any], rendition: dict[str, Any], config: dict[str, Any]) -> str:
    configured = rendition.get("target_path")
    if configured:
        value = _validate_relative(configured, f"{asset.get('key')}.target_path")
    else:
        key = str(asset.get("key", "asset")).replace(".", "/")
        extension = str(rendition.get("media_type", "image/webp")).split("/")[-1] or "bin"
        value = f"{config['assets_path'].rstrip('/')}/{key}.{extension}"
    assets_prefix = config["assets_path"].rstrip("/") + "/"
    if not value.casefold().startswith(assets_prefix.casefold()):
        raise DeliveryError(422, f"{asset.get('key')}: target path must be under export.assets_path")
    return value


def _release_assets(
    store: ProjectStore,
    manifest: dict[str, Any],
    config: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[str]]:
    records: list[dict[str, Any]] = []
    issues: list[str] = []
    targets: set[str] = set()
    seen_keys: set[str] = set()
    raw_assets = manifest.get("assets", [])
    if not isinstance(raw_assets, list):
        return records, ["manifest assets must be a list"]
    for asset in sorted(raw_assets, key=lambda value: str(value.get("key", "")) if isinstance(value, dict) else ""):
        if not isinstance(asset, dict) or not asset.get("key"):
            issues.append("manifest contains an invalid asset entry")
            continue
        asset_key = str(asset["key"])
        if asset_key in seen_keys:
            issues.append(f"manifest contains duplicate asset key: {asset_key}")
            continue
        seen_keys.add(asset_key)
        entry = {
            "key": asset_key,
            "kind": str(asset.get("kind", "")),
            "subtype": str(asset.get("subtype", "")),
            "revision_id": str(asset.get("revision_id", "")),
            "content_hash": str(asset.get("content_hash", "")),
            "dependency_hash": str(asset.get("dependency_hash", "")),
            "content": asset.get("content"),
            "renditions": [],
        }
        raw_renditions = asset.get("renditions", [])
        if not isinstance(raw_renditions, list):
            issues.append(f"{asset_key}: renditions must be a list")
            continue
        asset_failed = False
        asset_targets: set[str] = set()
        for rendition in sorted(raw_renditions, key=lambda value: str(value.get("id", "")) if isinstance(value, dict) else ""):
            if not isinstance(rendition, dict):
                issues.append(f"{asset_key}: invalid rendition")
                asset_failed = True
                continue
            source = str(rendition.get("path") or rendition.get("normalized_path") or "")
            if not source or source.startswith("workspace/"):
                issues.append(f"{asset_key}: rendition is not durable")
                asset_failed = True
                continue
            try:
                source_path = safe_join(store.root, source)
            except StorageError as exc:
                issues.append(f"{asset_key}: {exc}")
                asset_failed = True
                continue
            if not source_path.is_file():
                issues.append(f"{asset_key}: runtime blob is missing: {source}")
                asset_failed = True
                continue
            expected_hash = str(rendition.get("sha256", ""))
            try:
                actual_hash = sha256_file(source_path)
            except OSError as exc:
                issues.append(f"{asset_key}: runtime blob cannot be verified: {exc}")
                asset_failed = True
                continue
            if actual_hash != expected_hash:
                issues.append(f"{asset_key}: runtime blob hash mismatch")
                asset_failed = True
                continue
            try:
                target = _target_path(entry, rendition, config)
            except DeliveryError as exc:
                issues.append(str(exc))
                asset_failed = True
                continue
            folded = target.casefold()
            if folded in targets or folded in asset_targets:
                issues.append(f"target path collision: {target}")
                asset_failed = True
                continue
            asset_targets.add(folded)
            try:
                byte_size = int(rendition.get("byte_size", source_path.stat().st_size))
            except (OSError, TypeError, ValueError) as exc:
                issues.append(f"{asset_key}: rendition byte size is invalid: {exc}")
                asset_failed = True
                continue
            entry["renditions"].append({
                "id": str(rendition.get("id", "")),
                "artifact_id": rendition.get("artifact_id"),
                "path": source,
                "target_path": target,
                "media_type": str(rendition.get("media_type", "application/octet-stream")),
                "sha256": expected_hash,
                "width": rendition.get("width"),
                "height": rendition.get("height"),
                "byte_size": byte_size,
            })
        if not asset_failed:
            targets.update(asset_targets)
            records.append(entry)
    return records, issues


def release_preflight(session: Session, *, project_id: str, release_id: str) -> dict[str, Any]:
    project = _project(session, project_id)
    release = _release(session, project_id, release_id)
    issues: list[str] = []
    store = ProjectStore(project.root_path)
    manifest: dict[str, Any] | None = None
    manifest_hash: str | None = None
    try:
        manifest_path = safe_join(store.root, release.manifest_path)
        manifest = store.read_json(manifest_path)
        manifest_hash = sha256_file(manifest_path)
    except (OSError, StorageError) as exc:
        issues.append(f"release manifest cannot be read: {exc}")

    if manifest_hash is not None and manifest_hash != release.manifest_hash:
        issues.append("release manifest hash no longer matches the Release index")
    if manifest is not None:
        if manifest.get("format_version") != 2:
            issues.append("external Delivery requires a Release Manifest v2")
        if str(manifest.get("project_id")) != project.id:
            issues.append("release manifest belongs to another Project")
        if manifest.get("format_version") == 2:
            expected_snapshot = manifest_snapshot_hash(manifest)
            if manifest.get("snapshot_hash") != expected_snapshot:
                issues.append("release snapshot hash is invalid")
            if release.snapshot_hash and release.snapshot_hash != expected_snapshot:
                issues.append("release snapshot hash does not match the Release index")

    try:
        config = export_config(store)
    except DeliveryError as exc:
        issues.append(str(exc))
        config = {}
    assets: list[dict[str, Any]] = []
    if manifest is not None and config:
        assets, asset_issues = _release_assets(store, manifest, config)
        issues.extend(asset_issues)
    return {
        "project_id": project.id,
        "release_id": release.id,
        "manifest_version": manifest.get("format_version") if manifest else release.manifest_version,
        "manifest_hash": _qualified(manifest_hash) if manifest_hash else None,
        "snapshot_hash": manifest.get("snapshot_hash") if manifest else release.snapshot_hash,
        "issues": issues,
        "blocking": bool(issues),
        "assets": assets,
        "export_config": config,
    }


def _read_lock(root: Path, config: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
    path = safe_join(root, config["lock_path"])
    if not path.exists():
        return None, []
    try:
        value = json.loads(path.read_text("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("lock must be an object")
        managed = value.get("managed_files", [])
        if not isinstance(managed, list):
            raise ValueError("lock.managed_files must be a list")
        issues: list[str] = []
        for index, item in enumerate(managed):
            if not isinstance(item, dict) or not isinstance(item.get("path"), str) or not item.get("path"):
                issues.append(f"lock.managed_files[{index}] has an invalid path")
            elif not isinstance(item.get("sha256"), str) or not item.get("sha256"):
                issues.append(f"lock.managed_files[{index}] has an invalid sha256")
        return value, issues
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return None, [f"lock cannot be read: {exc}"]


def _generated_files(
    store: ProjectStore,
    manifest: dict[str, Any],
    assets: list[dict[str, Any]],
    config: dict[str, Any],
) -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    content_payload = [
        {"key": item["key"], "kind": item["kind"], "subtype": item["subtype"], "content": item["content"]}
        for item in assets
        if item["kind"] != "media"
    ]
    if content_payload:
        content_bytes = (
            "export const contentRegistry = "
            + json.dumps(content_payload, ensure_ascii=False, sort_keys=True, indent=2)
            + " as const;\nexport default contentRegistry;\n"
        ).encode("utf-8")
        files.append({"path": f"{config['content_path'].rstrip('/')}/registry.ts", "bytes": content_bytes, "kind": "content"})

    manifest_assets = []
    runtime_entries: list[dict[str, Any]] = []
    preload_groups = {
        "portrait": "portraits-core",
        "background": "backgrounds",
        "cg": "cgs",
        "icon": "icons",
        "ending": "endings",
    }
    for item in assets:
        media = []
        for rendition in item["renditions"]:
            media.append({key: rendition.get(key) for key in (
                "artifact_id", "target_path", "media_type", "sha256", "width", "height", "byte_size"
            )})
            source_path = safe_join(store.root, rendition["path"])
            files.append({"path": rendition["target_path"], "source": source_path, "kind": "media"})
            content = item.get("content") if isinstance(item.get("content"), dict) else {}
            fallback_key = content.get("fallback_key")
            runtime_entry: dict[str, Any] = {
                "key": item["key"],
                "type": item.get("subtype") or "media",
                "path": rendition["target_path"],
                "width": rendition.get("width"),
                "height": rendition.get("height"),
                "preloadGroup": content.get("preload_group")
                or preload_groups.get(str(item.get("subtype")), "media"),
            }
            if content.get("character_id") is not None:
                runtime_entry["characterId"] = content["character_id"]
            if content.get("expression") is not None:
                runtime_entry["expression"] = content["expression"]
            if fallback_key:
                runtime_entry["fallbackKey"] = fallback_key
            else:
                runtime_entry["fallbackPolicy"] = content.get("fallback_policy") or "none"
            runtime_entries.append(runtime_entry)
        manifest_assets.append({
            key: item.get(key)
            for key in ("key", "kind", "subtype", "revision_id", "content_hash", "dependency_hash")
        } | {"media": media})
    manifest_payload = {
        "format_version": 2,
        "release_id": manifest.get("release_id"),
        "project_id": manifest.get("project_id"),
        "snapshot_hash": manifest.get("snapshot_hash"),
        "assets": manifest_assets,
    }
    # The JSON Release remains the canonical v2 snapshot. The generated
    # TypeScript adapter exposes the stable array contract consumed by Emperor's
    # runtime (and keeps the type aliases broad enough for other game projects).
    manifest_bytes = (
        "export type AssetType = string;\n"
        "export type AssetPreloadGroup = string;\n"
        "export interface AssetManifestEntryBase { key: string; type: AssetType; path: string; width: number; height: number; preloadGroup: AssetPreloadGroup; characterId?: string; expression?: string }\n"
        "export type AssetManifestEntry = AssetManifestEntryBase & ({ fallbackKey: string; fallbackPolicy?: never } | { fallbackKey?: never; fallbackPolicy: string })\n"
        "export const releaseManifest = "
        + json.dumps(manifest_payload, ensure_ascii=False, sort_keys=True, indent=2)
        + " as const;\n"
        "export const assetManifest = "
        + json.dumps(sorted(runtime_entries, key=lambda value: value["key"]), ensure_ascii=False, indent=2)
        + " as const satisfies readonly AssetManifestEntry[];\n"
        "export default assetManifest;\n"
    ).encode("utf-8")
    files.append({"path": config["manifest_path"], "bytes": manifest_bytes, "kind": "manifest"})
    return sorted(files, key=lambda value: value["path"])


def _file_facts(root: Path, files: list[dict[str, Any]]) -> list[dict[str, Any]]:
    facts: list[dict[str, Any]] = []
    for item in files:
        target = safe_join(root, item["path"])
        if "bytes" in item:
            data = item["bytes"]
            digest = sha256_bytes(data)
            size = len(data)
        else:
            digest = sha256_file(item["source"])
            size = item["source"].stat().st_size
        facts.append({"path": item["path"], "sha256": _qualified(digest), "byte_size": size, "kind": item["kind"]})
    return facts


def _lock_for(
    project: Project,
    release: Release,
    manifest: dict[str, Any],
    facts: list[dict[str, Any]],
    config: dict[str, Any],
) -> dict[str, Any]:
    return {
        "format_version": 1,
        "project_id": project.id,
        "release_id": release.id,
        "release_manifest_hash": _qualified(release.manifest_hash),
        "snapshot_hash": manifest["snapshot_hash"],
        "exporter_version": "gams-exporter/2",
        "delivered_at": utcnow().isoformat(),
        "managed_files": [
            {"path": fact["path"], "sha256": fact["sha256"], "byte_size": fact["byte_size"]}
            for fact in sorted(facts, key=lambda value: value["path"])
        ],
        "manifest_path": config["manifest_path"],
    }


def _preview_changes(root: Path, old_lock: dict[str, Any] | None, desired: list[dict[str, Any]], lock_path: str) -> tuple[list[dict[str, Any]], list[str]]:
    issues: list[str] = []
    desired_facts = _file_facts(root, desired)
    desired_by_path = {fact["path"]: fact for fact in desired_facts}
    old_files = old_lock.get("managed_files", []) if isinstance(old_lock, dict) else []
    old_by_path = {str(item.get("path")): item for item in old_files if isinstance(item, dict)}
    changes: list[dict[str, Any]] = []
    for path, old in sorted(old_by_path.items()):
        try:
            target = safe_join(root, path)
        except StorageError as exc:
            issues.append(f"managed file path is invalid: {path} ({exc})")
            continue
        if not target.is_file():
            if path not in desired_by_path:
                issues.append(f"managed file is missing: {path}")
            else:
                issues.append(f"managed path is not a file: {path}")
            continue
        actual = _qualified(sha256_file(target))
        if actual != str(old.get("sha256")):
            issues.append(f"managed file was modified outside GAMS: {path}")
    for path, fact in sorted(desired_by_path.items()):
        try:
            target = safe_join(root, path)
        except StorageError as exc:
            issues.append(str(exc))
            continue
        if not target.exists():
            changes.append({"path": path, "action": "add", "sha256": fact["sha256"], "byte_size": fact["byte_size"]})
        elif not target.is_file():
            issues.append(f"target path is occupied by a non-file: {path}")
            changes.append({"path": path, "action": "conflict", "sha256": fact["sha256"], "byte_size": fact["byte_size"]})
        elif path == lock_path and isinstance(old_lock, dict):
            # The lock is the transaction marker and is intentionally not part
            # of managed_files.  A parseable existing lock is nevertheless a
            # GAMS-owned file, so it may be replaced after the other files pass.
            changes.append({"path": path, "action": "write", "sha256": fact["sha256"], "byte_size": fact["byte_size"]})
        elif path not in old_by_path:
            issues.append(f"target path is occupied by an unmanaged file: {path}")
            changes.append({"path": path, "action": "conflict", "sha256": fact["sha256"], "byte_size": fact["byte_size"]})
        elif _qualified(sha256_file(target)) == fact["sha256"]:
            changes.append({"path": path, "action": "keep", "sha256": fact["sha256"], "byte_size": fact["byte_size"]})
        else:
            changes.append({"path": path, "action": "update", "sha256": fact["sha256"], "byte_size": fact["byte_size"]})
    for path in sorted(set(old_by_path) - set(desired_by_path)):
        changes.append({"path": path, "action": "delete", "sha256": str(old_by_path[path].get("sha256"))})
    if lock_path in desired_by_path:
        # The lock is always written last, and its timestamp means it is not a
        # reliable no-op signal; compare the previous release/snapshot instead.
        changes = [change for change in changes if change["path"] != lock_path]
    return changes, issues


def export_preview(session: Session, *, project_id: str, release_id: str, game_root: str | None = None) -> dict[str, Any]:
    project = _project(session, project_id)
    release = _release(session, project_id, release_id)
    base = release_preflight(session, project_id=project_id, release_id=release_id)
    store = ProjectStore(project.root_path)
    config = base["export_config"]
    try:
        root = _checkout_root(store, game_root, require_exists=False)
    except DeliveryError as exc:
        issues = list(base["issues"]) + [str(exc)]
        return {
            **base,
            "game_root": game_root,
            "changes": [],
            "issues": issues,
            "blocking": True,
            "no_op": False,
        }
    if not root.is_dir():
        issues = list(base["issues"]) + [f"game checkout does not exist: {root}"]
        return {
            **base,
            "game_root": root.as_posix(),
            "changes": [],
            "issues": issues,
            "blocking": True,
            "no_op": False,
        }
    if base["blocking"] or not config:
        return {
            **base,
            "game_root": root.as_posix(),
            "checkout_fingerprint": _checkout_fingerprint(root),
            "previous_release_id": None,
            "changes": [],
            "no_op": False,
            "validation_commands": config.get("validation_commands", []),
        }
    old_lock, lock_issues = _read_lock(root, config)
    if isinstance(old_lock, dict) and old_lock.get("project_id") not in {None, project.id}:
        lock_issues.append("gams-lock.json belongs to another Project")
    try:
        _release_store, manifest, _manifest_hash = _read_release_manifest(project, release)
    except DeliveryError as exc:
        # The release was checked above, but reading it again closes the race
        # between preflight and planning without exposing a JSON/IO exception.
        lock_issues.append(str(exc))
        manifest = None
    if manifest is None:
        return {
            **base,
            "game_root": root.as_posix(),
            "checkout_fingerprint": _checkout_fingerprint(root),
            "previous_release_id": old_lock.get("release_id") if isinstance(old_lock, dict) else None,
            "changes": [],
            "issues": list(base["issues"]) + lock_issues,
            "blocking": True,
            "no_op": False,
            "validation_commands": config["validation_commands"],
        }
    desired = _generated_files(store, manifest, base["assets"], config)
    desired_facts = _file_facts(root, desired)
    provisional_lock = _lock_for(project, release, manifest, desired_facts, config)
    lock_bytes = (json.dumps(provisional_lock, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    desired_with_lock = desired + [{"path": config["lock_path"], "bytes": lock_bytes, "kind": "lock"}]
    changes, conflicts = _preview_changes(root, old_lock, desired_with_lock, config["lock_path"])
    issues = list(base["issues"]) + lock_issues + conflicts
    current_release = old_lock.get("release_id") if isinstance(old_lock, dict) else None
    no_op = bool(
        not issues
        and isinstance(old_lock, dict)
        and old_lock.get("release_id") == release.id
        and old_lock.get("snapshot_hash") == base["snapshot_hash"]
        and all(change["action"] == "keep" for change in changes)
    )
    return {
        **base,
        "game_root": root.as_posix(),
        "checkout_fingerprint": _checkout_fingerprint(root),
        "previous_release_id": current_release,
        "changes": changes,
        "issues": issues,
        "blocking": bool(issues),
        "no_op": no_op,
        "validation_commands": config["validation_commands"],
    }


def _atomic_checkout_write(root: Path, relative: str, content: bytes) -> None:
    target = safe_join(root, relative)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    os.close(descriptor)
    try:
        Path(temporary).write_bytes(content)
        with open(temporary, "rb") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _run_commands(root: Path, commands: list[dict[str, Any]]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for command in commands:
        argv = list(command["argv"])
        started = datetime.now(UTC)
        try:
            completed = subprocess.run(
                argv,
                cwd=root,
                shell=False,
                check=False,
                capture_output=True,
                text=True,
                timeout=600,
                env={**os.environ, **command.get("env", {})},
            )
            result = {
                "argv": argv,
                "cwd": root.name,
                "started_at": started.isoformat(),
                "ended_at": datetime.now(UTC).isoformat(),
                "exit_code": completed.returncode,
                "stdout": completed.stdout[-4000:],
                "stderr": completed.stderr[-4000:],
            }
        except (OSError, subprocess.TimeoutExpired) as exc:
            result = {
                "argv": argv,
                "cwd": root.name,
                "started_at": started.isoformat(),
                "ended_at": datetime.now(UTC).isoformat(),
                "exit_code": -1,
                "stdout": "",
                "stderr": str(exc),
            }
        results.append(result)
        if result["exit_code"] != 0:
            break
    return results


def _record_delivery(
    session: Session,
    *,
    project: Project,
    release: Release,
    status: str,
    root: Path,
    previous_release_id: str | None,
    files: list[dict[str, Any]],
    validation_results: list[dict[str, Any]],
    rollback: dict[str, Any] | None,
) -> Delivery:
    created_at = utcnow()
    receipt = {
        "id": new_id().replace("-", ""),
        "project_id": project.id,
        "release_id": release.id,
        "release_manifest_hash": _qualified(release.manifest_hash),
        "snapshot_hash": release.snapshot_hash or "",
        "checkout_fingerprint": _checkout_fingerprint(root),
        "display_path": root.as_posix(),
        "status": status,
        "previous_release_id": previous_release_id,
        "files": files,
        "validation_results": validation_results,
        "rollback": rollback,
        "created_at": created_at.isoformat(),
    }
    store = ProjectStore(project.root_path)
    try:
        store.write_delivery(receipt)
    except (OSError, StorageError) as exc:
        raise DeliveryError(500, f"cannot write Delivery receipt: {exc}") from exc
    row = Delivery(
        id=receipt["id"],
        project_id=project.id,
        release_id=release.id,
        release_manifest_hash=receipt["release_manifest_hash"],
        snapshot_hash=receipt["snapshot_hash"],
        checkout_fingerprint=receipt["checkout_fingerprint"],
        display_path=receipt["display_path"],
        status=status,
        previous_release_id=previous_release_id,
        files_json=files,
        validation_results_json=validation_results,
        rollback_json=rollback,
        created_at=created_at,
    )
    session.add(row)
    session.commit()
    return row


def apply_export(
    session: Session,
    *,
    project_id: str,
    release_id: str,
    game_root: str | None = None,
    run_commands: bool = True,
    rollback_from: str | None = None,
) -> Delivery:
    project = _project(session, project_id)
    release = _release(session, project_id, release_id)
    preview = export_preview(session, project_id=project_id, release_id=release_id, game_root=game_root)
    # Preview is the user-visible plan, but the checkout may change while the
    # request is waiting in the API.  Re-read it immediately before staging
    # so a managed-file edit cannot slip between preview and apply.
    if not preview["blocking"]:
        preview = export_preview(session, project_id=project_id, release_id=release_id, game_root=game_root)
    if preview["blocking"]:
        raise DeliveryError(409, "export preview failed:\n- " + "\n- ".join(preview["issues"]))
    raw_root = preview.get("game_root")
    if not isinstance(raw_root, str) or not raw_root:
        raise DeliveryError(409, "export preview did not resolve a game checkout")
    root = Path(raw_root)
    if preview["no_op"]:
        return _record_delivery(
            session,
            project=project,
            release=release,
            status="no_op",
            root=root,
            previous_release_id=preview.get("previous_release_id"),
            files=preview["changes"],
            validation_results=[],
            rollback={"from_release_id": rollback_from} if rollback_from else None,
        )

    store, manifest, _manifest_hash = _read_release_manifest(project, release)
    config = preview["export_config"]
    assets = preview["assets"]
    desired = _generated_files(store, manifest, assets, config)
    desired_facts = _file_facts(root, desired)
    lock = _lock_for(project, release, manifest, desired_facts, config)
    lock_bytes = (json.dumps(lock, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    desired_with_lock = desired + [{"path": config["lock_path"], "bytes": lock_bytes, "kind": "lock"}]
    delivery_id = new_id().replace("-", "")
    staging = Path(tempfile.mkdtemp(prefix=f".gams-stage-{delivery_id}-", dir=root))
    backup = staging / "backup"
    changed_files = [change for change in preview["changes"] if change["action"] != "keep"]
    previous_release_id = preview.get("previous_release_id")
    backups: list[tuple[Path, Path]] = []
    created: list[Path] = []
    validation_results: list[dict[str, Any]] = []
    keep_staging = False
    try:
        for item in desired_with_lock:
            target = safe_join(root, item["path"])
            stage_path = safe_join(staging, item["path"])
            stage_path.parent.mkdir(parents=True, exist_ok=True)
            if "bytes" in item:
                stage_path.write_bytes(item["bytes"])
            else:
                shutil.copy2(item["source"], stage_path)
            if target.exists():
                backup_path = safe_join(backup, item["path"])
                backup_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, backup_path)
                backups.append((target, backup_path))
            else:
                created.append(target)
        old_lock, _ = _read_lock(root, config)
        old_by_path = {
            str(item.get("path")): item
            for item in (old_lock or {}).get("managed_files", [])
            if isinstance(item, dict)
        }
        for path in sorted(set(old_by_path) - {item["path"] for item in desired_with_lock}):
            target = safe_join(root, path)
            if target.exists():
                backup_path = safe_join(backup, path)
                backup_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, backup_path)
                backups.append((target, backup_path))
        for item in desired_with_lock:
            if item["path"] == config["lock_path"]:
                continue
            stage_path = safe_join(staging, item["path"])
            _atomic_checkout_write(root, item["path"], stage_path.read_bytes())
        for path in sorted(set(old_by_path) - {item["path"] for item in desired_with_lock}):
            safe_join(root, path).unlink(missing_ok=True)
        if run_commands:
            validation_results = _run_commands(root, config["validation_commands"])
            if any(result["exit_code"] != 0 for result in validation_results):
                raise DeliveryError(409, "game checkout validation failed")
        # The lock is the final visible write and therefore the success marker.
        lock_stage = safe_join(staging, config["lock_path"])
        _atomic_checkout_write(root, config["lock_path"], lock_stage.read_bytes())
        status = "rolled_back" if rollback_from else "succeeded"
        files = changed_files + [{"path": config["lock_path"], "action": "write", "sha256": _qualified(sha256_bytes(lock_bytes)), "byte_size": len(lock_bytes)}]
        shutil.rmtree(staging, ignore_errors=True)
        return _record_delivery(
            session,
            project=project,
            release=release,
            status=status,
            root=root,
            previous_release_id=previous_release_id,
            files=files,
            validation_results=validation_results,
            rollback={"from_release_id": rollback_from} if rollback_from else None,
        )
    except Exception as exc:
        recovery_errors: list[str] = []
        for target in reversed(created):
            try:
                target.unlink(missing_ok=True)
            except OSError as restore_error:
                recovery_errors.append(f"cannot remove {target}: {restore_error}")
        for target, backup_path in reversed(backups):
            try:
                _atomic_checkout_write(root, target.relative_to(root).as_posix(), backup_path.read_bytes())
            except (OSError, StorageError) as restore_error:
                recovery_errors.append(f"cannot restore {target}: {restore_error}")
        status = "recovery_required" if recovery_errors else "failed"
        if recovery_errors:
            keep_staging = True
        try:
            _record_delivery(
                session,
                project=project,
                release=release,
                status=status,
                root=root,
                previous_release_id=previous_release_id,
                files=changed_files,
                validation_results=validation_results,
                rollback={"from_release_id": rollback_from} if rollback_from else None,
            )
        except Exception:
            session.rollback()
        if isinstance(exc, DeliveryError) and not recovery_errors:
            raise
        if recovery_errors:
            detail = "export apply failed; manual recovery is required: " + "; ".join(recovery_errors)
            raise DeliveryError(409, detail) from exc
        raise DeliveryError(409, f"export apply failed and was restored: {exc}") from exc
    finally:
        if staging.exists() and not keep_staging:
            shutil.rmtree(staging, ignore_errors=True)


def verify_export(
    session: Session,
    *,
    project_id: str,
    release_id: str | None = None,
    game_root: str | None = None,
    run_commands: bool = False,
) -> dict[str, Any]:
    project = _project(session, project_id)
    store = ProjectStore(project.root_path)
    root = _checkout_root(store, game_root, require_exists=True)
    config = export_config(store)
    lock, lock_issues = _read_lock(root, config)
    selected_release_id = release_id or (lock or {}).get("release_id")
    if not selected_release_id:
        raise DeliveryError(409, "checkout has no gams-lock.json release")
    preview = export_preview(session, project_id=project_id, release_id=str(selected_release_id), game_root=root.as_posix())
    issues = list(lock_issues) + list(preview.get("issues", []))
    if lock is None:
        issues.append("gams-lock.json is missing")
    else:
        if lock.get("release_id") != selected_release_id:
            issues.append("gams-lock.json points at another Release")
        if lock.get("snapshot_hash") != preview["snapshot_hash"]:
            issues.append("gams-lock.json snapshot hash does not match the Release")
        for item in lock.get("managed_files", []):
            if not isinstance(item, dict):
                issues.append("gams-lock.json contains an invalid managed file")
                continue
            path = str(item.get("path", ""))
            try:
                target = safe_join(root, path)
            except StorageError as exc:
                issues.append(str(exc))
                continue
            if not target.is_file():
                issues.append(f"managed file is missing: {path}")
            elif _qualified(sha256_file(target)) != str(item.get("sha256")):
                issues.append(f"managed file hash mismatch: {path}")
    validation_results = _run_commands(root, config["validation_commands"]) if run_commands else []
    if any(result["exit_code"] != 0 for result in validation_results):
        issues.append("game checkout validation failed")
    return {
        "project_id": project.id,
        "release_id": selected_release_id,
        "game_root": root.as_posix(),
        "snapshot_hash": preview["snapshot_hash"],
        "ok": not issues,
        "issues": issues,
        "validation_results": validation_results,
        "lock": lock,
    }


def list_deliveries(session: Session, *, project_id: str) -> list[Delivery]:
    return list(
        session.scalars(
            select(Delivery).where(Delivery.project_id == project_id).order_by(Delivery.created_at.desc())
        ).all()
    )
