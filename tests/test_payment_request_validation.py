"""The input rules for a member's deposit or withdrawal request, no database."""

import pytest

from app.savings.request_validation import normalize_phone, parse_request
from app.savings.validation import InvalidRequest

GOOD_MPESA = {
    "kind": "deposit",
    "method": "mpesa",
    "amount": 1000,
    "reference": "ref-1",
    "phone": "0712 345 678",
}
GOOD_BANK = {
    "kind": "withdrawal",
    "method": "bank",
    "amount": 2500,
    "reference": "ref-2",
    "bank_name": "Equity Bank",
    "account_ref": "0123456789",
}


def test_phones_are_stored_in_one_form():
    for typed in ("0712345678", "0712 345 678", "+254 712 345 678", "254712345678"):
        assert normalize_phone(typed) == "254712345678"
    assert normalize_phone("0112345678") == "254112345678"


@pytest.mark.parametrize("typed", [None, "", "0812345678", "071234567", "abc"])
def test_a_bad_phone_is_refused(typed):
    with pytest.raises(InvalidRequest):
        normalize_phone(typed)


def test_a_clean_request_keeps_only_the_fields_of_its_method():
    mpesa = parse_request(GOOD_MPESA)
    assert mpesa["phone"] == "254712345678"
    assert mpesa["bank_name"] is None and mpesa["account_ref"] is None
    bank = parse_request(GOOD_BANK)
    assert bank["bank_name"] == "Equity Bank" and bank["phone"] is None


@pytest.mark.parametrize("kind", [None, "loan", "Deposit"])
def test_kind_must_be_deposit_or_withdrawal(kind):
    with pytest.raises(InvalidRequest, match="kind must be"):
        parse_request({**GOOD_MPESA, "kind": kind})


@pytest.mark.parametrize("method", [None, "cash", "MPESA"])
def test_method_must_be_mpesa_or_bank(method):
    with pytest.raises(InvalidRequest, match="method must be"):
        parse_request({**GOOD_MPESA, "method": method})


@pytest.mark.parametrize("field", ["bank_name", "account_ref"])
def test_bank_details_are_required_text(field):
    body = {**GOOD_BANK, field: 12345}
    with pytest.raises(InvalidRequest, match=f"{field} is required."):
        parse_request(body)
    missing = {key: value for key, value in GOOD_BANK.items() if key != field}
    with pytest.raises(InvalidRequest, match=f"{field} is required."):
        parse_request(missing)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("bank_name", "E"),
        ("bank_name", "x" * 61),
        ("account_ref", "12"),
        ("account_ref", "9" * 41),
        ("bank_name", "   "),
    ],
)
def test_bank_details_have_a_length_range(field, value):
    with pytest.raises(InvalidRequest, match="characters"):
        parse_request({**GOOD_BANK, field: value})
