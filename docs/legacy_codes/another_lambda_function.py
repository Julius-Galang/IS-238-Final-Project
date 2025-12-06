"""Lambda entry point for Telegram webhook and download redirects."""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import secrets as secrets_lib
from typing import Any

import requests

from shared import cloudflare, config, dynamodb, s3_utils, telegram

logger = logging.getLogger(__name__)
logger.setLevel(os.getenv("LOG_LEVEL", "INFO"))


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    # Single entry: route webhook posts vs download redirects
    cfg = config.get_config()
    path = event.get("requestContext", {}).get("http", {}).get("path", "")
    method = (event.get("requestContext", {}).get("http", {}).get("method") or "GET").upper()

    if path == "/telegram/webhook" and method == "POST":
        return _handle_telegram_update(cfg, event)
    if path.startswith("/email/") and method == "GET":
        return _handle_email_download(cfg, event)

    return {"statusCode": 404, "body": "Not Found"}


def _handle_telegram_update(cfg: config.RuntimeConfig, event: dict[str, Any]) -> dict[str, Any]:
    # Parse Telegram update payloads and dispatch message vs callback flows
    try:
        update = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        logger.warning("Telegram webhook payload is not JSON", extra={"body": event.get("body")})
        return {"statusCode": 200, "body": "ignored"}

    token = telegram.get_bot_token(cfg.telegram_secret_arn)

    if "message" in update:
        _handle_message(cfg, token, update["message"])
    elif "callback_query" in update:
        _handle_callback_query(cfg, token, update["callback_query"])

    return {"statusCode": 200, "body": "ok"}


def _handle_message(cfg: config.RuntimeConfig, token: str, message: dict[str, Any]) -> None:
    # Classic Telegram text commands: /start, /aliases, /create, /disable
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    if not chat_id:
        return

    user_record = _ensure_user(cfg, chat_id, message)
    text = (message.get("text") or "").strip()

    if text.startswith("/start") or text.startswith("/aliases"):
        _send_alias_overview(cfg, token, chat_id, user_record)
    elif text.startswith("/create"):
        _create_alias_flow(cfg, token, chat_id)
    elif text.startswith("/disable"):
        parts = text.split()
        if len(parts) < 2:
            telegram.send_message(token, chat_id=chat_id, text="Usage: /disable <alias>")
            return
        _disable_alias_flow(cfg, token, chat_id, parts[1].lower())
    else:
        telegram.send_message(
            token,
            chat_id=chat_id,
            text="Commands: /aliases, /create, /disable <alias>",
        )


def _handle_callback_query(cfg: config.RuntimeConfig, token: str, payload: dict[str, Any]) -> None:
    # Inline button callbacks (currently disable-only)
    data = payload.get("data", "")
    chat_id = payload.get("message", {}).get("chat", {}).get("id")
    if data.startswith("disable:") and chat_id:
        alias_id = data.split(":", 1)[1]
        _disable_alias_flow(cfg, token, chat_id, alias_id)
    callback_id = payload.get("id")
    if callback_id:
        telegram_api_url = f"{telegram.TELEGRAM_API_BASE}/bot{token}/answerCallbackQuery"
        requests.post(telegram_api_url, json={"callback_query_id": callback_id}, timeout=10)


def _send_alias_overview(cfg: config.RuntimeConfig, token: str, chat_id: int, user_record: dict[str, Any] | None) -> None:
    # Reply with the caller's current aliases and basic guidance
    aliases = _list_aliases(cfg, chat_id)
    if aliases:
        lines = ["Your aliases:"]
        for alias in aliases:
            status = alias.get("status", "UNKNOWN")
            email_address = alias.get("email_address") or f"{alias.get('alias_id')}@?"
            lines.append(f"- {email_address} ({status})")
        lines.append("\nUse /create to add another alias or /disable <alias> to deactivate.")
    else:
        lines = ["No aliases yet. Use /create to generate one."]

    greeting = user_record.get("first_name") if user_record else None
    if greeting:
        lines.insert(0, f"Hi {greeting}!")

    telegram.send_message(token, chat_id=chat_id, text="\n".join(lines))


