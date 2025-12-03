# shared/telegram.py
"""Helpers for talking to Telegram Bot API."""

from __future__ import annotations

import json
import logging
from typing import Any, Dict

import boto3
import requests

logger = logging.getLogger(__name__)

TELEGRAM_API_BASE = "https://api.telegram.org"

_sm = boto3.client("secretsmanager")


def get_bot_token(secret_arn: str) -> str:
    """Load bot token from Secrets Manager.

    Secret value, expected to be JSON with key `bot_token`.
    """
    resp = _sm.get_secret_value(SecretId=secret_arn)
    secret_str = resp.get("SecretString") or ""
    data = json.loads(secret_str)
    token = data.get("bot_token")
    if not token:
        raise RuntimeError("Telegram bot token not found in secret")
    return token


def send_message(
    token: str,
    chat_id: int,
    text: str,
    parse_mode: str | None = "Markdown",
    reply_markup: Dict[str, Any] | None = None,
) -> None:
    payload: Dict[str, Any] = {"chat_id": chat_id, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if reply_markup:
        payload["reply_markup"] = reply_markup

    url = f"{TELEGRAM_API_BASE}/bot{token}/sendMessage"
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as exc:  # pragma: no cover
        logger.warning("Failed to send Telegram message", extra={"error": str(exc)})
