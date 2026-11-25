#!/bin/sh
# Container entrypoint.
#   RUN_MIGRATIONS=1  apply `flask db upgrade` before the server starts (ONE instance only).
#   SEED_DEMO=1       load the demo accounts once (needs DEMO_PASSWORD, DEMO_PIN and
#                     DEMO_CONFIRM_DB = the exact database name). Remove SEED_DEMO afterwards.
set -e

if [ "${RUN_MIGRATIONS:-0}" = "1" ]; then
  echo "Running database migrations..."
  flask --app app db upgrade
fi

if [ "${SEED_DEMO:-0}" = "1" ]; then
  echo "Seeding demo accounts..."
  python scripts/seed_demo_remote.py --confirm "${DEMO_CONFIRM_DB:-}" \
    || echo "Demo seeding failed; the server will still start."
fi

exec gunicorn "app:create_app()" \
  --bind "0.0.0.0:${PORT:-10000}" \
  --workers "${WEB_CONCURRENCY:-2}" \
  --worker-class gthread --threads 4 \
  --timeout 60 \
  --access-logfile - \
  --error-logfile -