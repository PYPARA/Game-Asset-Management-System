from __future__ import annotations

import argparse
import json
from typing import Any

import uvicorn

from .database import Database
from .domain import ProviderKind, RunInspectRead
from .models import GenerationPlan, ProviderProfile
from .production import inspect_run, resume_recoverable_jobs
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
            print(f"{job['task_id']}: {job['status']} / {job['stage']}{reason}")
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
        profile = session.get(ProviderProfile, plan.provider_profile_id)
        jobs = resume_recoverable_jobs(
            session,
            plan=plan,
            credentials_available=bool(profile and profile.kind == ProviderKind.FAKE.value),
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


def main(argv: list[str] | None = None) -> None:
    arguments = _parser().parse_args(argv)
    settings = Settings()
    if arguments.command in {None, "serve"}:
        raise SystemExit(_serve(settings))
    if arguments.command == "run" and arguments.run_command == "inspect":
        raise SystemExit(_inspect(settings, arguments.plan_id, as_json=arguments.as_json))
    if arguments.command == "run" and arguments.run_command == "resume":
        raise SystemExit(_resume(settings, arguments.plan_id, as_json=arguments.as_json))
    raise SystemExit(2)


if __name__ == "__main__":
    main()
