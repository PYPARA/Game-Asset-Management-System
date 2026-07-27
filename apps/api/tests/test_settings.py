from pathlib import Path

from game_assets_api.settings import Settings


def test_project_and_application_state_roots_are_separate() -> None:
    settings = Settings()

    assert settings.projects_root == (
        Path.home()
        / "Library"
        / "Mobile Documents"
        / "com~apple~CloudDocs"
        / "Game-Projects"
    )
    assert settings.state_dir == (
        Path.home() / "Library" / "Application Support" / "Game-Asset-Management-System"
    )
    assert settings.resolved_database_url.endswith(
        "/Library/Application Support/Game-Asset-Management-System/index.sqlite3"
    )
