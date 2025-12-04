"""Lambda entry point for Telegram webhook and email download redirects.

This replaces the older version that directly used:
- DYNAMODB_TABLE
- EMAIL_DOMAIN
- TELEGRAM_BOT_TOKEN

It now uses:
- shared.config for looking up table names, bucket, and secret ARNs
- shared.dynamodb helpers for reading/writing DynamoDB
- shared.cloudflare to create/disable aliases
- shared.s3_utils to generate pre-signed URLs
- shared.telegram to send messages via Telegram

Also adds an HTTP GET endpoint to generate pre-signed S3 download links
for raw emails (used by the "Download email" button in summaries).
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import secrets as secrets_lib
from typing import Any
from urllib import request as urlrequest 

from shared import cloudflare, config, dynamodb, s3_utils, telegram

logger = logging.getLogger(__name__)
logger.setLevel(os.getenv("LOG_LEVEL", "INFO"))

# For the callback-query acknowledgement
TELEGRAM_API_BASE = "https://api.telegram.org"


# ===== Small helper, Telegram HTTP calls (no 'requests') ==================


def _telegram_api_post(token: str, method: str, payload: dict[str, Any]) -> None:
    """
    Call Telegram Bot API using only the Python standard library.

    Example:
      _telegram_api_post(token, "answerCallbackQuery", {"callback_query_id": "123"})
    """
    url = f"{TELEGRAM_API_BASE}/bot{token}/{method}"
    data = json.dumps(payload).encode("utf-8")
    req = urlrequest.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urlrequest.urlopen(req, timeout=10) as resp:
            # We don't need the response body; just read it to complete the request.
            resp.read()
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "Telegram API call failed",
            extra={"method": method, "error": str(exc)},
        )


# ===== Main Lambda entrypoint ===========


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """
    Single Lambda entrypoint.

    - POST /telegram/webhook    → Telegram updates (messages + callback buttons)
    - GET  /email/{aliasId}/{messageId} → redirect to S3 pre-signed URL
    """
    cfg = config.get_config()

    request_ctx = event.get("requestContext", {}).get("http", {}) or {}
    path = request_ctx.get("path", "") or ""
    method = (request_ctx.get("method") or "GET").upper()

    # 1) Telegram webhook (JSON POST from Telegram)
    if path.endswith("/telegram/webhook") and method == "POST":
        return _handle_telegram_update(cfg, event)

    # 2) Download redirect (user clicks "Download email" link in Telegram)
    if path.startswith("/email/") and method == "GET":
        return _handle_email_download(cfg, event)

    # Anything else → 404
    return {"statusCode": 404, "body": "Not Found"}


# ===== Telegram update routing ===============


def _handle_telegram_update(cfg: config.RuntimeConfig, event: dict[str, Any]) -> dict[str, Any]:
    """Parse Telegram update payload and route to message vs callback logic."""
    body = event.get("body") or "{}"
    try:
        update = json.loads(body)
    except json.JSONDecodeError:
        logger.warning("Telegram webhook payload is not valid JSON", extra={"body": body})
        # Always return 200 so Telegram does not keep retrying
        return {"statusCode": 200, "body": "ignored"}

    # Get bot token from Secrets Manager via shared.telegram helper
    bot_token = telegram.get_bot_token(cfg.telegram_secret_arn)

    if "message" in update:
        _handle_message(cfg, bot_token, update["message"])
    elif "callback_query" in update:
        _handle_callback_query(cfg, bot_token, update["callback_query"])

    # Telegram only needs 200 OK
    return {"statusCode": 200, "body": "ok"}


def _handle_message(cfg: config.RuntimeConfig, token: str, message: dict[str, Any]) -> None:
    """
    Handle normal text messages.

    Supported commands (we accept several aliases so your groupmates' old
    commands still work):
      - /start          → greet + show current aliases
      - /list           → show aliases
      - /aliases        → same as /list
      - /register       → create new alias
      - /newemail       → same as /register
      - /create         → same as /register
      - /deactivate X   → disable alias X
      - /disable X      → same as /deactivate X
    """
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    if not chat_id:
        return

    # Ensure there is a user record in the users table
    user_record = _ensure_user(cfg, chat_id, message)

    text = (message.get("text") or "").strip()
    lower = text.lower()

    # ----- list / start -----
    if lower.startswith("/start") or lower.startswith("/list") or lower.startswith("/aliases"):
        _send_alias_overview(cfg, token, chat_id, user_record)
        return

    # ----- register / newemail / create -----
    if lower.startswith("/register") or lower.startswith("/newemail") or lower.startswith("/create"):
        _create_alias_flow(cfg, token, chat_id)
        return

    # ----- deactivate / disable + argument -----
    if lower.startswith("/deactivate") or lower.startswith("/disable"):
        parts = text.split()
        if len(parts) < 2:
            telegram.send_message(
                token,
                chat_id=chat_id,
                text="Usage: /deactivate <alias-id or full-email>",
            )
            return
        alias_input = parts[1]
        alias_id = _normalize_alias_input(alias_input)
        _disable_alias_flow(cfg, token, chat_id, alias_id)
        return

    # Fallback help text
    telegram.send_message(
        token,
        chat_id=chat_id,
        text=(
            "Available commands:\n"
            "• /list – show your email aliases\n"
            "• /register – create a new alias\n"
            "• /deactivate <alias-id> – disable an alias"
        ),
    )


def _handle_callback_query(cfg: config.RuntimeConfig, token: str, payload: dict[str, Any]) -> None:
    """
    Handle button presses from inline keyboards.

    We support callback data formats like:
      - disable:abcd1234
      - deactivate:abcd1234
      - deactivate:abcd1234@your-domain.com
    """
    data = payload.get("data", "") or ""
    chat_id = payload.get("message", {}).get("chat", {}).get("id")

    if chat_id and ":" in data:
        action, value = data.split(":", 1)
        if action in ("disable", "deactivate"):
            alias_id = _normalize_alias_input(value)
            _disable_alias_flow(cfg, token, chat_id, alias_id)

    # Always answer the callback so Telegram stops the "loading" spinner
    callback_id = payload.get("id")
    if callback_id:
        _telegram_api_post(token, "answerCallbackQuery", {"callback_query_id": callback_id})


# ===== User and alias helpers =================


def _send_alias_overview(
    cfg: config.RuntimeConfig,
    token: str,
    chat_id: int,
    user_record: dict[str, Any] | None,
) -> None:
    """Send a message listing all aliases owned by this Telegram chat."""
    aliases = _list_aliases(cfg, chat_id)

    if aliases:
        lines = []
        if user_record and user_record.get("first_name"):
            lines.append(f"Hi {user_record['first_name']}!")
        lines.append("Your current email aliases:")
        for alias in aliases:
            status = alias.get("status", "UNKNOWN")
            email_address = alias.get("email_address") or f"{alias.get('alias_id')}@?"
            lines.append(f"- {email_address} ({status})")
        lines.append("\nUse /register to create a new alias.")
        lines.append("Use /deactivate <alias-id> to disable one.")
    else:
        lines = [
            "You have no aliases yet.",
            "Use /register to generate a new email address.",
        ]

    telegram.send_message(token, chat_id=chat_id, text="\n".join(lines))


def _create_alias_flow(cfg: config.RuntimeConfig, token: str, chat_id: int) -> None:
    """Generate a new Cloudflare alias + DynamoDB record for this Telegram user."""
    if not cfg.aliases_table:
        telegram.send_message(token, chat_id=chat_id, text="Alias table not configured.")
        return

    try:
        alias_record = _provision_alias(cfg, chat_id)
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception("Alias creation failed", extra={"chat_id": chat_id, "error": str(exc)})
        telegram.send_message(token, chat_id=chat_id, text="Could not create alias. Please try again later.")
        return

    telegram.send_message(
        token,
        chat_id=chat_id,
        text=(
            "New email alias created!\n"
            f"Address: `{alias_record['email_address']}`\n\n"
            "Send or forward emails to this address to receive summaries here."
        ),
        parse_mode="Markdown",
    )


def _disable_alias_flow(cfg: config.RuntimeConfig, token: str, chat_id: int, alias_id: str) -> None:
    """
    Disable an alias:

    - checks that the alias belongs to this chat
    - disables Cloudflare routing rule
    - updates DynamoDB status = DISABLED
    """
    if not cfg.aliases_table:
        telegram.send_message(token, chat_id=chat_id, text="Alias table not configured.")
        return

    alias_record = dynamodb.get_item(cfg.aliases_table, {"alias_id": alias_id})
    if not alias_record or alias_record.get("telegram_chat_id") != str(chat_id):
        telegram.send_message(token, chat_id=chat_id, text="Alias not found.")
        return

    if alias_record.get("status") == "DISABLED":
        telegram.send_message(token, chat_id=chat_id, text="Alias is already disabled.")
        return

    # 1) Disable Cloudflare rule (catch-all routing for this alias)
    rule_id = alias_record.get("cloudflare_rule_id")
    if rule_id:
        try:
            cloudflare.disable_alias(cfg.cloudflare_secret_arn, rule_id)
        except Exception as exc:  
            logger.warning(
                "Failed to disable Cloudflare rule",
                extra={"alias_id": alias_id, "error": str(exc)},
            )

    # 2) Update DynamoDB record
    now_iso = dt.datetime.now(dt.timezone.utc).isoformat()
    dynamodb.update_item(
        cfg.aliases_table,
        {"alias_id": alias_id},
        UpdateExpression="SET status = :status, disabled_at = :ts",
        ExpressionAttributeValues={":status": "DISABLED", ":ts": now_iso},
    )

    telegram.send_message(token, chat_id=chat_id, text=f"Alias `{alias_id}` disabled.", parse_mode="Markdown")


def _list_aliases(cfg: config.RuntimeConfig, chat_id: int) -> list[dict[str, Any]]:
    """Query aliases owned by this Telegram chat ID using a GSI."""
    if not cfg.aliases_table:
        return []
    # shared.dynamodb should have a helper for this (used also by Lambda #1)
    return dynamodb.query_aliases_by_chat(cfg.aliases_table, str(chat_id))


def _ensure_user(
    cfg: config.RuntimeConfig,
    chat_id: int,
    message: dict[str, Any],
) -> dict[str, Any] | None:
    """
    Ensure we have a user record in the users table.

    Primary key: telegram_chat_id (string)
    """
    if not cfg.users_table:
        return None

    chat_id_str = str(chat_id)
    existing = dynamodb.get_item(cfg.users_table, {"telegram_chat_id": chat_id_str})
    if existing:
        return existing

    user_meta = message.get("from", {}) or {}
    now_iso = dt.datetime.now(dt.timezone.utc).isoformat()

    item = {
        "telegram_chat_id": chat_id_str,
        "username": user_meta.get("username"),
        "first_name": user_meta.get("first_name"),
        "last_name": user_meta.get("last_name"),
        "locale": user_meta.get("language_code"),
        "status": "ACTIVE",
        "created_at": now_iso,
    }
    dynamodb.upsert_item(cfg.users_table, item)
    return item


def _provision_alias(cfg: config.RuntimeConfig, chat_id: int) -> dict[str, Any]:
    """
    Create a Cloudflare routing rule for a new alias and store its metadata.

    - alias_id becomes the local-part (before @) that we control
    - Cloudflare returns the full email (e.g., abcd1234@your-domain.com)
    - We persist everything in the aliases table
    """
    alias_id = _generate_alias_id()

    # Guard against collision (very unlikely, but safe)
    existing = dynamodb.get_item(cfg.aliases_table, {"alias_id": alias_id})
    if existing:
        return _provision_alias(cfg, chat_id)

    # Ask Cloudflare to create a rule for this alias
    cf_result = cloudflare.create_alias(cfg.cloudflare_secret_arn, alias_id)
    email_address = cf_result.get("name")  # full email, e.g., abcd1234@domain.com
    rule_id = cf_result.get("id")         # Cloudflare rule ID

    now_iso = dt.datetime.now(dt.timezone.utc).isoformat()
    record = {
        "alias_id": alias_id,
        "email_address": email_address,
        "telegram_chat_id": str(chat_id),
        "status": "ACTIVE",
        "cloudflare_rule_id": rule_id,
        "created_at": now_iso,
    }
    dynamodb.upsert_item(cfg.aliases_table, record)
    return record


def _generate_alias_id(length: int = 8) -> str:
    """Short random identifier (used as email local-part)."""
    alphabet = "abcdefghijklmnopqrstuvwxyz0123456789"
    return "".join(secrets_lib.choice(alphabet) for _ in range(length))


def _normalize_alias_input(value: str) -> str:
    """
    Convert user input into a clean alias_id.

    Accepts:
      - "abcd1234"                → "abcd1234"
      - "abcd1234@domain.com"     → "abcd1234"
    """
    value = value.strip().lower()
    if "@" in value:
        value = value.split("@", 1)[0]
    return value


# ===== Download redirect handler =================


def _handle_email_download(cfg: config.RuntimeConfig, event: dict[str, Any]) -> dict[str, Any]:
    """
    Handle GET /email/{aliasId}/{messageId}.

    This is used by the "Download Raw Email" link that Lambda #2 embeds in the
    Telegram summary. Flow:

      1. API Gateway forwards the HTTP request with path parameters.
      2. We look up the email in the emails table by message_id.
      3. Check that the alias_id matches (simple safety check).
      4. Use shared.s3_utils.generate_presigned_url() to get a temporary URL.
      5. Return HTTP 302 redirect to that pre-signed URL.
    """
    path_params = event.get("pathParameters") or {}
    alias_id = path_params.get("aliasId")
    message_id = path_params.get("messageId")

    if not alias_id or not message_id:
        return {"statusCode": 400, "body": "Missing aliasId or messageId"}

    if not cfg.emails_table:
        return {"statusCode": 500, "body": "Emails table not configured"}

    # Load email record (Lambda #1 and #2 write to this table)
    record = dynamodb.get_item(cfg.emails_table, {"message_id": message_id})
    if not record or record.get("alias_id") != alias_id:
        return {"statusCode": 404, "body": "Not Found"}

    key = record.get("s3_key")
    if not key:
        return {"statusCode": 500, "body": "Missing S3 key for email"}

    # Build pre-signed URL (expiration is handled by shared.s3_utils)
    url = s3_utils.generate_presigned_url(cfg.raw_email_bucket, key)

    return {
        "statusCode": 302,
        "headers": {"Location": url},
        "body": "",
    }
