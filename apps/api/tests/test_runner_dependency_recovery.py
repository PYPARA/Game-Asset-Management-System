from __future__ import annotations

from sqlalchemy import select

from game_assets_api.database import Database
from game_assets_api.models import Asset, AssetRevision, GenerationJob, GenerationPlan, Project, ProviderProfile, RunEvent
from game_assets_api.runner import JobRunner
from game_assets_api.settings import Settings


def test_untouched_dependent_requeues_when_all_upstream_outputs_recover(tmp_path) -> None:
    settings = Settings(
        projects_root=tmp_path / "projects",
        state_dir=tmp_path / "state",
        database_url=f"sqlite:///{tmp_path / 'test.sqlite3'}",
        frontend_dist=None,
    )
    database = Database(settings)
    database.create_schema()
    with database.sessions() as session:
        project = Project(name="Test", root_path=str(tmp_path / "projects"))
        provider = ProviderProfile(name="Fake", kind="fake", base_url="https://fake.invalid", text_model="text", image_model="image")
        session.add_all([project, provider])
        session.flush()
        asset = Asset(project_id=project.id, key="portrait.test", kind="media", subtype="portrait", title="Test")
        plan = GenerationPlan(project_id=project.id, provider_profile_id=provider.id, name="Test", status="awaiting_user", tasks_json=[], estimated_calls=2)
        session.add_all([asset, plan])
        session.flush()
        upstream = GenerationJob(plan_id=plan.id, project_id=project.id, provider_profile_id=provider.id, task_id="profile", task_kind="text", request_json={}, status="awaiting_user")
        downstream = GenerationJob(
            plan_id=plan.id, project_id=project.id, provider_profile_id=provider.id,
            task_id="portrait", task_kind="image",
            request_json={"asset_id": asset.id, "depends_on": ["profile"]},
            status="awaiting_user", error_category="validation",
            error_message="dependency profile requires attention",
        )
        session.add_all([upstream, downstream])
        session.commit()
        upstream_id, downstream_id = upstream.id, downstream.id

    runner = JobRunner(database.sessions, None, settings)  # type: ignore[arg-type]
    with database.sessions() as session:
        runner._recover_blocked_dependents(session)
        assert session.get(GenerationJob, downstream_id).status == "awaiting_user"
        upstream = session.get(GenerationJob, upstream_id)
        revision = AssetRevision(
            asset_id=asset.id, sequence=1, format="json", content_json={"name": "Test"},
            content_hash="ready", input_hash="input", file_path="production/test.json",
        )
        session.add(revision)
        session.flush()
        upstream.status = "candidate_ready"
        upstream.result_revision_id = revision.id
        session.commit()

    with database.sessions() as session:
        runner._recover_blocked_dependents(session)
        downstream = session.get(GenerationJob, downstream_id)
        assert downstream.status == "queued"
        assert downstream.error_message is None
        assert downstream.attempt_count == 0
        assert session.scalar(select(RunEvent).where(RunEvent.job_id == downstream_id, RunEvent.event_type == "run.resumed")) is not None
        session.commit()

    with database.sessions() as session:
        runner._recover_blocked_dependents(session)
        assert len(session.scalars(select(RunEvent).where(RunEvent.job_id == downstream_id, RunEvent.event_type == "run.resumed")).all()) == 1
    database.engine.dispose()
