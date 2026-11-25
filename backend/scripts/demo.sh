#!/usr/bin/env bash
# Walks through the API against a seeded development database and prints what
# each step shows. Start the API first (see docs/demo.md), then run:
#   bash backend/scripts/demo.sh            # API at http://localhost:5000
#   API=http://localhost:5077 bash backend/scripts/demo.sh
set -u
API="${API:-http://localhost:5000}"
V1="$API/api/v1"
PW="Dev-Seed-Pass-2026"   # the seed's development password
J='Content-Type: application/json'
STAMP=$(date +%s)

step() { printf '\n\033[1m== %s\033[0m\n' "$1"; }
show() { python3 -c 'import sys,json
raw=sys.stdin.read()
try: print(json.dumps(json.loads(raw), indent=2)[:int(sys.argv[1])])
except Exception: print(raw[:300])' "${1:-700}"; }
call() { # call METHOD PATH [TOKEN] [BODY] -> prints "HTTP <code>" then the body
  local method=$1 path=$2 token=${3:-} body=${4:-}
  local args=(-s -w '\n%{http_code}' -X "$method" "$V1$path" -H "$J")
  [ -n "$token" ] && args+=(-H "Authorization: Bearer $token")
  [ -n "$body" ] && args+=(-d "$body")
  local out; out=$(curl "${args[@]}")
  echo "HTTP ${out##*$'\n'}"; printf '%s' "${out%$'\n'*}" | show "${5:-700}"
}
token() { curl -s -X POST "$V1/auth/login" -H "$J" -d "{\"email\":\"$1\",\"password\":\"$PW\"}" |
  python3 -c 'import sys,json; print(json.load(sys.stdin).get("access_token",""))'; }

step "Health (database reachable)"
curl -s "$API/api/health" | show

OWNER=$(token owner@example.com); ADMIN=$(token alpha-admin@example.com)
STAFF=$(token alpha-finance@example.com); MEMBER=$(token alpha-m1@example.com)
[ -n "$OWNER" ] && [ -n "$ADMIN" ] && [ -n "$STAFF" ] && [ -n "$MEMBER" ] ||
  { echo "Login failed: is the database seeded? (python backend/scripts/seed.py)"; exit 1; }
echo "Logged in as: platform owner, SACCO admin, finance officer, member (4 tokens)"

step "ADMIN DASHBOARD (Alpha SACCO admin)"
echo "-- summary"; call GET /admin/saccos/1/reports/summary "$ADMIN"
echo "-- accounts"; call GET "/admin/saccos/1/accounts?per_page=3" "$ADMIN" "" 600
echo "-- audit log, read by the platform owner (newest first)"; call GET "/admin/saccos/1/audit?per_page=3" "$OWNER" "" 600
echo "-- the SACCO admin cannot read the audit log: 403"; call GET /admin/saccos/1/audit "$ADMIN" "" 300

step "MEMBER DASHBOARD (alpha-m1)"
echo "-- my accounts"; call GET /savings/accounts/me "$MEMBER"
echo "-- history"; call GET "/savings/accounts/1/transactions?per_page=3" "$MEMBER" "" 600

step "MONEY: deposit with an idempotency reference, then the same request again"
REF="demo-$STAMP"
echo "-- first call: 201, new entry"; call POST /savings/accounts/1/deposits "$STAFF" "{\"amount\":2500,\"reference\":\"$REF\"}" 400
echo "-- retry, same reference: 200, replayed=true, no second deposit"
call POST /savings/accounts/1/deposits "$STAFF" "{\"amount\":2500,\"reference\":\"$REF\"}" 400
echo "-- withdrawal larger than the balance: 409"
call POST /savings/accounts/1/withdrawals "$STAFF" "{\"amount\":99000000,\"reference\":\"big-$STAMP\"}" 300
echo "-- no reference: 400 (a retry could not be made safe)"
call POST /savings/accounts/1/deposits "$STAFF" '{"amount":100}' 300

step "SECURITY: roles and tenant isolation"
echo "-- member posting a deposit: 403"; call POST /savings/accounts/1/deposits "$MEMBER" "{\"amount\":1,\"reference\":\"x-$STAMP\"}" 300
echo "-- member opening the audit log: 403"; call GET /admin/saccos/1/audit "$MEMBER" "" 300
echo "-- Alpha admin reading Beta SACCO: 404 (never 403, ids cannot be probed)"
call GET /admin/saccos/2/reports/summary "$ADMIN" "" 300
echo "-- no token: 401, same error shape everywhere"; call GET /members/me "" "" 300

step "SESSIONS: logout-all kills every token at once"
TEMP=$(token alpha-m2@example.com)
echo "-- before"; call GET /members/me "$TEMP" "" 120
call POST /auth/logout-all "$TEMP" "" 200 >/dev/null
echo "-- after logout-all, the same access token: 401"; call GET /members/me "$TEMP" "" 300

step "REGISTER and email verification"
EMAIL="demo-$STAMP@example.com"
echo "-- register (a verification link is emailed; locally it prints in the API log)"
call POST /auth/register "" "{\"email\":\"$EMAIL\",\"password\":\"Demo-Pass-$STAMP\"}" 300
echo "-- forgot-password always answers the same, known address or not"
call POST /auth/forgot-password "" "{\"email\":\"$EMAIL\"}" 300
call POST /auth/forgot-password "" '{"email":"nobody@example.com"}' 300
echo "-- unverified users cannot be added to a SACCO: 409"
call POST /saccos/1/members "$ADMIN" "{\"email\":\"$EMAIL\"}" 300

step "Done. Interactive API reference: docs/openapi.yaml (paste into https://editor.swagger.io)"
