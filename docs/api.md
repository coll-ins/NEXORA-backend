# NEXORA API reference

Generated from [`openapi.yaml`](openapi.yaml) by `python backend/scripts/gen_api_docs.py`. Do not edit by hand: change the spec and regenerate. A test fails when this file is out of date.

Examples show field names and types built from the schemas, not real data.

Backend API for multi-tenant SACCO management. Every resource route is served under /api/v1; the health check is unversioned.

**Errors** on every route: `{"error": {"code": "not_found", "message": "Not found."}}`. Codes: 400 validation_error, 401 unauthorized, 403 forbidden, 404 not_found, 405 method_not_allowed, 409 conflict, 413 payload_too_large, 423 locked, 429 rate_limited, 500 internal_error.

**Tenancy:** another SACCO's records answer 404, never 403. A suspended SACCO answers 423.

**Money** is an integer in minor units (KES: 100 = 1.00). Money routes need a `reference` (idempotency key).

**Pagination:** `page` from 1 and `per_page` (default 20, capped at 100); a bad value is 400.

**Auth:** `Authorization: Bearer <access_token>`. Access tokens last 15 minutes and stop working at once after change-password, reset-password, logout-all or an email change.

## GET /api/health

**Health check (unversioned)**

**Who:** anyone.

**Rate limit:** 120 per minute. Each call opens a database connection.

Response **200**: Database reachable.

```json
{
  "status": "healthy",
  "database": "connected"
}
```

Error statuses: 503, 429

## POST /api/v1/auth/register

**Register an account**

**Who:** anyone

**Rate limit:** 5 per minute (per IP).

The password must be 8 to 128 characters and must not contain the email. The address starts unverified; login does not need it, but a SACCO admin can only add a verified user.

Request body:

```json
{
  "email": "member@example.com",
  "password": "a-long-passphrase-1"
}
```

Response **201**: Created. A verification link is emailed.

```json
{
  "id": 1,
  "email": "member@example.com"
}
```

Error statuses: 400, 409, 429

## POST /api/v1/auth/join

**Join a SACCO with a code**

**Who:** a signed-in user holding a join code.

The code is six digits, valid for the SACCO's `join_code_ttl_minutes` (15 by default) and usable once. Five wrong entries burn it. Only a membership that is currently `invited` can be joined; a cancelled invite answers like a wrong code. A phone may be invited to several SACCOs: the code decides which one is joined, and a wrong code counts against every live code for that phone. On success the phone is linked to the user and a setup token (valid 10 minutes, tied to this code) is returned for setting the SACCO PIN; a re-invite makes older setup tokens invalid.

Request body:

```json
{
  "phone": "+254700000000",
  "code": "code"
}
```

Response **200**: Phone linked. Send the setup token to the PIN route.

```json
{
  "setup_token": "setup_token",
  "membership_id": 1
}
```

Error statuses: 400, 401, 409, 410, 429

## POST /api/v1/auth/join/pin

**Set the SACCO PIN and activate the membership**

**Who:** the signed-in user who just joined.

Sets a four-digit PIN (stored hashed). The membership changes from invited to active.

Request body:

```json
{
  "setup_token": "setup_token",
  "pin": "pin"
}
```

Response **200**: PIN set, membership active.

```json
{
  "membership_id": 1,
  "status": "active"
}
```

Error statuses: 400, 401, 403, 409, 429

## POST /api/v1/auth/join/rules

**The SACCO's rules, before the PIN step**

**Who:** the signed-in user who just joined (send the `setup_token` from `/auth/join`).

Returns every rule of the SACCO being joined, so the member can read them before accepting.

Request body:

```json
{
  "setup_token": "setup_token"
}
```

Response **200**: The current rules and whether this member accepted them (`rules` is null if the SACCO has none), plus the SACCO whose invitation is being read.

```json
{
  "rules": "rules",
  "sacco": {
    "id": 1,
    "name": "name"
  }
}
```

Error statuses: 401, 403

## POST /api/v1/auth/join/rules/accept

**Accept the SACCO's rules before joining**

**Who:** the signed-in user who just joined.

Records that this member accepted this version. `/auth/join/pin` answers 409 `RULES_NOT_ACCEPTED` until it is done.

Request body:

```json
{
  "setup_token": "setup_token",
  "version": 1
}
```

Response **200**: Accepted.

```json
{
  "rules": "rules"
}
```

Error statuses: 401, 403, 404, 409

## POST /api/v1/auth/join/rules/decline

**Decline the SACCO's rules and refuse the invitation**

**Who:** the signed-in user who just joined, after reading the rules.
`reason` is required and is kept for the officers of the SACCO, who see it on the member (`decline_reason`). The membership becomes `declined` and is never activated: its join code is spent, so declining ends the invitation, and an officer must invite the person again to restart. Audited as `member.decline`. **Rate limit:** 30 per minute.

Request body:

```json
{
  "setup_token": "setup_token",
  "version": 1,
  "reason": "reason"
}
```

Response **200**: `{membership_id, status: declined}`.

Error statuses: 400, 401, 403, 409

## POST /api/v1/auth/sacco-login

**Check the SACCO PIN**

**Who:** a signed-in user with an active membership in the SACCO.

Five wrong PINs lock the PIN for 15 minutes (PIN_LOCKED).

Request body:

```json
{
  "sacco_id": 1,
  "pin": "pin"
}
```

Response **200**: PIN correct.

```json
{
  "ok": true,
  "membership_id": 1,
  "role": "role"
}
```

Error statuses: 401, 403, 423, 429

## POST /api/v1/auth/login

**Sign in**

**Who:** anyone

**Rate limit:** 5 per minute (per IP).

Five failed logins lock the account for 15 minutes (423). A suspended SACCO does not block login: the user sees it in GET /members/me/saccos, and the SACCO's own endpoints answer 423.

Request body:

```json
{
  "email": "member@example.com",
  "password": "a-long-passphrase-1"
}
```

Response **200**: Token pair plus who signed in. Access token lasts 15 minutes, refresh token 7 days.

```json
{
  "access_token": "access_token",
  "refresh_token": "refresh_token",
  "token_type": "Bearer",
  "user": "user"
}
```

Error statuses: 401, 423, 429

## POST /api/v1/auth/refresh

**Rotate a refresh token**

**Who:** anyone holding a refresh token

**Rate limit:** 30 per minute.

Each refresh token works once. Replaying a spent token more than 10 seconds later revokes all of the user's refresh tokens.

Request body:

```json
{
  "refresh_token": "refresh_token"
}
```

Response **200**: A new token pair. Always store the newest refresh token.

```json
{
  "access_token": "access_token",
  "refresh_token": "refresh_token",
  "token_type": "Bearer"
}
```

Error statuses: 400, 401, 423, 429

## POST /api/v1/auth/logout

**Revoke one refresh token**

**Who:** anyone holding a refresh token

**Rate limit:** 30 per minute.

Request body:

```json
{
  "refresh_token": "refresh_token"
}
```

Response **200**: Logged out.

```json
{
  "message": "message"
}
```

Error statuses: 400, 401, 429

## POST /api/v1/auth/logout-all

**End every session of the signed-in user**

**Who:** any signed-in user, even if their SACCO is suspended

**Rate limit:** 30 per minute.

Writes the audit row `auth.logout_all`.

Response **200**: Every refresh token is revoked and every access token issued so far stops working, including this one.

```json
{
  "message": "message"
}
```

Error statuses: 401, 429

## POST /api/v1/auth/change-password

**Change the signed-in user's password**

**Who:** any signed-in user, even if their SACCO is suspended

**Rate limit:** 5 per minute (per IP).

A wrong `current_password` is 401 and counts as a failed login (same lockout). The new password follows the register rules and must differ from the current one. Writes the audit row `auth.password_change`.

Request body:

```json
{
  "current_password": "current_password",
  "new_password": "new_password"
}
```

Response **200**: Password changed. All sessions end; sign in again.

```json
{
  "message": "message"
}
```

Error statuses: 400, 401, 423, 429

## POST /api/v1/auth/verify-email

**Confirm an email address**

**Who:** anyone holding the emailed token

**Rate limit:** 5 per minute (per IP).

The link is single-use and valid for 24 hours. 400 for a wrong, used or expired token.

Request body:

```json
{
  "token": "token"
}
```

Response **200**: Verified.

```json
{
  "message": "message"
}
```

Error statuses: 400, 429

## POST /api/v1/auth/resend-verification

**Send a new verification link**

**Who:** anyone

**Rate limit:** 5 per minute (per IP).

Replaces any earlier link for the user.

Request body:

```json
{
  "email": "member@example.com"
}
```

Response **200**: Always the same answer, whether or not the address exists or needs verifying.

```json
{
  "message": "message"
}
```

Error statuses: 429

## POST /api/v1/auth/forgot-password

**Email a password-reset link**

**Who:** anyone

