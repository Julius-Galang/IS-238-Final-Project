"""Lambda entry point for Telegram webhook and email download redirects.

This Lambda handles:
- Telegram webhook messages (commands from users)
- Bot migration when users switch to new bot
- Email download redirects
- User registration and alias management
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import secrets as secrets_lib
from typing import Any
from urllib import request as urlrequest
from urllib import parse

import boto3

from shared import cloudflare, config, dynamodb, s3_utils, telegram

logger = logging.getLogger(__name__)
logger.setLevel(os.getenv("LOG_LEVEL", "INFO"))

# For the callback-query acknowledgement
TELEGRAM_API_BASE = "https://api.telegram.org"

# Initialize DynamoDB resource
_dynamodb = boto3.resource("dynamodb")


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


def _handle_telegram_update(cfg: config.RuntimeConfig, event: dict[str, Any]) -> dict[str, Any]:
    """Parse Telegram update payload and route to message vs callback logic."""
    body = event.get("body") or "{}"
    try:
        update = json.loads(body)
    except json.JSONDecodeError:
        logger.warning("Telegram webhook payload is not valid JSON", extra={"body": body})
        # Always return 200 so Telegram does not keep retrying
        return {"statusCode": 200, "body": "ignored"}

    # Get bot token from Secrets Manager
    bot_token = telegram.get_bot_token(cfg.telegram_secret_arn)

    if "message" in update:
        _handle_message(cfg, bot_token, update["message"])
    elif "callback_query" in update:
        _handle_callback_query(cfg, bot_token, update["callback_query"])

    # Telegram only needs 200 OK
    return {"statusCode": 200, "body": "ok"}


def _handle_message(cfg: config.RuntimeConfig, token: str, message: dict[str, Any]) -> None:
    """
    Handle normal text messages with bot migration support.

    Supported commands:
      - /start           → Welcome + bot migration
      - /list, /aliases  → Show current aliases
      - /register, /newemail, /create → Create new alias
      - /deactivate, /disable <alias> → Disable alias
    """
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    if not chat_id:
        return

    # Get current bot info
    try:
        bot_info = telegram.get_bot_info(token)
        current_bot_id = bot_info.get("id")
        current_bot_username = bot_info.get("username", "")
    except Exception as e:
        logger.error(f"Failed to get bot info: {e}")
        current_bot_id = "unknown"
        current_bot_username = "unknown"

    # Ensure user record exists with current bot info
    user_record = _ensure_user(cfg, chat_id, message, current_bot_id, current_bot_username)

    text = (message.get("text") or "").strip()
    lower = text.lower()

    # ----- /start command (CRITICAL for bot migration) -----
    if lower.startswith("/start"):
        # Update user with current bot info (even if they already exist)
        _update_user_with_bot_info(cfg, chat_id, current_bot_id, current_bot_username, message)
        
        # Migrate user's aliases to new bot
        _migrate_user_aliases_to_bot(cfg, str(chat_id), current_bot_id, current_bot_username)
        
        # Send welcome message
        telegram.send_message(
            token,
            chat_id=chat_id,
            text=(
                f"🤖 Welcome to @{current_bot_username}!\n\n"
                "✅ Your email aliases have been migrated to this bot.\n\n"
                "Available commands:\n"
                "• /list - Show your email aliases\n"
                "• /register - Create a new alias\n"
                "• /deactivate <alias> - Disable an alias\n\n"
                "Send emails to your aliases to receive AI-summarized versions here!"
            ),
            parse_mode="Markdown",
        )
        return

    # ----- list / aliases -----
    if lower.startswith("/list") or lower.startswith("/aliases"):
        _send_alias_overview(cfg, token, chat_id, user_record)
        return

    # ----- register / newemail / create -----
    if lower.startswith("/register") or lower.startswith("/newemail") or lower.startswith("/create"):
        _create_alias_flow(cfg, token, chat_id, current_bot_id, current_bot_username)
        return

    # ----- deactivate / disable + argument -----
    if lower.startswith("/deactivate") or lower.startswith("/disable"):
        parts = text.split()
        if len(parts) < 2:
            telegram.send_message(
                token,
                chat_id=chat_id,
                text="Usage: /deactivate <alias-id or full-email>\nExample: /deactivate abc123 or /deactivate abc123@domain.com",
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
            "• /deactivate <alias-id> – disable an alias\n\n"
            "Need help? Just type /start to begin."
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
    message_id = payload.get("message", {}).get("message_id")

    if chat_id and ":" in data:
        action, value = data.split(":", 1)
        if action in ("disable", "deactivate"):
            alias_id = _normalize_alias_input(value)
            _disable_alias_flow(cfg, token, chat_id, alias_id)

    # Always answer the callback so Telegram stops the "loading" spinner
    callback_id = payload.get("id")
    if callback_id:
        _telegram_api_post(token, "answerCallbackQuery", {"callback_query_id": callback_id})


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
            lines.append(f"Hi {user_record['first_name']}! 👋")
        lines.append("Your current email aliases:")
        
        for alias in aliases:
            status = alias.get("status", "UNKNOWN")
            email_address = alias.get("email_address") or f"{alias.get('alias_id')}@?"
            
            # Add emoji based on status
            if status == "ACTIVE":
                status_emoji = "✅"
            elif status == "DISABLED":
                status_emoji = "❌"
            else:
                status_emoji = "❓"
            
            lines.append(f"{status_emoji} `{email_address}` ({status})")
        
        lines.append("\nUse /register to create a new alias.")
        lines.append("Use /deactivate <alias-id> to disable one.")
    else:
        lines = [
            "You have no aliases yet. 😔",
            "Use /register to generate a new email address.",
            "",
            "Once created, you can:",
            "• Send emails to your alias",
            "• Receive AI-summarized versions here",
            "• Download original emails when needed"
        ]

    telegram.send_message(token, chat_id=chat_id, text="\n".join(lines), parse_mode="Markdown")


def _create_alias_flow(
    cfg: config.RuntimeConfig, 
    token: str, 
    chat_id: int,
    bot_id: str,
    bot_username: str
) -> None:
    """Generate a new Cloudflare alias + DynamoDB record for this Telegram user."""
    if not cfg.aliases_table:
        telegram.send_message(token, chat_id=chat_id, text="❌ Alias table not configured.")
        return

    try:
        alias_record = _provision_alias(cfg, chat_id, bot_id, bot_username)
    except Exception as exc:
        logger.exception("Alias creation failed", extra={"chat_id": chat_id, "error": str(exc)})
        telegram.send_message(token, chat_id=chat_id, text="❌ Could not create alias. Please try again later.")
        return

    telegram.send_message(
        token,
        chat_id=chat_id,
        text=(
            "🎉 New email alias created!\n\n"
            f"**Address:** `{alias_record['email_address']}`\n\n"
            "📧 **How to use:**\n"
            "1. Send or forward emails to this address\n"
            "2. Receive AI-summarized versions here\n"
            "3. Download original emails when needed\n\n"
            "⚙️ **Manage:** Use /deactivate to disable this alias."
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
        telegram.send_message(token, chat_id=chat_id, text="❌ Alias table not configured.")
        return

    alias_record = dynamodb.get_item(cfg.aliases_table, {"alias_id": alias_id})
    if not alias_record or alias_record.get("telegram_chat_id") != str(chat_id):
        telegram.send_message(token, chat_id=chat_id, text="❌ Alias not found.")
        return

    if alias_record.get("status") == "DISABLED":
        telegram.send_message(token, chat_id=chat_id, text="ℹ️ Alias is already disabled.")
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
        UpdateExpression="SET #status = :status, disabled_at = :ts",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={":status": "DISABLED", ":ts": now_iso},
    )

    telegram.send_message(
        token, 
        chat_id=chat_id, 
        text=f"✅ Alias `{alias_id}` has been disabled.", 
        parse_mode="Markdown"
    )


def _list_aliases(cfg: config.RuntimeConfig, chat_id: int) -> list[dict[str, Any]]:
    """Query aliases owned by this Telegram chat ID."""
    if not cfg.aliases_table:
        return []
    
    try:
        # Query using GSI on telegram_chat_id
        table = _dynamodb.Table(cfg.aliases_table)
        response = table.query(
            IndexName="TelegramChatIndex",  # You need to create this GSI
            KeyConditionExpression=boto3.dynamodb.conditions.Key("telegram_chat_id").eq(str(chat_id))
        )
        return response.get("Items", [])
    except Exception:
        # Fallback: scan (inefficient)
        logger.warning("GSI query failed, falling back to scan")
        response = table.scan(
            FilterExpression="telegram_chat_id = :chat_id",
            ExpressionAttributeValues={":chat_id": str(chat_id)}
        )
        return response.get("Items", [])


def _ensure_user(
    cfg: config.RuntimeConfig,
    chat_id: int,
    message: dict[str, Any],
    bot_id: str,
    bot_username: str,
) -> dict[str, Any] | None:
    """
    Ensure we have a user record in the users table with current bot info.
    """
    if not cfg.users_table:
        return None

    chat_id_str = str(chat_id)
    existing = dynamodb.get_item(cfg.users_table, {"telegram_chat_id": chat_id_str})
    
    user_meta = message.get("from", {}) or {}
    now_iso = dt.datetime.now(dt.timezone.utc).isoformat()

    if existing:
        # Update existing user with current bot info and activity
        item = {
            "telegram_chat_id": chat_id_str,
            "telegram_bot_id": bot_id,
            "telegram_bot_username": bot_username,
            "last_active_at": now_iso,
            "username": user_meta.get("username") or existing.get("username"),
            "first_name": user_meta.get("first_name") or existing.get("first_name"),
            "last_name": user_meta.get("last_name") or existing.get("last_name"),
            "status": "ACTIVE",
        }
        
        # Only update if bot has changed or user info is missing
        if (existing.get("telegram_bot_id") != bot_id or 
            not existing.get("username") and user_meta.get("username")):
            dynamodb.upsert_item(cfg.users_table, item)
        
        return existing
    
    else:
        # Create new user
        item = {
            "telegram_chat_id": chat_id_str,
            "telegram_bot_id": bot_id,
            "telegram_bot_username": bot_username,
            "username": user_meta.get("username"),
            "first_name": user_meta.get("first_name"),
            "last_name": user_meta.get("last_name"),
            "locale": user_meta.get("language_code"),
            "status": "ACTIVE",
            "created_at": now_iso,
            "last_active_at": now_iso,
        }
        dynamodb.upsert_item(cfg.users_table, item)
        return item


def _update_user_with_bot_info(
    cfg: config.RuntimeConfig,
    chat_id: int,
    bot_id: str,
    bot_username: str,
    message: dict[str, Any]
) -> None:
    """Update user record with current bot information."""
    chat_id_str = str(chat_id)
    user_meta = message.get("from", {}) or {}
    now_iso = dt.datetime.now(dt.timezone.utc).isoformat()

    item = {
        "telegram_chat_id": chat_id_str,
        "telegram_bot_id": bot_id,
        "telegram_bot_username": bot_username,
        "last_active_at": now_iso,
        "status": "ACTIVE",
    }

    # Add optional user info if available
    if user_meta.get("username"):
        item["username"] = user_meta["username"]
    if user_meta.get("first_name"):
        item["first_name"] = user_meta["first_name"]
    if user_meta.get("last_name"):
        item["last_name"] = user_meta["last_name"]
    if user_meta.get("language_code"):
        item["locale"] = user_meta["language_code"]

    dynamodb.upsert_item(cfg.users_table, item)
    logger.info(f"Updated user {chat_id_str} with bot @{bot_username}")


def _migrate_user_aliases_to_bot(
    cfg: config.RuntimeConfig,
    chat_id_str: str,
    new_bot_id: str,
    new_bot_username: str
) -> None:
    """Migrate all user's aliases to new bot."""
    aliases = _list_aliases(cfg, int(chat_id_str))
    
    migrated_count = 0
    for alias in aliases:
        alias_id = alias.get("alias_id")
        
        # Update alias with new bot info
        dynamodb.update_item(
            cfg.aliases_table,
            {"alias_id": alias_id},
            UpdateExpression="""
                SET telegram_bot_id = :bot_id,
                    telegram_bot_username = :bot_username,
                    bot_migrated_at = :now
            """,
            ExpressionAttributeValues={
                ":bot_id": new_bot_id,
                ":bot_username": new_bot_username,
                ":now": dt.datetime.now(dt.timezone.utc).isoformat()
            }
        )
        
        # Migrate pending emails for this alias
        migrated_emails = _migrate_pending_emails_for_alias(
            cfg, alias_id, chat_id_str, new_bot_id, new_bot_username
        )
        
        migrated_count += 1
        logger.info(f"Migrated alias {alias_id} to bot @{new_bot_username} ({migrated_emails} emails)")
    
    if migrated_count > 0:
        logger.info(f"✅ Migrated {migrated_count} aliases to new bot for user {chat_id_str}")


