"""The rules screens must say which rules NEXORA stores but does not apply.

There is no frontend test runner, so this reads the component source: it fails
if interest, the late fine or guarantors lose their "recorded, not enforced"
label, or if a rule that is enforced gets one.
"""

import re
from pathlib import Path

COMPONENTS = Path(__file__).resolve().parents[1] / "frontend" / "src" / "components"
NOT_ENFORCED = ("Interest or service charge", "Late-payment fine", "Guarantors")
ENFORCED = ("Most you can borrow", "Repay within", "Wait before your first loan")


def _source(name):
    return (COMPONENTS / name).read_text(encoding="utf-8")


def test_the_label_text_says_recorded_not_enforced():
    assert "recorded, not enforced" in _source("NotEnforced.jsx")


def test_the_view_and_the_form_label_every_unenforced_rule():
    for name in ("RulesView.jsx", "RulesForm.jsx"):
        source = _source(name)
        for label in NOT_ENFORCED:
            pattern = rf"(label=\{{<>|<Row label=\{{<>){label}[^<]*<NotEnforced />"
            assert re.search(pattern, source), f"{label} unlabelled in {name}"


def test_enforced_rules_are_not_labelled():
    source = _source("RulesView.jsx")
    for label in ENFORCED:
        assert f'<Row label="{label}">' in source
