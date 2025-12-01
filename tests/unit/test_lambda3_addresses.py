import pytest

# NOTE:
#
# Lambda #3 (Telegram webhook)
# expected behavior regarding address management:
# - /register: create a new random email address for a Telegram user
# - /list: show all addresses for that user
# - /deactivate: mark one address as inactive
#
# Tests are marked as "skipped" so that they don't fail CI
# until Lambda #3 and DynamoDB integration are ready.
#
# Future import (adjust path and names as needed):
# from lambda3_telegram_webhook import handle_register, handle_list, handle_deactivate

pytestmark = pytest.mark.skip(
    reason=(
        "Lambda #3 address management behaviour is not wired to tests yet. "
        "Remove this skip when the real implementation is ready."
    )
)


def test_register_creates_new_address_for_user():
    """
    /register should create a new, unique email address for a Telegram user.

    High-level expectations (from Problem Specs + Test Plan):
    - Address format follows company domain.
    - New address is stored with active = true for that Telegram user.
    """
    telegram_user_id = 123456789

    # When ready, call the real handler function instead of this placeholder:
    # result = handle_register(telegram_user_id, dynamodb_table=...)
    result = {
        "telegramUserId": telegram_user_id,
        "emailAddress": "random123@companydomain.com",
        "active": True,
    }

    assert result["telegramUserId"] == telegram_user_id
    assert result["active"] is True
    assert "@" in result["emailAddress"]


def test_list_returns_all_addresses_for_user():
    """
    /list should return all addresses currently associated with a Telegram user.

    Expectations:
    - Includes both active and inactive addresses.
    - Each item indicates whether it is active.
    """
    telegram_user_id = 123456789

    # Placeholder idea of what the handler might return in the future
    addresses = [
        {"emailAddress": "addr1@companydomain.com", "active": True},
        {"emailAddress": "addr2@companydomain.com", "active": False},
    ]

    # Later: addresses = handle_list(telegram_user_id, dynamodb_table=...)

    assert len(addresses) == 2
    assert any(a["active"] for a in addresses)
    assert any(not a["active"] for a in addresses)


def test_deactivate_marks_address_inactive():
    """
    /deactivate should mark specific address as inactive for that user.

    Expectations:
    - The address is still present in the database, but active = false.
    - Further emails to that address should not generate new summaries
      (this is verified by higher-level tests).
    """
    telegram_user_id = 123456789
    address_to_deactivate = "addr1@companydomain.com"

    # Placeholder starting state
    record = {
        "telegramUserId": telegram_user_id,
        "emailAddress": address_to_deactivate,
        "active": True,
    }

    # Later: updated = handle_deactivate(telegram_user_id, address_to_deactivate, dynamodb_table=...)
    updated = {**record, "active": False}

    assert updated["telegramUserId"] == telegram_user_id
    assert updated["emailAddress"] == address_to_deactivate
    assert updated["active"] is False
