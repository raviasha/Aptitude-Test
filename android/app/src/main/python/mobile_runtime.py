"""Start the existing Aptitude Lab FastAPI engine inside the Android process."""

from __future__ import annotations

import os


def start_server(data_dir: str, static_dir: str, app_secret: str, drive_folder_url: str) -> None:
    os.environ.update(
        {
            "APTITUDE_MOBILE_MODE": "1",
            "APTITUDE_DATA_DIR": data_dir,
            "APTITUDE_STATIC_DIR": static_dir,
            "APTITUDE_MOBILE_APP_SECRET": app_secret,
            "APTITUDE_DRIVE_FOLDER_URL": drive_folder_url,
            "SESSION_SECRET": app_secret,
        }
    )

    import app as core
    import mobile_api  # noqa: F401 - importing attaches the mobile routes.
    import uvicorn

    uvicorn.run(
        core.app,
        host="127.0.0.1",
        port=8000,
        log_level="warning",
        access_log=False,
        use_colors=False,
    )
