# NEXORA

One shared SACCO management application

A full-stack web application built with **React**, **Flask** and **PostgreSQL**.

## Tech stack

| Layer     | Technology                                                                 |
|-----------|----------------------------------------------------------------------------|
| Frontend  | React 19 + Vite                                                            |
| Backend   | Flask (Python), Flask-SQLAlchemy, Flask-Migrate, psycopg 3, pytest, Ruff   |
| Database  | PostgreSQL 18                                                              |

## Repository structure

```
NEXORA/
├── backend/     # Flask API (app.py, requirements.txt, .env.example)
├── frontend/    # React + Vite app
├── docs/        # API reference: openapi.yaml, api.md, test-matrix.md
├── .gitignore
└── README.md
```

API reference lives in [`docs/`](docs/): [`openapi.yaml`](docs/openapi.yaml) (the source), [`api.md`](docs/api.md) (generated from it) and [`test-matrix.md`](docs/test-matrix.md). Deployment steps are at the end of this file.

## Prerequisites

- Git
- Python 3.11+
- Node.js 20+ and npm
- PostgreSQL 18 (the `psql` command must be on your PATH)

## Local setup

Clean-clone check, 2026-11-18, WSL2 Ubuntu, Python 3.14: a fresh `git clone`, `python3 -m venv venv`, `pip install -r requirements.txt` and `.env` from `.env.example`, then `pytest -m "schema or not schema"` gave 778 passed, 1 skipped, and `ruff check backend/app tests` passed. The `CREATE DATABASE` steps were not re-run (the databases already existed and need a superuser).

### 1. Clone and create your branch

```bash
git clone git@github.com:SDF-FTH18M6-Capstones/NEXORA.git
cd NEXORA
```

Make sure your Git identity is set:

```bash
git config user.name "Your Name"
git config user.email "you@example.com"
```

### 2. Database

Create the development and test databases (all three are needed to run the tests):

```bash
psql -U postgres -c "CREATE DATABASE nexora_dev;"
psql -U postgres -c "CREATE DATABASE nexora_test;"
psql -U postgres -c "CREATE DATABASE nexora_schema_test;"
```

If your app uses a non-superuser role, add `OWNER <that_role>` to each statement. On Linux or WSL, `psql -U postgres` can fail with a peer-authentication error: use `psql -h 127.0.0.1 -U postgres` to log in with the password instead.

### 3. Backend

Requires **Python 3.12 or newer** (check with `python --version`).

```bash
cd backend
python3 -m venv venv
source venv/Scripts/activate      # Windows (Git Bash)
# source venv/bin/activate        # macOS / Linux
pip install -r requirements.txt
cp .env.example .env
```

Edit `backend/.env`: set `SECRET_KEY`, and replace `YOUR_PASSWORD` in `DATABASE_URL` and `TEST_DATABASE_URL` with your local PostgreSQL password. Keep the `postgresql+psycopg://` prefix (plain `postgresql://` makes SQLAlchemy look for the wrong driver, psycopg2).

```
DATABASE_URL=postgresql+psycopg://postgres:YOUR_PASSWORD@localhost:5432/nexora_dev
TEST_DATABASE_URL=postgresql+psycopg://postgres:YOUR_PASSWORD@localhost:5432/nexora_test
```

Create the tables and load demo data (run from the repo root, with the virtualenv active):

```bash
cd backend && flask --app app db upgrade && cd ..
python backend/scripts/seed.py
```

`seed.py` refuses to run unless the database name ends in `_dev`, and it is safe to run twice. It prints the seeded emails and roles; every seeded user shares one fake development password, defined at the top of the script.

Run the API:

```bash
cd backend
python app.py
```

Daily check of one SACCO's books (balances against the ledger), emailed to the members who can read its reports: `flask --app app eod-status --sacco-id 1` from `backend/`. It exits 1 if any balance disagrees with its ledger.

The API runs on <http://localhost:5000>. The API reference is `docs/api.md`.

### Running the tests

From the repo root, with the virtualenv active:

```bash
pytest -m "schema or not schema"   # everything: what CI runs
ruff check backend/app tests       # lint
```

A few tests in `tests/test_app_role_privileges.py` skip (and say why) unless the `nexora_app` database role was provisioned once by an administrator.

Plain `pytest` skips the schema tests (ledger, balances, reversals); `pytest -m schema` runs only those. The backend needs a database on `localhost`, so run the tests with `backend/.env` filled in.

The schema tests drop and rebuild their whole database, so they use their own
`<TEST_DATABASE_URL name minus _test>_schema_test` database (`nexora_schema_test`).
Create it once as shown in step 2.

### 4. Frontend

In a second terminal:

```bash
cd frontend
npm install
npm run dev
```

The app runs on <http://localhost:5173>.

## Smoke checks

