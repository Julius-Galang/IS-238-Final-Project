import json
import logging
import os
from typing import Any, Dict

import boto3
import urllib.request
import urllib.error

logger = logging.getLogger(__name__)
logger.setLevel(os.getenv("LOG_LEVEL", "INFO"))

TELEGRAM_API_BASE = os.getenv("TELEGRAM_API_BASE", "https://api.telegram.org")

_secrets_client = boto3.client("secretsmanager")


def get_bot_token(secret_arn: str) -> str:
    """
    Load bot token from Secrets Manager."""
    resp = _secrets_client.get_secret_value(SecretId=secret_arn)
    secret = resp.get("SecretString") or ""

    token = ""
    try:
        data = json.loads(secret)
        if isinstance(data, dict):
            token = data.get("bot_token", "")
    except json.JSONDecodeError:
        # Not JSON → treat whole string as token
        token = secret

    if not token:
        raise RuntimeError("Could not find Telegram token in secret")

    return token


def send_message(
    token: str,
    chat_id: int,
    text: str,
    reply_markup: Dict[str, Any] | None = None,
    parse_mode: str = "Markdown",
) -> bool:
    """Send a Telegram message via the Bot API using only stdlib HTTP."""
    url = f"{TELEGRAM_API_BASE}/bot{token}/sendMessage"

    payload: Dict[str, Any] = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = resp.read().decode("utf-8")
            if resp.status >= 400:
                logger.error(
                    "Telegram sendMessage failed",
                    extra={"status": resp.status, "body": body},
                )
                return False
        return True
    except urllib.error.HTTPError as e:
        logger.error(
            "Telegram sendMessage HTTPError",
            extra={"code": e.code, "reason": e.reason},
        )
    except urllib.error.URLError as e:
        logger.error(
            "Telegram sendMessage URLError",
            extra={"reason": str(e.reason)},
        )
    except Exception as exc:
        logger.exception(
            "Unexpected error calling Telegram",
            extra={"error": str(exc)},
        )

    return False
