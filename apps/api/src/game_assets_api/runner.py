from __future__ import annotations

import asyncio
from contextlib import suppress
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from .domain import (
    ErrorCategory,
    GenerationStatus,
    QARunCreate,
    RevisionCreate,
    RevisionFormat,
    TaskKind,
)
from .models import (
    Asset,
    GenerationAttempt,
    GenerationJob,
    Project,
    ProviderProfile,
    Rendition,
    new_id,
    utcnow,
)
from .providers import CredentialVault, ProviderError, ProviderResult, build_provider
from .qa import normalize_image
from .services import ServiceError, asset_descriptor, create_revision, run_qa
from .settings import Settings
from .storage import ProjectStore, atomic_write_bytes, relative_to_root, safe_join


class JobRunner:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        vault: CredentialVault,
        settings: Settings,
    ) -> None:
        self.sessions = sessions
        self.vault = vault
        self.settings = settings
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()

    async def start(self) -> None:
        self._stopping.clear()
        self.recover_interrupted_jobs()
        self._task = asyncio.create_task(self._loop(), name="game-assets-job-runner")

    async def stop(self) -> None:
        self._stopping.set()
        if self._task:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
        self._task = None

    def recover_interrupted_jobs(self) -> None:
        with self.sessions() as session:
            jobs = session.scalars(
                select(GenerationJob).where(GenerationJob.status == GenerationStatus.RUNNING.value)
            ).all()
            for job in jobs:
                profile = session.get(ProviderProfile, job.provider_profile_id)
                unlocked = profile and (
                    profile.kind == "fake" or self.vault.is_unlocked(profile.id)
                )
                job.status = (
                    GenerationStatus.QUEUED.value
                    if unlocked
                    else GenerationStatus.CREDENTIALS_LOCKED.value
                )
                job.error_category = None
                job.error_message = "recovered after service restart"
                job.updated_at = utcnow()
            session.commit()

    async def _loop(self) -> None:
        while not self._stopping.is_set():
            try:
                processed = await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                # The loop must survive one corrupt job. run_once records expected failures itself.
                processed = False
            if not processed:
                await asyncio.sleep(self.settings.job_poll_interval)

    def _ready_job(self, session: Session) -> GenerationJob | None:
        jobs = session.scalars(
            select(GenerationJob)
            .where(GenerationJob.status == GenerationStatus.QUEUED.value)
            .order_by(GenerationJob.created_at.asc())
        ).all()
        for job in jobs:
            dependencies = list(job.request_json.get("depends_on", []))
            if not dependencies:
                return job
            dependency_jobs = session.scalars(
                select(GenerationJob).where(
                    GenerationJob.plan_id == job.plan_id,
                    GenerationJob.task_id.in_(dependencies),
                )
            ).all()
            statuses = {dependency.task_id: dependency.status for dependency in dependency_jobs}
            terminal_failure = next(
                (
                    task_id
                    for task_id in dependencies
                    if statuses.get(task_id)
                    in {
                        GenerationStatus.FAILED.value,
                        GenerationStatus.CANCELLED.value,
                    }
                ),
                None,
            )
            if terminal_failure:
                job.status = GenerationStatus.FAILED.value
                job.error_category = ErrorCategory.VALIDATION.value
                job.error_message = f"dependency {terminal_failure} did not succeed"
                job.updated_at = utcnow()
                continue
            if all(statuses.get(task_id) == GenerationStatus.SUCCEEDED.value for task_id in dependencies):
                return job
        return None

    async def run_once(self) -> bool:
        with self.sessions() as session:
            job = self._ready_job(session)
            if job is None:
                session.commit()
                return False
            if job.cancel_requested:
                job.status = GenerationStatus.CANCELLED.value
                job.error_category = ErrorCategory.CANCELLED.value
                job.error_message = "cancelled before provider invocation"
                job.updated_at = utcnow()
                session.commit()
                return True
            profile = session.get(ProviderProfile, job.provider_profile_id)
            asset = session.get(Asset, str(job.request_json.get("asset_id")))
            if profile is None or asset is None:
                job.status = GenerationStatus.FAILED.value
                job.error_category = ErrorCategory.VALIDATION.value
                job.error_message = "job references a missing provider or asset"
                job.updated_at = utcnow()
                session.commit()
                return True
            try:
                provider = build_provider(profile, self.vault)
            except ProviderError:
                job.status = GenerationStatus.CREDENTIALS_LOCKED.value
                job.error_category = ErrorCategory.AUTH.value
                job.error_message = "provider credentials are locked"
                job.updated_at = utcnow()
                session.commit()
                return True
            job.status = GenerationStatus.RUNNING.value
            job.progress = 0.05
            job.error_category = None
            job.error_message = None
            asset.generation_status = GenerationStatus.RUNNING.value
            job.updated_at = utcnow()
            profile_id = profile.id
            job_id = job.id
            max_retries = profile.max_retries
            session.commit()

        last_error: ProviderError | None = None
        for retry_index in range(max_retries + 1):
            attempt_id, attempt_number = self._start_attempt(job_id)
            try:
                with self.sessions() as session:
                    profile = session.get(ProviderProfile, profile_id)
                    if profile is None:
                        raise ProviderError("provider profile was removed", ErrorCategory.VALIDATION)
                    provider = build_provider(profile, self.vault)
                    job = session.get(GenerationJob, job_id)
                    if job is None:
                        return True
                    request = dict(job.request_json)
                    project = session.get(Project, job.project_id)
                    asset = session.get(Asset, str(request["asset_id"]))
                    if project is None or asset is None:
                        raise ProviderError("job project or asset is missing", ErrorCategory.VALIDATION)
                    schema = request.get("schema")
                    if request["kind"] == TaskKind.TEXT.value and schema is None:
                        schema = ProjectStore(project.root_path).read_schema(asset.schema_ref)

                result = await self._invoke(provider, job_id=job_id, request=request, schema=schema)
                self._finish_attempt(attempt_id, result=result)
                revision_id = self._stage_result(
                    job_id=job_id,
                    attempt_number=attempt_number,
                    result=result,
                    schema=schema,
                )
                self._complete_job(job_id, revision_id)
                return True
            except ProviderError as exc:
                last_error = exc
                self._finish_attempt(attempt_id, error=exc)
                if exc.category == ErrorCategory.AUTH:
                    self._lock_job(job_id)
                    return True
                if not exc.retryable or retry_index >= max_retries:
                    break
                await asyncio.sleep(exc.retry_after if exc.retry_after is not None else min(2**retry_index, 8))
            except (ServiceError, OSError, ValueError) as exc:
                last_error = ProviderError(str(exc), ErrorCategory.VALIDATION)
                self._finish_attempt(attempt_id, error=last_error)
                break
            except Exception as exc:
                last_error = ProviderError(
                    f"generation result could not be processed: {type(exc).__name__}",
                    ErrorCategory.INVALID_RESPONSE,
                )
                self._finish_attempt(attempt_id, error=last_error)
                break
        self._fail_job(job_id, last_error or ProviderError("generation failed"))
        return True

    async def _invoke(
        self,
        provider,
        *,
        job_id: str,
        request: dict[str, Any],
        schema: dict[str, Any] | None,
    ) -> ProviderResult:
        if request["kind"] == TaskKind.TEXT.value:
            if not schema:
                raise ProviderError("text task has no schema", ErrorCategory.VALIDATION)
            return await provider.structured_text(prompt=str(request["prompt"]), schema=schema)
        reference = None
        if request["kind"] == TaskKind.IMAGE_EDIT.value:
            reference_path = request.get("reference_path")
            if not reference_path:
                raise ProviderError("image edit task needs a reference path", ErrorCategory.VALIDATION)
            with self.sessions() as session:
                job = session.get(GenerationJob, job_id)
                if job is None:
                    raise ProviderError("job is missing", ErrorCategory.VALIDATION)
                project = session.get(Project, job.project_id)
                if project is None:
                    raise ProviderError("project is missing", ErrorCategory.VALIDATION)
                path = safe_join(Path(project.root_path), reference_path)
                if not path.is_file():
                    raise ProviderError("reference image does not exist", ErrorCategory.VALIDATION)
                reference = path.read_bytes()
        return await provider.image(
            prompt=str(request["prompt"]),
            width=request.get("width"),
            height=request.get("height"),
            reference=reference,
        )

    def _start_attempt(self, job_id: str) -> tuple[str, int]:
        with self.sessions() as session:
            job = session.get(GenerationJob, job_id)
            if job is None:
                raise ProviderError("job was removed", ErrorCategory.VALIDATION)
            number = job.attempt_count + 1
            attempt = GenerationAttempt(
                id=new_id(), job_id=job.id, number=number, status=GenerationStatus.RUNNING.value
            )
            job.attempt_count = number
            job.updated_at = utcnow()
            session.add(attempt)
            session.commit()
            return attempt.id, number

    def _finish_attempt(
        self,
        attempt_id: str,
        *,
        result: ProviderResult | None = None,
        error: ProviderError | None = None,
    ) -> None:
        with self.sessions() as session:
            attempt = session.get(GenerationAttempt, attempt_id)
            if attempt is None:
                return
            attempt.status = (
                GenerationStatus.SUCCEEDED.value if error is None else GenerationStatus.FAILED.value
            )
            attempt.request_id = result.request_id if result else error.request_id if error else None
            attempt.error_category = error.category.value if error else None
            attempt.error_message = str(error) if error else None
            attempt.completed_at = utcnow()
            session.commit()

    def _stage_result(
        self,
        *,
        job_id: str,
        attempt_number: int,
        result: ProviderResult,
        schema: dict[str, Any] | None,
    ) -> str:
        with self.sessions() as session:
            job = session.get(GenerationJob, job_id)
            if job is None:
                raise ServiceError(404, "job not found")
            project = session.get(Project, job.project_id)
            profile = session.get(ProviderProfile, job.provider_profile_id)
            asset = session.get(Asset, str(job.request_json["asset_id"]))
            if project is None or profile is None or asset is None:
                raise ServiceError(422, "job references missing records")
            provider_snapshot = {
                "profileId": profile.id,
                "kind": profile.kind,
                "baseUrl": profile.base_url,
                "textModel": profile.text_model,
                "imageModel": profile.image_model,
                "quality": profile.quality,
                "requestId": result.request_id or "unknown",
            }
            if job.task_kind == TaskKind.TEXT.value:
                revision = create_revision(
                    session,
                    RevisionCreate(
                        asset_id=asset.id,
                        format=RevisionFormat.JSON,
                        content=result.value,
                        prompt_recipe=job.request_json.get("prompt_recipe"),
                        provider_snapshot=provider_snapshot,
                    ),
                )
                return revision.id

            if not isinstance(result.value, bytes):
                raise ServiceError(422, "image provider did not return bytes")
            store = ProjectStore(project.root_path)
            attempt_dir = store.candidate_dir(job.id) / f"attempt-{attempt_number}"
            source = attempt_dir / "source.bin"
            normalized = attempt_dir / "normalized.webp"
            atomic_write_bytes(source, result.value, immutable=True)
            info = normalize_image(
                result.value,
                normalized,
                expected_width=job.request_json.get("width"),
                expected_height=job.request_json.get("height"),
                transparent=bool(job.request_json.get("transparent")),
            )
            normalized_relative = relative_to_root(store.root, normalized)
            revision = create_revision(
                session,
                RevisionCreate(
                    asset_id=asset.id,
                    format=RevisionFormat.MEDIA,
                    content={
                        "prompt": job.request_json["prompt"],
                        "width": info["width"],
                        "height": info["height"],
                        "colorKeyRemoved": info["colorKeyRemoved"],
                    },
                    provider_snapshot=provider_snapshot,
                    candidate_path=normalized_relative,
                ),
            )
            rendition = Rendition(
                id=new_id(),
                revision_id=revision.id,
                media_type=info["mediaType"],
                source_path=relative_to_root(store.root, source),
                normalized_path=normalized_relative,
                target_path=job.request_json.get("target_path"),
                sha256=info["sha256"],
                width=info["width"],
                height=info["height"],
                byte_size=info["byteSize"],
            )
            session.add(rendition)
            session.commit()
            run_qa(
                session,
                QARunCreate(
                    rendition_id=rendition.id,
                    expected_width=job.request_json.get("width"),
                    expected_height=job.request_json.get("height"),
                    require_alpha=bool(job.request_json.get("transparent")),
                ),
            )
            return revision.id

    def _complete_job(self, job_id: str, revision_id: str) -> None:
        with self.sessions() as session:
            job = session.get(GenerationJob, job_id)
            if job is None:
                return
            asset = session.get(Asset, str(job.request_json["asset_id"]))
            job.result_revision_id = revision_id
            job.progress = 1.0
            if job.cancel_requested:
                job.status = GenerationStatus.CANCELLED.value
                job.error_category = ErrorCategory.CANCELLED.value
                job.error_message = "provider call completed and candidate was staged after cancellation"
            else:
                job.status = GenerationStatus.SUCCEEDED.value
                job.error_category = None
                job.error_message = None
            job.updated_at = utcnow()
            if asset:
                asset.generation_status = job.status
                project = session.get(Project, asset.project_id)
                if project:
                    ProjectStore(project.root_path).write_asset(asset_descriptor(asset))
            session.commit()

    def _lock_job(self, job_id: str) -> None:
        with self.sessions() as session:
            job = session.get(GenerationJob, job_id)
            if job:
                job.status = GenerationStatus.CREDENTIALS_LOCKED.value
                job.error_category = ErrorCategory.AUTH.value
                job.error_message = "provider credentials are locked"
                job.updated_at = utcnow()
                session.commit()

    def _fail_job(self, job_id: str, error: ProviderError) -> None:
        with self.sessions() as session:
            job = session.get(GenerationJob, job_id)
            if job is None:
                return
            asset = session.get(Asset, str(job.request_json.get("asset_id")))
            job.status = (
                GenerationStatus.CANCELLED.value
                if job.cancel_requested
                else GenerationStatus.FAILED.value
            )
            job.progress = 1.0
            job.error_category = (
                ErrorCategory.CANCELLED.value if job.cancel_requested else error.category.value
            )
            job.error_message = (
                "cancelled after provider invocation" if job.cancel_requested else str(error)
            )
            job.updated_at = utcnow()
            if asset:
                asset.generation_status = job.status
            session.commit()
