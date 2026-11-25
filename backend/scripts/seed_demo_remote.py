"""One-time: load the demo accounts into a hosted (non-_dev) database.

seed.py refuses any database whose name does not end in '_dev', because it
creates users with a known password. This wrapper lets you seed the hosted demo
database on purpose, but with a password and PIN YOU choose, never the published
defaults, and only after you type the exact database name to confirm.

Run from the backend/ folder, with the venv active:

    DATABASE_URL='<External Database URL from Render>' \\
    DEMO_PASSWORD='<a strong password you invent>' \\
    DEMO_PIN='<4 to 6 digit PIN you invent>' \\
    python scripts/seed_demo_remote.py --confirm <database-name>

Safe to run twice: existing users and SACCOs are left alone (their passwords
are NOT changed on a second run).
"""

import argparse
import os
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--confirm", required=True, help="exact database name")
    args = parser.parse_args()

    for key in ("DATABASE_URL", "DEMO_PASSWORD", "DEMO_PIN"):
        if not os.environ.get(key):
            raise SystemExit(f"Set {key} in the environment first (see docstring).")
    password, pin = os.environ["DEMO_PASSWORD"], os.environ["DEMO_PIN"]
    if password == "Dev-Seed-Pass-2026" or pin == "2468":
        raise SystemExit("Do not use the published seed password or PIN.")

    import seed as seed_mod  # backend/scripts/seed.py
    from sqlalchemy.engine import make_url

    from app import create_app

    app = create_app()
    url = make_url(app.config["SQLALCHEMY_DATABASE_URI"])
    if url.database != args.confirm:
        raise SystemExit(
            f"Refusing: the database is {url.database!r} on {url.host!r}, "
            f"but you confirmed {args.confirm!r}."
        )

    seed_mod.DEV_PASSWORD = password
    seed_mod.DEV_PIN = pin
    seeded = seed_mod.seed(app, require_dev=False)

    print(f"Seeded {url.database} on {url.host}.")
    print("Every demo user has the DEMO_PASSWORD and DEMO_PIN you supplied.\n")
    for row in seeded:
        where = row["sacco"] or "-"
        print(f"  {row['email']:<28} {row['role']:<15} {where}")


if __name__ == "__main__":
    main()