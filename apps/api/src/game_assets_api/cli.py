from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import uvicorn
from sqlalchemy import select

from .database import Database
from .delivery import DeliveryError, apply_export, export_preview, release_preflight, verify_export
from .domain import ProviderKind, ReleaseCreate, RunInspectRead
from .models import GenerationPlan, Project, ProviderProfile, Release
from .production import inspect_run, resume_recoverable_jobs
from .services import create_release
from .settings import Settings


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gams", description="Game Asset Management System")
    subcommands = parser.add_subparsers(dest="command")
    subcommands.add_parser("serve", help="start the local API and workbench")
    run = subcommands.add_parser("run", help="inspect or safely resume a production run")
    run_commands = run.add_subparsers(dest="run_command", required=True)
    inspect = run_commands.add_parser("inspect", help="show persisted run state and evidence")
    inspect.add_argument("plan_id")
    inspect.add_argument("--json", action="store_true", dest="as_json")
    resume = run_commands.add_parser("resume", help="resume only jobs with persisted safe state")
    resume.add_argument("plan_id")
    resume.add_argument("--json", action="store_true", dest="as_json")
    release = subcommands.add_parser("release", help="validate or create immutable Releases")
    release_commands = release.add_subparsers(dest="release_command", required=True)
    preflight = release_commands.add_parser("preflight", help="check approved assets for a v2 Release")
    preflight.add_argument("project")
    preflight.add_argument("--json", action="store_true", dest="as_json")
    create = release_commands.add_parser("create", help="create an immutable Release Manifest v2")
    create.add_argument("project")
    create.add_argument("--name", required=True)
    create.add_argument("--json", action="store_true", dest="as_json")
    export = subcommands.add_parser("export", help="deliver a Release to a game checkout")
    export_commands = export.add_subparsers(dest="export_command", required=True)
    for command in ("preview", "apply", "verify", "rollback"):
        export_parser = export_commands.add_parser(command)
        export_parser.add_argument("release", nargs="?" if command == "verify" else None)
        export_parser.add_argument("--project", required=True)
        export_parser.add_argument("--game-root")
        export_parser.add_argument("--run-commands", action="store_true")
        export_parser.add_argument("--json", action="store_true", dest="as_json")
    return parser


def _database(settings: Settings) -> Database:
    database = Database(settings)
    database.create_schema()
    return database


def _print(value: Any, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(value, ensure_ascii=False, indent=2, default=str))
        return
    if isinstance(value, dict) and "plan" in value:
        plan = value["plan"]
        print(f"{plan['name']}  {plan['id']}  [{plan['status']}]")
        print(
            f"calls {plan['actual_calls']}/{plan['estimated_calls']} base, "
            f"remediation {plan['extra_calls_used']}/{plan['extra_call_budget']}"
        )
        for job in value["jobs"]:
            reason = f" — {job['error_message']}" if job.get("error_message") else ""
            snapshot = job.get("provider_snapshot") or {}
            provider = snapshot.get("name") or job.get("provider_profile_id") or "unknown-provider"
            model = snapshot.get("model") or (job.get("request") or {}).get("model") or "unknown-model"
            print(
                f"{job['task_id']}: {job['status']} / {job['stage']} "
                f"[{provider} / {model}]{reason}"
            )
        return
    print(value)


def _inspect(settings: Settings, plan_id: str, *, as_json: bool) -> int:
    database = _database(settings)
    with database.sessions() as session:
        plan = session.get(GenerationPlan, plan_id)
        if plan is None:
            raise SystemExit(f"generation plan not found: {plan_id}")
        payload = RunInspectRead.model_validate(inspect_run(session, plan)).model_dump(mode="json")
    _print(payload, as_json=as_json)
    return 0


def _resume(settings: Settings, plan_id: str, *, as_json: bool) -> int:
    database = _database(settings)
    with database.sessions() as session:
        plan = session.get(GenerationPlan, plan_id)
        if plan is None:
            raise SystemExit(f"generation plan not found: {plan_id}")
        available_provider_ids = {
            profile.id
            for profile in session.scalars(
                select(ProviderProfile).where(
                    ProviderProfile.kind == ProviderKind.FAKE.value
                )
            ).all()
        }
        jobs = resume_recoverable_jobs(
            session,
            plan=plan,
            available_provider_ids=available_provider_ids,
        )
        payload = [
            {
                "id": job.id,
                "task_id": job.task_id,
                "status": job.status,
                "stage": job.stage,
                "error_message": job.error_message,
            }
            for job in jobs
        ]
    _print(payload, as_json=as_json)
    return 0