**Rate limit:** 5 per minute (per IP).

The link lasts 1 hour and replaces any earlier reset link. This is also how an account locked by repeated wrong guesses is recovered.

Request body:

```json
{
  "email": "member@example.com"
}
```

Response **200**: Always the same answer, whether or not the address has an account.

```json
{
  "message": "message"
}
```

Error statuses: 429

## POST /api/v1/auth/reset-password

**Set a new password from an emailed link**

**Who:** anyone holding the emailed token

**Rate limit:** 5 per minute (per IP).

The link works once. The new password follows the register rules. A successful reset also marks the email verified, clears the lockout and revokes every session. A weak password is 400 and does not spend the link. Writes the audit row `auth.password_reset`.

Request body:

```json
{
  "token": "token",
  "new_password": "new_password"
}
```

Response **200**: Password reset. Every session ends; sign in with the new password.

```json
{
  "message": "message"
}
```

Error statuses: 400, 429

## GET /api/v1/members/me

**The caller's own profile**

**Who:** any signed-in user

**Rate limit:** 120 per minute.

Response **200**: Profile with memberships.

```json
{
  "id": 1,
  "email": "member@example.com",
  "created_at": "2026-11-18T09:00:00Z",
  "memberships": [
    {
      "membership_id": 1,
      "sacco_id": 1,
      "role": "role",
      "status": "status"
    }
  ]
}
```

Error statuses: 401, 423, 429

## PATCH /api/v1/members/me

**Change the caller's own email**

**Who:** any signed-in user

**Rate limit:** 30 per minute.

Needs `current_password`. A wrong one is 403 and counts toward the lockout. Only `email` can change.

Request body:

```json
{
  "email": "member@example.com",
  "current_password": "current_password"
}
```

Response **200**: Updated profile. The new address is unverified, a link is emailed, and every session ends (sign in again).

```json
{
  "id": 1,
  "email": "member@example.com",
  "created_at": "2026-11-18T09:00:00Z",
  "memberships": [
    {
      "membership_id": 1,
      "sacco_id": 1,
      "role": "role",
      "status": "status"
    }
  ]
}
```

Error statuses: 400, 401, 403, 409, 423, 429

## GET /api/v1/members/me/photo

**Read the caller's profile photo**

**Who:** the signed-in account owner. The photo is private to that account.

**Rate limit:** 120 per minute.

Response **200**: The caller's saved profile photo.

Response **204**: No profile photo has been saved.

Error statuses: 401, 429

## PUT /api/v1/members/me/photo

**Save the caller's profile photo**

**Who:** the signed-in account owner. Send a `photo` file using `multipart/form-data`. JPEG, PNG and WebP images are accepted, up to 1.5 MB.

**Rate limit:** 30 per minute.

Response **200**: The photo was saved to the caller's account.

```json
{
  "saved": true
}
```

Error statuses: 400, 401, 413, 429

## DELETE /api/v1/members/me/photo

**Remove the caller's profile photo**

**Who:** the signed-in account owner.

**Rate limit:** 30 per minute.

Response **200**: The photo was removed from the caller's account.

```json
{
  "deleted": true
}
```

Error statuses: 401, 429

## GET /api/v1/saccos

**List SACCOs**

**Who:** platform owner

**Rate limit:** 120 per minute.

Response **200**: All SACCOs.

```json
{
  "saccos": [
    {
      "id": 1,
      "name": "name",
      "currency": "currency",
      "status": "active",
      "member_count": 1,
      "created_at": "2026-11-18T09:00:00Z",
      "registration_number": "registration_number",
      "sasra_licence_number": "sasra_licence_number",
      "registration_date": "2026-11-18"
    }
  ]
}
```

Error statuses: 401, 403, 429

## POST /api/v1/saccos

**Create a SACCO**

**Who:** platform owner

**Rate limit:** 30 per minute.

`currency` defaults to KES. The name is unique, up to 255 characters, with no control or zero-width characters. Optional: `registration_number`, `sasra_licence_number`, `registration_date` (see the Sacco schema); a bad value is 400 with the field named, a number already used by another SACCO is 409.

Request body:

```json
{
  "name": "name",
  "currency": "currency"
}
```

Response **201**: Created.

```json
{
  "id": 1,
  "name": "name",
  "currency": "currency",
  "status": "active",
  "member_count": 1,
  "created_at": "2026-11-18T09:00:00Z",
  "registration_number": "registration_number",
  "sasra_licence_number": "sasra_licence_number",
  "registration_date": "2026-11-18"
}
```

Error statuses: 400, 401, 403, 409, 429

## GET /api/v1/saccos/{sacco_id}

**Get a SACCO**

**Who:** any active member of the SACCO (`sacco.view`), or the platform owner

**Rate limit:** 120 per minute.

Parameters: `sacco_id` (path, integer)

Response **200**: The SACCO.

```json
{
  "id": 1,
  "name": "name",
  "currency": "currency",
  "status": "active",
  "member_count": 1,
  "created_at": "2026-11-18T09:00:00Z",
  "registration_number": "registration_number",
  "sasra_licence_number": "sasra_licence_number",
  "registration_date": "2026-11-18"
}
```

Error statuses: 401, 403, 404, 423, 429

## PATCH /api/v1/saccos/{sacco_id}

**Rename a SACCO or record its registration details**

**Who:** holders of `sacco.update` (the SACCO admin), or the platform owner

**Rate limit:** 30 per minute.

Send any of `name`, `registration_number`, `sasra_licence_number`, `registration_date`; nothing else (400). `null` or an empty string clears a registration field. A bad value is 400 with `error.fields`; a number another SACCO already uses is 409. Audited as `sacco.rename` and `sacco.registration_update` (field names only, never the numbers).

Parameters: `sacco_id` (path, integer)

Request body:

```json
{
  "name": "name"
}
```

Response **200**: Updated.

```json
{
  "id": 1,
  "name": "name",
  "currency": "currency",
  "status": "active",
  "member_count": 1,
  "created_at": "2026-11-18T09:00:00Z",
  "registration_number": "registration_number",
  "sasra_licence_number": "sasra_licence_number",
  "registration_date": "2026-11-18"
}
```

Error statuses: 400, 401, 403, 404, 409, 423, 429

## POST /api/v1/saccos/{sacco_id}/activate

**Set a SACCO active**

**Who:** platform owner

**Rate limit:** 30 per minute.

Suspending makes every endpoint of that SACCO answer 423 (reads and writes, joining and PIN login); login, refresh, /members/me and /members/me/saccos keep working and show the status.

Parameters: `sacco_id` (path, integer)

Response **200**: The SACCO, now active.

```json
{
  "id": 1,
  "name": "name",
  "currency": "currency",
  "status": "active",
  "member_count": 1,
  "created_at": "2026-11-18T09:00:00Z",
  "registration_number": "registration_number",
  "sasra_licence_number": "sasra_licence_number",
  "registration_date": "2026-11-18"
}
```

Error statuses: 401, 403, 404, 429

## POST /api/v1/saccos/{sacco_id}/deactivate

**Set a SACCO suspended**

**Who:** platform owner

**Rate limit:** 30 per minute.

Suspending makes every endpoint of that SACCO answer 423 (reads and writes, joining and PIN login); login, refresh, /members/me and /members/me/saccos keep working and show the status.

Parameters: `sacco_id` (path, integer)

Response **200**: The SACCO, now suspended.

```json
{
  "id": 1,
  "name": "name",
  "currency": "currency",
  "status": "active",
  "member_count": 1,
  "created_at": "2026-11-18T09:00:00Z",
  "registration_number": "registration_number",
  "sasra_licence_number": "sasra_licence_number",
  "registration_date": "2026-11-18"
}
```

Error statuses: 401, 403, 404, 429

## GET /api/v1/saccos/{sacco_id}/members

**List a SACCO's members**

**Who:** platform owner, or an officer holding `member.view` (admin, secretary, loans officer); members get 403

**Rate limit:** 120 per minute.

Parameters: `sacco_id` (path, integer)

Response **200**: Members.

```json
{
  "members": [
    {
      "id": 1,
      "user_id": 1,
      "email": "member@example.com",
      "role": "member",
      "status": "active",
      "created_at": "2026-11-18T09:00:00Z"
    }
  ]
}
```

Error statuses: 401, 403, 404, 423, 429

## POST /api/v1/saccos/{sacco_id}/members

**Add a registered user, or invite a phone, to a SACCO**

**Who:** holders of `member.manage` (the SACCO admin) or the platform owner; adding an admin needs `admin.manage` (platform owner only)

**Rate limit:** 30 per minute.

