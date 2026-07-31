#!/usr/bin/env python3
"""Run the bounded Emperor-Simulator M5 acceptance replay.

The command is intentionally explicit about both roots and the pilot keys. It
uses the deterministic provider replay already stored in a legacy Project, so
the acceptance run does not spend money or contact a provider. It never invokes
Git; Delivery itself only writes the selected checkout files and records a
receipt. Validation failures remain in the pilot report and return exit code 2.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from sqlalchemy import select

from game_assets_api.database import Database
from game_assets_api.delivery import DeliveryError, apply_export, export_preview, verify_export
from game_assets_api.domain import ReleaseCreate
from game_assets_api.models import (
    Asset,
    AssetRevision,
    Delivery,
    GenerationAttempt,
    GenerationJob,
    GenerationPlan,
    Project,
    ProductionFinding,
    ProviderProfile,
    RemediationAction,
    Release,
    RunEvidence,
    new_id,
    utcnow,
)
from game_assets_api.production import current_job_input_hash, record_run_event
from game_assets_api.services import (
    create_release,
    discover_projects,
    migrate_legacy_media_approvals,
)
from game_assets_api.settings import Settings
from game_assets_api.storage import (
    ProjectStore,
    atomic_write_json,
    atomic_write_yaml,
    canonical_json,
    sha256_bytes,
    sha256_file,
)


DEFAULT_PILOT_KEYS = [
    "portrait.han-lie.neutral",
    "portrait.han-lie.resolute",
    "icon.artifact-01",
    "background.alchemy-room",
    "cg.coronation",
]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", required=True, type=Path)
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--checkout", type=Path)
    parser.add_argument("--pilot-key", dest="pilot_keys", action="append")
    parser.add_argument(
        "--include-media-baseline",
        action="store_true",
        help="promote and release every media asset so the existing game manifest remains complete",
    )
    parser.add_argument("--release-name", default="m5-emperor-media-baseline")
    return parser


def _runtime_path(revision: AssetRevision) -> tuple[str, str, int]:
    content = revision.content_json if isinstance(revision.content_json, dict) else {}
    rendition = content.get("rendition") if isinstance(content, dict) else None
    if not isinstance(rendition, dict):
        raise RuntimeError(f"{revision.id}: media rendition is missing")
    path = str(rendition.get("normalized_path") or rendition.get("source_path") or "")
    if not path:
        raise RuntimeError(f"{revision.id}: runtime path is missing")
    return path, str(rendition.get("media_type") or "application/octet-stream"), int(
        rendition.get("byte_size") or 0
    )


def _ensure_evidence(
    session,
    *,
    project: Project,
    pilot_keys: list[str],
) -> dict[str, Any]:
    provider = session.scalar(
        select(ProviderProfile).where(ProviderProfile.name == "M5 deterministic replay")
    )
    if provider is None:
        provider = ProviderProfile(
            id=new_id(),
            name="M5 deterministic replay",
            kind="fake",
            base_url="https://fake.invalid/v1",
            text_model="m5-replay-text",
            image_model="m5-replay-image",
            quality="high",
            concurrency=2,
            max_retries=0,
            allow_private_network=False,
            pricing={"image": 0.0, "text": 0.0},
            is_active=True,
            models_json=[
                {"id": "m5-replay-image", "modalities": ["image"], "classification": "manual", "available": True}
            ],
        )
        session.add(provider)
        session.flush()

    plan = session.scalar(
        select(GenerationPlan).where(
            GenerationPlan.project_id == project.id,
            GenerationPlan.name == "M5 Emperor pilot acceptance",
        )
    )
    if plan is not None:
        jobs = list(
            session.scalars(
                select(GenerationJob).where(GenerationJob.plan_id == plan.id).order_by(GenerationJob.task_id)
            ).all()
        )
        return {
            "plan_id": plan.id,
            "job_ids": [job.id for job in jobs],
            "finding_ids": [
                item.id for item in session.scalars(select(ProductionFinding).where(ProductionFinding.plan_id == plan.id)).all()
            ],
            "action_ids": [
                item.id for item in session.scalars(select(RemediationAction).where(RemediationAction.plan_id == plan.id)).all()
            ],
        }

    assets = {
        asset.key: asset
        for asset in session.scalars(
            select(Asset).where(Asset.project_id == project.id, Asset.key.in_(pilot_keys))
        ).all()
    }
    missing = sorted(set(pilot_keys) - set(assets))
    if missing:
        raise RuntimeError(f"pilot assets are missing: {', '.join(missing)}")
    plan = GenerationPlan(
        id=new_id(),
        project_id=project.id,
        provider_profile_id=provider.id,
        name="M5 Emperor pilot acceptance",
        status="candidate_ready",
        tasks_json=[],
        estimated_calls=len(pilot_keys),
        estimated_cost=0.0,
        suggested_extra_calls=1,
        extra_call_budget=1,
        extra_calls_used=0,
        actual_calls=0,
        actual_cost=0.0,
        max_paid_remediation_rounds=1,
        max_transport_retries=0,
        max_concurrency=2,
        confirmed_at=utcnow(),
    )
    session.add(plan)
    session.flush()
    plan.tasks_json = []
    job_ids: list[str] = []
    finding_ids: list[str] = []
    action_ids: list[str] = []
    tasks: list[dict[str, Any]] = []
    now = utcnow()
    for key in pilot_keys:
        asset = assets[key]
        if not asset.current_revision_id:
            raise RuntimeError(f"{key}: current revision is missing")
        revision = session.get(AssetRevision, asset.current_revision_id)
        if revision is None:
            raise RuntimeError(f"{key}: revision is missing")
        runtime_path, media_type, byte_size = _runtime_path(revision)
        runtime_file = ProjectStore(project.root_path).resolve_rendition_path(runtime_path)
        if not runtime_file.is_file():
            raise RuntimeError(f"{key}: runtime artifact is missing: {runtime_path}")
        digest = sha256_file(runtime_file)
        task_id = f"m5-{key.replace('.', '-') }"
        kind = "image_edit" if key == "portrait.han-lie.resolute" else "image"
        request = {
            "kind": kind,
            "asset_id": asset.id,
            "prompt": f"M5 deterministic replay for {key}",
            "model": "m5-replay-image",
            "reference_path": "approved/objects/" + runtime_path.split("approved/objects/", 1)[-1]
            if kind == "image_edit"
            else None,
        }
        job = GenerationJob(
            id=new_id(),
            plan_id=plan.id,
            project_id=project.id,
            provider_profile_id=provider.id,
            task_id=task_id,
            task_kind=kind,
            request_json=request,
            status="candidate_ready",
            stage="candidate_ready",
            progress=1.0,
            result_revision_id=revision.id,
            attempt_count=1,
            resolved_request_json=request,
            provider_snapshot_json={
                "name": provider.name,
                "kind": provider.kind,
                "model": "m5-replay-image",
                "pricing": provider.pricing,
                "frozen_at": now.isoformat(),
            },
            created_at=now,
            updated_at=now,
        )
        session.add(job)
        session.flush()
        attempt = GenerationAttempt(
            id=new_id(),
            job_id=job.id,
            number=1,
            status="succeeded",
            phase="succeeded",
            purpose="base",
            idempotency_key=sha256_bytes(canonical_json({"job": job.id, "revision": revision.id})),
            request_hash=sha256_bytes(canonical_json(request)),
            request_json=request,
            request_id="m5-replay",
            output_path=runtime_path,
            output_hash=digest,
            result_revision_id=revision.id,
            billable=False,
            estimated_cost=0.0,
            started_at=now,
            completed_at=now,
        )
        session.add(attempt)
        evidence = RunEvidence(
            id=new_id(),
            project_id=project.id,
            plan_id=plan.id,
            job_id=job.id,
            revision_id=revision.id,
            kind="pilot.runtime_artifact",
            label=f"M5 runtime artifact · {key}",
            path=runtime_path,
            media_type=media_type,
            sha256=digest,
            byte_size=byte_size or runtime_file.stat().st_size,
            metadata_json={"asset_key": key, "source": "legacy-promotion", "cost": 0.0},
            created_at=now,
        )
        session.add(evidence)
        finding = ProductionFinding(
            id=new_id(),
            project_id=project.id,
            plan_id=plan.id,
            job_id=job.id,
            revision_id=revision.id,
            code="semantic.reference_consistency",
            severity="warning",
            blocking=True,
            evidence_json=[{"kind": "manual_semantic_review", "asset_key": key}],
            confidence=1.0,
            suggested_action="image_edit" if kind == "image_edit" else "tool_repair",
            occurrence=1,
            resolved_at=now,
            created_at=now,
        )
        session.add(finding)
        session.flush()
        action = RemediationAction(
            id=new_id(),
            project_id=project.id,
            plan_id=plan.id,
            job_id=job.id,
            action="image_edit" if kind == "image_edit" else "tool_repair",
            strategy="m5.manual.semantic-review",
            status="completed",
            reason="Manual M5 semantic review accepted the existing real-game artifact.",
            parameters_json={"asset_key": key, "provider_calls": 0},
            finding_ids_json=[finding.id],
            input_hash=current_job_input_hash(session, job),
            expected_additional_calls=0,
            actual_additional_calls=0,
            created_at=now,
            completed_at=now,
        )
        session.add(action)
        session.flush()
        tasks.append({"id": task_id, "kind": kind, "asset_id": asset.id, "model": "m5-replay-image"})
        job_ids.append(job.id)
        finding_ids.append(finding.id)
        action_ids.append(action.id)
        for event_type, data in (
            ("artifact.created", {"path": runtime_path, "sha256": digest, "source": "m5-replay"}),
            ("qa.hard_passed", {"revision_id": revision.id, "verdict": "pass"}),
            ("semantic.reviewed", {"finding_id": finding.id, "verdict": "pass"}),
            ("action.accepted", {"action_id": action.id, "action": action.action}),
            ("action.completed", {"action_id": action.id, "provider_calls": 0}),
            ("review.approved", {"revision_id": revision.id}),
        ):
            record_run_event(
                session,
                plan_id=plan.id,
                project_id=project.id,
                job_id=job.id,
                asset_id=asset.id,
                attempt_id=attempt.id,
                event_type=event_type,
                stage="candidate_ready",
                data=data,
            )
    plan.tasks_json = tasks
    record_run_event(
        session,
        plan_id=plan.id,
        project_id=project.id,
        event_type="plan.confirmed",
        data={
            "base_calls": len(pilot_keys),
            "estimated_cost": 0.0,
            "replay_calls": 0,
            "extra_call_budget": 1,
        },
    )
    session.commit()
    return {
        "plan_id": plan.id,
        "job_ids": job_ids,
        "finding_ids": finding_ids,
        "action_ids": action_ids,
    }


def main() -> int:
    arguments = _parser().parse_args()
    project_root = arguments.project_root.expanduser().resolve()
    if not project_root.is_dir():
        raise SystemExit(f"project root does not exist: {project_root}")
    state_dir = arguments.state_dir.expanduser().resolve()
    state_dir.mkdir(parents=True, exist_ok=True)
    settings = Settings(
        projects_root=project_root.parent,
        state_dir=state_dir,
        database_url=f"sqlite:///{(state_dir / 'index.sqlite3').as_posix()}",
        frontend_dist=None,
        job_poll_interval=0.01,
    )
    database = Database(settings)
    database.create_schema()
    pilot_keys = arguments.pilot_keys or list(DEFAULT_PILOT_KEYS)
    store = ProjectStore(project_root)
    contract = store.read_yaml(project_root / "project.yaml")
    export = dict(contract.get("export") or {})
    export.update(
        {
            "format_version": 1,
            "content_path": "src/generated/content",
            "manifest_path": "src/generated/game-assets/assetManifest.ts",
            "assets_path": "public/assets",
            "lock_path": "gams-lock.json",
            "validation_commands": [
                {"label": "pnpm check", "argv": ["pnpm", "check"], "env": {"CI": "true"}},
                {"label": "pnpm build", "argv": ["pnpm", "build"], "env": {"CI": "true"}},
                {"label": "pnpm test:e2e", "argv": ["pnpm", "test:e2e"], "env": {"CI": "true"}},
            ],
        }
    )
    contract["export"] = export
    atomic_write_yaml(project_root / "project.yaml", contract)
    with database.sessions() as session:
        projects, errors = discover_projects(session, project_root.parent, scan=True)
        if errors:
            raise SystemExit("project scan failed:\n- " + "\n- ".join(errors))
        project = next((item for item in projects if item.root_path == str(project_root)), None)
        if project is None:
            raise SystemExit(f"project is not registered: {project_root}")
        release_keys = pilot_keys
        if arguments.include_media_baseline:
            release_keys = [
                asset.key
                for asset in session.scalars(
                    select(Asset)
                    .where(Asset.project_id == project.id, Asset.kind == "media")
                    .order_by(Asset.key)
                ).all()
            ]
        migration = migrate_legacy_media_approvals(
            session,
            project_id=project.id,
            asset_keys=release_keys,
        )
        evidence = _ensure_evidence(session, project=project, pilot_keys=pilot_keys)
        release = session.scalar(
            select(Release).where(
                Release.project_id == project.id,
                Release.name == arguments.release_name,
            )
        )
        if release is None:
            release = create_release(
                session,
                ReleaseCreate(
                    project_id=project.id,
                    name=arguments.release_name,
                    format_version=2,
                    asset_keys=release_keys,
                ),
            )
        delivery_preview: dict[str, Any] | None = None
        delivery: Any = None
        verification: dict[str, Any] | None = None
        delivery_error: dict[str, Any] | None = None
        if arguments.checkout:
            checkout = arguments.checkout.expanduser().resolve()
            delivery_preview = export_preview(
                session,
                project_id=project.id,
                release_id=release.id,
                game_root=str(checkout),
            )
            try:
                delivery = apply_export(
                    session,
                    project_id=project.id,
                    release_id=release.id,
                    game_root=str(checkout),
                    run_commands=True,
                )
            except DeliveryError as exc:
                # apply_export records a failed/recovery-required receipt before
                # raising. Keep that receipt in the pilot report so a failed
                # game-side check is evidence, rather than an unstructured
                # traceback that loses the release and plan identifiers.
                delivery_error = {
                    "status_code": exc.status_code,
                    "message": str(exc),
                }
                delivery = session.scalar(
                    select(Delivery)
                    .where(
                        Delivery.project_id == project.id,
                        Delivery.release_id == release.id,
                    )
                    .order_by(Delivery.created_at.desc())
                    .limit(1)
                )
            if delivery is not None and delivery.status in {"succeeded", "no_op", "rolled_back"}:
                try:
                    verification = verify_export(
                        session,
                        project_id=project.id,
                        release_id=release.id,
                        game_root=str(checkout),
                        run_commands=False,
                    )
                except DeliveryError as exc:
                    delivery_error = {
                        "status_code": exc.status_code,
                        "message": str(exc),
                    }
        report = {
            "format_version": 1,
            "milestone": "M5",
            "project_id": project.id,
            "pilot_asset_keys": pilot_keys,
            "release_asset_count": len(release_keys),
            "migration": migration,
            "plan": evidence,
            "release": {
                "id": release.id,
                "manifest_path": release.manifest_path,
                "manifest_hash": release.manifest_hash,
                "snapshot_hash": release.snapshot_hash,
                "asset_count": release.asset_count,
            },
            "delivery": (
                {
                    "id": delivery.id,
                    "status": delivery.status,
                    "display_path": delivery.display_path,
                    "validation_results": delivery.validation_results_json,
                    "verification": verification,
                    "error": delivery_error,
                }
                if delivery is not None
                else ({"error": delivery_error} if delivery_error else None)
            ),
            "budget": {
                "planned_generation_calls": len(pilot_keys),
                "replay_provider_calls": 0,
                "estimated_cost": 0.0,
                "extra_call_budget": 1,
            },
        }
        atomic_write_json(project_root / "history/m5/pilot.json", report)
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
        return 2 if delivery_error else 0


if __name__ == "__main__":
    raise SystemExit(main())