| Check | Command / URL | Expected result |
|-------|---------------|-----------------|
| API + database | `curl -s http://localhost:5000/api/health` | `{"database":"connected","status":"healthy"}` |
| Missing config | Run the API without `.env` | `503` with `"database":"not_configured"` |
| Frontend | <http://localhost:5173> | Vite + React starter page loads |
| Lint | `cd frontend && npm run lint` | No errors |
| Build | `cd frontend && npm run build` | Build completes |

## Environment variables

Secrets are **never committed**. Only `.env.example` files are tracked; each developer copies them to a local `.env` (ignored by Git).

| Variable       | Where    | Description                          |
|----------------|----------|--------------------------------------|
| `SECRET_KEY`   | backend  | Token signing secret, 32+ random characters (the app refuses to start with less) |
| `DATABASE_URL` | backend  | PostgreSQL connection string         |
| `TEST_DATABASE_URL` | backend | Test database; name must end in `_test` |
| `TRUSTED_PROXIES` | backend | Number of reverse proxies in front (0 = none) |
| `RATELIMIT_STORAGE_URI` | backend | `memory://` locally, `redis://...` in production |
| `FLASK_DEBUG`  | backend  | `1` enables Flask debug mode         |
| `APP_ENV` | backend | `development` (default) or `production`. Production refuses to start with debug on, `memory://` rate limits, a short or placeholder `SECRET_KEY`, a `*` origin, or the `log` mail backend |
| `MAIL_BACKEND`, `MAIL_SERVER`, `MAIL_PORT`, `MAIL_FROM`, `MAIL_USERNAME`, `MAIL_PASSWORD` | backend | Verification email. `log` (default) prints the link in the server log; production needs `smtp` and `MAIL_SERVER` |
| `FRONTEND_URL` | backend | Where the verification link points (default `http://localhost:5173`) |
| `CORS_ORIGINS` | backend | Exact browser origins allowed to call the API, comma-separated (e.g. `http://localhost:5173`). Empty = no cross-origin access |

If your database password contains special characters (`@`, `#`, `/`, `:`), URL-encode them (for example `@` becomes `%40`).

## Git workflow

- Each developer works on their own `feature/<name>-...` branch.
- Run `git pull` before starting work and before pushing.
- Do not commit `.env`, `venv/` or `node_modules/`.
- Shared files (for example `App.jsx`, `vite.config.js`, `app.py`) are agreed with the team before changes.
- Tasks and blockers are tracked in GitHub Projects.

## Troubleshooting

| Problem | Fix |
|---------|-----|
| `psql: command not found` | Add `C:\Program Files\PostgreSQL\18\bin` to your PATH, then reopen the terminal |
| `password authentication failed` | The password in `.env` doesn't match your PostgreSQL `postgres` user password |
| Health check returns `disconnected` | Confirm PostgreSQL is running and `nexora_dev` exists |
| Tests hang, or fail with `canceling statement due to lock timeout` | Another connection has an open transaction on the test database (a killed earlier run, DBeaver, pgAdmin, `psql`). Find it: `SELECT pid, state, left(query,60) FROM pg_stat_activity WHERE datname='nexora_test';` then `SELECT pg_terminate_backend(<pid>);` for any `idle in transaction` row |
| `ImportError: cannot import name 'UTC'` | Python is older than 3.11. Install Python 3.12+ and recreate the virtualenv |
| `No module named 'psycopg2'` | `DATABASE_URL` / `TEST_DATABASE_URL` must start with `postgresql+psycopg://` |
| `Could not create database 'nexora_schema_test'` | Create it once: `psql -U postgres -c "CREATE DATABASE nexora_schema_test OWNER <your_role>;"` |
| `npm error Missing script: "lint"` | You're in the wrong folder. Run it from `frontend/` |

## Status

- [x] Move 1: project skeleton, schema, migrations
- [x] Move 2: auth (register, login, rotating refresh tokens, lockout), SACCOs, members, tenant guard
- [x] Move 3: savings accounts, deposits, withdrawals, reversals, ledger, audit log, admin reports
- [x] Move 4: security headers, CORS and rate limits, production checks, seed script, savings and audit read endpoints, change-password and logout-all
- [x] Move 4 hardening: one error format, access-token revocation, email verification, forgot-password, required `reference`, separation of duties
- [x] OpenAPI spec for every route (`docs/openapi.yaml`), readable reference (`docs/api.md`), route-to-test matrix (`docs/test-matrix.md`)
- [x] Coverage floor in CI (`fail_under` in `pyproject.toml`)
- [ ] Move 4 remainder: final sign-off
- [x] Loans: request, one or two approvals, disbursement, repayment

Release 1 loans are interest-free. Interest is planned for release 2.
- [ ] Frontend connected to backend