Two forms. **Email:** adds a registered user (status `active` or `inactive`; `invited` is 400). 404 if no user has that email; 409 if the user is already a member, or has not verified their email yet. **Phone** (send `phone` and no `email`): creates an `invited` membership and answers 201 with a one-time `join_code` and `expires_at`, shown once; the person joins with POST /auth/join. 409 if the phone was already invited to this SACCO. Every new membership gets the job title its role implies (staff: secretary). **Email invite** (send `email`, `role` and `invite: true`): creates an `invited` membership for that verified account and emails it a one-time join code. The code is never in the response (it carries `id`, `email`, `role`, `title`, `status` and `expires_at`); the person joins with POST /auth/join sending only the `code`. Same 404 and 409 answers as the email form.

Parameters: `sacco_id` (path, integer)

Request body:

```json
{
  "email": "member@example.com",
  "phone": "+254700000000",
  "role": "member",
  "status": "active"
}
```

Response **201**: Added.

```json
{
  "id": 1,
  "user_id": 1,
  "email": "member@example.com",
  "role": "member",
  "status": "active",
  "created_at": "2026-11-18T09:00:00Z"
}
```

Error statuses: 400, 401, 403, 404, 409, 423, 429

## GET /api/v1/saccos/{sacco_id}/members/{membership_id}

**Get one member**

**Who:** platform owner, or an officer holding `member.view` (admin, secretary, loans officer); members get 403

**Rate limit:** 120 per minute.

Parameters: `sacco_id` (path, integer), `membership_id` (path, integer)

Response **200**: The member.

```json
{
  "id": 1,
  "user_id": 1,
  "email": "member@example.com",
  "role": "member",
  "status": "active",
  "created_at": "2026-11-18T09:00:00Z"
}
```

Error statuses: 401, 403, 404, 423, 429

## PATCH /api/v1/saccos/{sacco_id}/members/{membership_id}

**Change a member's role or status**

**Who:** holders of `member.manage` (the SACCO admin) or the platform owner; touching an admin needs `admin.manage` (platform owner only)

**Rate limit:** 30 per minute.

Send `role` and/or `status`. Writes an audit row. An active member never goes back to `invited`; an inactive phone member can be invited again (`status: invited`), which answers with a fresh `join_code` and kills the old ones. An invited or PIN-less phone member becomes active only by joining with their code (409). Setting `inactive` kills unused join codes. Making or removing an admin moves the `sacco_admin` title with the role.

Parameters: `sacco_id` (path, integer), `membership_id` (path, integer)

Request body:

```json
{
  "role": "member",
  "status": "active"
}
```

Response **200**: Updated member. A re-invite (`status` inactive -> invited) also returns `join_code` and `expires_at`, shown once.

```json
{
  "id": 1,
  "user_id": 1,
  "email": "member@example.com",
  "role": "member",
  "status": "active",
  "created_at": "2026-11-18T09:00:00Z"
}
```

Error statuses: 400, 401, 403, 404, 409, 423, 429

## POST /api/v1/savings/accounts

**Open a savings account for a member**

**Who:** a member of this SACCO holding `savings.post` (SACCO admin or finance officer)

**Rate limit:** 30 per minute.

404 for a membership in another SACCO or one that does not exist (same body).

Request body:

```json
{
  "membership_id": 1
}
```

Response **201**: Opened.

```json
{
  "id": 1,
  "membership_id": 1,
  "sacco_id": 1,
  "balance": 1,
  "created_at": "2026-11-18T09:00:00Z"
}
```

Error statuses: 400, 401, 403, 404, 409, 423, 429

## GET /api/v1/savings/accounts/me

**The caller's own accounts**

**Who:** any signed-in user

**Rate limit:** 120 per minute.

Response **200**: Accounts across the caller's active memberships in active SACCOs (a list: a user may belong to several SACCOs).

```json
{
  "accounts": [
    {
      "id": 1,
      "membership_id": 1,
      "sacco_id": 1,
      "balance": 1,
      "created_at": "2026-11-18T09:00:00Z"
    }
  ]
}
```

Error statuses: 401, 429

## GET /api/v1/savings/accounts/{account_id}

**Get one account and its balance**

**Who:** the account's owner, `savings.view_all` (SACCO admin, secretary, finance officer), platform owner

**Rate limit:** 120 per minute.

A member can read only their own account; another member's account is 404.

Parameters: `account_id` (path, integer)

Response **200**: The account. `balance` is derived from the ledger.

```json
{
  "id": 1,
  "membership_id": 1,
  "sacco_id": 1,
  "balance": 1,
  "created_at": "2026-11-18T09:00:00Z"
}
```

Error statuses: 401, 403, 404, 423, 429

## POST /api/v1/savings/accounts/{account_id}/deposits

**Record a manual deposit from an M-Pesa receipt**

**Who:** a member of this SACCO holding `savings.post` (SACCO admin or finance officer)

**Rate limit:** 20 per minute.

