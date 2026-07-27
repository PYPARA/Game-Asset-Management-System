from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from .domain import STABLE_KEY_RE


class StorageError(RuntimeError):
    pass


class UnsafePathError(StorageError):
    pass


class ImmutableRevisionError(StorageError):
    pass


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def stable_id(prefix: str, *values: str) -> str:
    digest = hashlib.sha256("\0".join(values).encode("utf-8")).hexdigest()[:24]
    return f"{prefix}_{digest}"


def safe_join(root: Path, relative: str | Path) -> Path:
    root = root.expanduser().resolve()
    value = Path(relative)
    if value.is_absolute():
        raise UnsafePathError("absolute paths are not accepted here")
    candidate = (root / value).resolve(strict=False)
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise UnsafePathError(f"path escapes project root: {relative}") from exc
    return candidate


def relative_to_root(root: Path, path: Path) -> str:
    resolved_root = root.expanduser().resolve()
    resolved_path = path.expanduser().resolve(strict=False)
    try:
        return resolved_path.relative_to(resolved_root).as_posix()
    except ValueError as exc:
        raise UnsafePathError(f"path escapes project root: {path}") from exc


def atomic_write_bytes(path: Path, content: bytes, *, immutable: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if immutable and path.exists():
        raise ImmutableRevisionError(f"immutable file already exists: {path}")
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as temp:
            temp.write(content)
            temp.flush()
            os.fsync(temp.fileno())
        if immutable and path.exists():
            raise ImmutableRevisionError(f"immutable file already exists: {path}")
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def atomic_write_json(path: Path, content: Any, *, immutable: bool = False) -> str:
    data = json.dumps(content, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8") + b"\n"
    atomic_write_bytes(path, data, immutable=immutable)
    return sha256_bytes(data)


def atomic_write_yaml(path: Path, content: Any) -> None:
    data = yaml.safe_dump(content, allow_unicode=True, sort_keys=False).encode("utf-8")
    atomic_write_bytes(path, data)


class ProjectLockRegistry:
    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._locks: dict[str, threading.RLock] = {}

    @contextmanager
    def acquire(self, project_root: Path) -> Iterator[None]:
        root_key = str(project_root.resolve())
        with self._guard:
            thread_lock = self._locks.setdefault(root_key, threading.RLock())
        with thread_lock:
            lock_path = project_root / "workspace" / ".write.lock"
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            with lock_path.open("a+b") as handle:
                try:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                except (ImportError, OSError):
                    pass
                try:
                    yield
                finally:
                    try:
                        import fcntl

                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                    except (ImportError, OSError):
                        pass


LOCKS = ProjectLockRegistry()


class ProjectStore:
    """The only supported on-disk project contract.

    Project metadata lives at the project root. Assets are grouped in catalog
    collection documents; immutable records live in history; disposable work
    lives in workspace. No alternate or hidden project layout is supported.
    """

    def __init__(self, root: str | Path):
        path = Path(root).expanduser()
        if not path.is_absolute():
            raise StorageError("project root must be absolute")
        self.root = path.resolve()
        self.catalog = self.root / "catalog"
        self.history = self.root / "history"
        self.workspace = self.root / "workspace"
        self.output = self.workspace

    @contextmanager
    def lock(self) -> Iterator[None]:
        with LOCKS.acquire(self.root):
            yield

    def initialize(self, *, project_id: str, name: str, default_language: str = "zh-CN") -> None:
        if not self.root.exists() or not self.root.is_dir():
            raise StorageError("project root must be an existing directory")
        with self.lock():
            for relative in (
                "schemas",
                "catalog/content/stories",
                "catalog/content/events",
                "catalog/entities",
                "catalog/media",
                "production/prompt-recipes",
                "production/sources",
                "approved/assets",
                "history/objects",
                "history/reviews",
                "history/qa",
                "releases",
                "imports",
                "workspace/candidates",
                "workspace/rejected",
                "workspace/qa",
                "workspace/tmp",
                "workspace/logs",
            ):
                (self.root / relative).mkdir(parents=True, exist_ok=True)

            project_path = self.root / "project.yaml"
            if project_path.exists():
                existing = self.read_yaml(project_path)
                if existing.get("format_version") != 1:
                    raise StorageError("unsupported project format")
                existing_id = existing.get("id")
                if existing_id and existing_id != project_id:
                    raise StorageError("project directory is already registered with a different id")
            atomic_write_yaml(
                project_path,
                {
                    "format_version": 1,
                    "id": project_id,
                    "name": name,
                    "default_language": default_language,
                    "export": {
                        "content_path": "src/generated/content",
                        "manifest_path": "src/generated/game-assets/assetManifest.ts",
                        "assets_path": "public/assets",
                    },
                },
            )
            local_path = self.root / "project.local.yaml"
            if not local_path.exists():
                atomic_write_yaml(local_path, {"game_root": None})
            atomic_write_bytes(
                self.root / ".gitignore",
                b"project.local.yaml\nworkspace/\n.DS_Store\n",
            )
            atomic_write_bytes(self.workspace / ".gitignore", b"*\n!.gitignore\n")
            schema_path = self.root / "schemas" / "asset.schema.json"
            if not schema_path.exists():
                atomic_write_json(
                    schema_path,
                    {
                        "$schema": "https://json-schema.org/draft/2020-12/schema",
                        "$id": "asset.schema.json",
                        "type": "object",
                        "additionalProperties": True,
                    },
                )
            relations_path = self.catalog / "relations.json"
            if not relations_path.exists():
                atomic_write_json(relations_path, {"format_version": 1, "relations": []})

    def update_name(self, *, project_id: str, name: str) -> None:
        project_path = self.root / "project.yaml"
        with self.lock():
            contract = self.read_yaml(project_path)
            if contract.get("format_version") != 1:
                raise StorageError("unsupported project format")
            if contract.get("id") != project_id:
                raise StorageError("project contract id does not match the registered project")
            contract["name"] = name
            atomic_write_yaml(project_path, contract)

    @staticmethod
    def read_yaml(path: Path) -> dict[str, Any]:
        try:
            loaded = yaml.safe_load(path.read_text("utf-8")) or {}
        except (OSError, yaml.YAMLError) as exc:
            raise StorageError(f"cannot read YAML {path}: {exc}") from exc
        if not isinstance(loaded, dict):
            raise StorageError(f"expected a mapping in {path}")
        return loaded

    @staticmethod
    def read_json(path: Path) -> dict[str, Any]:
        try:
            loaded = json.loads(path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise StorageError(f"cannot read JSON {path}: {exc}") from exc
        if not isinstance(loaded, dict):
            raise StorageError(f"expected an object in {path}")
        return loaded

    @staticmethod
    def _slug(value: str, fallback: str = "general") -> str:
        slug = value.strip().lower().replace("_", "-")
        return slug if STABLE_KEY_RE.fullmatch(slug) else fallback

    def collection_path(self, descriptor: dict[str, Any]) -> Path:
        kind = str(descriptor["kind"])
        subtype = self._slug(str(descriptor.get("subtype", "general")))
        metadata = descriptor.get("metadata")
        domain = self._slug(str(metadata.get("domain", "general"))) if isinstance(metadata, dict) else "general"
        if kind == "content":
            if subtype in {"story", "story-arc", "story_arc", "chain"}:
                return self.catalog / "content" / "stories" / f"{domain}.json"
            if subtype in {"event", "random-event", "random_event"}:
                return self.catalog / "content" / "events" / f"{domain}.json"
            if subtype == "memorial":
                return self.catalog / "content" / "memorials.json"
            if subtype == "ending":
                return self.catalog / "content" / "endings.json"
            return self.catalog / "content" / f"{subtype}.json"
        if kind == "entity":
            plural = {
                "character": "characters",
                "location": "locations",
                "item": "items",
                "achievement": "achievements",
            }.get(subtype, subtype)
            return self.catalog / "entities" / f"{plural}.json"
        if kind == "media":
            plural = {
                "portrait": "portraits",
                "background": "backgrounds",
                "cg": "cgs",
                "icon": "icons",
                "ending": "endings",
                "ending-illustration": "endings",
            }.get(subtype, subtype)
            return self.catalog / "media" / f"{plural}.json"
        return self.catalog / f"{kind}.json"

    def _read_collection(self, path: Path) -> dict[str, Any]:
        if not path.exists():
            return {"format_version": 1, "assets": []}
        collection = self.read_json(path)
        if collection.get("format_version") != 1 or not isinstance(collection.get("assets"), list):
            raise StorageError(f"invalid catalog collection: {path}")
        return collection

    def _find_asset(self, key: str) -> tuple[Path, dict[str, Any], dict[str, Any]]:
        if not STABLE_KEY_RE.fullmatch(key):
            raise StorageError("invalid stable asset key")
        for path in sorted(self.catalog.rglob("*.json")):
            if path.name == "relations.json":
                continue
            collection = self._read_collection(path)
            for descriptor in collection["assets"]:
                if isinstance(descriptor, dict) and descriptor.get("key") == key:
                    return path, collection, descriptor
        raise StorageError(f"asset descriptor not found: {key}")

    def write_asset(self, descriptor: dict[str, Any]) -> str:
        key = str(descriptor["key"])
        if not STABLE_KEY_RE.fullmatch(key):
            raise StorageError("invalid stable asset key")
        path = self.collection_path(descriptor)
        with self.lock():
            collection = self._read_collection(path)
            assets = [item for item in collection["assets"] if item.get("key") != key]
            assets.append(descriptor)
            collection["assets"] = sorted(assets, key=lambda item: str(item["key"]))
            atomic_write_json(path, collection)
        return relative_to_root(self.root, path)

    def _update_asset(self, key: str, updates: dict[str, Any]) -> None:
        with self.lock():
            path, collection, descriptor = self._find_asset(key)
            descriptor.update(updates)
            collection["assets"] = sorted(collection["assets"], key=lambda item: str(item["key"]))
            atomic_write_json(path, collection)

    def update_asset_state(
        self,
        *,
        kind: str,
        key: str,
        content_status: str,
        publication_status: str,
        current_revision_id: str | None,
        latest_candidate_revision_id: str | None = None,
    ) -> None:
        self._update_asset(
            key,
            {
                "content_status": content_status,
                "publication_status": publication_status,
                "current_revision_id": current_revision_id,
                "latest_candidate_revision_id": latest_candidate_revision_id,
            },
        )

    def update_asset_candidate(
        self,
        *,
        kind: str,
        key: str,
        latest_candidate_revision_id: str,
        superseded_revision_ids: list[str],
        updated_at: str,
    ) -> None:
        _, _, descriptor = self._find_asset(key)
        existing = {str(value) for value in descriptor.get("superseded_revision_ids", []) if value}
        self._update_asset(
            key,
            {
                "superseded_revision_ids": sorted(existing | set(superseded_revision_ids)),
                "latest_candidate_revision_id": latest_candidate_revision_id,
                "updated_at": updated_at,
            },
        )

    def update_asset_approval(
        self,
        *,
        kind: str,
        key: str,
        title: str,
        content_status: str,
        publication_status: str,
        current_revision_id: str | None,
        latest_candidate_revision_id: str | None,
        updated_at: str,
    ) -> None:
        self._update_asset(
            key,
            {
                "title": title,
                "content_status": content_status,
                "publication_status": publication_status,
                "current_revision_id": current_revision_id,
                "latest_candidate_revision_id": latest_candidate_revision_id,
                "updated_at": updated_at,
            },
        )

    def update_asset_relations(
        self, *, kind: str, key: str, relation_type: str, target_keys: list[str]
    ) -> None:
        _, _, descriptor = self._find_asset(key)
        relations = [
            relation
            for relation in descriptor.get("relations", [])
            if isinstance(relation, dict) and relation.get("relation_type") != relation_type
        ]
        relations.extend(
            {"relation_type": relation_type, "target_key": target_key}
            for target_key in sorted(set(target_keys))
        )
        self._update_asset(key, {"relations": relations})

    def publish_style_bible(
        self,
        *,
        markdown: str,
        revision_id: str,
        markdown_path: str = "production/style-bible.md",
        profile_path: str = "production/style-bible.json",
    ) -> None:
        markdown_target = safe_join(self.root, markdown_path)
        profile_target = safe_join(self.root, profile_path)
        content = markdown.encode("utf-8")
        with self.lock():
            atomic_write_bytes(markdown_target, content)
            atomic_write_json(
                profile_target,
                {
                    "format_version": 1,
                    "key": "style.primary",
                    "status": "approved",
                    "sha256": sha256_bytes(content),
                    "revision_id": revision_id,
                },
            )

    def write_revision(self, *, kind: str, key: str, revision: dict[str, Any]) -> str:
        object_hash = sha256_bytes(canonical_json(revision))
        path = self.history / "objects" / object_hash[:2] / f"{object_hash}.json"
        with self.lock():
            if path.exists():
                if self.read_json(path) != revision:
                    raise ImmutableRevisionError(f"history object hash collision: {path}")
            else:
                atomic_write_json(path, revision, immutable=True)
        return relative_to_root(self.root, path)

    def write_review(self, *, kind: str, key: str, review: dict[str, Any]) -> str:
        review_id = str(review["id"])
        if "/" in review_id or "\\" in review_id or review_id in {".", ".."}:
            raise StorageError("invalid review id")
        created_at = str(review.get("created_at") or datetime.now(UTC).isoformat())
        try:
            date = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise StorageError("review created_at must be an ISO timestamp") from exc
        path = self.history / "reviews" / f"{date.year:04d}" / f"{date.month:02d}" / f"{review_id}.json"
        with self.lock():
            atomic_write_json(path, review, immutable=True)
        return relative_to_root(self.root, path)

    def write_qa_record(self, record: dict[str, Any]) -> str:
        path = self.history / "qa" / f"{record['id']}.json"
        with self.lock():
            atomic_write_json(path, record)
        return relative_to_root(self.root, path)

    def read_schema(self, schema_ref: str | None) -> dict[str, Any] | None:
        if not schema_ref:
            return None
        path = safe_join(self.root / "schemas", schema_ref)
        if path.suffix != ".json":
            raise StorageError("schemas must be JSON files")
        return self.read_json(path)

    def write_relations(self, relations: list[dict[str, Any]]) -> None:
        with self.lock():
            atomic_write_json(
                self.catalog / "relations.json",
                {"format_version": 1, "relations": relations},
            )

    def scan(
        self,
    ) -> tuple[
        list[dict[str, Any]],
        list[dict[str, Any]],
        list[dict[str, Any]],
        list[dict[str, Any]],
        list[dict[str, Any]],
        list[str],
    ]:
        assets: list[dict[str, Any]] = []
        revisions: list[dict[str, Any]] = []
        renditions: list[dict[str, Any]] = []
        qa_runs: list[dict[str, Any]] = []
        relations: list[dict[str, Any]] = []
        errors: list[str] = []
        superseded_revision_ids: set[str] = set()
        if not (self.root / "project.yaml").is_file():
            return assets, revisions, renditions, qa_runs, relations, ["project.yaml is missing"]

        for collection_path in sorted(self.catalog.rglob("*.json")):
            try:
                if collection_path.name == "relations.json":
                    payload = self.read_json(collection_path)
                    if payload.get("format_version") != 1 or not isinstance(payload.get("relations"), list):
                        raise StorageError("invalid relations collection")
                    relations.extend(payload["relations"])
                    continue
                collection = self._read_collection(collection_path)
                for descriptor in collection["assets"]:
                    if not isinstance(descriptor, dict):
                        raise StorageError("asset collection entries must be objects")
                    if self.collection_path(descriptor) != collection_path:
                        raise StorageError("asset kind/subtype does not match collection path")
                    assets.append(descriptor)
                    superseded_revision_ids.update(
                        str(value)
                        for value in descriptor.get("superseded_revision_ids", [])
                        if value
                    )
            except (StorageError, KeyError, TypeError, ValueError) as exc:
                errors.append(f"{relative_to_root(self.root, collection_path)}: {exc}")

        for path in sorted((self.history / "objects").glob("*/*.json")):
            try:
                revision = self.read_json(path)
                if str(revision.get("id", "")) in superseded_revision_ids:
                    revision["review_status"] = "superseded"
                revision["file_path"] = relative_to_root(self.root, path)
                revisions.append(revision)
                if revision.get("format") == "media":
                    content = revision.get("content")
                    rendition = content.get("rendition") if isinstance(content, dict) else None
                    if not isinstance(rendition, dict):
                        raise StorageError("media revision is missing content.rendition")
                    required = {
                        "media_type",
                        "source_path",
                        "sha256",
                        "width",
                        "height",
                        "byte_size",
                    }
                    missing = sorted(required - rendition.keys())
                    if missing:
                        raise StorageError(f"media rendition is missing: {', '.join(missing)}")
                    source_path = str(rendition["source_path"])
                    normalized_path = rendition.get("normalized_path")
                    preview_path = str(normalized_path or source_path)
                    source_file = safe_join(self.root, source_path)
                    preview_file = safe_join(self.root, preview_path)
                    if not source_file.is_file():
                        raise StorageError(f"rendition source is missing: {source_path}")
                    if not preview_file.is_file():
                        raise StorageError(f"rendition preview is missing: {preview_path}")
                    expected_hash = str(rendition["sha256"])
                    if sha256_file(preview_file) != expected_hash:
                        raise StorageError(f"rendition hash mismatch: {preview_path}")
                    revision_id = str(revision["id"])
                    renditions.append(
                        {
                            "id": stable_id("rendition", revision_id, expected_hash),
                            "revision_id": revision_id,
                            "media_type": str(rendition["media_type"]),
                            "source_path": source_path,
                            "normalized_path": normalized_path,
                            "target_path": rendition.get("target_path"),
                            "sha256": expected_hash,
                            "width": rendition.get("width"),
                            "height": rendition.get("height"),
                            "byte_size": int(rendition["byte_size"]),
                        }
                    )
            except (StorageError, KeyError, TypeError, ValueError) as exc:
                errors.append(f"{relative_to_root(self.root, path)}: {exc}")
        for path in sorted((self.history / "qa").glob("*.json")):
            try:
                qa_runs.append(self.read_json(path))
            except StorageError as exc:
                errors.append(f"{relative_to_root(self.root, path)}: {exc}")

        reviews: dict[str, dict[str, Any]] = {}
        for path in sorted((self.history / "reviews").glob("*/*/*.json")):
            try:
                review = self.read_json(path)
                reviews[str(review.get("revision_id", ""))] = review
            except StorageError as exc:
                errors.append(f"{relative_to_root(self.root, path)}: {exc}")
        for revision in revisions:
            review = reviews.get(str(revision.get("id", "")))
            if review:
                revision["review_status"] = (
                    "approved" if review.get("verdict") == "approve" else "rejected"
                )
                revision["reviewed_at"] = review.get("created_at")
        return assets, revisions, renditions, qa_runs, relations, errors

    def resolve_rendition_path(self, stored_path: str) -> Path:
        return safe_join(self.root, stored_path)

    def candidate_dir(self, job_id: str) -> Path:
        if not STABLE_KEY_RE.fullmatch(job_id.replace("-", "")):
            raise StorageError("invalid job id")
        return safe_join(self.workspace / "candidates", job_id)

    def qa_report_path(self, qa_id: str) -> Path:
        return safe_join(self.workspace / "qa", f"{qa_id}.json")

    def write_release(self, release_id: str, manifest: dict[str, Any]) -> tuple[str, str]:
        release_dir = safe_join(self.root / "releases", release_id)
        versioned = release_dir / "manifest.json"
        with self.lock():
            digest = atomic_write_json(versioned, manifest, immutable=True)
            atomic_write_json(self.root / "release.json", manifest)
        return relative_to_root(self.root, versioned), digest

    def atomic_publish(self, candidate: Path, target_relative: str) -> tuple[str, str | None]:
        if not candidate.is_file():
            raise StorageError(f"candidate does not exist: {candidate}")
        target = safe_join(self.root, target_relative)
        backup_relative: str | None = None
        with self.lock():
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                old_hash = sha256_file(target)
                backup = self.workspace / "backups" / old_hash / target_relative
                backup.parent.mkdir(parents=True, exist_ok=True)
                if not backup.exists():
                    shutil.copy2(target, backup)
                backup_relative = relative_to_root(self.root, backup)
            descriptor, temp_name = tempfile.mkstemp(
                prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
            )
            os.close(descriptor)
            try:
                shutil.copy2(candidate, temp_name)
                with open(temp_name, "rb") as staged:
                    os.fsync(staged.fileno())
                os.replace(temp_name, target)
            finally:
                if os.path.exists(temp_name):
                    os.unlink(temp_name)
        return relative_to_root(self.root, target), backup_relative
