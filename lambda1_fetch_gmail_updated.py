"""Lambda #1 – Fetch emails from Gmail with Telegram bot tracking."""

from __future__ import annotations

import datetime as dt
import imaplib
import json
import logging
import os
import uuid
from email import policy
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime
from typing import Any

import boto3

from shared import config, dynamodb, gmail_client, s3_utils, telegram

logger = logging.getLogger(__name__)
logger.setLevel(os.getenv("LOG_LEVEL", "INFO"))

EMAIL_TTL_SECONDS = 14 * 24 * 60 * 60  # 14 days


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Main Lambda handler to fetch and process unread Gmail emails."""
    cfg = config.get_config()
    logger.info("Starting Gmail email fetch")

    try:
        # Get current bot info
        bot_info = _get_current_bot_info(cfg)
        bot_username = bot_info.get("username", "unknown")
        bot_id = bot_info.get("id", "unknown")
        logger.info(f"Current bot: @{bot_username} (ID: {bot_id})")

        # Fetch emails
        client = gmail_client.GmailClient(cfg.gmail_secret_arn, cfg.gmail_processed_label)
        messages = client.fetch_unread()
        
        processed = 0
        for message in messages:
            if _process_single_email(cfg, message, bot_info):
                processed += 1

        logger.info("Email fetch completed", extra={"processed": processed, "total": len(messages)})
        return {
            "statusCode": 200,
            "body": json.dumps({
                "processed": processed,
                "total": len(messages),
                "bot": bot_username,
                "bot_id": bot_id
            })
        }

    except Exception as exc:
        logger.exception("Failed to fetch emails", extra={"error": str(exc)})
        return {
            "statusCode": 500,
            "body": json.dumps({"error": str(exc)})
        }


def _process_single_email(
    cfg: config.RuntimeConfig, 
    message: gmail_client.GmailMessage, 
    bot_info: dict
) -> bool:
    """Process a single email and save to S3 + DynamoDB."""
    try:
        # Parse email
        email_obj = BytesParser(policy=policy.default).parsebytes(message.raw_email)
        
        # Extract alias
        alias_id, alias_address = _extract_alias(email_obj)
        if not alias_id:
            logger.warning("Unable to determine alias for message", extra={"gmail_uid": message.uid})
            return False

        logger.info(f"Found alias: {alias_id} ({alias_address})")

        # Get alias record
        alias_record = dynamodb.get_item(cfg.aliases_table, {"alias_id": alias_id})
        if not alias_record:
            logger.error(f"Alias not found in database: {alias_id}")
            return False

        if alias_record.get("status") == "DISABLED":
            logger.info(f"Alias {alias_id} is disabled, skipping")
            return False

        # Get Telegram chat ID
        telegram_chat_id = alias_record.get("telegram_chat_id")
        if not telegram_chat_id:
            logger.error(f"Alias {alias_id} has no telegram_chat_id")
            return False

        # Get stored bot info for this alias
        stored_bot_id = alias_record.get("telegram_bot_id")
        current_bot_id = bot_info.get("id")
        
        # Check if alias needs migration to current bot
        needs_migration = bool(stored_bot_id and stored_bot_id != current_bot_id)
        
        # Generate message ID
        message_id = _generate_message_id(email_obj, message.uid)

        # Check for duplicates
        existing = dynamodb.get_item(cfg.emails_table, {"message_id": message_id})
        if existing:
            logger.info(f"Duplicate message detected: {message_id}")
            return False

        # Parse timestamp
        received_at = _parse_received_timestamp(email_obj)

        # Save to S3
        s3_key = _build_s3_key(alias_id, received_at, message_id)
        s3_utils.put_raw_email(
            cfg.raw_email_bucket, 
            s3_key, 
            message.raw_email,
            metadata={"alias_id": alias_id, "message_id": message_id}
        )

        # Save to DynamoDB with bot tracking
        ttl_expiry = int(received_at.timestamp()) + EMAIL_TTL_SECONDS
        
        email_item = {
            "message_id": message_id,
            "alias_id": alias_id,
            "telegram_chat_id": str(telegram_chat_id),
            "telegram_bot_id": current_bot_id,
            "telegram_bot_username": bot_info.get("username", ""),
            "recipient_email": alias_address or "",
            "from_email": email_obj.get("From", "")[:200],
            "subject": email_obj.get("Subject", "(no subject)")[:500],
            "s3_key": s3_key,
            "s3_bucket": cfg.raw_email_bucket,
            "received_at": received_at.isoformat(),
            "state": "PENDING",
            "needs_migration": needs_migration,
            "original_bot_id": stored_bot_id if needs_migration else current_bot_id,
            "ttl_expiry": ttl_expiry,
            "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        }

        dynamodb.upsert_item(cfg.emails_table, email_item)

        # Update alias last message timestamp
        _update_alias_timestamp(cfg, alias_id, received_at)

        logger.info(f"✅ Saved email {message_id} for alias {alias_id} (chat: {telegram_chat_id})")
        if needs_migration:
            logger.warning(f"⚠️ Email needs migration from bot {stored_bot_id} to {current_bot_id}")

        return True

    except Exception as exc:
        logger.exception(f"Failed to process email", extra={"error": str(exc)})
        return False


def _get_current_bot_info(cfg: config.RuntimeConfig) -> dict:
    """Get current bot information."""
    try:
        token = telegram.get_bot_token(cfg.telegram_secret_arn)
        return telegram.get_bot_info(token)
    except Exception as e:
        logger.error(f"Failed to get bot info: {e}")
        # Return minimal info to avoid crashes
        return {"id": "unknown", "username": "unknown", "is_bot": True}


def _extract_alias(email_obj) -> tuple[str | None, str | None]:
    """Extract alias from email headers."""
    candidate_headers = ["X-Original-To", "Delivered-To", "To", "Envelope-To"]
    
    for header in candidate_headers:
        value = email_obj.get(header)
        if not value:
            continue
            
        addresses = getaddresses([value])
        for _, address in addresses:
            if not address or "@" not in address:
                continue
                
            address_lower = address.lower()
            local_part = address_lower.split("@")[0]
            
            # Basic validation
            if local_part and len(local_part) >= 3:
                return local_part, address_lower
    
    return None, None


def _generate_message_id(email_obj, gmail_uid: str) -> str:
    """Generate unique message ID."""
    # Try to use Message-ID header
    msg_id = email_obj.get("Message-ID")
    if msg_id:
        # Sanitize Message-ID
        msg_id = msg_id.strip().strip("<>")
        msg_id = "".join(c for c in msg_id if c.isalnum() or c in ("-", "_", "."))
        if msg_id:
            return msg_id[:100]  # Limit length
    
    # Fallback to Gmail UID with timestamp
    timestamp = dt.datetime.now().strftime("%Y%m%d%H%M%S")
    return f"gmail-{gmail_uid}-{timestamp}"


def _parse_received_timestamp(email_obj) -> dt.datetime:
    """Parse email timestamp."""
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
    """Build S3 key path."""
    # Sanitize message_id for filename
    safe_message_id = "".join(c for c in message_id if c.isalnum() or c in ("-", "_"))
    date_prefix = received_at.strftime("%Y/%m/%d")
    unique_suffix = uuid.uuid4().hex[:8]
    
    return f"{alias_id}/{date_prefix}/{safe_message_id}-{unique_suffix}.eml"


def _update_alias_timestamp(cfg: config.RuntimeConfig, alias_id: str, timestamp: dt.datetime) -> None:
    """Update last_message_at timestamp for alias."""
    try:
        dynamodb.update_item(
            cfg.aliases_table,
            {"alias_id": alias_id},
            UpdateExpression="SET last_message_at = :ts",
            ExpressionAttributeValues={":ts": timestamp.isoformat()},
        )
    except Exception as exc:
        logger.warning(f"Failed to update alias timestamp: {exc}")
