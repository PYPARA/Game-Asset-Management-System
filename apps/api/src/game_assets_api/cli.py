from __future__ import annotations

import uvicorn

from .settings import Settings


def main() -> None:
    settings = Settings()
    uvicorn.run(
        "game_assets_api.main:app",
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level,
        reload=False,
        access_log=True,
    )


if __name__ == "__main__":
    main()