A manual deposit needs `amount` (minor units), `mpesa_receipt` (the code from the M-Pesa message, 8 to 12 capital letters and digits) and `phone` (the payer). The ledger row is marked `source: manual`. One receipt is one payment per SACCO: recording it again with the same amount is a retry (**200**, `replayed: true`); any other repeat, or a receipt an M-Pesa callback already recorded, is 409 `DUPLICATE_RECEIPT`. A later callback for a receipt entered here is linked to it, not added. A **flagged** deposit (a manual entry at or over the SACCO's limit, a large amount, or the same amount again and again) is not posted: it answers **202** `{queued: true, request}` with this officer's first approval, and a different officer confirms it in the request queue. Holders of `savings.post` post money, others cannot (403), and nobody may post on their own account (403). Amounts are integers in minor units.

Parameters: `account_id` (path, integer)

Request body:

```json
{
  "amount": 1,
  "reference": "dep-2026-11-18-001"
}
```

Response **201**: New entry (`source: manual`, with `request_id` and `mpesa_receipt`).

```json
{
  "id": 1,
  "account_id": 1,
  "type": "deposit",
  "amount": 1,
  "reference": "dep-2026-11-18-001",
  "balance": 1,
  "created_at": "2026-11-18T09:00:00Z",
  "replayed": true,
  "source": "source",
  "request_id": 1,
  "mpesa_receipt": "mpesa_receipt"
}
```

Response **202**: Flagged: `{queued: true, request}`. Nothing is posted yet.

Response **200**: Retry of the same receipt entered earlier.

```json
{
  "id": 1,
  "account_id": 1,
  "type": "deposit",
  "amount": 1,
  "reference": "dep-2026-11-18-001",
  "balance": 1,
  "created_at": "2026-11-18T09:00:00Z",
  "replayed": true,
  "source": "source",
  "request_id": 1,
  "mpesa_receipt": "mpesa_receipt"
}
```

Error statuses: 400, 401, 403, 404, 409, 423, 429

## POST /api/v1/savings/accounts/{account_id}/withdrawals

**Post a withdrawal**

**Who:** a member of this SACCO holding `savings.post` (SACCO admin or finance officer)

**Rate limit:** 20 per minute.

Insufficient funds is 409 `Insufficient funds.`. `reference` is required. A new entry answers **201**; repeating the same reference with the same request answers **200** with `replayed: true`; the same reference with a different request is 409. Holders of `savings.post` (admin, finance officer) post money, others cannot (403), and nobody may post on their own account (403). A withdrawal that is **flagged** (large, the same amount again and again, or soon after a deposit; see `deposit-controls`) is 409 `NEEDS_SECOND_APPROVAL`: it goes through the member's withdrawal request, which two officers approve. A withdrawal that would leave the savings below the loan cap of an open loan is 409 `LOAN_OVER_CAP` (SACCOs with published rules). Amounts are integers in minor units.

Parameters: `account_id` (path, integer)

Request body:

```json
{
  "amount": 1,
  "reference": "dep-2026-11-18-001"
}
```

Response **201**: New entry.

```json
{
  "id": 1,
  "account_id": 1,
  "type": "deposit",
  "amount": 1,
  "reference": "dep-2026-11-18-001",
  "balance": 1,
  "created_at": "2026-11-18T09:00:00Z",
  "replayed": true,
  "source": "source",
  "request_id": 1,
  "mpesa_receipt": "mpesa_receipt"
}
```

Response **200**: Replay of an earlier request with the same reference.

```json
{
  "id": 1,
  "account_id": 1,
  "type": "deposit",
  "amount": 1,
  "reference": "dep-2026-11-18-001",
  "balance": 1,
  "created_at": "2026-11-18T09:00:00Z",
  "replayed": true,
  "source": "source",
  "request_id": 1,
  "mpesa_receipt": "mpesa_receipt"
}
```

Error statuses: 400, 401, 403, 404, 409, 423, 429

## GET /api/v1/savings/accounts/{account_id}/transactions

**An account's ledger history, newest first**

**Who:** the account's owner, `savings.view_all` (SACCO admin, secretary, finance officer), platform owner

**Rate limit:** 120 per minute.

Parameters: `account_id` (path, integer), `page` (query, integer), `per_page` (query, integer)

Response **200**: A page of entries.

```json
{
  "transactions": [
    {
      "id": 1,
      "type": "deposit",
      "amount": 1,
      "reference": "dep-2026-11-18-001",
      "created_at": "2026-11-18T09:00:00Z",
      "reverses_id": 1
    }
  ],
  "page": 1,
  "per_page": 1,
  "total": 1
}
```

Error statuses: 400, 401, 403, 404, 423, 429

## POST /api/v1/savings/transactions/{transaction_id}/reversals

**Ask for a reversal of a ledger entry (first of two officers)**

**Who:** a member of this SACCO holding `savings.reverse` (SACCO admin or finance officer)

**Rate limit:** 20 per minute.

Reversals take two officers (maker-checker). This call only **asks**: send `reason` (10 to 140 characters, one line). Nothing is posted; a different officer approves with `POST /savings/reversal-requests/{request_id}/approve`. Nobody may ask to reverse an entry on their own account (403). An entry that is itself a reversal, a loan entry, or one already reversed is 409. One request may be open per entry: the same officer asking again gets the same request (**200**); another officer gets 409.

Parameters: `transaction_id` (path, integer)

Request body:

```json
{
  "reason": "reason"
}
```

Response **201**: The request: `{id, sacco_id, transaction_id, account_id, type, amount, requested_by, reason, status: pending, ...}`.

Response **200**: The same officer already has this request open.

Error statuses: 400, 401, 403, 404, 409

## GET /api/v1/savings/saccos/{sacco_id}/reversal-requests

**This SACCO's reversal requests**

**Who:** a member holding `savings.reverse`. Newest first, at most 100. `?status=` is `pending`, `approved` or `rejected`.

**Rate limit:** read limit.

Parameters: `sacco_id` (path, integer)

Response **200**: `{items: [request]}`.

Error statuses: 400, 401, 403, 404

## POST /api/v1/savings/reversal-requests/{request_id}/approve

**Approve a reversal request (the second officer) and append the entry**

**Who:** a member holding `savings.reverse`, never the officer who asked (403 `SECOND_OFFICER_REQUIRED`).

**Rate limit:** 20 per minute.

Send `reference` (the idempotency key). The opposite entry is appended now, with the same rules as before: nobody reverses an entry on their own account, a reversal that would take the balance below zero is 409, and one that would leave savings below an open loan's cap is 409 `LOAN_OVER_CAP`. A SACCO admin (`savings.reverse_override`) may force that one by sending `override_reason` (10 to 140 characters, one line); it is audited as `savings.reverse_override`. A retried approval with the same `reference` answers **200** with `replayed: true`.

Parameters: `request_id` (path, integer)

Request body:

```json
{
  "reference": "dep-2026-11-18-001",
  "override_reason": "override_reason"
}
```

Response **201**: The new opposite entry.

```json
{
  "id": 1,
  "reverses_id": 1,
  "account_id": 1,
  "type": "deposit",
  "amount": 1,
  "reference": "dep-2026-11-18-001",
  "balance": 1,
  "created_at": "2026-11-18T09:00:00Z",
  "replayed": true
}
```

Response **200**: Replay of an approval that already went through.

```json
{
  "id": 1,
  "reverses_id": 1,
  "account_id": 1,
  "type": "deposit",
  "amount": 1,
  "reference": "dep-2026-11-18-001",
  "balance": 1,
  "created_at": "2026-11-18T09:00:00Z",
  "replayed": true
}
```

Error statuses: 400, 401, 403, 404, 409

## POST /api/v1/savings/reversal-requests/{request_id}/reject

**Reject a reversal request (a different officer)**

**Who:** a member holding `savings.reverse`, never the officer who asked (403 `SECOND_OFFICER_REQUIRED`). Send `reason` (10 to 140 characters). Nothing is posted.

**Rate limit:** 30 per minute.

Parameters: `request_id` (path, integer)

Request body:

```json
{
  "reason": "reason"
}
```

Response **200**: The request with `status: rejected`.

Error statuses: 400, 401, 403, 404, 409

## GET /api/v1/admin/saccos/{sacco_id}/reports/summary

**SACCO totals**

**Who:** `report.view` (SACCO admin, secretary, finance or loans officer), platform owner

**Rate limit:** 120 per minute.

Parameters: `sacco_id` (path, integer), `from` (query, string), `to` (query, string)

Response **200**: Totals. `deposit_total` and `withdrawal_total` leave out reversed entries and reversals; `from`/`to` filter the totals, not the balance.

```json
{
  "sacco_id": 1,
  "from": "2026-11-18",
  "to": "2026-11-18",
  "account_count": 1,
  "total_balance": 1,
  "deposit_total": 1,
  "withdrawal_total": 1,
  "reversal_count": 1
}
```

Error statuses: 400, 401, 403, 404, 423, 429

## GET /api/v1/admin/saccos/{sacco_id}/reports/member-activity

**Per-member activity**

**Who:** `report.view` (SACCO admin, secretary, finance or loans officer), platform owner

**Rate limit:** 120 per minute.

Parameters: `sacco_id` (path, integer), `page` (query, integer), `per_page` (query, integer)

Response **200**: A page of members. No `total`: keep paging until a page is shorter than `per_page`.

```json
{
  "members": [
    {
      "membership_id": 1,
      "email": "member@example.com",
      "account_id": 1,
      "transaction_count": 1,
      "last_transaction_at": "2026-11-18T09:00:00Z"
    }
  ],
  "page": 1,
  "per_page": 1
}
```

Error statuses: 400, 401, 403, 404, 423, 429

## GET /api/v1/admin/saccos/{sacco_id}/accounts

**List a SACCO's accounts with balances**

**Who:** `savings.view_all` (SACCO admin, secretary, finance officer), platform owner

**Rate limit:** 120 per minute.

Parameters: `sacco_id` (path, integer), `page` (query, integer), `per_page` (query, integer)

Response **200**: A page of accounts.

```json
{
  "accounts": [
    {
      "account_id": 1,
      "membership_id": 1,
      "email": "member@example.com",
      "balance": 1
    }
  ],
  "page": 1,
  "per_page": 1,
  "total": 1
}
```

Error statuses: 400, 401, 403, 404, 423, 429

## GET /api/v1/admin/saccos/{sacco_id}/audit

**A SACCO's audit log, newest first**

**Who:** the secretary (`audit.view`) or the platform owner. The SACCO admin gets 403.

**Rate limit:** 120 per minute.

Read-only. Every query is filtered by SACCO.

Parameters: `sacco_id` (path, integer), `page` (query, integer), `per_page` (query, integer), `action` (query, string), `from` (query, string), `to` (query, string)

Response **200**: A page of audit rows.

```json
{
  "entries": [
    {
      "id": 1,
      "actor_id": 1,
      "action": "action",
      "target": "target",
      "created_at": "2026-11-18T09:00:00Z"
    }
  ],
  "total": 1,
  "page": 1,
  "per_page": 1
}
```

Error statuses: 400, 401, 403, 404, 423, 429

## GET /api/v1/admin/audit

**The platform audit log, newest first**

**Who:** platform owner only

**Rate limit:** 120 per minute.

Optional `sacco_id` filter. Rows with no SACCO (such as `auth.password_change`) appear only here.

Parameters: `page` (query, integer), `per_page` (query, integer), `action` (query, string), `from` (query, string), `to` (query, string), `sacco_id` (query, integer)

Response **200**: A page of audit rows.

```json
{
  "entries": [
    {
      "id": 1,
      "actor_id": 1,
      "action": "action",
      "target": "target",
      "created_at": "2026-11-18T09:00:00Z"
    }
  ],
  "total": 1,
  "page": 1,
  "per_page": 1
}
```

Error statuses: 400, 401, 403, 429

## POST /api/v1/savings/accounts/{account_id}/interest

**Credit interest**

**Who:** admin or finance officer (`savings.post`). Same reference rules as deposits.

Parameters: `account_id` (path, integer)

Request body:

```json
{
  "amount": 1,
  "reference": "dep-2026-11-18-001"
}
```

Response **201**: New entry.

```json
{
  "id": 1,
  "account_id": 1,
  "type": "deposit",
  "amount": 1,
  "reference": "dep-2026-11-18-001",
  "balance": 1,
  "created_at": "2026-11-18T09:00:00Z",
  "replayed": true,
  "source": "source",
  "request_id": 1,
  "mpesa_receipt": "mpesa_receipt"
}
```

Response **200**: Replay of the same request.

```json
{
  "id": 1,
  "account_id": 1,
  "type": "deposit",
  "amount": 1,
  "reference": "dep-2026-11-18-001",
  "balance": 1,
  "created_at": "2026-11-18T09:00:00Z",
  "replayed": true,
  "source": "source",
  "request_id": 1,
  "mpesa_receipt": "mpesa_receipt"
}
```

Error statuses: 400, 401, 403, 404, 409, 423

## POST /api/v1/savings/accounts/{account_id}/fees

**Charge a fee**

**Who:** admin or finance officer (`savings.post`). Same reference rules as deposits.

Parameters: `account_id` (path, integer)

Request body:

```json
{
  "amount": 1,
  "reference": "dep-2026-11-18-001"
}
```

Response **201**: New entry.

```json
{
  "id": 1,
  "account_id": 1,
  "type": "deposit",
  "amount": 1,
  "reference": "dep-2026-11-18-001",
  "balance": 1,
  "created_at": "2026-11-18T09:00:00Z",
  "replayed": true,
  "source": "source",
  "request_id": 1,
  "mpesa_receipt": "mpesa_receipt"
}
```

Response **200**: Replay of the same request.

```json
{
  "id": 1,
  "account_id": 1,
  "type": "deposit",
  "amount": 1,
  "reference": "dep-2026-11-18-001",
  "balance": 1,
  "created_at": "2026-11-18T09:00:00Z",
  "replayed": true,
  "source": "source",
  "request_id": 1,
  "mpesa_receipt": "mpesa_receipt"
}
```

Error statuses: 400, 401, 403, 404, 409, 423

## GET /api/v1/saccos/{sacco_id}/members/{membership_id}/roles

**A member's job titles**

**Who:** holders of `member.manage` (the SACCO admin), or the platform owner.

Parameters: `sacco_id` (path, integer), `membership_id` (path, integer)

Response **200**: The titles held.

```json
{
  "membership_id": 1,
  "roles": [
    "sacco_admin"
  ]
}
```

Error statuses: 401, 403, 404, 423

## PUT /api/v1/saccos/{sacco_id}/members/{membership_id}/roles

**Set a member's job titles**

**Who:** holders of `member.manage` (the SACCO admin), or the platform owner. Replaces the whole list. Only the platform owner edits an admin's titles; nobody changes their own titles; the membership must be active (409). `sacco_admin` follows the SACCO admin role: keep it on an admin and leave it off everyone else (409), and make or remove admins with PATCH /saccos/{sacco_id}/members/{membership_id}, which moves the title with the role.

Parameters: `sacco_id` (path, integer), `membership_id` (path, integer)

Request body:

```json
{
  "roles": [
    "roles"
  ]
}
```

Response **200**: The titles now held.

```json
{
  "membership_id": 1,
  "roles": [
    "sacco_admin"
  ]
}
```

Error statuses: 400, 401, 403, 404, 409, 423

## GET /api/v1/saccos/{sacco_id}/rules

**The SACCO's rules**

**Who:** any active member (`sacco.view`), or the platform owner. `rules` is null until a SACCO has published rules. `accepted` says whether the caller accepted the current version (null for a platform owner). The rules are locked while `locked` is true.

Parameters: `sacco_id` (path, integer)

Response **200**: The current rules.

```json
{
  "rules": "rules"
}
```

Error statuses: 401, 403, 404

## POST /api/v1/saccos/{sacco_id}/rules

**Publish the SACCO's first rules**

**Who:** the SACCO admin (`sacco.update`) or the platform owner.

Only the first version. After that the rules are locked until the cycle ends (409 `RULES_LOCKED`) and change only by member vote. Amounts are whole cents. `interest_percent` has no default and must be sent. `loan_cap_multiple` defaults to 3 and `cycle_months` to 12. Every 400 carries `error.fields`, a plain-words reason for each bad field.

Parameters: `sacco_id` (path, integer)

Request body:

```json
{
  "contribution_amount": 1,
  "contribution_frequency": "weekly",
  "loan_cap_multiple": 1,
  "interest_percent": 1.0,
  "repayment_period_value": 1,
  "repayment_period_unit": "days",
  "late_fine_amount": 1,
  "cycle_months": 1,
  "guarantors_required": 1,
  "min_membership_days": 1,
  "exit_rules": "exit_rules",
  "loan_purposes": "loan_purposes"
}
```

Response **201**: Published, version 1, cycle starts today.

```json
{
  "rules": "rules"
}
```

Error statuses: 400, 401, 403, 404, 409

## POST /api/v1/saccos/{sacco_id}/rules/accept

**Accept the current rules**

**Who:** an active member. Send the `version` you read. Idempotent.

Parameters: `sacco_id` (path, integer)

Request body:

```json
{
  "version": 1
}
```

Response **200**: Accepted.

```json
{
  "rules": "rules"
}
```

Error statuses: 401, 403, 404, 409

## GET /api/v1/saccos/{sacco_id}/payment-details

**Where members pay in**

**Who:** any active member of the SACCO (`sacco.view`), or the platform owner. Returns `details` (the live values, or `null` when the SACCO has not set any) and `pending` (the change waiting for a second officer, or `null`). `pending` is only filled for holders of `payin.manage`; a member never sees a change before it is live. An approval by a second officer takes effect at once, so there is no window between approval and the values members see: `updated_at` is that approval. `bank_manual_confirm_only` is true when the bank uses a shared paybill.

**Rate limit:** 120 per minute.

Parameters: `sacco_id` (path, integer)

Response **200**: `{details, pending}`. Members never get a pending change.

Error statuses: 401, 403, 404, 423, 429

## POST /api/v1/saccos/{sacco_id}/payment-details/changes

**Propose new pay-in details**

**Who:** holders of `payin.manage` (the SACCO admin and the finance officer). Changes nothing members see: the change waits until a different officer approves it. A newer proposal replaces the one waiting. Give M-Pesa details, bank details or both. M-Pesa: `mpesa_type` (`paybill`, `till` or `pochi`), `mpesa_number` (5 to 7 digits; for `pochi` a Safaricom phone number, shown to members but never prompted), `display_name`, optional `account_reference_hint` (paybill only). Bank: `bank_name`, `bank_account_name`, `bank_account_number` (6 to 20 digits), optional `bank_branch`. Shared bank paybill: `bank_uses_shared_paybill` true with `bank_paybill_number` (5 to 7 digits). Every proposal is audited (`payin.propose`, `payin.supersede`) and every officer is emailed. Text fields take Latin letters, ASCII digits and ordinary punctuation on one line (invisible, look-alike and non-Latin letters are refused). A SACCO may send 10 proposals an hour (429).

**Rate limit:** 30 per minute.

Parameters: `sacco_id` (path, integer)

Request body:

```json
{
  "mpesa_type": "paybill",
  "mpesa_number": "mpesa_number",
  "display_name": "display_name",
  "account_reference_hint": "account_reference_hint",
  "bank_name": "bank_name",
  "bank_account_name": "bank_account_name",
  "bank_account_number": "bank_account_number",
  "bank_branch": "bank_branch",
  "bank_uses_shared_paybill": true,
  "bank_paybill_number": "bank_paybill_number"
}
```

Response **201**: `{change}` with `status: pending`.

Error statuses: 400, 401, 403, 404, 409, 423, 429

## POST /api/v1/saccos/{sacco_id}/payment-details/changes/{change_id}/approve

**Approve a waiting change, which puts it live at once**

**Who:** holders of `payin.manage`, never the officer who proposed the change (403 `PAYIN_SAME_OFFICER`). The change is named by id, so nobody approves values that changed after they read them. The approver must have become an officer before the change was proposed (403 `PAYIN_NEW_OFFICER`), and the proposer must still be an officer (409). Officer since is the earliest `granted_at` of a title that carries `payin.manage` (editing other titles keeps it); an admin with no title rows counts from when the membership was created. Audited as `payin.approve`. The approval is the moment it takes effect: the values members see change in the same request, with no waiting period. Every officer (the admin included) is emailed, without the numbers, and members get no email. Everyone is also emailed when someone gains `payin.manage` (member add, role or title change).

**Rate limit:** 30 per minute.

Parameters: `sacco_id` (path, integer), `change_id` (path, integer)

Response **200**: `{change}` with `status: approved`, `approved_at` and `go_live_at` (the same moment). Members see the new details from now on.

Error statuses: 401, 403, 404, 409, 423, 429

## GET /api/v1/saccos/{sacco_id}/rules/proposals

**Proposed rule changes**

**Who:** any active member. The ten newest, with the vote count and the caller's own vote.

Parameters: `sacco_id` (path, integer)

Response **200**: Proposals.

```json
{
  "proposals": [
    {}
  ]
}
```

Error statuses: 401, 403, 404

## POST /api/v1/saccos/{sacco_id}/rules/proposals

**Propose a rule change**

**Who:** the SACCO admin or the platform owner, only after the cycle has ended (409 `RULES_LOCKED` before). Send the full set of rules. One open proposal at a time.

Parameters: `sacco_id` (path, integer)

Request body:

```json
{
  "contribution_amount": 1,
  "contribution_frequency": "weekly",
  "loan_cap_multiple": 1,
  "interest_percent": 1.0,
  "repayment_period_value": 1,
  "repayment_period_unit": "days",
  "late_fine_amount": 1,
  "cycle_months": 1,
  "guarantors_required": 1,
  "min_membership_days": 1,
  "exit_rules": "exit_rules",
  "loan_purposes": "loan_purposes"
}
```

Response **201**: Open for a vote.

```json
{
  "proposal": {}
}
```

Error statuses: 400, 401, 403, 404, 409

## POST /api/v1/saccos/{sacco_id}/rules/proposals/{proposal_id}/votes

**Vote on a proposed change**

**Who:** an active member. More than half of ALL active members counted when it was proposed (not only those who accepted the rules) must approve; then it is adopted as the next version and starts a new cycle. The acceptance of members who voted yes carries over to the new version; members who voted no, or did not vote, must accept it again before they can ask for a loan. It is rejected once it can no longer win. A member may change their vote while it is open.

Parameters: `sacco_id` (path, integer), `proposal_id` (path, integer)

Request body:

```json
{
  "approve": true
}
```

Response **200**: The proposal with its tally.

```json
{
  "proposal": {}
}
```

Error statuses: 400, 401, 403, 404, 409

## GET /api/v1/saccos/{sacco_id}/members/me/data-consent

**The data-protection wording and my consent**

**Who:** an active member of the SACCO. Returns `notice` (`version`, `purpose`, `text`) and `consent` (`purpose`, `notice_version`, `granted_at`, or `null`).

**Rate limit:** 120 per minute.

Parameters: `sacco_id` (path, integer)

Response **200**: `{notice, consent}`.

Error statuses: 401, 403, 404, 423

## POST /api/v1/saccos/{sacco_id}/members/me/data-consent

**Agree to NEXORA keeping my personal details**

**Who:** the member, for themself only (nobody can consent for someone else). Send the `notice_version` you read; another version is 409. Personal details (full name, national ID, gender, date of birth) can be stored only while this consent is active. Repeating it returns the existing consent (200). Audited as `consent.grant`.

**Rate limit:** 30 per minute.

Parameters: `sacco_id` (path, integer)

Request body:

```json
{
  "notice_version": 1
}
```

Response **201**: `{consent}`: new.

Response **200**: `{consent}`: already given.

Error statuses: 400, 409, 401, 403, 404, 423

## POST /api/v1/saccos/{sacco_id}/members/me/data-consent/withdraw

**Withdraw consent; my personal details are erased**

**Who:** the member. Works for the member's own membership whatever its status, even after deactivation or suspension. Ends the consent and erases full name, national ID, gender and date of birth at once. Savings, loans, share capital and the audit trail stay. Audited as `consent.withdraw` with the names of the erased fields, never the values.

**Rate limit:** 30 per minute.

Parameters: `sacco_id` (path, integer)

Response **200**: `{consent: null, erased: [field names]}`.

Error statuses: 409, 401, 403, 404, 423

## GET /api/v1/saccos/{sacco_id}/members/{membership_id}/profile

**A member's personal details, share capital and consent**

**Who:** the member themself, or a holder of `member.manage` (the SACCO admin). Nobody else, the platform owner included. Personal details never appear in the member list.

**Rate limit:** 120 per minute.

Parameters: `sacco_id` (path, integer), `membership_id` (path, integer)

Response **200**: `{membership_id, full_name, national_id, gender, date_of_birth, share_capital, consent}`. Every field can be `null`.

Error statuses: 401, 403, 404, 423

## PATCH /api/v1/saccos/{sacco_id}/members/{membership_id}/profile

**Set a member's personal details or share capital**

**Who:** the member themself or a holder of `member.manage`; `national_id` and `share_capital` only by a holder of `member.manage` (403 otherwise: a member who could set their own ID could probe which numbers other members hold). Send any of `full_name` (2 to 120 letters, spaces, `'` `.` `-`), `national_id` (7 or 8 digits, unique within the SACCO), `gender` (`female`, `male`, `other`), `date_of_birth` (`YYYY-MM-DD`, 1900 to today), `share_capital` (whole minor units, zero or more). `null` or an empty string clears a field. Every field is optional. Setting a personal detail needs the member's own active consent (409 `CONSENT_REQUIRED`); clearing one and `share_capital` do not. Audited as `member.profile_update` with the field names only.

**Rate limit:** 30 per minute.

Parameters: `sacco_id` (path, integer), `membership_id` (path, integer)

Request body:

```json
{
  "full_name": "full_name",
  "national_id": "national_id",
  "gender": "female",
  "date_of_birth": "2026-11-18",
  "share_capital": 1
}
```

Response **200**: The updated profile (same shape as GET).

Error statuses: 400, 409, 401, 403, 404, 423

## GET /api/v1/saccos/{sacco_id}/deposit-controls

**The SACCO's deposit-control thresholds**

**Who:** `sacco.update` or `report.view`. Returns the thresholds in force (the defaults when none are saved) and `defaults`. Amounts are minor units.

**Rate limit:** read limit.

Parameters: `sacco_id` (path, integer)

Response **200**: `{auto_confirm_limit, large_amount_limit, manual_flag_limit, repeat_count, repeat_window_hours, quick_withdrawal_hours, defaults}`.

Error statuses: 401, 403, 404

## PUT /api/v1/saccos/{sacco_id}/deposit-controls

**Change the deposit-control thresholds**

**Who:** `sacco.update` (the SACCO admin). Send any of the settings; the rest keep their value. `auto_confirm_limit` (0 to 500000, default 0: every deposit waits for an officer; a deposit at or under it, with no flag, posts at once after a paid M-Pesa prompt), `large_amount_limit` (100 to 20000000, default 10000000), `manual_flag_limit` (100 to 5000000, default 2000000), `repeat_count` (2 to 20, default 3), `repeat_window_hours` (1 to 720, default 24), `quick_withdrawal_hours` (1 to 720, default 24). The large-amount flag also adds up a member's payments over `repeat_window_hours`, so splitting does not avoid it. Audited as `deposit_controls.set`, and the officers are emailed.

**Rate limit:** write limit.

Parameters: `sacco_id` (path, integer)

Request body:

```json
{
  "auto_confirm_limit": 1,
  "large_amount_limit": 1,
  "manual_flag_limit": 1,
  "repeat_count": 1,
  "repeat_window_hours": 1,
  "quick_withdrawal_hours": 1
}
```

Response **200**: The thresholds now in force.

Error statuses: 400, 401, 403, 404

## GET /api/v1/saccos/{sacco_id}/flagged-payments

**The audit report of flagged payments**

**Who:** `report.view` (SACCO admin, finance officer, secretary). The newest 200 payment requests that carry a flag, with who gave the first and the second approval. `labels` maps a flag code to its plain-language name.

**Rate limit:** read limit.

Parameters: `sacco_id` (path, integer)

Response **200**: `{items: [{id, member, kind, source, amount_minor, mpesa_receipt, flags, status, first_approved_by, second_approved_by, decided_by, ledger_transaction_id, created_at}], labels}`.

Error statuses: 401, 403, 404

## GET /api/v1/saccos/{sacco_id}/reconciliation

**The ledger against the STK records**

**Who:** `report.view`. Compares every paid STK push with the deposit it produced and lists what does not match (`mismatches`, each with a `kind`: `paid_not_recorded`, `amount_differs`, `paid_but_rejected`, `paid_not_credited` (paid over a day ago, still unconfirmed), `ledger_missing`, `ledger_amount_differs`, `credit_reversed`, `no_paid_push`). `unverified_manual` lists manual receipts no callback has matched: not a mismatch, because a member may pay the paybill directly. Nothing is changed; the ledger is never edited. Audited as `ledger.reconcile_mpesa`.

**Rate limit:** read limit.

Parameters: `sacco_id` (path, integer)

Response **200**: `{checked, ok, mismatches, unverified_manual}`.

Error statuses: 401, 403, 404

## GET /api/v1/saccos/{sacco_id}/daraja-settings

**Whether this SACCO has its own Daraja credentials saved**

**Who:** `sacco.update` (the SACCO admin). Off by default: with `DARAJA_ENABLED` unset this route answers 404. Returns `{configured: false}` or `{configured: true, shortcode, updated_at}`. The secrets are never returned.

**Rate limit:** read limit.

Parameters: `sacco_id` (path, integer)

Response **200**: `{configured, shortcode?, updated_at?}`.

Error statuses: 401, 403, 404

## PUT /api/v1/saccos/{sacco_id}/daraja-settings

**Save this SACCO's own Daraja credentials (encrypted at rest)**

**Who:** `sacco.update` (the SACCO admin). Off by default (404). Send `shortcode` (5 to 7 digits: the paybill, or the head-office number of a till), `consumer_key`, `consumer_secret` and `passkey` (printable, no spaces). They are stored encrypted with `DARAJA_ENCRYPTION_KEY` and never returned or logged; the audit row (`daraja.credentials_set`) names the fields, not the values. Saving again replaces them. Answers 503 if the encryption key is not set. In production these are the only credentials a push can use.

**Rate limit:** write limit.

Parameters: `sacco_id` (path, integer)

Request body:

```json
{
  "shortcode": "shortcode",
  "consumer_key": "consumer_key",
  "consumer_secret": "consumer_secret",
  "passkey": "passkey"
}
```

Response **200**: `{configured: true, shortcode}`.

Error statuses: 400, 503, 401, 403, 404

## POST /api/v1/saccos/{sacco_id}/stk-push

**Ask M-Pesa to prompt my phone**

**Who:** an active member (`savings.request`). **Off by default:** with `DARAJA_ENABLED` unset this route answers 404 as if it did not exist. `DARAJA_ENV` picks Safaricom's sandbox (default) or live API; a production app starts with Daraja on only for `DARAJA_ENV=production` (see `docs/deployment-checklist.md`). Send `amount` (minor units, whole shillings, KES 1 to KES 150,000) and `phone` (07.., 01.. or 254..). At most 3 prompts may wait per member (429). The member then enters their M-Pesa PIN on the phone. Nexora holds no funds: a paid answer becomes a pending deposit that an officer confirms (or that posts at once under the SACCO's auto-confirm limit, if it set one).

