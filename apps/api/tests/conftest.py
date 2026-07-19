from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from game_assets_api.main import create_app
from game_assets_api.settings import Settings


@pytest.fixture
def project_root(tmp_path: Path) -> Path:
    root = tmp_path / "sample-game"
    root.mkdir()
    return root


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    settings = Settings(
        data_dir=tmp_path / "api-data",
        database_url=f"sqlite:///{tmp_path / 'test.sqlite3'}",
        frontend_dist=None,
        job_poll_interval=0.01,
    )
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def create_project(client: TestClient, root: Path) -> dict:
    response = client.post("/api/projects", json={"root_path": str(root), "name": "Sample"})
    assert response.status_code == 201, response.text
    return response.json()


def create_asset(
    client: TestClient,
    project_id: str,
    *,
    key: str = "character.hero",
    kind: str = "entity",
    subtype: str = "character",
    title: str = "主角",
) -> dict:
    response = client.post(
        "/api/assets",
        json={
            "project_id": project_id,
            "key": key,
            "kind": kind,
            "subtype": subtype,
            "title": title,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def create_fake_provider(client: TestClient) -> dict:
    response = client.post(
        "/api/providers",
        json={
            "name": "Deterministic fake",
            "kind": "fake",
            "base_url": "https://fake.invalid/v1",
            "text_model": "fake-text",
            "image_model": "fake-image",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()

