from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager
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
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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
            lock_path = project_root / ".game-assets" / ".write.lock"
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
    def __init__(self, root: str | Path):
        path = Path(root).expanduser()
        if not path.is_absolute():
            raise StorageError("project root must be absolute")
        self.root = path.resolve()
        self.meta = self.root / ".game-assets"
        self.output = self.root / "output" / "game-assets"

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
                "assets",
                "styles",
                "prompt-recipes",
                "releases",
                "backups",
            ):
                (self.meta / relative).mkdir(parents=True, exist_ok=True)
            for relative in ("candidates", "rejected", "qa"):
                (self.output / relative).mkdir(parents=True, exist_ok=True)
            atomic_write_bytes(self.output / ".gitignore", b"*\n!.gitignore\n")
            project_path = self.meta / "project.yaml"
            if project_path.exists():
                existing = self.read_yaml(project_path)
                existing_id = existing.get("id")
                if existing_id and existing_id != project_id:
                    raise StorageError("project directory is already registered with a different id")
            atomic_write_yaml(
                project_path,
                {
                    "version": 1,
                    "id": project_id,
                    "name": name,
                    "defaultLanguage": default_language,
                },
            )
            schema_path = self.meta / "schemas" / "asset-extension.schema.json"
            if not schema_path.exists():
                atomic_write_json(
                    schema_path,
                    {
                        "$schema": "https://json-schema.org/draft/2020-12/schema",
                        "$id": "asset-extension.schema.json",
                        "type": "object",
                        "additionalProperties": True,
                    },
                )

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

    def asset_dir(self, kind: str, key: str) -> Path:
        if not STABLE_KEY_RE.fullmatch(key):
            raise StorageError("invalid stable asset key")
        if not STABLE_KEY_RE.fullmatch(kind):
            raise StorageError("invalid asset kind")
        return safe_join(self.meta / "assets", Path(kind) / key)

    def write_asset(self, descriptor: dict[str, Any]) -> str:
        directory = self.asset_dir(str(descriptor["kind"]), str(descriptor["key"]))
        relative = relative_to_root(self.root, directory / "asset.yaml")
        with self.lock():
            (directory / "revisions").mkdir(parents=True, exist_ok=True)
            atomic_write_yaml(directory / "asset.yaml", descriptor)
        return relative

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
        path = self.asset_dir(kind, key) / "asset.yaml"
        with self.lock():
            descriptor = self.read_yaml(path)
            descriptor["contentStatus"] = content_status
            descriptor["publicationStatus"] = publication_status
            descriptor["currentRevisionId"] = current_revision_id
            descriptor["latestCandidateRevisionId"] = latest_candidate_revision_id
            atomic_write_yaml(path, descriptor)

    def write_revision(self, *, kind: str, key: str, revision: dict[str, Any]) -> str:
        revision_id = str(revision["id"])
        if not STABLE_KEY_RE.fullmatch(revision_id.replace("-", "")):
            # UUIDs are safe but the check also rejects separators and path components.
            if "/" in revision_id or "\\" in revision_id or revision_id in {".", ".."}:
                raise StorageError("invalid revision id")
        path = self.asset_dir(kind, key) / "revisions" / f"{revision_id}.json"
        with self.lock():
            atomic_write_json(path, revision, immutable=True)
        return relative_to_root(self.root, path)

    def write_review(self, *, kind: str, key: str, review: dict[str, Any]) -> str:
        review_id = str(review["id"])
        if "/" in review_id or "\\" in review_id or review_id in {".", ".."}:
            raise StorageError("invalid review id")
        path = self.asset_dir(kind, key) / "reviews" / f"{review_id}.json"
        with self.lock():
            atomic_write_json(path, review, immutable=True)
        return relative_to_root(self.root, path)

    def read_schema(self, schema_ref: str | None) -> dict[str, Any] | None:
        if not schema_ref:
            return None
        path = safe_join(self.meta / "schemas", schema_ref)
        if path.suffix != ".json":
            raise StorageError("schemas must be JSON files")
        return self.read_json(path)

    def relations_path(self) -> Path:
        return self.meta / "relations.json"

    def write_relations(self, relations: list[dict[str, Any]]) -> None:
        with self.lock():
            atomic_write_json(self.relations_path(), {"version": 1, "relations": relations})

    def scan(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
        assets: list[dict[str, Any]] = []
        revisions: list[dict[str, Any]] = []
        errors: list[str] = []
        assets_root = self.meta / "assets"
        if not assets_root.exists():
            return assets, revisions, errors
        for descriptor_path in sorted(assets_root.glob("*/*/asset.yaml")):
            try:
                descriptor = self.read_yaml(descriptor_path)
                key = str(descriptor.get("key", ""))
                kind = str(descriptor.get("kind", ""))
                if descriptor_path.parent != self.asset_dir(kind, key):
                    raise StorageError("descriptor kind/key does not match its path")
                assets.append(descriptor)
                review_state: dict[str, dict[str, Any]] = {}
                for review_path in sorted((descriptor_path.parent / "reviews").glob("*.json")):
                    review = self.read_json(review_path)
                    revision_id = str(review.get("revisionId", ""))
                    if revision_id:
                        review_state[revision_id] = review
                for revision_path in sorted((descriptor_path.parent / "revisions").glob("*.json")):
                    revision = self.read_json(revision_path)
                    review = review_state.get(str(revision.get("id", "")))
                    if review:
                        revision["reviewStatus"] = (
                            "approved" if review.get("verdict") == "approve" else "rejected"
                        )
                        revision["reviewedAt"] = review.get("createdAt")
                    revision["filePath"] = relative_to_root(self.root, revision_path)
                    revisions.append(revision)
            except (StorageError, KeyError, TypeError) as exc:
                errors.append(f"{relative_to_root(self.root, descriptor_path)}: {exc}")
        return assets, revisions, errors

    def candidate_dir(self, job_id: str) -> Path:
        if not STABLE_KEY_RE.fullmatch(job_id.replace("-", "")):
            raise StorageError("invalid job id")
        return safe_join(self.output / "candidates", job_id)

    def qa_report_path(self, qa_id: str) -> Path:
        return safe_join(self.output / "qa", f"{qa_id}.json")

    def write_release(self, release_id: str, manifest: dict[str, Any]) -> tuple[str, str]:
        releases = self.meta / "releases"
        versioned = safe_join(releases, f"{release_id}.json")
        with self.lock():
            digest = atomic_write_json(versioned, manifest, immutable=True)
            atomic_write_json(self.meta / "runtime-manifest.json", manifest)
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
                backup = self.meta / "backups" / old_hash / target_relative
                backup.parent.mkdir(parents=True, exist_ok=True)
                if not backup.exists():
                    shutil.copy2(target, backup)
                backup_relative = relative_to_root(self.root, backup)
            descriptor, temp_name = tempfile.mkstemp(
                prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
            )
            os.close(descriptor)
            try:
                shutil.copyfile(candidate, temp_name)
                with open(temp_name, "rb") as temp:
                    os.fsync(temp.fileno())
                os.replace(temp_name, target)
            finally:
                if os.path.exists(temp_name):
                    os.unlink(temp_name)
        return relative_to_root(self.root, target), backup_relative
