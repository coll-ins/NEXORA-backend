"""docs/openapi.yaml lists every route (and nothing else)."""

import re
from pathlib import Path

SPEC = Path(__file__).resolve().parent.parent / "docs" / "openapi.yaml"


def test_the_spec_lists_every_route_and_nothing_else(app):
    in_spec = set(re.findall(r"^  (/api\S*):$", SPEC.read_text(), flags=re.M))
    in_code = {
        re.sub(r"<(?:int:)?(\w+)>", r"{\1}", rule.rule)
        for rule in app.url_map.iter_rules()
        if rule.rule.startswith("/api")
    }
    assert in_spec == in_code


def test_api_md_is_regenerated_from_the_spec():
    import subprocess
    import sys

    script = SPEC.parent.parent / "backend" / "scripts" / "gen_api_docs.py"
    result = subprocess.run(
        [sys.executable, str(script), "--check"], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stdout


def test_the_test_matrix_has_a_row_for_every_route(app):
    matrix = (SPEC.parent / "test-matrix.md").read_text()
    rows = set(re.findall(r"^\| (\w+) \| `(/api\S*)` \|", matrix, flags=re.M))
    in_code = {
        (method, rule.rule)
        for rule in app.url_map.iter_rules()
        if rule.rule.startswith("/api")
        for method in rule.methods - {"HEAD", "OPTIONS"}
    }
    assert rows == in_code, "run route_matrix.py (see docs/test-matrix.md)"