## Deployment: Render, Vercel and production M-Pesa

Read this before deploying or setting `DARAJA_ENV=production`. Every box is a thing to check on the real server, not in the code.

### Legal sign-off

NEXORA holds no funds and is not paid through STK push. Each SACCO prompts its own members into its own paybill or till, using its own Daraja credentials; an officer's confirmation credits a deposit. A lawyer has given the go-ahead for production use.

| Item | Value |
|---|---|
| Date of the written advice | **[TO BE FILLED BY COLLINS: date]** |
| One-line summary of the advice | **[TO BE FILLED BY COLLINS: one line, from the written advice]** |
| Where the written advice is kept | **[TO BE FILLED BY COLLINS: location]** |

Do not go live until the three rows above are filled in from the written advice.

### 0. Hosting: Render (API) and Vercel (frontend)

Files: `render.yaml` (API, Postgres, Key Value), `backend/gunicorn.conf.py` (2 workers, 30 s timeout), `frontend/vercel.json` (single-page rewrite). No secret is in any of them.

**Render**

- [ ] New > Blueprint, point it at the repo; it reads `render.yaml`. Fill each variable marked `sync: false` in the dashboard: `CORS_ORIGINS` (the Vercel origin, no trailing slash), `FRONTEND_URL`, `MAIL_FROM`, `MAIL_SERVER`, `MAIL_USERNAME`, `MAIL_PASSWORD`, and the `DARAJA_*` values (see sections 1 to 4; leave `DARAJA_ENABLED=0` until they are done). Leave `DARAJA_CONSUMER_KEY`, `DARAJA_CONSUMER_SECRET`, `DARAJA_PASSKEY` and `DARAJA_SHORTCODE` empty in production.
- [ ] `SECRET_KEY` is generated by Render. `DATABASE_URL` and `RATELIMIT_STORAGE_URI` come from the database and Key Value store in the blueprint.
- [ ] `DATABASE_URL`: Render supplies `postgresql://...`. The app rewrites `postgres://` and `postgresql://` to `postgresql+psycopg://` (`config.database_url`). If you set it by hand, either scheme works.
- [ ] `TRUSTED_PROXIES=1` (Render is one proxy). Check: five wrong logins from one address lock that address, not everyone; the access log shows real client IPs.
- [ ] Start command `gunicorn "app:create_app()"` from `backend/` (`app` is a package, so there is no `app:app`). Health check path `/health` (200 `{"status":"ok"}`; 503 `{"status":"unavailable"}` with no detail). `/api/health` stays for manual checks.
- [ ] Migrations run in `preDeployCommand` (`flask --app app db upgrade`), which needs a paid plan. On a free plan run it from the Render shell before the first start. Head must be `0041`.
- [ ] `PYTHON_VERSION` in `render.yaml` is `3.12.8`, the same minor version as CI. `requirements.txt` installs on Python 3.12 and the suite was run on it; 3.13 and 3.14 were not needed.
- [ ] After the first deploy: one failed login, then the Key Value store holds a `LIMITS:*` key; the logs do not say "Rate-limit storage ... is unreachable".

**Vercel**

- [ ] Import the repo with root directory `frontend`. `vercel.json` sets the Vite build and rewrites every path to `index.html`, so a refresh on `/requests` works.
- [ ] Set `VITE_API_URL` (Production) to `https://<render-host>/api/v1`; it is read at build time, so redeploy after changing it. Do not set `VITE_MOCK`.
- [ ] Add the Vercel URL to the API's `CORS_ORIGINS` and `FRONTEND_URL`, then redeploy the API.

### 1. Environment settings

Set these on the server (never in git; `backend/.env` stays untracked).

| Setting | Production value | Why |
|---|---|---|
| `APP_ENV` | `production` | Turns on the production start-up checks. |
| `DARAJA_ENV` | `production` | Uses `https://api.safaricom.co.ke`. The default, `sandbox`, uses `https://sandbox.safaricom.co.ke`. Any other value stops the app at start-up. |
| `DARAJA_ENABLED` | `1` | Daraja stays off unless this is set. |
| `DARAJA_ENCRYPTION_KEY` | a Fernet key | Encrypts each SACCO's saved credentials. Required: the app refuses to start without it. |
| `DARAJA_CALLBACK_URL` | `https://<host>/api/v1/daraja/callback/<DARAJA_CALLBACK_TOKEN>` | Must be `https` and end with the token. |
| `DARAJA_CALLBACK_TOKEN` | 32 or more random letters, digits, `-` or `_` | The only proof a callback came from Safaricom. |
| `DARAJA_CONSUMER_KEY`, `DARAJA_CONSUMER_SECRET`, `DARAJA_PASSKEY`, `DARAJA_SHORTCODE` | leave **unset** | These are shared sandbox keys. In production they are never used: a push needs that SACCO's own saved credentials. |

