"""Every SACCO-scoped endpoint decides access with can(), never by role name."""

import re
from pathlib import Path

APP = Path(__file__).resolve().parent.parent / "backend" / "app"

# Where role names may appear: the catalogue itself, the models, and parsing the
# API's `role` field. Nowhere else may a route or service branch on a role.
ALLOWED = {
    "security/permissions.py",
    "members/schemas.py",
    "members/self_service.py",  # shows the role in the profile, decides nothing
}
# Naming a role value, or testing a role against a set of roles. (Comparing a
# role with its own earlier value, to see whether it changed, decides nothing.)
ROLE_CHECK = re.compile(
    r"MembershipRole\.(SACCO_ADMIN|STAFF|MEMBER)|\.role\s+(not\s+)?in\s"
)


def test_no_route_or_service_decides_by_role_name():
    offenders = []
    for path in APP.rglob("*.py"):
        relative = path.relative_to(APP).as_posix()
        if relative.startswith("models/") or relative in ALLOWED:
            continue
        for number, line in enumerate(path.read_text().splitlines(), 1):
            if ROLE_CHECK.search(line):
                offenders.append(f"{relative}:{number}: {line.strip()}")
    assert offenders == [], "\n".join(offenders)
