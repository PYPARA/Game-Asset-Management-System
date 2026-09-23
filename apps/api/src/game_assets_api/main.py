from __future__ import annotations

import asyncio

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .api import router
from .codex_adapter import build_codex_adapter, AppServerCodexAdapter
from .planning_provider import ProviderPlanningAdapter, serve_credentials
from .database import Database
from .generation_planning import GenerationPlanningRunner
from .planning_runtime import PlanningQueueClient
from .providers import CredentialVault
from .runner import JobRunner
from .services import ServiceError, discover_projects
from .settings import Settings


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    database = Database(settings)
    vault = CredentialVault()
    runner = JobRunner(database.sessions, vault, settings)
    codex_adapter = build_codex_adapter(
        settings.codex_command,
        codex_bin=settings.codex_bin,
        timeout_seconds=settings.codex_timeout_seconds,
    )
    planning_adapter = codex_adapter
    if isinstance(codex_adapter, AppServerCodexAdapter):
        codex_adapter.isolated_home = settings.state_dir / "planning-codex" / "capabilities"
        planning_adapter = ProviderPlanningAdapter(codex_adapter, database.sessions, settings, vault)
    generation_planning_runner = (PlanningQueueClient if settings.planning_external_worker else GenerationPlanningRunner)(
        database.sessions,
        planning_adapter,
        settings,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        database.create_schema()
        with database.sessions() as session:
            discover_projects(session, settings.projects_root, scan=True)
        codex_start = getattr(codex_adapter, "start", None)
        if codex_start is not None:
            await codex_start()
        credential_server = await serve_credentials(settings.state_dir, vault) if settings.planning_external_worker else None
        await generation_planning_runner.start()
        await runner.start()
        try:
            yield
        finally:
            await runner.stop()
            await generation_planning_runner.stop()
            codex_close = getattr(codex_adapter, "close", None)
            if codex_close is not None:
                try:
                    await asyncio.wait_for(codex_close(), timeout=5)
                except TimeoutError:
                    pass
            if credential_server is not None:
                credential_server.close()
                await credential_server.wait_closed()
            vault.clear()

    app = FastAPI(
        title="Game Assets API",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.database = database
    app.state.vault = vault
    app.state.runner = runner
    app.state.codex_adapter = codex_adapter
    app.state.generation_planning_runner = generation_planning_runner
    # Short alias retained for integrations/tests that refer to the feature as
    # a planning runner rather than the full state attribute name.
    app.state.planning_runner = generation_planning_runner
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "Last-Event-ID"],
    )
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=["127.0.0.1", "localhost", "[::1]", "testserver"],
    )

    @app.middleware("http")
    async def security_headers(request: Request, call_next):  # type: ignore[no-untyped-def]
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: blob:; connect-src 'self' https: http:; font-src 'self'; "
            "object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(ServiceError)
    async def service_error(_request: Request, exc: ServiceError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"detail": str(exc)})

    app.include_router(router)
    if settings.frontend_dist and settings.frontend_dist.is_dir():
        app.mount("/", StaticFiles(directory=settings.frontend_dist, html=True), name="web")
    return app


app = create_app()
