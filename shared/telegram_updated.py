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
            
            # DEBUG: Log the raw response
            logger.info(f"Telegram raw response: {body[:200]}...")
            
            # Parse JSON response
            try:
                response_data = json.loads(body)
            except json.JSONDecodeError:
                logger.error(f"Telegram returned invalid JSON: {body}")
                return False
            
            # CRITICAL: Check Telegram's "ok" field
            if not response_data.get("ok", False):
                error_msg = response_data.get("description", "Unknown error")
                error_code = response_data.get("error_code", "N/A")
                logger.error(f"Telegram API error {error_code}: {error_msg}")
                return False
            
            # SUCCESS!
            logger.info(f"Telegram message sent successfully to chat_id {chat_id}")
            return True
            
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8") if hasattr(e, 'read') else str(e)
        logger.error(
            "Telegram sendMessage HTTPError",
            extra={"code": e.code, "reason": e.reason, "body": error_body},
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


def test_bot_token(token: str) -> bool:
    """Test if bot token is valid."""
    url = f"{TELEGRAM_API_BASE}/bot{token}/getMe"
    
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read().decode("utf-8")
            data = json.loads(body)
            
            if data.get("ok", False):
                bot_info = data.get("result", {})
                logger.info(f"Bot is valid: @{bot_info.get('username')} (ID: {bot_info.get('id')})")
                return True
            else:
                logger.error(f"Bot token invalid: {data.get('description')}")
                return False
                
    except Exception as e:
        logger.error(f"Failed to test bot token: {e}")
        return False