The app refuses to start (`RuntimeError` naming the setting) if `APP_ENV=production` and `DARAJA_ENABLED=1` but `DARAJA_ENV` is not `production`, the callback URL is not `https` or does not end with the token, the token is weak, or the encryption key is missing or not a valid Fernet key.

Generate the key once:

```
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Generate the callback token:

```
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

- [ ] All settings above are in the server's environment, not in a file in the repo.
- [ ] `DARAJA_CONSUMER_KEY`, `DARAJA_CONSUMER_SECRET`, `DARAJA_PASSKEY`, `DARAJA_SHORTCODE` are unset.
- [ ] `git status` on the server's checkout shows no `.env`.

### 2. HTTPS and the callback URL

- [ ] The site is served over HTTPS with a valid certificate (Safaricom will not post to plain `http`).
- [ ] `DARAJA_CALLBACK_URL` is reachable from the public internet and ends with `/api/v1/daraja/callback/<token>`.
- [ ] A `POST` to the callback URL with a wrong token answers 404 (the token is the secret). Do not log or share the URL.
- [ ] The reverse proxy passes the request body through unchanged and does not rewrite the path.

### 3. Each SACCO before it takes live prompts

- [ ] The SACCO's officers have approved its pay-in details (two officers: one proposes, another approves, and the approval takes effect at once).
- [ ] The SACCO admin saved its **own** Daraja shortcode, consumer key, consumer secret and passkey (`PUT /saccos/{id}/daraja-settings`). In production there is no fallback to anything else.
- [ ] For a **paybill**, the saved shortcode equals the approved paybill (a mismatch answers 409 and no prompt is sent).
- [ ] For a **till**, the saved shortcode is the till's head-office number and the approved number is the till. Prompts go to the approved till.
- [ ] Pochi la Biashara SACCOs stay record only: no prompt can be sent.
- [ ] Deposit controls reviewed (`GET /saccos/{id}/deposit-controls`): auto-confirm is off unless the SACCO chose a limit; the large-amount, manual-entry, repeat and quick-withdrawal thresholds suit the SACCO.

### 4. Key backup

`DARAJA_ENCRYPTION_KEY` is the only way to read the saved credentials.

- [ ] The key is stored in at least two places that are not the server and not git (for example a password manager and an offline copy).
- [ ] Whoever holds the backup is written down.
- [ ] Everyone knows the cost of losing it: every SACCO's saved credentials become unreadable. Prompts for that SACCO answer 409 until its admin saves the credentials again. No money is lost and no ledger row changes.
- [ ] Rotating the key means each SACCO admin saves its credentials again. Do not replace the key without telling them first.

### 5. Before the first real payment

- [ ] `alembic upgrade head` ran: one head, `0041`.
- [ ] Take a database backup.
- [ ] Do a small live payment with an officer's own number into a SACCO that agreed to it. Confirm the callback created a pending deposit, an officer confirmed it, the ledger row says `stk`, and `GET /saccos/{id}/reconciliation` lists no mismatch.
- [ ] Check `GET /saccos/{id}/flagged-payments` is readable by the secretary.

### 6. Rollback

Rolling back is safe at any point because NEXORA holds no funds and the ledger is append-only.

1. **Stop new prompts at once:** set `DARAJA_ENABLED=0` and restart. The STK routes and the callback then answer 404. Members can still pay the paybill or till directly; officers record those with a manual deposit (M-Pesa receipt, amount, payer phone).
2. **Pending deposits stay.** Paid pushes already recorded stay in the request queue for officers to confirm. A callback that arrives while Daraja is off is lost to NEXORA: officers record it as a manual deposit from the M-Pesa message, and the reconciliation report shows what M-Pesa took.
3. **Back to sandbox:** set `DARAJA_ENV=sandbox` and, if the app runs with `APP_ENV=production`, also `DARAJA_ENABLED=0` (production refuses sandbox Daraja at start-up).
4. **Code rollback:** redeploy the previous release. Do not run `alembic downgrade` to undo `0038` on a database that has taken live payments: the downgrade drops the reversal requests and the deposit-control settings. Restore from the backup instead if you must.
5. A mistaken ledger entry is never edited or deleted. Reverse it: one officer asks, a different officer approves.

### 7. Where to look when something is wrong

| Symptom | Look at |
|---|---|
| App will not start | The `RuntimeError` message names the setting. |
| A member says they paid but no deposit shows | `GET /saccos/{id}/reconciliation` (`paid_not_recorded`, `paid_not_credited`), then the request queue. |
| A prompt answers 409 | The message says why: no saved credentials, the paybill does not match the approved one, no approved pay-in details, or credentials unreadable (key lost or changed). |
| An entry looks wrong | Ask for a reversal; do not edit. |
