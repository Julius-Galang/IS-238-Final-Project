"""Lambda #2 – process new S3 emails, summarize, and notify Telegram.

Refactored version of the original lambda2_process_email.py:
- Triggered by S3 ObjectCreated events
- Parses the email and sends it to Telegram
- Now uses:
  * shared.config for env + table names
  * shared.s3_utils for S3 access
  * shared.dynamodb for Emails table
  * shared.telegram for Telegram Bot token
  * Optional OpenAI summary
  * Download link + 'Disable this address' button
"""

from __future__ import annotations

import datetime as dt
import email
import logging
import os
from typing import Any

import requests

from shared import config, dynamodb, s3_utils, telegram

logger = logging.getLogger(__name__)
logger.setLevel(os.getenv("LOG_LEVEL", "INFO"))

OPENAI_API_URL = os.getenv("OPENAI_API_URL", "https://api.openai.com/v1/chat/completions")


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Entry point for S3 event → summarize → Telegram."""
    cfg = config.get_config()
    logger.info("Email processor invoked", extra={"event": event})

    records = event.get("Records") or []
    processed = 0

    for record in records:
        try:
            # triggered by S3
            if record.get("eventSource") == "aws:s3":
                bucket = record["s3"]["bucket"]["name"]
                key = record["s3"]["object"]["key"]
                if _handle_s3_email(cfg, bucket, key):
                    processed += 1
        except Exception as exc: 
            logger.exception("Failed to handle S3 record", extra={"error": str(exc)})
    return {"processed": processed}


def _handle_s3_email(cfg: config.RuntimeConfig, bucket: str, key: str) -> bool:
    logger.info("Processing S3 object", extra={"bucket": bucket, "key": key})

    # Inferred alias/message ids from key; here formalized:
    alias_id, message_id = _extract_ids_from_key(key)
    if not alias_id or not message_id:
        logger.warning("Could not parse alias/message ids from key", extra={"key": key})
        return False

    if not cfg.emails_table:
        logger.error("Emails table not configured")
        return False

    # Emails table rather than a generic DYNAMODB_TABLE
    email_record = dynamodb.get_item(cfg.emails_table, {"message_id": message_id})
    if not email_record:
        logger.warning("Email metadata not found", extra={"message_id": message_id})
        return False

    # Avoiding reprocess the same email
    if email_record.get("state") == "PROCESSED":
        logger.info("Email already processed", extra={"message_id": message_id})
        return False

    telegram_chat_id = email_record.get("telegram_chat_id")
    if not telegram_chat_id:
        logger.warning("Missing telegram_chat_id on email record", extra={"message_id": message_id})
        return False

    # === S3 → get raw email (in place of the original: s3.get_object) ===
    raw_bytes = s3_utils.get_raw_email(bucket, key)

    # Parse and extract subject/body
    msg = email.message_from_bytes(raw_bytes)
    subject = msg.get("Subject", "(no subject)")
    body_text = _extract_body_text(msg)

    # === Summarize via OpenAI (new, but fits the same flow) ===
    summary = _summarize_email(subject, body_text)

    # Mark as processed and store summary_sent_at
    now_iso = dt.datetime.now(dt.timezone.utc).isoformat()
    dynamodb.update_item(
        cfg.emails_table,
        {"message_id": message_id},
        UpdateExpression="SET #state = :state, summary_sent_at = :ts",
        ExpressionAttributeNames={"#state": "state"},
        ExpressionAttributeValues={":state": "PROCESSED", ":ts": now_iso},
    )

    # Build download URL exposed via Lambda #3 (/email/{aliasId}/{messageId})
    base_url = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
    if not base_url:
        logger.warning("PUBLIC_BASE_URL not set; download links will be missing")
        download_url = None
    else:
        download_url = f"{base_url}/email/{alias_id}/{message_id}"

    # === Send to Telegram (logic as original, but now via shared.telegram) ===
    _notify_telegram(cfg, telegram_chat_id, alias_id, subject, summary, download_url)
    return True


def _extract_ids_from_key(key: str) -> tuple[str | None, str | None]:
    """Extract aliasId + messageId from S3 key.

    Expected pattern (from Lambda #1):
      aliasId/YYYY/MM/DD/messageId-uuid.eml
    """
    parts = key.split("/")
    if len(parts) < 2:
        return None, None
    alias_id = parts[0]
    tail = parts[-1]
    msg_id_part = tail.split("-", 1)[0]
    return alias_id, msg_id_part or None


def _extract_body_text(msg: email.message.EmailMessage) -> str:
    """Prefer text/plain; fall back to simple HTML stripping (instead of html2text)."""
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = (part.get("Content-Disposition") or "").lower()
            if "attachment" in disp:
                continue
            if ctype == "text/plain":
                payload = part.get_payload(decode=True) or b""
                return payload.decode(part.get_content_charset() or "utf-8", "ignore")
        for part in msg.walk():
            ctype = part.get_content_type()
            if ctype == "text/html":
                payload = part.get_payload(decode=True) or b""
                html = payload.decode(part.get_content_charset() or "utf-8", "ignore")
                return _strip_html(html)
    else:
        payload = msg.get_payload(decode=True) or b""
        return payload.decode(msg.get_content_charset() or "utf-8", "ignore")
    return ""


def _strip_html(html: str) -> str:
    """Very small, dependency-free HTML → text helper to replace html2text."""
    import re

    text = re.sub(r"<(script|style).*?</\1>", "", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</p\s*>", "\n\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _summarize_email(subject: str, body: str) -> str:
    """Call OpenAI if OPENAI_API_KEY is set; otherwise return truncated body."""
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        logger.warning("OPENAI_API_KEY not set; returning truncated body instead of summary")
        return body[:1000]

    prompt = f"Summarize this email for a busy Telegram user.\n\nSubject: {subject}\n\nBody:\n{body}"
    payload = {
        "model": os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
        "messages": [
            {"role": "system", "content": "You are an assistant that writes short, clear email summaries."},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": 256,
        "temperature": 0.2,
    }

    try:
        resp = requests.post(
            OPENAI_API_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"].strip()
    except Exception as exc:
        logger.exception("OpenAI summarization failed", extra={"error": str(exc)})
        return body[:1000]


def _notify_telegram(
    cfg: config.RuntimeConfig,
    telegram_chat_id: str,
    alias_id: str,
    subject: str,
    summary: str,
    download_url: str | None,
) -> None:
    """Send summary to Telegram with a 'Disable this address' button."""
    token = telegram.get_bot_token(cfg.telegram_secret_arn)
    lines = [
        "*New email summary*",
        "",
        f"*Subject:* {subject}",
        "",
        "*Summary:*",
        summary,
    ]
    if download_url:
        lines.extend(["", f"[Download raw email]({download_url})"])

    text = "\n".join(lines)

    keyboard = {
        "inline_keyboard": [
            [
                {
                    "text": "Disable this address",
                    "callback_data": f"disable:{alias_id}",
                }
            ]
        ]
    }

    telegram.send_message(
        token,
        chat_id=int(telegram_chat_id),
        text=text,
        reply_markup=keyboard,
    )