def _serve(settings: Settings) -> int:
    uvicorn.run(
        "game_assets_api.main:app",
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level,
        reload=False,
        access_log=True,
    )
    return 0


def _resolve_project(session, value: str) -> Project:
    path = Path(value).expanduser()
    project = None
    if path.is_absolute() or "/" in value or "\\" in value:
        try:
            project = session.query(Project).filter(Project.root_path == str(path.resolve())).first()
        except OSError:
            project = None
    if project is None:
        project = session.get(Project, value)
    if project is None:
        raise SystemExit(f"project not found: {value}")
    return project


def _release_preflight(settings: Settings, value: str, *, as_json: bool) -> int:
    database = _database(settings)
    with database.sessions() as session:
        project = _resolve_project(session, value)
        # A preflight before Release creation uses the same deterministic v2
        # checks as the service; the release command itself remains explicit.
        from .services import _release_preflight as collect_release_preflight

        entries, _assets, issues = collect_release_preflight(session, project=project)
        payload = {
            "project_id": project.id,
            "manifest_version": 2,
            "blocking": bool(issues),
            "issues": issues,
            "asset_count": len(entries),
        }
    _print(payload, as_json=as_json)
    return 0 if not payload["blocking"] else 2


def _create_release(settings: Settings, value: str, name: str, *, as_json: bool) -> int:
    database = _database(settings)
    with database.sessions() as session:
        project = _resolve_project(session, value)
        release = create_release(
            session,
            ReleaseCreate(project_id=project.id, name=name, format_version=2),
        )
        payload = {
            "id": release.id,
            "project_id": release.project_id,
            "name": release.name,
            "manifest_version": release.manifest_version,
            "manifest_path": release.manifest_path,
            "manifest_hash": release.manifest_hash,
            "snapshot_hash": release.snapshot_hash,
            "asset_count": release.asset_count,
        }
    _print(payload, as_json=as_json)
    return 0


def _export(settings: Settings, arguments: argparse.Namespace) -> int:
    database = _database(settings)
    with database.sessions() as session:
        if arguments.export_command == "verify":
            payload = verify_export(
                session,
                project_id=arguments.project,
                release_id=arguments.release,
                game_root=arguments.game_root,
                run_commands=arguments.run_commands,
            )
        elif arguments.export_command == "preview":
            payload = export_preview(
                session,
                project_id=arguments.project,
                release_id=arguments.release,
                game_root=arguments.game_root,
            )
        elif arguments.export_command == "apply":
            payload = apply_export(
                session,
                project_id=arguments.project,
                release_id=arguments.release,
                game_root=arguments.game_root,
                run_commands=arguments.run_commands,
            )
        else:
            preview = export_preview(
                session,
                project_id=arguments.project,
                release_id=arguments.release,
                game_root=arguments.game_root,
            )
            payload = apply_export(
                session,
                project_id=arguments.project,
                release_id=arguments.release,
                game_root=arguments.game_root,
                run_commands=arguments.run_commands,
                rollback_from=preview.get("previous_release_id"),
            )
        if hasattr(payload, "__table__"):
            payload = {
                "id": payload.id,
                "project_id": payload.project_id,
                "release_id": payload.release_id,
                "status": payload.status,
                "display_path": payload.display_path,
                "files": payload.files_json,
                "validation_results": payload.validation_results_json,
            }
    _print(payload, as_json=arguments.as_json)
    if arguments.export_command == "verify" and not payload.get("ok", False):
        return 2
    return 0


def main(argv: list[str] | None = None) -> None:
    arguments = _parser().parse_args(argv)
    settings = Settings()
    if arguments.command in {None, "serve"}:
        raise SystemExit(_serve(settings))
    if arguments.command == "run" and arguments.run_command == "inspect":
        raise SystemExit(_inspect(settings, arguments.plan_id, as_json=arguments.as_json))
    if arguments.command == "run" and arguments.run_command == "resume":
        raise SystemExit(_resume(settings, arguments.plan_id, as_json=arguments.as_json))
    if arguments.command == "release" and arguments.release_command == "preflight":
        raise SystemExit(_release_preflight(settings, arguments.project, as_json=arguments.as_json))
    if arguments.command == "release" and arguments.release_command == "create":
        raise SystemExit(_create_release(settings, arguments.project, arguments.name, as_json=arguments.as_json))
    if arguments.command == "export":
        try:
            raise SystemExit(_export(settings, arguments))
        except DeliveryError as exc:
            raise SystemExit(f"export failed: {exc}") from exc
    raise SystemExit(2)


if __name__ == "__main__":
    main()
