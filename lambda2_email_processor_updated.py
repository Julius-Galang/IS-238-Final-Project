"""Lambda #2 – Fixed to work with corrected telegram.py"""

from __future__ import annotations

import datetime as dt
import email
import logging
import os
from typing import Any

from shared import config, dynamodb, s3_utils, telegram

logger = logging.getLogger(__name__)
logger.setLevel(os.getenv("LOG_LEVEL", "INFO"))


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Entry point for S3 event → summarize → Telegram."""
    cfg = config.get_config()
    logger.info("Email processor invoked")

    for record in event.get("Records", []):
        if record.get("eventSource") == "aws:s3":
            bucket = record["s3"]["bucket"]["name"]
            key = record["s3"]["object"]["key"]
            
            # DEBUG
            logger.info(f"Processing S3: {bucket}/{key}")
            _process_single_email(cfg, bucket, key)
    
    return {"status": "processed"}


def _process_single_email(cfg: config.RuntimeConfig, bucket: str, key: str):
    """Process a single email."""
    try:
        # Extract IDs
        parts = key.split("/")
        alias_id = parts[0] if len(parts) > 0 else None
        filename = parts[-1] if parts else ""
        message_id = filename.split("-")[0].split(".")[0]
        
        if not alias_id or not message_id:
            logger.error(f"Could not extract IDs from key: {key}")
            return
        
        logger.info(f"Processing: alias={alias_id}, message={message_id}")
        
        # Get email record
        email_record = dynamodb.get_item(cfg.emails_table, {"message_id": message_id})
        if not email_record:
            logger.error(f"No email record found for {message_id}")
            return
        
        # Check state
        if email_record.get("state") == "PROCESSED":
            logger.info(f"Already processed: {message_id}")
            return
        
        # Get chat ID
        telegram_chat_id = email_record.get("telegram_chat_id")
        if not telegram_chat_id:
            logger.error(f"No telegram_chat_id for {message_id}")
            return
        
        logger.info(f"Chat ID: {telegram_chat_id} (type: {type(telegram_chat_id)})")
        
        # Parse email
        raw_bytes = s3_utils.get_raw_email(bucket, key)
        msg = email.message_from_bytes(raw_bytes)
        subject = msg.get("Subject", "(no subject)")
        
        # Get body
        body = _extract_body(msg)
        
        # Create download URL
        base_url = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
        download_url = f"{base_url}/email/{alias_id}/{message_id}" if base_url else None
        
        # Send to Telegram
        success = _send_telegram_notification(
            cfg, telegram_chat_id, alias_id, subject, body, download_url
        )
        
        if success:
            # Mark as processed
            now_iso = dt.datetime.now(dt.timezone.utc).isoformat()
            dynamodb.update_item(
                cfg.emails_table,
                {"message_id": message_id},
                UpdateExpression="SET #state = :state, processed_at = :ts",
                ExpressionAttributeNames={"#state": "state"},
                ExpressionAttributeValues={":state": "PROCESSED", ":ts": now_iso},
            )
            logger.info(f"✅ Successfully sent to Telegram and marked as PROCESSED")
        else:
            logger.error(f"❌ Failed to send to Telegram for {message_id}")
            
    except Exception as e:
        logger.exception(f"Error processing email: {e}")


def _extract_body(msg: email.message.EmailMessage) -> str:
    """Extract text body from email."""
    body = ""
    
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    body = payload.decode('utf-8', 'ignore')
                    break
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            body = payload.decode('utf-8', 'ignore')
    
    # Truncate
    if len(body) > 1000:
        body = body[:1000] + "..."
    
    return body


def _send_telegram_notification(
    cfg: config.RuntimeConfig,
    telegram_chat_id: str,
    alias_id: str,
    subject: str,
    body: str,
    download_url: str | None,
) -> bool:
    """Send notification to Telegram. Returns True if successful."""
    try:
        # Get token
        token = telegram.get_bot_token(cfg.telegram_secret_arn)
        logger.info("Got Telegram token")
        
        # Convert chat_id to int
        try:
            chat_id_int = int(telegram_chat_id)
        except ValueError:
            logger.error(f"Invalid chat_id (not a number): {telegram_chat_id}")
            return False
        
        # Build message
        text = f"📧 *New Email*\n\n*Subject:* {subject}\n\n{body}"
        
        if download_url:
            text += f"\n\n[📎 Download Email]({download_url})"
        
        # Build keyboard
        keyboard = {
            "inline_keyboard": [[
                {"text": "Disable this address", "callback_data": f"disable:{alias_id}"}
            ]]
        }
        
        # Send message
        logger.info(f"Sending to Telegram chat_id: {chat_id_int}")
        success = telegram.send_message(
            token=token,
            chat_id=chat_id_int,
            text=text,
            parse_mode="Markdown",
            reply_markup=keyboard
        )
        
        return success
        
    except Exception as e:
        logger.exception(f"Error in Telegram notification: {e}")
        return False