def _migrate_pending_emails_for_alias(
    cfg: config.RuntimeConfig,
    alias_id: str,
    chat_id_str: str,
    new_bot_id: str,
    new_bot_username: str
) -> int:
    """Migrate pending emails for an alias to new bot."""
    table = _dynamodb.Table(cfg.emails_table)
    
    # Scan for pending emails for this alias
    response = table.scan(
        FilterExpression="alias_id = :alias AND #state = :state",
        ExpressionAttributeNames={"#state": "state"},
        ExpressionAttributeValues={
            ":alias": alias_id,
            ":state": "PENDING"
        }
    )
    
    migrated = 0
    now_iso = dt.datetime.now(dt.timezone.utc).isoformat()
    
    for email_item in response.get('Items', []):
        email_bot_id = email_item.get("telegram_bot_id")
        
        # Only migrate if email is for different bot
        if email_bot_id != new_bot_id:
            table.update_item(
                Key={'message_id': email_item['message_id']},
                UpdateExpression="""
                    SET telegram_chat_id = :chat_id,
                        telegram_bot_id = :bot_id,
                        telegram_bot_username = :bot_username,
                        needs_migration = :false,
                        migrated_at = :now
                """,
                ExpressionAttributeValues={
                    ":chat_id": chat_id_str,
                    ":bot_id": new_bot_id,
                    ":bot_username": new_bot_username,
                    ":false": False,
                    ":now": now_iso
                }
            )
            migrated += 1
    
    return migrated


