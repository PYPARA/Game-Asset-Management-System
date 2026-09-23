from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import uvicorn
from sqlalchemy import select

from .database import Database
from .delivery import DeliveryError, apply_export, export_preview, release_preflight, verify_export
from .codex_adapter import build_codex_adapter
from .domain import AgentSessionRead, ProviderKind, ReleaseCreate, RunInspectRead
from .models import GenerationJob, GenerationPlan, Project, ProviderProfile, Release
from .agent import diagnose_job
from .production import inspect_run, resume_recoverable_jobs
from .services import create_release
from .settings import Settings


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gams", description="Game Asset Management System")
    subcommands = parser.add_subparsers(dest="command")
    subcommands.add_parser("planning-worker", help="run the durable planning worker")
    serve = subcommands.add_parser("serve", help="start the local API and workbench")
    serve.add_argument(
        "--reload",
        action="store_true",
        help="restart the development API when Python files change",
    )
    run = subcommands.add_parser("run", help="inspect or safely resume a production run")
    run_commands = run.add_subparsers(dest="run_command", required=True)
    inspect = run_commands.add_parser("inspect", help="show persisted run state and evidence")
    inspect.add_argument("plan_id")
    inspect.add_argument("--json", action="store_true", dest="as_json")
    reconcile = run_commands.add_parser("reconcile-local-failures", help="repair proven pre-send DNS failures without executing jobs")
    reconcile.add_argument("plan_id")
    reconcile.add_argument("--json", action="store_true", dest="as_json")
    resume = run_commands.add_parser("resume", help="resume only jobs with persisted safe state")
    resume.add_argument("plan_id")
    resume.add_argument("--json", action="store_true", dest="as_json")
    agent = subcommands.add_parser("agent", help="request an isolated Codex diagnosis")
    agent_commands = agent.add_subparsers(dest="agent_command", required=True)
    diagnose = agent_commands.add_parser("diagnose", help="diagnose one persisted Job")
    diagnose.add_argument("job_id")
    diagnose.add_argument("--json", action="store_true", dest="as_json")
    release = subcommands.add_parser("release", help="validate or create immutable Releases")
    release_commands = release.add_subparsers(dest="release_command", required=True)
    preflight = release_commands.add_parser("preflight", help="check approved assets for a v2 Release")
    preflight.add_argument("project")
    preflight.add_argument(
        "--asset",
        dest="asset_keys",
        action="append",
        help="limit the preflight to an explicit stable asset key (repeatable)",
    )
    preflight.add_argument("--json", action="store_true", dest="as_json")
    create = release_commands.add_parser("create", help="create an immutable Release Manifest v2")
    create.add_argument("project")
    create.add_argument("--name", required=True)
    create.add_argument(
        "--asset",
        dest="asset_keys",
        action="append",
        help="release an explicit stable asset key (repeatable)",
    )
    create.add_argument("--json", action="store_true", dest="as_json")
    acceptance = subcommands.add_parser(
        "acceptance", help="run repeatable release acceptance rehearsals"
    )
    acceptance_commands = acceptance.add_subparsers(dest="acceptance_command", required=True)
    m6 = acceptance_commands.add_parser("m6", help="run the M6 v1 release rehearsal")
    m6.add_argument(
        "--output",
        default="m6-acceptance.json",
        help="structured JSON receipt path (default: ./m6-acceptance.json)",
    )
    m6.add_argument(
        "--provider-url",
        help="optional live OpenAI-compatible provider base URL",
    )
    m6.add_argument(
        "--api-key",
        default=os.environ.get("GAME_ASSETS_M6_API_KEY"),
        help="optional live provider API key; prefer GAME_ASSETS_M6_API_KEY",
    )
    m6.add_argument(
        "--require-live-provider",
        action="store_true",
        help="fail when a live provider URL and key are not supplied",
    )
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
                    (ProviderProfile.kind == ProviderKind.FAKE.value)
                    | ProviderProfile.credential_mode.in_(("optional", "none"))
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


def _diagnose(settings: Settings, job_id: str, *, as_json: bool) -> int:
    database = _database(settings)
    adapter = build_codex_adapter(
        settings.codex_command,
        timeout_seconds=settings.codex_timeout_seconds,
    )
    with database.sessions() as session:
        job = session.get(GenerationJob, job_id)
        if job is None:
            raise SystemExit(f"generation job not found: {job_id}")
        result = asyncio.run(
            diagnose_job(
                session,
                job,
                adapter=adapter,
                budget=settings.agent_budget,
            )
        )
        payload = AgentSessionRead.model_validate(result).model_dump(mode="json")
    _print(payload, as_json=as_json)
    return 0 if payload["status"] == "completed" else 2


