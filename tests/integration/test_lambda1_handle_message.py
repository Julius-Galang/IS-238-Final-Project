"""
Small integration test for Lambda 1 (_handle_message).

Goal:
- If an email comes in for an ACTIVE alias,
  then _handle_message should:
    * write one object to S3 (via s3_utils.put_raw_email)
    * write one record to the emails table in DynamoDB
    * return True
"""
import pathlib
import sys
import types
import datetime as dt
from email.message import EmailMessage

ROOT_DIR = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT_DIR))

# --- Make fake "shared" package so lambda_functions can import it ---
shared_module = types.ModuleType("shared")
shared_module.config = types.SimpleNamespace()
shared_module.dynamodb = types.SimpleNamespace()
shared_module.gmail_client = types.SimpleNamespace()
shared_module.s3_utils = types.SimpleNamespace()
sys.modules.setdefault("shared", shared_module)

import lambda_functions  # noqa: E402


class FakeConfig:
    """Very small config object for Lambda 1."""

    def __init__(self) -> None:
        self.aliases_table = "aliases"
        self.emails_table = "emails"
        self.raw_email_bucket = "raw-email-bucket"


class FakeDynamoDB:
    """In-memory imitation of the dynamodb helper."""

    def __init__(self) -> None:
        # alias_id to alias record
        self.alias_table: dict[str, dict] = {}
        # message_id to email record
        self.email_table: dict[str, dict] = {}
        self.update_calls: list[tuple] = []

    def get_item(self, table: str, key: dict):
        if table == "aliases":
            return self.alias_table.get(key["alias_id"])
        if table == "emails":
            return self.email_table.get(key["message_id"])
        return None

    def upsert_item(self, table: str, item: dict):
        if table == "emails":
            self.email_table[item["message_id"]] = item

    def update_item(self, table: str, key: dict, **kwargs):
        self.update_calls.append((table, key, kwargs))


class FakeS3Utils:
    """Record calls to put_raw_email instead of calling real S3."""

    def __init__(self) -> None:
        self.put_calls: list[dict] = []

    def put_raw_email(self, bucket: str, key: str, raw_email: bytes, metadata: dict):
        self.put_calls.append(
            {
                "bucket": bucket,
                "key": key,
                "raw_email": raw_email,
                "metadata": metadata,
            }
        )


class FakeGmailMessage:
    """Minimal Gmail message object used by _handle_message."""

    def __init__(self, uid: str, raw_email: bytes) -> None:
        self.uid = uid
        self.raw_email = raw_email


def _make_sample_email(alias_address: str) -> bytes:
    """Build simple RFC5322 email with headers Lambda 1 will read."""
    text = (
        "Message-ID: <IDABC-12@hostexample>\r\n"
        "Date: Sat, 01 Nov 2025 12:34:56 +0000\r\n"
        f"To: {alias_address}\r\n"
        "Subject: Hello from test\r\n"
        "\r\n"
        "This is the body of the test email.\r\n"
    )
    return text.encode("utf-8")


def test_handle_message_stores_email_for_active_alias():
    # Arrange: wire fake helpers into lambda_functions
    fake_dynamo = FakeDynamoDB()
    fake_s3 = FakeS3Utils()

    lambda_functions.dynamodb = fake_dynamo
    lambda_functions.s3_utils = fake_s3

    cfg = FakeConfig()
    alias_id = "testalias123"
    alias_address = f"{alias_id}@example.com"

    # Mark this alias ACTIVE in the fake aliases table
    fake_dynamo.alias_table[alias_id] = {
        "status": "ENABLED",
        "telegram_chat_id": 999999,
    }

    # Build fake Gmail message for this alias
    raw_email = _make_sample_email(alias_address)
    message = FakeGmailMessage(uid="GMAIL-UID-1", raw_email=raw_email)

    # Act: call the Lambda 1 core handler
    result = lambda_functions._handle_message(cfg, message)

    # Assert: handler reports success
    assert result is True

    # One object should have been "written" to S3
    assert len(fake_s3.put_calls) == 1
    put_call = fake_s3.put_calls[0]
    assert put_call["bucket"] == cfg.raw_email_bucket
    assert put_call["metadata"]["alias_id"] == alias_id

    # One email record should exist in the fake emails table
    assert len(fake_dynamo.email_table) == 1
    (message_id, email_record), = fake_dynamo.email_table.items()
    assert email_record["alias_id"] == alias_id
    assert email_record["state"] == "PENDING"

    # TTL should be in the future
    received_at = dt.datetime.fromisoformat(email_record["received_at"])
    assert email_record["ttl_expiry"] > received_at.timestamp()