def _provision_alias(
    cfg: config.RuntimeConfig, 
    chat_id: int, 
    bot_id: str,
    bot_username: str
) -> dict[str, Any]:
    """
    Create a Cloudflare routing rule for a new alias and store its metadata.
    """
    alias_id = _generate_alias_id()

    # Guard against collision (very unlikely, but safe)
    existing = dynamodb.get_item(cfg.aliases_table, {"alias_id": alias_id})
    if existing:
        # Try again with different ID
        return _provision_alias(cfg, chat_id, bot_id, bot_username)

    # Ask Cloudflare to create a rule for this alias
    cf_result = cloudflare.create_alias(cfg.cloudflare_secret_arn, alias_id)
    email_address = cf_result.get("name")  # full email, e.g., abcd1234@domain.com
    rule_id = cf_result.get("id")         # Cloudflare rule ID

    now_iso = dt.datetime.now(dt.timezone.utc).isoformat()
    record = {
        "alias_id": alias_id,
        "email_address": email_address,
        "telegram_chat_id": str(chat_id),
        "telegram_bot_id": bot_id,
        "telegram_bot_username": bot_username,
        "status": "ACTIVE",
        "cloudflare_rule_id": rule_id,
        "created_at": now_iso,
    }
    
    dynamodb.upsert_item(cfg.aliases_table, record)
    
    # Also update user's last activity
    _update_user_with_bot_info(cfg, chat_id, bot_id, bot_username, {"from": {}})
    
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


