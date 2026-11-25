"""Safaricom Daraja STK push.

`DARAJA_ENV` picks the API: `sandbox` (the default) or `production`. In
production a push uses only the SACCO's own saved credentials, never the shared
keys in the environment, which are sandbox keys: sandbox credentials are never
sent to the live URL.

Nexora holds no funds. It asks M-Pesa to prompt the member's phone, into the
SACCO's own paybill or till, and records the answer. A paid answer becomes a
pending deposit; an officer confirms it (see `savings.deposits`).
"""

import base64
import http.client
import json
import re
import time
import urllib.request
from datetime import UTC, datetime

from flask import current_app

from app.payments.credentials import Credentials

BASES = {
    "sandbox": "https://sandbox.safaricom.co.ke",
    "production": "https://api.safaricom.co.ke",
}
PHONE = re.compile(r"254[17][0-9]{8}")
_tokens: dict[
    tuple[str, str, str], tuple[str, float]
] = {}  # (base url, key, secret) -> (token, expiry)


class DarajaError(Exception):
    """Daraja did not answer or refused. The message is safe to show."""


def environment() -> str:
    return current_app.config.get("DARAJA_ENV") or "sandbox"


def base_url() -> str:
    return BASES[environment()]


def enabled() -> bool:
    """DARAJA_ENABLED, and in a production app only with DARAJA_ENV=production."""
    cfg = current_app.config
    if not cfg.get("DARAJA_ENABLED"):
        return False
    return cfg.get("APP_ENV") != "production" or environment() == "production"


def msisdn(value) -> str | None:
    """254XXXXXXXXX from 07.., 01.., +254.. or 254.., else None."""
    phone = re.sub(r"[\s\-()+]", "", str(value or ""))
    phone = re.sub(r"^0", "254", phone)
    return phone if PHONE.fullmatch(phone) else None


def _call(path: str, payload=None, headers=None) -> dict:
    data = None if payload is None else json.dumps(payload).encode()
    request = urllib.request.Request(
        base_url() + path,
        data=data,
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as reply:
            body = json.load(reply)
    except (OSError, http.client.HTTPException, ValueError) as exc:
        raise DarajaError("M-Pesa did not answer. Try again in a moment.") from exc
    if not isinstance(body, dict):
        raise DarajaError("M-Pesa sent an answer Nexora cannot read.")
    return body


def _bearer(key: str, secret: str) -> dict:
    slot = (base_url(), key, secret)
    cached = _tokens.get(slot)
    if cached is None or time.time() >= cached[1]:
        basic = base64.b64encode(f"{key}:{secret}".encode()).decode()
        reply = _call(
            "/oauth/v1/generate?grant_type=client_credentials",
            headers={"Authorization": f"Basic {basic}"},
        )
        if not reply.get("access_token"):
            raise DarajaError("M-Pesa refused the sign-in.")
        expires = max(int(reply.get("expires_in", 3599)) - 60, 0)
        cached = _tokens[slot] = (reply["access_token"], time.time() + expires)
    return {"Authorization": f"Bearer {cached[0]}"}


def stk_push(
    phone: str,
    shillings: int,
    reference: str,
    creds: Credentials | None = None,
    till: str | None = None,
) -> dict:
    """Ask M-Pesa to prompt `phone` for `shillings`. Returns Daraja's reply.

    `creds` are the SACCO's own (sandbox only: the environment's shared keys when
    none are given). In production they must be the SACCO's own: the shared keys
    are sandbox keys and are never sent to the live URL. A `till` number pays a
    Buy Goods till; its shortcode is the head-office number.
    """
    cfg = current_app.config
    if environment() == "production":
        if creds is None or not creds.own:
            raise DarajaError("Live M-Pesa prompts use the SACCO's own credentials.")
    elif creds is None:
        creds = Credentials(
            cfg["DARAJA_SHORTCODE"],
            cfg["DARAJA_CONSUMER_KEY"],
            cfg["DARAJA_CONSUMER_SECRET"],
            cfg["DARAJA_PASSKEY"],
        )
    stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    password = base64.b64encode(f"{creds.shortcode}{creds.passkey}{stamp}".encode())
    reply = _call(
        "/mpesa/stkpush/v1/processrequest",
        {
            "BusinessShortCode": creds.shortcode,
            "Password": password.decode(),
            "Timestamp": stamp,
            "TransactionType": "CustomerBuyGoodsOnline"
            if till
            else "CustomerPayBillOnline",
            "Amount": shillings,
            "PartyA": phone,
            "PartyB": till or creds.shortcode,
            "PhoneNumber": phone,
            "CallBackURL": cfg["DARAJA_CALLBACK_URL"],
            "AccountReference": reference,
            "TransactionDesc": "SACCO deposit",
        },
        _bearer(creds.key, creds.secret),
    )
    if reply.get("ResponseCode") != "0" or not reply.get("CheckoutRequestID"):
        raise DarajaError("M-Pesa could not start the payment.")
    return reply