def _create_alias_flow(cfg: config.RuntimeConfig, token: str, chat_id: int) -> None:
    # Generate a new Cloudflare alias + DynamoDB record for this user
    if not cfg.aliases_table:
        telegram.send_message(token, chat_id=chat_id, text="Alias table not configured.")
        return
    try:
        alias_record = _provision_alias(cfg, chat_id)
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception("Alias creation failed", extra={"chat_id": chat_id, "error": str(exc)})
        telegram.send_message(token, chat_id=chat_id, text="Could not create alias. Try again later.")
        return

    telegram.send_message(
        token,
        chat_id=chat_id,
        text=(
            "New alias ready!\n"
            f"Address: {alias_record['email_address']}\n"
            "Share this address with senders to start receiving summaries."
        ),
    )


def _disable_alias_flow(cfg: config.RuntimeConfig, token: str, chat_id: int, alias_id: str) -> None:
    # Disable both Cloudflare routing and the DynamoDB alias state
    if not cfg.aliases_table:
        telegram.send_message(token, chat_id=chat_id, text="Alias table not configured.")
        return

    alias_record = dynamodb.get_item(cfg.aliases_table, {"alias_id": alias_id})
    if not alias_record or alias_record.get("telegram_chat_id") != str(chat_id):
        telegram.send_message(token, chat_id=chat_id, text="Alias not found.")
        return

    if alias_record.get("status") == "DISABLED":
        telegram.send_message(token, chat_id=chat_id, text="Alias already disabled.")
        return

    rule_id = alias_record.get("cloudflare_rule_id")
    if rule_id:
        try:
            cloudflare.disable_alias(cfg.cloudflare_secret_arn, rule_id)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Failed to disable Cloudflare rule", extra={"alias_id": alias_id, "error": str(exc)})

    now_iso = dt.datetime.now(dt.timezone.utc).isoformat()
    dynamodb.update_item(
        cfg.aliases_table,
        {"alias_id": alias_id},
        UpdateExpression="SET status = :status, disabled_at = :ts",
        ExpressionAttributeValues={":status": "DISABLED", ":ts": now_iso},
    )

    telegram.send_message(token, chat_id=chat_id, text=f"Alias {alias_id} disabled.")


def _handle_email_download(cfg: config.RuntimeConfig, event: dict[str, Any]) -> dict[str, Any]:
    # HTTP GET handler that swaps alias/message ids for a presigned S3 URL
    alias_id = event.get("pathParameters", {}).get("aliasId")
    message_id = event.get("pathParameters", {}).get("messageId")
    if not alias_id or not message_id:
        return {"statusCode": 400, "body": "Missing parameters"}
    if not cfg.emails_table:
        return {"statusCode": 500, "body": "Emails table unavailable"}

    record = dynamodb.get_item(cfg.emails_table, {"message_id": message_id})
    if not record or record.get("alias_id") != alias_id:
        return {"statusCode": 404, "body": "Not Found"}

    key = record.get("s3_key")
    if not key:
        return {"statusCode": 500, "body": "Missing S3 key"}

    url = s3_utils.generate_presigned_url(cfg.raw_email_bucket, key)
    return {"statusCode": 302, "headers": {"Location": url}, "body": ""}


def _list_aliases(cfg: config.RuntimeConfig, chat_id: int) -> list[dict[str, Any]]:
    # GSI lookup of aliases owned by the Telegram chat
    if not cfg.aliases_table:
        return []
    return dynamodb.query_aliases_by_chat(cfg.aliases_table, str(chat_id))


def _ensure_user(cfg: config.RuntimeConfig, chat_id: int, message: dict[str, Any]) -> dict[str, Any] | None:
    # Upsert a Telegram user profile the first time they message the bot
    if not cfg.users_table:
        return None
    chat_id_str = str(chat_id)
    existing = dynamodb.get_item(cfg.users_table, {"telegram_chat_id": chat_id_str})
    if existing:
        return existing

    user_meta = message.get("from", {})
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
    # Create a Cloudflare catch-all rule, persist alias metadata, return record
    alias_id = _generate_alias_id()
    alias_record = dynamodb.get_item(cfg.aliases_table, {"alias_id": alias_id})
    if alias_record:
        # Extremely unlikely collision, generate a new alias recursively.
        return _provision_alias(cfg, chat_id)

    cf_result = cloudflare.create_alias(cfg.cloudflare_secret_arn, alias_id)
    email_address = cf_result.get("name")
    rule_id = cf_result.get("id")
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
    # Short random identifier becomes the local-part users share
    alphabet = "abcdefghijklmnopqrstuvwxyz0123456789"
    return "".join(secrets_lib.choice(alphabet) for _ in range(length))