The prompt uses the SACCO's own saved Daraja credentials (see `daraja-settings`); in the sandbox, with none saved, the environment's shared sandbox keys. **In production there is no fallback:** a SACCO with no saved credentials answers 409, and the approved pay-in details must exist and, for a paybill, match the saved shortcode (409 otherwise). If the live pay-in is a till (`mpesa_type` `till`) the request is `CustomerBuyGoodsOnline` to that till; a paybill is `CustomerPayBillOnline`. A SACCO that takes Pochi la Biashara (`pochi`) is record only: the route answers 409. A SACCO with no usable credentials also answers 409.

**Rate limit:** money limit.

Parameters: `sacco_id` (path, integer)

Request body:

```json
{
  "amount": 1,
  "phone": "+254700000000"
}
```

Response **201**: `{id, status: pending, amount, phone_last3, message, created_at}`.

Error statuses: 400, 409, 502, 401, 403, 404

## GET /api/v1/saccos/{sacco_id}/stk-push/{push_id}

**The answer to one STK push**

**Who:** the member who asked, or a holder of `member.manage`; anyone else gets 404. `status` is `pending`, `succeeded` or `failed`. 404 while the feature is off.

**Rate limit:** 120 per minute.

Parameters: `sacco_id` (path, integer), `push_id` (path, integer)

