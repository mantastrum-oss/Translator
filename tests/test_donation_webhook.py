import pytest

from donation_webhook import parse_kofi_payload, validate_kofi_payload


VALID_PAYLOAD = {
    "verification_token": "secret",
    "type": "Donation",
    "currency": "EUR",
    "amount": "2.00",
    "kofi_transaction_id": "tx-123",
    "message": "Please credit Discord user 123456789012345678",
}


def test_valid_kofi_payload_maps_amount_and_user():
    normalized = validate_kofi_payload(VALID_PAYLOAD, "secret")

    assert normalized["transaction_id"] == "tx-123"
    assert normalized["user_id"] == "123456789012345678"
    assert normalized["amount"] == "2"
    assert normalized["chars_added"] == 100000


def test_form_encoded_data_is_decoded():
    payload = parse_kofi_payload({"data": '{"amount":"1.00"}'})

    assert payload["amount"] == "1.00"


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("currency", "USD", "unsupported_currency"),
        ("amount", "4.00", "unsupported_amount"),
        ("message", "no discord id", "missing_discord_user_id"),
        ("kofi_transaction_id", "", "missing_transaction_id"),
    ],
)
def test_invalid_donation_is_rejected(field, value, error):
    payload = dict(VALID_PAYLOAD)
    payload[field] = value

    with pytest.raises(ValueError, match=error):
        validate_kofi_payload(payload, "secret")


def test_invalid_verification_token_is_rejected():
    with pytest.raises(PermissionError, match="invalid_verification_token"):
        validate_kofi_payload(VALID_PAYLOAD, "wrong")
