from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
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


@dataclass(slots=True)
class ProjectScanResult:
    assets: list[dict[str, Any]]
    revisions: list[dict[str, Any]]
    renditions: list[dict[str, Any]]
    qa_runs: list[dict[str, Any]]
    relations: list[dict[str, Any]]
    artifacts: list[dict[str, Any]]
    reviews: list[dict[str, Any]]
    releases: list[dict[str, Any]]
    deliveries: list[dict[str, Any]]
    errors: list[str]
    agent_sessions: list[dict[str, Any]] = field(default_factory=list)
    agent_events: list[dict[str, Any]] = field(default_factory=list)
    changesets: list[dict[str, Any]] = field(default_factory=list)


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
                "production/parameters",
                "production/postprocess",
                "production/agent",
                "production/sources",
                "approved/assets",
                "approved/objects",
                "history/objects",
                "history/reviews",
                "history/qa",
                "history/deliveries",
                "history/agent/sessions",
                "history/agent/events",
                "history/agent/config",
                "history/changesets",
                "releases",
                "imports",
                "workspace/candidates",
                "workspace/rejected",
                "workspace/qa",
                "workspace/tmp",
                "workspace/logs",
                "workspace/agent/changesets",
                "workspace/agent/sessions",
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
                        "format_version": 1,
                        "content_path": "src/generated/content",
                        "manifest_path": "src/generated/game-assets/assetManifest.ts",
                        "assets_path": "public/assets",
                        "lock_path": "gams-lock.json",
                        "validation_commands": [],
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

    def export_contract(self) -> dict[str, Any]:
        """Return the committed export contract with safe defaults applied."""

        contract = self.read_yaml(self.root / "project.yaml")
        if contract.get("format_version") != 1:
            raise StorageError("unsupported project format")
        configured = contract.get("export")
        if configured is not None and not isinstance(configured, dict):
            raise StorageError("export must be a mapping")
        defaults: dict[str, Any] = {
            "format_version": 1,
            "content_path": "src/generated/content",
            "manifest_path": "src/generated/game-assets/assetManifest.ts",
            "assets_path": "public/assets",
            "lock_path": "gams-lock.json",
            "validation_commands": [],
        }
        result = {**defaults, **(configured or {})}
        if not isinstance(result.get("validation_commands"), list):
            raise StorageError("export.validation_commands must be a list")
        return result

    def local_export_config(self) -> dict[str, Any]:
        path = self.root / "project.local.yaml"
        if not path.exists():
            return {"game_root": None}
        value = self.read_yaml(path)
        return value if isinstance(value, dict) else {"game_root": None}

    def update_local_export_config(self, *, game_root: str | None) -> dict[str, Any]:
        value = {"game_root": game_root}
        with self.lock():
            atomic_write_yaml(self.root / "project.local.yaml", value)
        return value

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

    def history_object_path(self, record: dict[str, Any]) -> Path:
        object_hash = sha256_bytes(canonical_json(record))
        return self.history / "objects" / object_hash[:2] / f"{object_hash}.json"

    def _write_history_object(self, record: dict[str, Any]) -> tuple[str, bool]:
        path = self.history_object_path(record)
        created = False
        with self.lock():
            if path.exists():
                if self.read_json(path) != record:
                    raise ImmutableRevisionError(f"history object hash collision: {path}")
            else:
                atomic_write_json(path, record, immutable=True)
                created = True
        return relative_to_root(self.root, path), created

    def write_revision(self, *, kind: str, key: str, revision: dict[str, Any]) -> str:
        path, _created = self._write_history_object(revision)
        return path

    def write_artifact(self, artifact: dict[str, Any]) -> tuple[str, bool]:
        if artifact.get("object_type") != "artifact":
            raise StorageError("artifact history objects must declare object_type=artifact")
        return self._write_history_object(artifact)

    def promote_blob(
        self,
        source: Path,
        *,
        directory: str,
        extension: str,
        expected_hash: str | None = None,
    ) -> tuple[str, str, int]:
        if directory not in {"production/sources", "approved/objects"}:
            raise StorageError("unsupported durable artifact directory")
        extension = extension.lower().lstrip(".")
        if not extension or not extension.isalnum():
            raise StorageError("artifact extension must be alphanumeric")
        relative_to_root(self.root, source)
        if not source.is_file():
            raise StorageError(f"artifact source does not exist: {source}")
        digest = sha256_file(source)
        if expected_hash is not None and digest != expected_hash:
            raise StorageError("artifact source hash changed during promotion")
        target = safe_join(self.root, f"{directory}/{digest[:2]}/{digest}.{extension}")
        with self.lock():
            if target.exists():
                if not target.is_file() or sha256_file(target) != digest:
                    raise StorageError(f"content-addressed artifact is corrupt: {target}")
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                descriptor, temp_name = tempfile.mkstemp(
                    prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
                )
                os.close(descriptor)
                try:
                    shutil.copy2(source, temp_name)
                    if sha256_file(Path(temp_name)) != digest:
                        raise StorageError("artifact changed while it was copied")
                    with open(temp_name, "rb") as staged:
                        os.fsync(staged.fileno())
                    os.replace(temp_name, target)
                finally:
                    if os.path.exists(temp_name):
                        os.unlink(temp_name)
        return relative_to_root(self.root, target), digest, target.stat().st_size

    def review_path(self, review: dict[str, Any]) -> Path:
        review_id = str(review["id"])
        if "/" in review_id or "\\" in review_id or review_id in {".", ".."}:
            raise StorageError("invalid review id")
        created_at = str(review.get("created_at") or datetime.now(UTC).isoformat())
        try:
            date = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise StorageError("review created_at must be an ISO timestamp") from exc
        return self.history / "reviews" / f"{date.year:04d}" / f"{date.month:02d}" / f"{review_id}.json"

    def write_review(self, *, kind: str, key: str, review: dict[str, Any]) -> str:
        path = self.review_path(review)
        with self.lock():
            atomic_write_json(path, review, immutable=True)
        return relative_to_root(self.root, path)

    def write_qa_record(self, record: dict[str, Any]) -> str:
        path = self.history / "qa" / f"{record['id']}.json"
        with self.lock():
            if path.exists():
                if self.read_json(path) != record:
                    raise ImmutableRevisionError(f"immutable QA record already exists: {path}")
            else:
                atomic_write_json(path, record, immutable=True)
        return relative_to_root(self.root, path)

    def delivery_path(self, receipt: dict[str, Any]) -> Path:
        receipt_id = str(receipt["id"])
        if "/" in receipt_id or "\\" in receipt_id or receipt_id in {".", ".."}:
            raise StorageError("invalid delivery id")
        created_at = str(receipt.get("created_at") or datetime.now(UTC).isoformat())
        try:
            date = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise StorageError("delivery created_at must be an ISO timestamp") from exc
        return self.history / "deliveries" / f"{date.year:04d}" / f"{date.month:02d}" / f"{receipt_id}.json"

    def write_delivery(self, receipt: dict[str, Any]) -> str:
        path = self.delivery_path(receipt)
        with self.lock():
            atomic_write_json(path, receipt, immutable=True)
        return relative_to_root(self.root, path)

    def commit_media_approval(
        self,
        *,
        key: str,
        history_records: list[dict[str, Any]],
        qa_record: dict[str, Any],
        review: dict[str, Any],
        asset_updates: dict[str, Any],
        superseded_revision_ids: list[str],
    ) -> dict[str, str]:
        """Commit formal approval records and the Catalog pointer as one file transaction.

        Durable content-addressed blobs are promoted before this method. If any formal
        record or Catalog write fails, newly created history records are removed and the
        previous Catalog bytes are restored. Correct shared blobs may remain orphaned.
        """

        with self.lock():
            catalog_path, collection, descriptor = self._find_asset(key)
            original_catalog = catalog_path.read_bytes()
            created_paths: list[Path] = []
            written: dict[str, str] = {}
            qa_path = self.history / "qa" / f"{qa_record['id']}.json"
            review_path = self.review_path(review)
            try:
                for record in history_records:
                    object_path = self.history_object_path(record)
                    if object_path.exists():
                        if self.read_json(object_path) != record:
                            raise ImmutableRevisionError(
                                f"history object hash collision: {object_path}"
                            )
                    else:
                        atomic_write_json(object_path, record, immutable=True)
                        created_paths.append(object_path)
                    written[str(record["id"])] = relative_to_root(self.root, object_path)

                if qa_path.exists():
                    if self.read_json(qa_path) != qa_record:
                        raise ImmutableRevisionError(
                            f"immutable QA record already exists: {qa_path}"
                        )
                else:
                    atomic_write_json(qa_path, qa_record, immutable=True)
                    created_paths.append(qa_path)
                written[str(qa_record["id"])] = relative_to_root(self.root, qa_path)

                atomic_write_json(review_path, review, immutable=True)
                created_paths.append(review_path)
                written[str(review["id"])] = relative_to_root(self.root, review_path)

                existing_superseded = {
                    str(value) for value in descriptor.get("superseded_revision_ids", []) if value
                }
                descriptor.update(asset_updates)
                descriptor["superseded_revision_ids"] = sorted(
                    existing_superseded | set(superseded_revision_ids)
                )
                collection["assets"] = sorted(
                    collection["assets"], key=lambda item: str(item["key"])
                )
                atomic_write_json(catalog_path, collection)
            except Exception:
                atomic_write_bytes(catalog_path, original_catalog)
                for created_path in reversed(created_paths):
                    try:
                        created_path.unlink(missing_ok=True)
                    except OSError:
                        pass
                raise
        return written

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
        result = self.scan_full()
        return (
            result.assets,
            result.revisions,
            result.renditions,
            result.qa_runs,
            result.relations,
            result.errors,
        )

    def scan_full(self) -> ProjectScanResult:
        assets: list[dict[str, Any]] = []
        revisions: list[dict[str, Any]] = []
        renditions: list[dict[str, Any]] = []
        qa_runs: list[dict[str, Any]] = []
        relations: list[dict[str, Any]] = []
        artifacts: list[dict[str, Any]] = []
        review_records: list[dict[str, Any]] = []
        releases: list[dict[str, Any]] = []
        deliveries: list[dict[str, Any]] = []
        agent_sessions: list[dict[str, Any]] = []
        agent_events: list[dict[str, Any]] = []
        changesets: list[dict[str, Any]] = []
        errors: list[str] = []
        superseded_revision_ids: set[str] = set()
        current_revision_ids: set[str] = set()
        candidate_revision_ids: set[str] = set()
        if not (self.root / "project.yaml").is_file():
            return ProjectScanResult(
                assets,
                revisions,
                renditions,
                qa_runs,
                relations,
                artifacts,
                review_records,
                releases,
                deliveries,
                ["project.yaml is missing"],
            )

        try:
            contract = self.read_yaml(self.root / "project.yaml")
            project_id = str(contract["id"])
        except (StorageError, KeyError, TypeError, ValueError) as exc:
            project_id = ""
            errors.append(f"project.yaml: {exc}")

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
                    if descriptor.get("current_revision_id"):
                        current_revision_ids.add(str(descriptor["current_revision_id"]))
                    if descriptor.get("latest_candidate_revision_id"):
                        candidate_revision_ids.add(str(descriptor["latest_candidate_revision_id"]))
                    superseded_revision_ids.update(
                        str(value)
                        for value in descriptor.get("superseded_revision_ids", [])
                        if value
                    )
            except (StorageError, KeyError, TypeError, ValueError) as exc:
                errors.append(f"{relative_to_root(self.root, collection_path)}: {exc}")

        for path in sorted((self.history / "objects").glob("*/*.json")):
            try:
                record = self.read_json(path)
                if self.history_object_path(record) != path:
                    raise StorageError("history object path does not match its content hash")
                if record.get("object_type") == "artifact":
                    required = {
                        "id",
                        "project_id",
                        "revision_id",
                        "role",
                        "kind",
                        "media_type",
                        "path",
                        "sha256",
                        "byte_size",
                        "tool",
                        "created_at",
                    }
                    missing = sorted(required - record.keys())
                    if missing:
                        raise StorageError(f"artifact is missing: {', '.join(missing)}")
                    artifact_path = str(record["path"])
                    role = str(record["role"])
                    if str(record["project_id"]) != project_id:
                        raise StorageError("artifact belongs to a different project")
                    expected_prefix = (
                        "production/sources/" if role == "source" else "approved/objects/"
                    )
                    if role not in {"source", "runtime"} or not artifact_path.startswith(
                        expected_prefix
                    ):
                        raise StorageError("artifact role does not match its durable path")
                    blob = safe_join(self.root, artifact_path)
                    if not blob.is_file():
                        raise StorageError(f"artifact blob is missing: {artifact_path}")
                    expected_hash = str(record["sha256"])
                    artifact_relative = Path(artifact_path)
                    if (
                        artifact_relative.parent.name != expected_hash[:2]
                        or artifact_relative.stem != expected_hash
                    ):
                        raise StorageError("artifact path is not content addressed by its hash")
                    if sha256_file(blob) != expected_hash:
                        raise StorageError(f"artifact hash mismatch: {artifact_path}")
                    if blob.stat().st_size != int(record["byte_size"]):
                        raise StorageError(f"artifact byte size mismatch: {artifact_path}")
                    artifact = dict(record)
                    artifact["file_path"] = relative_to_root(self.root, path)
                    artifacts.append(artifact)
                    continue

                revision = record
                revision_id = str(revision.get("id", ""))
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
                    if revision_id in current_revision_ids and (
                        source_path.startswith("workspace/") or preview_path.startswith("workspace/")
                    ):
                        raise StorageError("approved rendition references disposable workspace")
                    source_file = safe_join(self.root, source_path)
                    preview_file = safe_join(self.root, preview_path)
                    inactive_workspace = (
                        revision_id not in current_revision_ids | candidate_revision_ids
                        and (
                            source_path.startswith("workspace/")
                            or preview_path.startswith("workspace/")
                        )
                    )
                    if inactive_workspace and (
                        not source_file.is_file() or not preview_file.is_file()
                    ):
                        continue
                    if not source_file.is_file():
                        raise StorageError(f"rendition source is missing: {source_path}")
                    if not preview_file.is_file():
                        raise StorageError(f"rendition preview is missing: {preview_path}")
                    expected_hash = str(rendition["sha256"])
                    if sha256_file(preview_file) != expected_hash:
                        raise StorageError(f"rendition hash mismatch: {preview_path}")
                    if preview_file.stat().st_size != int(rendition["byte_size"]):
                        raise StorageError(f"rendition byte size mismatch: {preview_path}")
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

        artifacts_by_id = {str(artifact["id"]): artifact for artifact in artifacts}
        revision_ids = {str(revision.get("id", "")) for revision in revisions}
        for artifact in artifacts:
            revision_id = str(artifact["revision_id"])
            if revision_id not in revision_ids:
                errors.append(
                    f"{artifact['file_path']}: artifact revision is missing: {revision_id}"
                )
            parent_id = artifact.get("parent_artifact_id")
            if parent_id:
                parent = artifacts_by_id.get(str(parent_id))
                if parent is None:
                    errors.append(
                        f"{artifact['file_path']}: parent artifact is missing: {parent_id}"
                    )
                elif str(parent_id) == str(artifact["id"]):
                    errors.append(f"{artifact['file_path']}: artifact cannot parent itself")
                elif str(parent["revision_id"]) != revision_id:
                    errors.append(
                        f"{artifact['file_path']}: parent artifact belongs to another revision"
                    )
        for revision in revisions:
            content = revision.get("content")
            rendition = content.get("rendition") if isinstance(content, dict) else None
            if not isinstance(rendition, dict):
                continue
            for id_field, path_field, hash_field in (
                ("source_artifact_id", "source_path", "source_sha256"),
                ("artifact_id", "normalized_path", "sha256"),
            ):
                artifact_id = rendition.get(id_field)
                if not artifact_id:
                    continue
                artifact = artifacts_by_id.get(str(artifact_id))
                if artifact is None:
                    errors.append(
                        f"{revision['file_path']}: referenced artifact is missing: {artifact_id}"
                    )
                    continue
                expected_role = "source" if id_field == "source_artifact_id" else "runtime"
                expected_path = rendition.get(path_field) or rendition.get("source_path")
                expected_hash = rendition.get(hash_field)
                if (
                    str(artifact.get("revision_id", "")) != str(revision.get("id", ""))
                    or artifact.get("role") != expected_role
                    or artifact.get("path") != expected_path
                    or (expected_hash and artifact.get("sha256") != expected_hash)
                ):
                    errors.append(
                        f"{revision['file_path']}: artifact metadata does not match rendition"
                    )

        known_rendition_ids = {str(rendition["id"]) for rendition in renditions}
        for path in sorted((self.history / "qa").glob("*.json")):
            try:
                qa = self.read_json(path)
                if path != self.history / "qa" / f"{qa['id']}.json":
                    raise StorageError("QA record path does not match its id")
                if str(qa.get("rendition_id", "")) in known_rendition_ids:
                    required = {"id", "rendition_id", "verdict", "checks", "created_at"}
                    missing = sorted(required - qa.keys())
                    if missing:
                        raise StorageError(f"QA record is missing: {', '.join(missing)}")
                    if qa.get("verdict") not in {"pass", "warning", "fail"}:
                        raise StorageError("unsupported QA verdict")
                    qa_runs.append(qa)
            except (StorageError, KeyError, TypeError, ValueError) as exc:
                errors.append(f"{relative_to_root(self.root, path)}: {exc}")

        reviews_by_revision: dict[str, dict[str, Any]] = {}
        for path in sorted((self.history / "reviews").glob("*/*/*.json")):
            try:
                review = self.read_json(path)
                required = {"id", "revision_id", "verdict", "dependency_hash", "created_at"}
                missing = sorted(required - review.keys())
                if missing:
                    raise StorageError(f"review is missing: {', '.join(missing)}")
                if review.get("verdict") not in {"approve", "reject"}:
                    raise StorageError("unsupported review verdict")
                if self.review_path(review) != path:
                    raise StorageError("review path does not match its id or creation date")
                review["file_path"] = relative_to_root(self.root, path)
                review_records.append(review)
                revision_id = str(review["revision_id"])
                previous = reviews_by_revision.get(revision_id)
                if previous is None or str(review["created_at"]) >= str(previous["created_at"]):
                    reviews_by_revision[revision_id] = review
            except (StorageError, KeyError, TypeError, ValueError) as exc:
                errors.append(f"{relative_to_root(self.root, path)}: {exc}")
        for revision in revisions:
            revision_id = str(revision.get("id", ""))
            review = reviews_by_revision.get(revision_id)
            if review and revision_id not in superseded_revision_ids:
                revision["review_status"] = (
                    "approved" if review.get("verdict") == "approve" else "rejected"
                )
                revision["reviewed_at"] = review.get("created_at")

        for manifest_path in sorted((self.root / "releases").glob("*/manifest.json")):
            try:
                manifest = self.read_json(manifest_path)
                release_id = str(manifest["release_id"])
                if release_id != manifest_path.parent.name:
                    raise StorageError("release id does not match its directory")
                if manifest.get("format_version") not in {1, 2}:
                    raise StorageError("unsupported release manifest format")
                if str(manifest.get("project_id", "")) != project_id:
                    raise StorageError("release belongs to a different project")
                if not isinstance(manifest.get("assets"), list):
                    raise StorageError("release assets must be a list")
                if manifest.get("format_version") == 2:
                    snapshot_payload = {
                        "format_version": 2,
                        "project_id": project_id,
                        "assets": manifest["assets"],
                    }
                    expected_snapshot = f"sha256:{sha256_bytes(canonical_json(snapshot_payload))}"
                    if manifest.get("snapshot_hash") != expected_snapshot:
                        raise StorageError("release snapshot hash is invalid")
                releases.append(
                    {
                        "id": release_id,
                        "project_id": project_id,
                        "name": str(manifest.get("name") or release_id),
                        "manifest_path": relative_to_root(self.root, manifest_path),
                        "manifest_hash": sha256_file(manifest_path),
                        "manifest_version": int(manifest.get("format_version", 1)),
                        "snapshot_hash": manifest.get("snapshot_hash"),
                        "asset_count": len(manifest["assets"]),
                        "created_at": manifest.get("created_at"),
                    }
                )
            except (StorageError, KeyError, TypeError, ValueError) as exc:
                errors.append(f"{relative_to_root(self.root, manifest_path)}: {exc}")

        for receipt_path in sorted((self.history / "deliveries").glob("**/*.json")):
            try:
                receipt = self.read_json(receipt_path)
                required = {
                    "id",
                    "project_id",
                    "release_id",
                    "release_manifest_hash",
                    "snapshot_hash",
                    "checkout_fingerprint",
                    "display_path",
                    "status",
                    "created_at",
                }
                missing = sorted(required - receipt.keys())
                if missing:
                    raise StorageError(f"delivery receipt is missing: {', '.join(missing)}")
                if str(receipt["project_id"]) != project_id:
                    raise StorageError("delivery belongs to a different project")
                if self.delivery_path(receipt) != receipt_path:
                    raise StorageError("delivery receipt path does not match its id or date")
                receipt["file_path"] = relative_to_root(self.root, receipt_path)
                deliveries.append(receipt)
            except (StorageError, KeyError, TypeError, ValueError) as exc:
                errors.append(f"{relative_to_root(self.root, receipt_path)}: {exc}")

        # Agent audit records are durable history, while the context package and
        # patch remain disposable workspace inputs.  They are intentionally
        # schema-light here: the API performs the stricter Controller validation
        # before accepting any action.
        for path in sorted((self.history / "agent" / "sessions").glob("*.json")):
            try:
                record = self.read_json(path)
                if str(record.get("id", "")) != path.stem:
                    raise StorageError("agent session path does not match its id")
                if str(record.get("project_id", "")) != project_id:
                    raise StorageError("agent session belongs to a different project")
                record["file_path"] = relative_to_root(self.root, path)
                agent_sessions.append(record)
            except (StorageError, KeyError, TypeError, ValueError) as exc:
                errors.append(f"{relative_to_root(self.root, path)}: {exc}")
        for path in sorted((self.history / "agent" / "events").glob("*/*.json")):
            try:
                record = self.read_json(path)
                if str(record.get("project_id", "")) != project_id:
                    raise StorageError("agent event belongs to a different project")
                record["file_path"] = relative_to_root(self.root, path)
                agent_events.append(record)
            except (StorageError, KeyError, TypeError, ValueError) as exc:
                errors.append(f"{relative_to_root(self.root, path)}: {exc}")
        for path in sorted((self.history / "changesets").glob("*.json")):
            try:
                record = self.read_json(path)
                if str(record.get("id", "")) != path.stem:
                    raise StorageError("ChangeSet path does not match its id")
                if str(record.get("project_id", "")) != project_id:
                    raise StorageError("ChangeSet belongs to a different project")
                record["file_path"] = relative_to_root(self.root, path)
                changesets.append(record)
            except (StorageError, KeyError, TypeError, ValueError) as exc:
                errors.append(f"{relative_to_root(self.root, path)}: {exc}")

        return ProjectScanResult(
            assets,
            revisions,
            renditions,
            qa_runs,
            relations,
            artifacts,
            review_records,
            releases,
            deliveries,
            errors,
            agent_sessions,
            agent_events,
            changesets,
        )

    def resolve_rendition_path(self, stored_path: str) -> Path:
        return safe_join(self.root, stored_path)

    def candidate_dir(self, job_id: str) -> Path:
        if not STABLE_KEY_RE.fullmatch(job_id.replace("-", "")):
            raise StorageError("invalid job id")
        return safe_join(self.workspace / "candidates", job_id)

    def qa_report_path(self, qa_id: str) -> Path:
        return safe_join(self.workspace / "qa", f"{qa_id}.json")

    def write_release(self, release_id: str, manifest: dict[str, Any]) -> tuple[str, str]:
        """Compatibility wrapper for callers that do not update Catalog state."""

        return self.commit_release(release_id, manifest, {})

    def commit_release(
        self,
        release_id: str,
        manifest: dict[str, Any],
        asset_updates: dict[str, dict[str, Any]],
    ) -> tuple[str, str]:
        release_dir = safe_join(self.root / "releases", release_id)
        versioned = release_dir / "manifest.json"
        pointer = self.root / "release.json"
        with self.lock():
            if versioned.exists():
                raise ImmutableRevisionError(f"immutable release already exists: {versioned}")

            remaining = set(asset_updates)
            catalog_updates: dict[Path, dict[str, Any]] = {}
            catalog_originals: dict[Path, bytes] = {}
            for path in sorted(self.catalog.rglob("*.json")):
                if path.name == "relations.json":
                    continue
                collection = self._read_collection(path)
                changed = False
                for descriptor in collection["assets"]:
                    key = str(descriptor.get("key", ""))
                    if key in asset_updates:
                        descriptor.update(asset_updates[key])
                        remaining.discard(key)
                        changed = True
                if changed:
                    collection["assets"] = sorted(
                        collection["assets"], key=lambda item: str(item["key"])
                    )
                    catalog_updates[path] = collection
                    catalog_originals[path] = path.read_bytes()
            if remaining:
                raise StorageError(
                    f"release assets disappeared from Catalog: {', '.join(sorted(remaining))}"
                )

            pointer_original = pointer.read_bytes() if pointer.exists() else None
            versioned_created = False
            try:
                digest = atomic_write_json(versioned, manifest, immutable=True)
                versioned_created = True
                atomic_write_json(pointer, manifest)
                for path, collection in catalog_updates.items():
                    atomic_write_json(path, collection)
            except Exception:
                for path, original in catalog_originals.items():
                    try:
                        atomic_write_bytes(path, original)
                    except OSError:
                        pass
                try:
                    if pointer_original is None:
                        pointer.unlink(missing_ok=True)
                    else:
                        atomic_write_bytes(pointer, pointer_original)
                except OSError:
                    pass
                if versioned_created:
                    try:
                        versioned.unlink(missing_ok=True)
                        versioned.parent.rmdir()
                    except OSError:
                        pass
                raise
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