Response **200**: `{id, status, amount, phone_last3, message, created_at}`.

Error statuses: 401, 403, 404

## POST /api/v1/daraja/callback/{token}

**Safaricom's answer to an STK push**

Called by Safaricom, not by the app. The unguessable `token` in the path is the only secret (`DARAJA_CALLBACK_TOKEN`); a wrong token is 404. Always acknowledges with `{ResultCode: 0}`, even for an unknown or repeated answer, and changes nothing for those. A success whose amount differs from the request is recorded as `failed`; a paid one creates a pending deposit for an officer (see `deposit-controls`). 404 while the feature is off.

Parameters: `token` (path, string)

Response **200**: Acknowledged.

Error statuses: 404

## GET /api/v1/saccos/{sacco_id}/settings

**The SACCO's loan rules**

**Who:** any active member of the SACCO (`sacco.view`), or the platform owner. Defaults are returned until the owner changes them.

Parameters: `sacco_id` (path, integer)

Response **200**: Current settings.

```json
{
  "sacco_id": 1,
  "loan_cap": 1,
  "savings_multiple": 1,
  "join_code_ttl_minutes": 1,
  "required_approvals": 1
}
```

Error statuses: 401, 403, 404, 423

## PATCH /api/v1/saccos/{sacco_id}/settings

