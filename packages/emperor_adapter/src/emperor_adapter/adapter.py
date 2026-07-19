"""Read-only parser and deterministic importer for Emperor Simulator.

The legacy TypeScript manifest is intentionally parsed as a narrowly defined
data source instead of executing JavaScript. This keeps dry-runs deterministic,
works without Node.js, and prevents arbitrary code execution from a checkout.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from hashlib import sha256
import json
import mimetypes
import os
from pathlib import Path, PurePosixPath
import re
import tempfile
from typing import Any, Literal

from .errors import InvalidLegacyProject, PathSafetyError

PathLike = str | os.PathLike[str]

_ADAPTER_VERSION = "0.1.0"
_VALID_KEY = re.compile(r"^[a-z0-9]+(?:[.-][a-z0-9]+)*$")
_VALID_STATUSES = {
    "planned",
    "generated",
    "normalized",
    "reviewed",
    "integrated",
    "approved",
}
_REQUIRED_FILES = (
    "docs/assets/style-bible.md",
    "docs/assets/asset-production.md",
    "src/content/assetManifest.ts",
    "src/content/assetProductionStatus.ts",
    "src/content/v2/registry.ts",
    "src/content/v3/registry.ts",
)
_QA_VALIDATION = "output/imagegen/remaining/qa/validation.json"
_QA_CONTACT_SHEETS = "output/imagegen/remaining/qa/contact-sheets.json"
_FORBIDDEN_FILENAMES = {"image-gen-key.json"}


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()


def _canonical_hash(value: Any) -> str:
    return sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _root_path(root: PathLike) -> Path:
    candidate = Path(root).expanduser()
    try:
        resolved = candidate.resolve(strict=True)
    except (FileNotFoundError, OSError) as exc:
        raise InvalidLegacyProject(f"legacy project does not exist: {candidate}") from exc
    if not resolved.is_dir():
        raise InvalidLegacyProject(f"legacy project is not a directory: {resolved}")
    for relative in _REQUIRED_FILES:
        _source_path(resolved, relative, required=True)
    return resolved


def _ensure_relative(value: str, *, label: str) -> PurePosixPath:
    if not value or "\\" in value or "\x00" in value:
        raise PathSafetyError(f"unsafe {label}: {value!r}")
    relative = PurePosixPath(value)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise PathSafetyError(f"unsafe {label}: {value!r}")
    if any(part.lower() in _FORBIDDEN_FILENAMES for part in relative.parts):
        raise PathSafetyError(f"credential path is never a valid adapter input: {value!r}")
    return relative


def _source_path(root: Path, relative: str, *, required: bool = False) -> Path:
    safe_relative = _ensure_relative(relative, label="legacy path")
    candidate = root.joinpath(*safe_relative.parts)
    try:
        resolved = candidate.resolve(strict=required)
    except (FileNotFoundError, OSError) as exc:
        if required:
            raise InvalidLegacyProject(f"required legacy input is missing: {relative}") from exc
        resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise PathSafetyError(f"legacy path escapes project root: {relative!r}") from exc
    if required and not resolved.is_file():
        raise InvalidLegacyProject(f"required legacy input is not a file: {relative}")
    return resolved


class _HashCache:
    def __init__(self) -> None:
        self._values: dict[Path, str] = {}

    def file(self, path: Path) -> str:
        resolved = path.resolve(strict=True)
        if resolved not in self._values:
            digest = sha256()
            with resolved.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            self._values[resolved] = digest.hexdigest()
        return self._values[resolved]


def _read_text(root: Path, relative: str) -> str:
    path = _source_path(root, relative, required=True)
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise InvalidLegacyProject(f"legacy input is not UTF-8: {relative}") from exc


def _read_json(root: Path, relative: str) -> Any:
    try:
        return json.loads(_read_text(root, relative))
    except json.JSONDecodeError as exc:
        raise InvalidLegacyProject(f"legacy JSON is invalid: {relative}: {exc}") from exc


def _decode_quoted(raw: str, quote: str) -> str:
    if quote == '"':
        try:
            return json.loads(f'"{raw}"')
        except json.JSONDecodeError as exc:
            raise InvalidLegacyProject(f"invalid TypeScript string literal: {raw!r}") from exc
    return raw.replace("\\'", "'").replace("\\\\", "\\")


def _quoted_strings(text: str) -> list[str]:
    pattern = re.compile(r'''(["'])(?P<body>(?:\\.|(?!\1).)*)\1''', re.DOTALL)
    return [_decode_quoted(match.group("body"), match.group(1)) for match in pattern.finditer(text)]


def _const_array(text: str, name: str) -> str:
    pattern = re.compile(
        rf"(?:export\s+)?const\s+{re.escape(name)}(?:\s*:[^=]+)?\s*=\s*\[(.*?)\]"
        rf"\s*(?:as\s+const|\.map)",
        re.DOTALL,
    )
    match = pattern.search(text)
    if not match:
        raise InvalidLegacyProject(f"cannot parse TypeScript array: {name}")
    return match.group(1)


def _parse_statuses(text: str) -> dict[str, str]:
    statuses = dict(re.findall(r'"([a-z0-9.-]+)"\s*:\s*"([a-z]+)"', text))
    invalid = {status for status in statuses.values() if status not in _VALID_STATUSES}
    if invalid:
        raise InvalidLegacyProject(f"unknown production statuses: {sorted(invalid)}")
    return statuses


def _parse_manifest(manifest_text: str, status_text: str) -> list[dict[str, Any]]:
    statuses = _parse_statuses(status_text)
    entries: list[dict[str, Any]] = []

    core_body = _const_array(manifest_text, "corePortraitSpecs")
    core_specs = re.findall(
        r"characterId\s*:\s*['\"]([a-z0-9-]+)['\"]\s*,\s*"
        r"expressions\s*:\s*\[\s*['\"]([a-z0-9-]+)['\"]\s*,\s*"
        r"['\"]([a-z0-9-]+)['\"]\s*\]",
        core_body,
    )
    if not core_specs:
        raise InvalidLegacyProject("core portrait specifications are empty")
    for character_id, first, second in core_specs:
        neutral_key = f"portrait.{character_id}.neutral"
        for expression in ("neutral", first, second):
            key = f"portrait.{character_id}.{expression}"
            entry = {
                "key": key,
                "type": "portrait",
                "path": f"public/assets/portraits/core/{character_id}/{expression}.webp",
                "width": 1024,
                "height": 1536,
                "preloadGroup": "portraits-core",
                "characterId": character_id,
                "expression": expression,
                "status": statuses.get(key, "planned"),
            }
            if expression == "neutral":
                entry["fallbackPolicy"] = "none"
            else:
                entry["fallbackKey"] = neutral_key
            entries.append(entry)

    emperor_match = re.search(
        r"const\s+emperorPortraits[^=]*=\s*\[(.*?)\]\.map", manifest_text, re.DOTALL
    )
    if not emperor_match:
        raise InvalidLegacyProject("cannot parse emperor portraits")
    for expression in _quoted_strings(emperor_match.group(1)):
        key = f"portrait.emperor.{expression}"
        entries.append(
            {
                "key": key,
                "type": "portrait",
                "path": f"public/assets/portraits/emperor/{expression}.webp",
                "width": 1024,
                "height": 1536,
                "preloadGroup": "portraits-emperor",
                "characterId": "emperor",
                "expression": expression,
                "fallbackPolicy": "none",
                "status": statuses.get(key, "planned"),
            }
        )

    for support_id in _quoted_strings(_const_array(manifest_text, "supportPortraitIds")):
        key = f"portrait.support-{support_id}.neutral"
        entries.append(
            {
                "key": key,
                "type": "portrait",
                "path": f"public/assets/portraits/support/{support_id}.webp",
                "width": 1024,
                "height": 1536,
                "preloadGroup": "portraits-support",
                "characterId": f"support-{support_id}",
                "expression": "neutral",
                "fallbackPolicy": "none",
                "status": statuses.get(key, "planned"),
            }
        )

    collection_specs = (
        ("backgroundIds", "background", "public/assets/backgrounds/{id}.webp", 1920, 1080, "backgrounds"),
        ("cgIds", "cg", "public/assets/cgs/{id}.webp", 2048, 1152, "cgs"),
        ("endingIds", "ending", "public/assets/endings/{id}.webp", 2048, 1152, "endings"),
    )
    for const_name, asset_type, path_template, width, height, preload_group in collection_specs:
        for item_id in _quoted_strings(_const_array(manifest_text, const_name)):
            key = f"{asset_type}.{item_id}"
            entries.append(
                {
                    "key": key,
                    "type": asset_type,
                    "path": path_template.format(id=item_id),
                    "width": width,
                    "height": height,
                    "preloadGroup": preload_group,
                    "fallbackPolicy": "none",
                    "status": statuses.get(key, "planned"),
                }
            )

    icons_match = re.search(
        r"const\s+icons[^=]*=\s*Array\.from\(\{\s*length\s*:\s*(\d+)\s*\}",
        manifest_text,
    )
    if not icons_match:
        raise InvalidLegacyProject("cannot parse icon count")
    for index in range(1, int(icons_match.group(1)) + 1):
        item_id = f"artifact-{index:02d}"
        key = f"icon.{item_id}"
        entries.append(
            {
                "key": key,
                "type": "icon",
                "path": f"public/assets/icons/{item_id}.webp",
                "width": 256,
                "height": 256,
                "preloadGroup": "icons",
                "fallbackPolicy": "none",
                "status": statuses.get(key, "planned"),
            }
        )

    keys = [entry["key"] for entry in entries]
    duplicates = sorted(key for key, count in Counter(keys).items() if count > 1)
    if duplicates:
        raise InvalidLegacyProject(f"duplicate manifest keys: {duplicates}")
    if set(statuses) - set(keys):
        raise InvalidLegacyProject(
            f"production status contains unknown keys: {sorted(set(statuses) - set(keys))[:5]}"
        )
    return entries


def _parse_production_ledger(text: str) -> dict[str, dict[str, str]]:
    header = re.compile(r"^###\s+([a-z0-9.-]+)\s+—\s+(.+?)\s*$", re.MULTILINE)
    matches = list(header.finditer(text))
    result: dict[str, dict[str, str]] = {}
    for index, match in enumerate(matches):
        key, name = match.group(1), match.group(2).strip()
        if not _VALID_KEY.fullmatch(key):
            raise InvalidLegacyProject(f"invalid production key: {key}")
        if key in result:
            raise InvalidLegacyProject(f"duplicate production ledger key: {key}")
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        block = text[match.end() : end]
        fields = {
            field.lower(): value.strip()
            for field, value in re.findall(
                r"^-\s+(Status|Target|Source|Reference|Prompt|QA|Notes):\s*(.*?)\s*$",
                block,
                re.MULTILINE,
            )
        }
        required = {"status", "target", "source", "reference", "prompt", "qa", "notes"}
        missing = required - set(fields)
        if missing:
            raise InvalidLegacyProject(f"ledger entry {key} is missing fields: {sorted(missing)}")
        result[key] = {"key": key, "name": name, **fields}
    if not result:
        raise InvalidLegacyProject("asset production ledger contains no entries")
    return result


def _extract_counts(text: str, registry_name: str) -> dict[str, int]:
    registry = text.find(registry_name)
    if registry < 0:
        raise InvalidLegacyProject(f"content registry is missing: {registry_name}")
    counts = text.find("counts:", registry)
    opening = text.find("{", counts)
    closing = text.find("}", opening)
    if counts < 0 or opening < 0 or closing < 0:
        raise InvalidLegacyProject(f"content registry counts are malformed: {registry_name}")
    parsed = {
        name: int(value)
        for name, value in re.findall(r"([A-Za-z][A-Za-z0-9]*)\s*:\s*(\d+)", text[opening:closing])
    }
    if not parsed:
        raise InvalidLegacyProject(f"content registry counts are empty: {registry_name}")
    return parsed


def _named_records(text: str) -> list[dict[str, str]]:
    pattern = re.compile(
        r"^\s*\{\s*\n\s*id:\s*([\"'])(?P<id>[^\"']+)\1\s*,\s*"
        r"name:\s*([\"'])(?P<name>.*?)\3\s*,",
        re.MULTILINE,
    )
    return [{"id": match.group("id"), "name": match.group("name")} for match in pattern.finditer(text)]


def _content_inventory(root: Path, hashes: _HashCache) -> dict[str, Any]:
    files: list[dict[str, Any]] = []
    for version in ("v2", "v3"):
        base = _source_path(root, f"src/content/{version}", required=False)
        if not base.is_dir():
            raise InvalidLegacyProject(f"content directory is missing: src/content/{version}")
        for path in sorted(base.rglob("*.ts")):
            resolved = path.resolve(strict=True)
            try:
                relative = resolved.relative_to(root).as_posix()
            except ValueError as exc:
                raise PathSafetyError(f"content source escapes project root: {path}") from exc
            if any(part.lower() in _FORBIDDEN_FILENAMES for part in resolved.parts):
                raise PathSafetyError(f"forbidden content source: {relative}")
            text = resolved.read_text(encoding="utf-8")
            files.append(
                {
                    "path": relative,
                    "version": version,
                    "sha256": hashes.file(resolved),
                    "bytes": resolved.stat().st_size,
                    "literal_id_occurrences": len(
                        re.findall(r"\bid\s*:\s*[\"'][a-z0-9.-]+[\"']", text)
                    ),
                }
            )

    v2_text = _read_text(root, "src/content/v2/registry.ts")
    v3_text = _read_text(root, "src/content/v3/registry.ts")
    v2 = _extract_counts(v2_text, "contentV2Registry")
    v3 = _extract_counts(v3_text, "contentV3Registry")
    v2_total = (
        v2.get("chains", 0)
        + v2.get("nodes", 0)
        + v2.get("achievements", 0)
        + v2.get("collectibles", 0)
        + v2.get("endings", 0)
    )
    v3_total = (
        v3.get("coreChains", 0)
        + v3.get("shortChains", 0)
        + v3.get("coreNodes", 0)
        + v3.get("shortNodes", 0)
        + v3.get("standaloneStories", 0)
        + v3.get("stateMemorials", 0)
        + v3.get("randomEvents", 0)
    )

    named: dict[str, list[dict[str, str]]] = {}
    for category, relative in (
        ("achievements", "src/content/v2/achievements.ts"),
        ("collectibles", "src/content/v2/collectibles.ts"),
        ("endings", "src/content/v2/endings.ts"),
    ):
        named[category] = _named_records(_read_text(root, relative))
    return {
        "versions": {
            "v2": {"declared_counts": v2, "parseable_record_count": v2_total},
            "v3": {"declared_counts": v3, "parseable_record_count": v3_total},
        },
        "parseable_record_count": v2_total + v3_total,
        "source_file_count": len(files),
        "source_files": files,
        "named_records": named,
    }


def _mime_type(relative: str) -> str:
    guessed = mimetypes.guess_type(relative)[0]
    if guessed:
        return guessed
    return "application/octet-stream"


def _rendition(
    root: Path,
    relative: str,
    *,
    role: str,
    hashes: _HashCache,
    width: int | None = None,
    height: int | None = None,
    alpha: bool | None = None,
) -> dict[str, Any]:
    path = _source_path(root, relative, required=False)
    exists = path.is_file()
    return {
        "role": role,
        "relative_path": relative,
        "mime_type": _mime_type(relative),
        "width": width,
        "height": height,
        "alpha": alpha,
        "sha256": hashes.file(path) if exists else None,
        "bytes": path.stat().st_size if exists else None,
        "exists": exists,
    }


def _scan(root: PathLike) -> dict[str, Any]:
    source_root = _root_path(root)
    hashes = _HashCache()
    manifest_relative = "src/content/assetManifest.ts"
    status_relative = "src/content/assetProductionStatus.ts"
    ledger_relative = "docs/assets/asset-production.md"
    style_relative = "docs/assets/style-bible.md"
    manifest = _parse_manifest(
        _read_text(source_root, manifest_relative), _read_text(source_root, status_relative)
    )
    ledger = _parse_production_ledger(_read_text(source_root, ledger_relative))

    anchors = [ledger[key] for key in sorted(ledger) if key.startswith("anchor.")]
    runtime_ledger = {key: value for key, value in ledger.items() if not key.startswith("anchor.")}
    manifest_keys = {entry["key"] for entry in manifest}
    if set(runtime_ledger) != manifest_keys:
        missing = sorted(manifest_keys - set(runtime_ledger))
        extra = sorted(set(runtime_ledger) - manifest_keys)
        raise InvalidLegacyProject(
            f"ledger/manifest key mismatch; missing={missing[:5]}, extra={extra[:5]}"
        )
    for entry in manifest:
        ledger_entry = runtime_ledger[entry["key"]]
        if ledger_entry["target"] != entry["path"]:
            raise InvalidLegacyProject(
                f"target mismatch for {entry['key']}: {ledger_entry['target']} != {entry['path']}"
            )
        if ledger_entry["status"] != entry["status"]:
            raise InvalidLegacyProject(
                f"status mismatch for {entry['key']}: {ledger_entry['status']} != {entry['status']}"
            )

    validation = _read_json(source_root, _QA_VALIDATION)
    if not isinstance(validation, dict) or not isinstance(validation.get("assets"), list):
        raise InvalidLegacyProject("QA validation must contain an assets array")
    qa_by_key: dict[str, dict[str, Any]] = {}
    for qa_entry in validation["assets"]:
        if not isinstance(qa_entry, dict) or not isinstance(qa_entry.get("key"), str):
            raise InvalidLegacyProject("QA validation contains an invalid asset record")
        qa_by_key[qa_entry["key"]] = qa_entry
    if set(qa_by_key) != manifest_keys:
        raise InvalidLegacyProject("QA validation keys do not exactly match the runtime manifest")

    contact_sheet_list = _read_json(source_root, _QA_CONTACT_SHEETS)
    if not isinstance(contact_sheet_list, list) or not all(
        isinstance(item, str) for item in contact_sheet_list
    ):
        raise InvalidLegacyProject("contact-sheets.json must be an array of paths")
    contact_sheets = []
    for relative in contact_sheet_list:
        sheet = _source_path(source_root, relative, required=True)
        contact_sheets.append(
            {
                "path": relative,
                "sha256": hashes.file(sheet),
                "bytes": sheet.stat().st_size,
            }
        )

    source_hashes = {}
    for relative in (*_REQUIRED_FILES, _QA_VALIDATION, _QA_CONTACT_SHEETS):
        path = _source_path(source_root, relative, required=True)
        source_hashes[relative] = hashes.file(path)

    runtime_items: list[dict[str, Any]] = []
    for entry in manifest:
        production = runtime_ledger[entry["key"]]
        asset_type = entry["type"]
        runtime_items.append(
            {
                **entry,
                "name": production["name"],
                "source": production["source"],
                "reference": None if production["reference"] == "无" else production["reference"],
                "prompt": production["prompt"],
                "qa_instructions": production["qa"],
                "notes": production["notes"],
                "renditions": [
                    _rendition(
                        source_root,
                        production["target"],
                        role="runtime",
                        hashes=hashes,
                        width=entry["width"],
                        height=entry["height"],
                        alpha=asset_type in {"portrait", "icon"},
                    ),
                    _rendition(
                        source_root,
                        production["source"],
                        role="source",
                        hashes=hashes,
                        width=entry["width"],
                        height=entry["height"],
                        alpha=None,
                    ),
                ],
                "qa_evidence": qa_by_key[entry["key"]],
            }
        )

    anchor_items = []
    dimensions = {"character": (1024, 1536, True), "background": (1920, 1080, False), "cg": (2048, 1152, False)}
    for anchor in anchors:
        subtype = anchor["key"].split(".", 1)[1]
        width, height, alpha = dimensions.get(subtype, (None, None, None))
        anchor_items.append(
            {
                **anchor,
                "reference": None if anchor["reference"] == "无" else anchor["reference"],
                "renditions": [
                    _rendition(
                        source_root,
                        anchor["target"],
                        role="approved_anchor",
                        hashes=hashes,
                        width=width,
                        height=height,
                        alpha=alpha,
                    ),
                    _rendition(
                        source_root,
                        anchor["source"],
                        role="source",
                        hashes=hashes,
                        width=width,
                        height=height,
                        alpha=None,
                    ),
                ],
            }
        )

    content = _content_inventory(source_root, hashes)
    by_type = dict(sorted(Counter(item["type"] for item in runtime_items).items()))
    by_status = dict(sorted(Counter(item["status"] for item in runtime_items).items()))
    missing_media = [
        {"key": item["key"], "role": rendition["role"], "path": rendition["relative_path"]}
        for item in runtime_items
        for rendition in item["renditions"]
        if not rendition["exists"]
    ]
    return {
        "schema_version": 1,
        "adapter": {"name": "emperor-simulator", "version": _ADAPTER_VERSION},
        "source_root": str(source_root),
        "project": {"name": "Emperor Simulator", "language": "zh-CN"},
        "anchors": {"count": len(anchor_items), "items": anchor_items},
        "runtime_assets": {
            "count": len(runtime_items),
            "by_type": by_type,
            "by_status": by_status,
            "items": runtime_items,
        },
        "qa": {
            "validation_path": _QA_VALIDATION,
            "asset_count": len(qa_by_key),
            "errors": validation.get("errors", 0),
            "warnings": validation.get("warnings", 0),
            "contact_sheet_count": len(contact_sheets),
            "contact_sheets": contact_sheets,
        },
        "content": content,
        "source_hashes": dict(sorted(source_hashes.items())),
        "missing_media": missing_media,
        "warnings": [] if not missing_media else [f"{len(missing_media)} legacy renditions are missing"],
    }


def dry_run(root: PathLike) -> dict[str, Any]:
    """Return a complete, JSON-serializable import preview without writing files.

    Only the two asset documents, manifest/status modules, QA evidence, contact
    sheets, referenced media, and the ``src/content/v2`` / ``v3`` TypeScript
    trees are read. Credentials are not discovered or accessed.
    """

    return _scan(root)


def _production_state(status: str) -> str:
    return {
        "planned": "queued",
        "generated": "generated",
        "normalized": "normalized",
        "reviewed": "review_ready",
        "integrated": "integrated",
        "approved": "approved",
    }[status]


def _publication_state(status: str) -> str:
    if status == "approved":
        return "approved"
    if status == "integrated":
        return "integrated"
    return "unpublished"


def _output_root(
    destination: PathLike | None,
    metadata_root: PathLike | None,
    *,
    forbidden_root: Path,
) -> Path:
    if destination is not None and metadata_root is not None:
        raise ValueError("pass destination or metadata_root, not both")
    selected = metadata_root if metadata_root is not None else destination
    if selected is None:
        raise ValueError("an import destination is required")
    candidate = Path(selected).expanduser()
    if candidate.name != ".game-assets":
        candidate = candidate / ".game-assets"
    prospective = candidate.resolve(strict=False)
    try:
        prospective.relative_to(forbidden_root)
    except ValueError:
        pass
    else:
        raise PathSafetyError("import destination must not be inside the legacy project")
    candidate.mkdir(parents=True, exist_ok=True)
    return candidate.resolve(strict=True)


def _safe_output_path(root: Path, relative: str) -> Path:
    safe_relative = _ensure_relative(relative, label="metadata output path")
    candidate = root.joinpath(*safe_relative.parts)
    candidate.parent.mkdir(parents=True, exist_ok=True)
    resolved_parent = candidate.parent.resolve(strict=True)
    try:
        resolved_parent.relative_to(root)
    except ValueError as exc:
        raise PathSafetyError(f"metadata output escapes destination: {relative!r}") from exc
    if candidate.exists() and candidate.is_symlink():
        raise PathSafetyError(f"refusing to replace symlink: {candidate}")
    return candidate


def _atomic_write(path: Path, payload: bytes) -> bool:
    if path.is_file() and path.read_bytes() == payload:
        return False
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.tmp-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()
    return True


def _revision_id(payload: Mapping[str, Any]) -> str:
    return f"legacy-{_canonical_hash(payload)[:16]}"


def _base_asset(
    *,
    key: str,
    name: str,
    kind: Literal["media", "production", "document", "entity"],
    subtype: str,
    legacy: Mapping[str, Any],
    states: Mapping[str, str],
    relations: list[dict[str, str]],
    renditions: list[dict[str, Any]],
    revision_metadata: Mapping[str, Any],
    approved: bool = False,
    tags: Iterable[str] = (),
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if not _VALID_KEY.fullmatch(key):
        raise InvalidLegacyProject(f"cannot import invalid asset key: {key}")
    revision_seed = {
        "key": key,
        "legacy": legacy,
        "relations": relations,
        "renditions": renditions,
        "metadata": revision_metadata,
    }
    revision = {
        "id": _revision_id(revision_seed),
        "created_at": "unknown",
        "source": "emperor-simulator-legacy-import",
        "input_hash": _canonical_hash(revision_metadata),
        "provider": "unknown",
        "prompt_recipe": "emperor-legacy-v1" if kind in {"media", "production"} else None,
        "review_status": "approved" if approved else str(legacy.get("status", "unknown")),
        "notes": str(revision_metadata.get("notes", "Imported from legacy metadata; lineage unknown.")),
        "metadata": dict(revision_metadata),
    }
    result: dict[str, Any] = {
        "schema_version": 1,
        "key": key,
        "name": name,
        "kind": kind,
        "subtype": subtype,
        "tags": sorted({"emperor-simulator", "legacy-import", *tags}),
        "legacy": dict(legacy),
        "states": dict(states),
        "current_revision": revision["id"],
        "relations": relations,
        "revisions": [revision],
        "renditions": renditions,
        "metadata": dict(metadata or {}),
    }
    if approved:
        result["approved_revision"] = revision["id"]
    return result


def _write_asset(root: Path, asset: dict[str, Any], counts: Counter[str]) -> None:
    key, kind = asset["key"], asset["kind"]
    base = f"assets/{kind}/{key}"
    revision = asset["revisions"][0]
    for relative, value in (
        (f"{base}/asset.yaml", asset),
        (f"{base}/revisions/{revision['id']}.json", revision),
    ):
        written = _atomic_write(_safe_output_path(root, relative), _json_bytes(value))
        counts["written" if written else "unchanged"] += 1


def _media_asset(item: Mapping[str, Any]) -> dict[str, Any]:
    relations: list[dict[str, str]] = []
    if fallback := item.get("fallbackKey"):
        relations.append({"type": "fallback_to", "target": str(fallback)})
    if item["type"] == "portrait" and item.get("characterId"):
        relations.append(
            {"type": "depicts", "target": f"entity.character.{item['characterId']}"}
        )
    if item["type"] == "icon":
        relations.append(
            {"type": "represents", "target": f"entity.item.{item['key'].split('.', 1)[1]}"}
        )
    status = str(item["status"])
    legacy = {
        "key": item["key"],
        "path": item["path"],
        "status": status,
        "source_path": item["source"],
        "reference": item.get("reference"),
        "fallback_key": item.get("fallbackKey"),
        "fallback_policy": item.get("fallbackPolicy"),
    }
    revision_metadata = {
        "prompt": item["prompt"],
        "qa_instructions": item["qa_instructions"],
        "qa_evidence": item["qa_evidence"],
        "notes": item["notes"],
        "lineage": {
            "request_id": "unknown",
            "model": "unknown",
            "cost": "unknown",
        },
    }
    return _base_asset(
        key=str(item["key"]),
        name=str(item["name"]),
        kind="media",
        subtype=str(item["type"]),
        legacy=legacy,
        states={
            "content": "ready",
            "production": _production_state(status),
            "publication": _publication_state(status),
        },
        relations=relations,
        renditions=[dict(value) for value in item["renditions"]],
        revision_metadata=revision_metadata,
        approved=status == "approved",
        tags=(str(item["type"]), str(item["preloadGroup"])),
        metadata={
            "preload_group": item["preloadGroup"],
            "character_id": item.get("characterId"),
            "expression": item.get("expression"),
            "external_media": True,
        },
    )


def _anchor_asset(item: Mapping[str, Any]) -> dict[str, Any]:
    subtype = str(item["key"]).split(".", 1)[1]
    status = str(item["status"])
    return _base_asset(
        key=str(item["key"]),
        name=str(item["name"]),
        kind="production",
        subtype="visual_anchor",
        legacy={
            "key": item["key"],
            "path": item["target"],
            "status": status,
            "source_path": item["source"],
            "reference": item.get("reference"),
        },
        states={
            "content": "ready",
            "production": _production_state(status),
            "publication": "not_applicable",
        },
        relations=[],
        renditions=[dict(value) for value in item["renditions"]],
        revision_metadata={
            "prompt": item["prompt"],
            "qa_instructions": item["qa"],
            "notes": item["notes"],
            "lineage": {"request_id": "unknown", "model": "unknown", "cost": "unknown"},
        },
        approved=status == "approved",
        tags=("anchor", subtype),
        metadata={"visual_family": subtype, "runtime": False, "external_media": True},
    )


def _entity_asset(key: str, name: str, subtype: str, legacy_key: str) -> dict[str, Any]:
    return _base_asset(
        key=key,
        name=name,
        kind="entity",
        subtype=subtype,
        legacy={"key": legacy_key, "path": None, "status": "imported"},
        states={"content": "ready", "production": "not_applicable", "publication": "unpublished"},
        relations=[],
        renditions=[],
        revision_metadata={"notes": "Entity inferred from the verified legacy manifest/content inventory."},
        tags=(subtype,),
        metadata={"inferred": True},
    )


def _document_asset(root: Path, source_root: Path, item: Mapping[str, Any]) -> dict[str, Any]:
    relative = str(item["path"])
    stem = PurePosixPath(relative).with_suffix("").parts[2:]
    key_tail = ".".join(re.sub(r"[^a-z0-9-]", "-", value.lower()) for value in stem)
    key = f"document.emperor.{key_tail}"
    return _base_asset(
        key=key,
        name=f"Emperor content: {'/'.join(stem)}",
        kind="document",
        subtype="typescript_content",
        legacy={"key": key, "path": relative, "status": "imported"},
        states={"content": "ready", "production": "not_applicable", "publication": "unpublished"},
        relations=[],
        renditions=[
            {
                "role": "legacy_source",
                "relative_path": relative,
                "mime_type": "text/typescript",
                "width": None,
                "height": None,
                "alpha": None,
                "sha256": item["sha256"],
                "bytes": item["bytes"],
                "exists": _source_path(source_root, relative, required=True).is_file(),
            }
        ],
        revision_metadata={
            "notes": "Read-only TypeScript content inventory; source is not copied.",
            "literal_id_occurrences": item["literal_id_occurrences"],
        },
        tags=(str(item["version"]), "content-inventory"),
        metadata={"external_source": True, "source_version": item["version"]},
    )


def _legacy_manifest_entries(scan: Mapping[str, Any]) -> list[dict[str, Any]]:
    allowed = (
        "key",
        "type",
        "path",
        "width",
        "height",
        "preloadGroup",
        "characterId",
        "expression",
        "fallbackKey",
        "fallbackPolicy",
        "status",
    )
    return [
        {field: item[field] for field in allowed if field in item}
        for item in sorted(scan["runtime_assets"]["items"], key=lambda value: value["key"])
    ]


def import_to(
    root: PathLike,
    destination: PathLike | None = None,
    *,
    metadata_root: PathLike | None = None,
) -> dict[str, Any]:
    """Import legacy metadata into a deterministic ``.game-assets`` tree.

    The legacy checkout remains read-only and all media renditions are external
    references carrying their original relative path and SHA-256. No image,
    contact sheet, or other large binary is copied.
    """

    scan = _scan(root)
    source_root = Path(scan["source_root"])
    output_root = _output_root(destination, metadata_root, forbidden_root=source_root)

    counts: Counter[str] = Counter()
    project = {
        "schema_version": 1,
        "project": {"name": "Emperor Simulator", "language": "zh-CN"},
        "paths": {
            "legacy_source": str(source_root),
            "assets": "assets",
            "styles": "styles",
            "prompt_recipes": "prompt-recipes",
            "releases": "releases",
            "runtime_manifest": "runtime-manifest.json",
        },
        "gates": ["normalized", "reviewed", "integrated", "approved"],
        "custom_schemas": [],
    }
    deterministic_files: list[tuple[str, bytes]] = [
        ("project.yaml", _json_bytes(project)),
        (
            "runtime-manifest.json",
            _json_bytes(
                {
                    "schema_version": 1,
                    "source": "emperor-simulator",
                    "assets": _legacy_manifest_entries(scan),
                }
            ),
        ),
        (
            "qa/emperor-validation.json",
            _json_bytes(
                {
                    "schema_version": 1,
                    "source_path": scan["qa"]["validation_path"],
                    "asset_count": scan["qa"]["asset_count"],
                    "errors": scan["qa"]["errors"],
                    "warnings": scan["qa"]["warnings"],
                    "contact_sheets": scan["qa"]["contact_sheets"],
                }
            ),
        ),
        ("content/emperor-inventory.json", _json_bytes(scan["content"])),
        (
            "prompt-recipes/emperor-legacy-v1.json",
            _json_bytes(
                {
                    "schema_version": 1,
                    "key": "emperor-legacy-v1",
                    "name": "Emperor Simulator verified legacy prompts",
                    "provider": "unknown",
                    "model": "unknown",
                    "source": "docs/assets/asset-production.md",
                    "source_sha256": scan["source_hashes"]["docs/assets/asset-production.md"],
                }
            ),
        ),
        (
            "import-reports/emperor.json",
            _json_bytes(
                {
                    "schema_version": 1,
                    "adapter_version": _ADAPTER_VERSION,
                    "source_root": str(source_root),
                    "source_hashes": scan["source_hashes"],
                    "anchors": scan["anchors"]["count"],
                    "runtime_assets": scan["runtime_assets"]["count"],
                    "asset_types": scan["runtime_assets"]["by_type"],
                    "content_records": scan["content"]["parseable_record_count"],
                    "media_copied": 0,
                    "lineage": {"request_id": "unknown", "model": "unknown", "cost": "unknown"},
                }
            ),
        ),
    ]
    style_text = _read_text(source_root, "docs/assets/style-bible.md").encode()
    deterministic_files.append(("styles/emperor-v1.md", style_text))
    deterministic_files.append(
        (
            "styles/emperor-v1.json",
            _json_bytes(
                {
                    "schema_version": 1,
                    "key": "style.emperor-v1",
                    "name": "Emperor Simulator style bible",
                    "status": "approved",
                    "source_path": "docs/assets/style-bible.md",
                    "sha256": scan["source_hashes"]["docs/assets/style-bible.md"],
                    "revision": "legacy-approved",
                }
            ),
        )
    )
    for relative, payload in deterministic_files:
        written = _atomic_write(_safe_output_path(output_root, relative), payload)
        counts["written" if written else "unchanged"] += 1

    assets: list[dict[str, Any]] = []
    assets.extend(_anchor_asset(item) for item in scan["anchors"]["items"])
    assets.extend(_media_asset(item) for item in scan["runtime_assets"]["items"])

    character_ids = sorted(
        {
            str(item["characterId"])
            for item in scan["runtime_assets"]["items"]
            if item.get("characterId")
        }
    )
    assets.extend(
        _entity_asset(
            f"entity.character.{character_id}", character_id, "character", character_id
        )
        for character_id in character_ids
    )
    collectibles = scan["content"]["named_records"]["collectibles"]
    assets.extend(
        _entity_asset(f"entity.item.{item['id']}", item["name"], "item", item["id"])
        for item in collectibles
    )
    assets.extend(
        _document_asset(output_root, source_root, item)
        for item in scan["content"]["source_files"]
    )

    asset_keys = [asset["key"] for asset in assets]
    duplicate_keys = sorted(key for key, count in Counter(asset_keys).items() if count > 1)
    if duplicate_keys:
        raise InvalidLegacyProject(f"generated duplicate import keys: {duplicate_keys}")
    for asset in sorted(assets, key=lambda value: value["key"]):
        _write_asset(output_root, asset, counts)

    return {
        "schema_version": 1,
        "source_root": str(source_root),
        "metadata_root": str(output_root),
        "anchors": scan["anchors"]["count"],
        "runtime_assets": scan["runtime_assets"]["count"],
        "entities": len(character_ids) + len(collectibles),
        "content_documents": len(scan["content"]["source_files"]),
        "media_copied": 0,
        "written_files": counts["written"],
        "unchanged_files": counts["unchanged"],
        "warnings": scan["warnings"],
    }


def _manifest_from_source(source: PathLike) -> tuple[list[dict[str, Any]], Path | None]:
    path = Path(source).expanduser()
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise InvalidLegacyProject(f"manifest JSON is invalid: {path}") from exc
        legacy_root = None
    else:
        resolved = path.resolve(strict=True)
        candidates = (
            resolved / ".game-assets" / "runtime-manifest.json",
            resolved / "runtime-manifest.json",
        )
        manifest_path = next((candidate for candidate in candidates if candidate.is_file()), None)
        if manifest_path:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
            legacy_root = None
        elif (resolved / "src/content/assetManifest.ts").is_file():
            manifest = _parse_manifest(
                _read_text(resolved, "src/content/assetManifest.ts"),
                _read_text(resolved, "src/content/assetProductionStatus.ts"),
            )
            return sorted(manifest, key=lambda item: item["key"]), resolved
        else:
            raise InvalidLegacyProject(f"no runtime manifest found under: {resolved}")
    entries = data.get("assets") if isinstance(data, dict) else data
    if not isinstance(entries, list) or not all(isinstance(item, dict) for item in entries):
        raise InvalidLegacyProject("runtime manifest must be an array or contain an assets array")
    required = {"key", "type", "path", "width", "height", "preloadGroup", "status"}
    for entry in entries:
        missing = required - set(entry)
        if missing:
            raise InvalidLegacyProject(f"manifest entry is missing fields: {sorted(missing)}")
        if not _VALID_KEY.fullmatch(str(entry["key"])):
            raise InvalidLegacyProject(f"manifest contains invalid key: {entry['key']!r}")
    return sorted((dict(item) for item in entries), key=lambda item: item["key"]), legacy_root


def export_manifest(
    source: PathLike,
    output: PathLike | None = None,
    *,
    format: Literal["json", "typescript", "ts"] = "json",
    typescript: bool | None = None,
) -> str:
    """Render a stable legacy-compatible manifest from old or imported metadata.

    ``typescript=True`` is a convenience alias for ``format="typescript"``.
    The source checkout is never modified; an explicit output inside a detected
    legacy checkout is rejected.
    """

    if typescript is not None:
        format = "typescript" if typescript else "json"
    if format not in {"json", "typescript", "ts"}:
        raise ValueError("format must be 'json', 'typescript', or 'ts'")
    entries, legacy_root = _manifest_from_source(source)
    json_text = json.dumps(entries, ensure_ascii=False, indent=2, sort_keys=False)
    if format in {"typescript", "ts"}:
        rendered = (
            "// Generated by emperor-adapter. Stable keys and legacy paths are preserved.\n"
            f"export const assetManifest = {json_text} as const\n"
            "\nexport type AssetManifestEntry = (typeof assetManifest)[number]\n"
        )
    else:
        rendered = json_text + "\n"

    if output is not None:
        output_path = Path(output).expanduser().resolve(strict=False)
        if legacy_root is not None:
            try:
                output_path.relative_to(legacy_root)
            except ValueError:
                pass
            else:
                raise PathSafetyError("export output must not be inside the legacy project")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        if output_path.exists() and output_path.is_symlink():
            raise PathSafetyError(f"refusing to replace symlink: {output_path}")
        _atomic_write(output_path, rendered.encode())
    return rendered
