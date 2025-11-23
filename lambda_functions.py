"""Lambda entry point for polling Gmail and storing messages in S3."""

from __future__ import annotations

import datetime as dt
import logging
import os
import uuid
from email import policy
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime
from typing import Any

from shared import config, dynamodb, gmail_client, s3_utils

logger = logging.getLogger(__name__)
logger.setLevel(os.getenv("LOG_LEVEL", "INFO"))

EMAIL_TTL_SECONDS = 14 * 24 * 60 * 60


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    # Main Lambda entry: poll Gmail, persist new messages, and fan out work
    cfg = config.get_config()
    logger.info("Starting Gmail ingestion run", extra={"event": event})

    client = gmail_client.GmailClient(cfg.gmail_secret_arn, cfg.gmail_processed_label)
    messages = client.fetch_unread()
    processed = 0

    for message in messages:
        try:
            processed += int(_handle_message(cfg, message))
        except Exception as exc:  # pragma: no cover - defensive logging
            logger.exception("Failed to process Gmail message", extra={"error": str(exc)})

    logger.info("Ingestion complete", extra={"count": processed})
    return {"processed": processed}


def _handle_message(cfg: config.RuntimeConfig, message: gmail_client.GmailMessage) -> bool:
    # Guardrails for alias ownership, dedupe, persistence, and metadata updates
    email_obj = BytesParser(policy=policy.default).parsebytes(message.raw_email)
    alias_id, alias_address = _extract_alias(email_obj)
    if not alias_id:
        logger.warning("Unable to determine alias for message", extra={"gmail_uid": message.uid})
        return False

    alias_record = dynamodb.get_item(cfg.aliases_table, {"alias_id": alias_id})
    if not alias_record or alias_record.get("status") == "DISABLED":
        logger.info("Alias missing or disabled; skipping", extra={"alias_id": alias_id, "gmail_uid": message.uid})
        return False

    message_id = _sanitize_message_id(email_obj.get("Message-ID")) or f"gmail-{message.uid}"
    existing = dynamodb.get_item(cfg.emails_table, {"message_id": message_id})
    if existing:
        logger.info("Duplicate message detected", extra={"message_id": message_id})
        return False

    received_at = _parse_received_timestamp(email_obj)
    key = _build_s3_key(alias_id, received_at, message_id)

    metadata = {
        "alias_id": alias_id,
        "message_id": message_id,
        "alias_address": alias_address or alias_id,
    }
    s3_utils.put_raw_email(cfg.raw_email_bucket, key, message.raw_email, metadata)

    ttl_expiry = int(received_at.timestamp()) + EMAIL_TTL_SECONDS
    dynamodb.upsert_item(
        cfg.emails_table,
        {
            "message_id": message_id,
            "alias_id": alias_id,
            "telegram_chat_id": alias_record.get("telegram_chat_id"),
            "s3_key": key,
            "state": "PENDING",
            "received_at": received_at.isoformat(),
            "ttl_expiry": ttl_expiry,
        },
    )

    dynamodb.update_item(
        cfg.aliases_table,
        {"alias_id": alias_id},
        UpdateExpression="SET last_message_at = :ts",
        ExpressionAttributeValues={":ts": received_at.isoformat()},
    )

    return True


def _extract_alias(email_obj) -> tuple[str | None, str | None]:
    # Heuristics for mapping incoming email headers to our generated alias id
    candidate_headers = ["X-Original-To", "Delivered-To", "To"]
    for header in candidate_headers:
        value = email_obj.get(header)
        if not value:
            continue
        for _, address in getaddresses([value]):
            if not address:
                continue
            local_part = address.split("@")[0].lower()
            if local_part:
                return local_part, address.lower()
    return None, None


def _sanitize_message_id(message_id: str | None) -> str | None:
    # Normalize Message-ID so it is safe for DynamoDB keys/S3 paths
    if not message_id:
        return None
    trimmed = message_id.strip().strip("<>")
    safe = "".join(ch for ch in trimmed if ch.isalnum() or ch in ("-", "_"))
    return safe or None


def _parse_received_timestamp(email_obj) -> dt.datetime:
    # Favor original Date header but fall back to now() if parsing fails
    header = email_obj.get("Date")
    if header:
        try:
            parsed = parsedate_to_datetime(header)
            if parsed and parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=dt.timezone.utc)
            if parsed:
                return parsed.astimezone(dt.timezone.utc)
        except (ValueError, TypeError):
            pass
    return dt.datetime.now(dt.timezone.utc)


def _build_s3_key(alias_id: str, received_at: dt.datetime, message_id: str) -> str:
    # Structured key keeps aliases partitioned and groups emails by day for cleanup
    date_prefix = received_at.strftime("%Y/%m/%d")
    unique_suffix = uuid.uuid4().hex
    return f"{alias_id}/{date_prefix}/{message_id}-{unique_suffix}.eml"