**Change the SACCO's loan rules**

**Who:** platform owner only. Send any of the fields; whole numbers only.

Parameters: `sacco_id` (path, integer)

Request body:

```json
{
  "loan_cap": 1,
  "savings_multiple": 1,
  "join_code_ttl_minutes": 1,
  "required_approvals": 1
}
```

Response **200**: Updated settings.

```json
{
  "sacco_id": 1,
  "loan_cap": 1,
  "savings_multiple": 1,
  "join_code_ttl_minutes": 1,
  "required_approvals": 1
}
```

Error statuses: 400, 401, 403, 404

## GET /api/v1/loans/saccos/{sacco_id}

**Loans in a SACCO, newest first**

**Who:** any active member. Officers (`loan.view`) see every loan; a member sees only their own.

Parameters: `sacco_id` (path, integer), `page` (query, integer), `per_page` (query, integer)

Response **200**: A page of loans.

```json
{
  "loans": [
    {
      "id": 1,
      "sacco_id": 1,
      "membership_id": 1,
      "principal": 1,
      "term_months": 1,
      "required_approvals": 1,
      "approvals_count": 1,
      "repaid_total": 1,
      "status": "requested",
      "disbursed_by_membership_id": 1,
      "disbursed_at": "2026-11-18T09:00:00Z",
      "disbursement_transaction_id": 1,
      "created_at": "2026-11-18T09:00:00Z",
      "sector": "agriculture",
      "disclosure_acknowledged_at": "2026-11-18T09:00:00Z"
    }
  ],
  "page": 1,
  "per_page": 1
}
```

Error statuses: 400, 401, 403, 404, 423

## POST /api/v1/loans/saccos/{sacco_id}

**Request a loan**

**Who:** an active member with a savings account (`loan.request`). One open loan per member (409). Within both the SACCO cap and the savings multiple the loan needs 1 approval; above either it needs the SACCO's `required_approvals` (a cap of 0 sends every loan there). The count is frozen on the loan. Optional `sector` (one of the Loan `sector` values; null or empty means not given) and `disclosure_acknowledged` (true records `disclosure_acknowledged_at`); anything else is 400. Neither is required.

Parameters: `sacco_id` (path, integer)

Request body:

```json
{
  "principal": 1,
  "term_months": 1
}
```

Response **201**: The new loan, status `requested`.

```json
{
  "id": 1,
  "sacco_id": 1,
  "membership_id": 1,
  "principal": 1,
  "term_months": 1,
  "required_approvals": 1,
  "approvals_count": 1,
  "repaid_total": 1,
  "status": "requested",
  "disbursed_by_membership_id": 1,
  "disbursed_at": "2026-11-18T09:00:00Z",
  "disbursement_transaction_id": 1,
  "created_at": "2026-11-18T09:00:00Z",
  "sector": "agriculture",
  "disclosure_acknowledged_at": "2026-11-18T09:00:00Z"
}
```

Error statuses: 400, 401, 403, 404, 409, 423

## GET /api/v1/loans/{loan_id}

**One loan with its decisions**

**Who:** the borrower, or an officer of the SACCO (`loan.view`). Anyone else gets 404.

Parameters: `loan_id` (path, integer)

Response **200**: The loan, plus a `decisions` list.

```json
{
  "id": 1,
  "sacco_id": 1,
  "membership_id": 1,
  "principal": 1,
  "term_months": 1,
  "required_approvals": 1,
  "approvals_count": 1,
  "repaid_total": 1,
  "status": "requested",
  "disbursed_by_membership_id": 1,
  "disbursed_at": "2026-11-18T09:00:00Z",
  "disbursement_transaction_id": 1,
  "created_at": "2026-11-18T09:00:00Z",
  "sector": "agriculture",
  "disclosure_acknowledged_at": "2026-11-18T09:00:00Z"
}
```

Error statuses: 401, 403, 404, 423

## POST /api/v1/loans/{loan_id}/decisions

**Approve or reject one step**

**Who:** loans officer (`loan.approve`; a SACCO admin never approves), never the borrower, and a different officer for each step. Steps go in order; the last required approval sets `approved`, a rejection ends the loan.

Parameters: `loan_id` (path, integer)

Request body:

```json
{
  "step": 1,
  "decision": "approve",
  "reason": "reason"
}
```

Response **201**: The loan after the decision.

```json
{
  "id": 1,
  "sacco_id": 1,
  "membership_id": 1,
  "principal": 1,
  "term_months": 1,
  "required_approvals": 1,
  "approvals_count": 1,
  "repaid_total": 1,
  "status": "requested",
  "disbursed_by_membership_id": 1,
  "disbursed_at": "2026-11-18T09:00:00Z",
  "disbursement_transaction_id": 1,
  "created_at": "2026-11-18T09:00:00Z",
  "sector": "agriculture",
  "disclosure_acknowledged_at": "2026-11-18T09:00:00Z"
}
```

Error statuses: 400, 401, 403, 404, 409, 423

## POST /api/v1/loans/{loan_id}/disbursements

**Pay out an approved loan**

