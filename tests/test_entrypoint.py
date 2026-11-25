"""`python app.py` (the documented way to start the API) reads backend/.env."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent / "backend"


@pytest.mark.skipif(
    not (BACKEND / ".env").is_file(), reason="needs a backend/.env (none in CI)"
)
def test_app_py_starts_from_backend_env_alone():
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in ("SECRET_KEY", "DATABASE_URL", "TEST_DATABASE_URL")
    }
    # Build the app the way `python app.py` does, without starting the server.
    result = subprocess.run(
        [sys.executable, "-c", "import runpy; runpy.run_path('app.py')"],
        cwd=BACKEND,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr[-800:]