def _handle_email_download(cfg: config.RuntimeConfig, event: dict[str, Any]) -> dict[str, Any]:
    """
    Handle GET /email/{aliasId}/{messageId}.

    This is used by the "Download Raw Email" link that Lambda #2 embeds in the
    Telegram summary.
    """
    path_params = event.get("pathParameters") or {}
    alias_id = path_params.get("aliasId")
    message_id = path_params.get("messageId")

    if not alias_id or not message_id:
        return {"statusCode": 400, "body": "Missing aliasId or messageId"}

    if not cfg.emails_table:
        return {"statusCode": 500, "body": "Emails table not configured"}

    # Load email record
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


def _telegram_api_post(token: str, method: str, payload: dict[str, Any]) -> None:
    """
    Call Telegram Bot API using only the Python standard library.
    """
    url = f"{TELEGRAM_API_BASE}/bot{token}/{method}"
    data = json.dumps(payload).encode("utf-8")
    req = urlrequest.Request(
        url, 
        data=data, 
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    
    try:
        with urlrequest.urlopen(req, timeout=10) as resp:
            resp.read()  # Read to complete request
    except Exception as exc:
        logger.warning(
            "Telegram API call failed",
            extra={"method": method, "error": str(exc)},
        )


# Helper function to check if user exists (for backward compatibility)
def _get_user(cfg: config.RuntimeConfig, chat_id: int) -> dict[str, Any] | None:
    """Get user record by chat_id."""
    if not cfg.users_table:
        return None
    return dynamodb.get_item(cfg.users_table, {"telegram_chat_id": str(chat_id)})


# Optional: Health check endpoint
def _handle_health_check() -> dict[str, Any]:
    """Handle health check requests."""
    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({
            "status": "ok",
            "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
            "service": "telegram-webhook"
        })
    }
