"""CI must lint and build the frontend, and the coverage floor must not drop."""

import tomllib
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def _steps(job):
    return [step.get("run", step.get("uses", "")) for step in job["steps"]]


def test_ci_has_a_frontend_job_that_installs_lints_and_builds():
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    job = workflow["jobs"]["frontend"]
    assert job["defaults"]["run"]["working-directory"] == "frontend"
    steps = _steps(job)
    for command in ("npm ci", "npm run lint", "npm run build"):
        assert command in steps
    assert (
        steps.index("npm ci")
        < steps.index("npm run lint")
        < steps.index("npm run build")
    )
    assert (ROOT / "frontend/package-lock.json").is_file()  # npm ci needs it


def test_the_scripts_ci_runs_exist_in_package_json():
    import json

    scripts = json.loads((ROOT / "frontend/package.json").read_text())["scripts"]
    assert {"lint", "build"} <= set(scripts)


def test_the_coverage_floor_is_not_lowered():
    config = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert config["tool"]["coverage"]["report"]["fail_under"] >= 94