def _serve(settings: Settings, *, reload: bool = False) -> int:
    # The supervisor lives outside uvicorn's reload child. API reloads never
    # cancel planning; a dead worker is restarted and reclaims persisted leases.
    _database(settings)  # Complete schema creation before starting sibling processes.
    stop = threading.Event()
    def supervise_worker() -> None:
        while not stop.is_set():
            child = subprocess.Popen([sys.executable, "-m", "game_assets_api.cli", "planning-worker"])
            while child.poll() is None and not stop.wait(1):
                pass
            if stop.is_set() and child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
            stop.wait(30 if child.returncode == 75 else 1)
    worker = None
    if settings.planning_external_worker and os.environ.get("GAME_ASSETS_MANAGE_PLANNING_WORKER", "true").lower() != "false":
        worker = threading.Thread(target=supervise_worker, daemon=True, name="planning-supervisor")
        worker.start()
    try:
        uvicorn.run(
            "game_assets_api.main:app",
            host=settings.host,
            port=settings.port,
            log_level=settings.log_level,
            reload=reload,
            access_log=True,
            timeout_graceful_shutdown=5,
        )
    finally:
        stop.set()
        if worker:
            worker.join(timeout=12)
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


def _release_preflight(
    settings: Settings,
    value: str,
    *,
    asset_keys: list[str] | None,
    as_json: bool,
) -> int:
    database = _database(settings)
    with database.sessions() as session:
        project = _resolve_project(session, value)
        # A preflight before Release creation uses the same deterministic v2
        # checks as the service; the release command itself remains explicit.
        from .services import _release_preflight as collect_release_preflight

        entries, _assets, issues = collect_release_preflight(
            session, project=project, asset_keys=asset_keys
        )
        payload = {
            "project_id": project.id,
            "manifest_version": 2,
            "asset_keys": asset_keys,
            "blocking": bool(issues),
            "issues": issues,
            "asset_count": len(entries),
        }
    _print(payload, as_json=as_json)
    return 0 if not payload["blocking"] else 2


def _create_release(
    settings: Settings,
    value: str,
    name: str,
    *,
    asset_keys: list[str] | None,
    as_json: bool,
) -> int:
    database = _database(settings)
    with database.sessions() as session:
        project = _resolve_project(session, value)
        release = create_release(
            session,
            ReleaseCreate(
                project_id=project.id,
                name=name,
                format_version=2,
                asset_keys=asset_keys,
            ),
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


def _acceptance_m6(arguments: argparse.Namespace) -> int:
    from .m6_acceptance import run_m6_acceptance, write_report

    report = run_m6_acceptance(
        live_provider_url=arguments.provider_url,
        live_provider_api_key=arguments.api_key,
        require_live_provider=arguments.require_live_provider,
    )
    output = write_report(report, arguments.output)
    payload = {"output": str(output), **report["summary"]}
    _print(payload, as_json=True)
    return 0 if report["summary"]["ok"] else 2


def main(argv: list[str] | None = None) -> None:
    arguments = _parser().parse_args(argv)
    if arguments.command in {None, "serve"}:
        os.environ.setdefault("GAME_ASSETS_PLANNING_EXTERNAL_WORKER", "true")
    settings = Settings()
    if arguments.command == "planning-worker":
        from .planning_runtime import run_worker
        async def worker_main() -> None:
            import signal
            task = asyncio.create_task(run_worker(settings))
            loop = asyncio.get_running_loop()
            for signum in (signal.SIGTERM, signal.SIGINT):
                loop.add_signal_handler(signum, task.cancel)
            try:
                await task
            except asyncio.CancelledError:
                pass
        asyncio.run(worker_main())
        return
    if arguments.command in {None, "serve"}:
        raise SystemExit(_serve(settings, reload=bool(getattr(arguments, "reload", False))))
    if arguments.command == "run" and arguments.run_command == "reconcile-local-failures":
        from .dispatch_repair import reconcile_local_failures
        database = _database(settings)
        with database.sessions() as session:
            plan = session.get(GenerationPlan, arguments.plan_id)
            if plan is None:
                raise SystemExit("generation plan not found")
            repaired = reconcile_local_failures(session, plan)
        _print({"repaired_attempts": repaired}, as_json=arguments.as_json)
        return
    if arguments.command == "run" and arguments.run_command == "inspect":
        raise SystemExit(_inspect(settings, arguments.plan_id, as_json=arguments.as_json))
    if arguments.command == "run" and arguments.run_command == "resume":
        raise SystemExit(_resume(settings, arguments.plan_id, as_json=arguments.as_json))
    if arguments.command == "agent" and arguments.agent_command == "diagnose":
        raise SystemExit(_diagnose(settings, arguments.job_id, as_json=arguments.as_json))
    if arguments.command == "release" and arguments.release_command == "preflight":
        raise SystemExit(
            _release_preflight(
                settings,
                arguments.project,
                asset_keys=arguments.asset_keys,
                as_json=arguments.as_json,
            )
        )
    if arguments.command == "release" and arguments.release_command == "create":
        raise SystemExit(
            _create_release(
                settings,
                arguments.project,
                arguments.name,
                asset_keys=arguments.asset_keys,
                as_json=arguments.as_json,
            )
        )
    if arguments.command == "export":
        try:
            raise SystemExit(_export(settings, arguments))
        except DeliveryError as exc:
            raise SystemExit(f"export failed: {exc}") from exc
    if arguments.command == "acceptance" and arguments.acceptance_command == "m6":
        raise SystemExit(_acceptance_m6(arguments))
    raise SystemExit(2)


if __name__ == "__main__":
    main()