**Who:** finance officer (`loan.disburse`) who approved no step (`LOAN_SAME_OFFICER`). Credits the principal to the borrower's savings account. The ledger row cannot be reversed. In a SACCO with published rules the cap and the pool are checked again at payout under the SACCO lock (409 `LOAN_OVER_CAP`, `LOAN_POOL_SHORT`).

Parameters: `loan_id` (path, integer)

Request body:

```json
{
  "reference": "dep-2026-11-18-001"
}
```

Response **201**: The loan, status `disbursed`.

```json
{
  "id": 1,
  "sacco_id": 1,
  "membership_id": 1,
  "principal": 1,
  "term_months": 1,
  "required_approvals": 1,
  "approvals_count": 1,
  "repaid_total": 1,
  "status": "requested",
  "disbursed_by_membership_id": 1,
  "disbursed_at": "2026-11-18T09:00:00Z",
  "disbursement_transaction_id": 1,
  "created_at": "2026-11-18T09:00:00Z",
  "sector": "agriculture",
  "disclosure_acknowledged_at": "2026-11-18T09:00:00Z"
}
```

Error statuses: 400, 401, 403, 404, 409, 423

## POST /api/v1/loans/{loan_id}/repayments

**Repay a disbursed loan from savings**

**Who:** the borrower, or a finance officer. Debits the borrower's savings account; the loan becomes `repaid` when the principal is covered. The ledger row cannot be reversed.

Parameters: `loan_id` (path, integer)

Request body:

```json
{
  "amount": 1,
  "reference": "dep-2026-11-18-001"
}
```

Response **201**: The repayment.

```json
{
  "repayment_id": 1,
  "loan_id": 1,
  "transaction_id": 1,
  "amount": 1,
  "repaid_total": 1,
  "loan_status": "loan_status",
  "account_balance": 1,
  "reference": "dep-2026-11-18-001",
  "created_at": "2026-11-18T09:00:00Z"
}
```

Error statuses: 400, 401, 403, 404, 409, 423

## GET /api/v1/admin/owners

**List the platform owners**

**Who:** platform owner only.

Response **200**: Every active platform owner.

```json
{
  "owners": [
    {
      "user_id": 1,
      "email": "member@example.com"
    }
  ]
}
```

Error statuses: 401, 403

## POST /api/v1/admin/owners

**Make a registered user a platform owner**

**Who:** platform owner only. The user must have verified their email (409) and not be an owner already (409).

Request body:

```json
{
  "email": "member@example.com"
}
```

Response **201**: The new owner.

```json
{
  "user_id": 1,
  "email": "member@example.com"
}
```

Error statuses: 400, 401, 403, 404, 409

## DELETE /api/v1/admin/owners/{user_id}

**Revoke a platform owner**

**Who:** platform owner only. Nobody revokes themselves (403); at least two owners always remain (409). The revoked owner loses platform access on their next request.

Parameters: `user_id` (path, integer)

Response **200**: The owners that remain.

```json
{
  "owners": [
    {
      "user_id": 1,
      "email": "member@example.com"
    }
  ]
}
```

Error statuses: 401, 403, 404, 409

## GET /api/v1/members/me/saccos

**Every SACCO I belong to**

**Who:** any signed-in user. One login covers every SACCO: a SACCO-scoped request names its SACCO, and the membership is checked again on every request (no re-login to switch). A suspended SACCO is listed with `status: suspended`.

Response **200**: My SACCOs, by name.

```json
{
  "saccos": [
    {
      "id": 1,
      "name": "name",
      "status": "active",
      "currency": "currency",
      "membership_id": 1,
      "membership_status": "invited",
      "roles": [
        "roles"
      ]
    }
  ]
}
```

Error statuses: 401

## GET /api/v1/saccos/{sacco_id}/support-access

**Support-access requests for a SACCO, newest first**

**Who:** the SACCO's admin, or a platform owner. Approvals whose time is up are shown as expired.

Parameters: `sacco_id` (path, integer)

Response **200**: The requests.

```json
{
  "requests": [
    {
      "id": 1,
      "sacco_id": 1,
      "requested_by_user_id": 1,
      "reason": "reason",
      "scope": "audit",
      "status": "requested",
      "decided_by_membership_id": 1,
      "decided_at": "2026-11-18T09:00:00Z",
      "expires_at": "2026-11-18T09:00:00Z",
      "revoked_by_membership_id": 1,
      "revoked_at": "2026-11-18T09:00:00Z",
      "created_at": "2026-11-18T09:00:00Z"
    }
  ]
}
```

Error statuses: 401, 403, 404

## POST /api/v1/saccos/{sacco_id}/support-access

**Ask to read one scope of a SACCO's data**

**Who:** a platform owner. A platform owner reads SACCO data (audit, members, savings, loans) only through an approved grant for that scope, read-only, while it lasts (at most 24 hours). Every read under a grant writes an audit row with the grant id. There is no bypass.

Parameters: `sacco_id` (path, integer)

Request body:

```json
{
  "reason": "reason",
  "scope": "audit"
}
```

Response **201**: The request, status `requested`.

```json
{
  "id": 1,
  "sacco_id": 1,
  "requested_by_user_id": 1,
  "reason": "reason",
  "scope": "audit",
  "status": "requested",
  "decided_by_membership_id": 1,
  "decided_at": "2026-11-18T09:00:00Z",
  "expires_at": "2026-11-18T09:00:00Z",
  "revoked_by_membership_id": 1,
  "revoked_at": "2026-11-18T09:00:00Z",
  "created_at": "2026-11-18T09:00:00Z"
}
```

Error statuses: 400, 401, 403, 404

## POST /api/v1/support-access/{request_id}/approve

**Approve a support-access request**

**Who:** an active admin of the request's SACCO, never the requester. Sets the expiry 1 to 24 hours ahead.

Parameters: `request_id` (path, integer)

Request body:

```json
{
  "hours": 1
}
```

Response **200**: The request after the change.

```json
{
  "id": 1,
  "sacco_id": 1,
  "requested_by_user_id": 1,
  "reason": "reason",
  "scope": "audit",
  "status": "requested",
  "decided_by_membership_id": 1,
  "decided_at": "2026-11-18T09:00:00Z",
  "expires_at": "2026-11-18T09:00:00Z",
  "revoked_by_membership_id": 1,
  "revoked_at": "2026-11-18T09:00:00Z",
  "created_at": "2026-11-18T09:00:00Z"
}
```

Error statuses: 400, 401, 403, 404, 409

## POST /api/v1/support-access/{request_id}/deny

**Deny a support-access request**

**Who:** an active admin of the request's SACCO, never the requester.

Parameters: `request_id` (path, integer)

Response **200**: The request after the change.

```json
{
  "id": 1,
  "sacco_id": 1,
  "requested_by_user_id": 1,
  "reason": "reason",
  "scope": "audit",
  "status": "requested",
  "decided_by_membership_id": 1,
  "decided_at": "2026-11-18T09:00:00Z",
  "expires_at": "2026-11-18T09:00:00Z",
  "revoked_by_membership_id": 1,
  "revoked_at": "2026-11-18T09:00:00Z",
  "created_at": "2026-11-18T09:00:00Z"
}
```

Error statuses: 400, 401, 403, 404, 409

## POST /api/v1/support-access/{request_id}/revoke

**Revoke an approved grant at any time**

**Who:** an active admin of the request's SACCO, never the requester.

Parameters: `request_id` (path, integer)

Response **200**: The request after the change.

```json
{
  "id": 1,
  "sacco_id": 1,
  "requested_by_user_id": 1,
  "reason": "reason",
  "scope": "audit",
  "status": "requested",
  "decided_by_membership_id": 1,
  "decided_at": "2026-11-18T09:00:00Z",
  "expires_at": "2026-11-18T09:00:00Z",
  "revoked_by_membership_id": 1,
  "revoked_at": "2026-11-18T09:00:00Z",
  "created_at": "2026-11-18T09:00:00Z"
}
```

Error statuses: 400, 401, 403, 404, 409

## POST /api/v1/assistant/chat

**Ask the help assistant**

**Who:** anyone. Send a Bearer token to let the assistant see the caller's own SACCOs and balances; without one it answers from the FAQ only.

**Rate limit:** 10 per minute and 30 per day per IP, 20 per hour per person, and a shared daily cap (`ASSISTANT_DAILY_CAP`).

Nothing is stored. The model gets no ids or email, only the question, the FAQ and the caller's own SACCO names, roles, statuses and balances. Answers can be wrong: the page is the official figure. Needs `GEMINI_API_KEY`; without it the route answers 503.

Request body:

```json
{
  "message": "message",
  "history": [
    {
      "role": "user",
      "text": "text"
    }
  ]
}
```

Response **200**: The assistant's plain-text answer.

```json
{
  "reply": "reply",
  "signed_in": true
}
```

Error statuses: 400, 401, 429, 503
