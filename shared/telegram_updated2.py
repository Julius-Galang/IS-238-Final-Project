"""Helpers for talking to Telegram Bot API with bot info support."""

from __future__ import annotations

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
    """Load bot token from Secrets Manager."""
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


def get_bot_info(token: str) -> Dict[str, Any]:
    """Get bot information (id, username, etc)."""
    url = f"{TELEGRAM_API_BASE}/bot{token}/getMe"
    
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read().decode("utf-8")
            data = json.loads(body)
            
            if not data.get("ok", False):
                error_msg = data.get("description", "Unknown error")
                raise RuntimeError(f"Telegram API error: {error_msg}")
            
            return data.get("result", {})
            
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8") if hasattr(e, 'read') else str(e)
        raise RuntimeError(f"HTTP error getting bot info: {e.code} - {error_body}")
    except Exception as e:
        raise RuntimeError(f"Error getting bot info: {e}")


def send_message(
    token: str,
    chat_id: int,
    text: str,
    reply_markup: Dict[str, Any] | None = None,
    parse_mode: str = "Markdown",
    max_retries: int = 2,
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
    
    for attempt in range(max_retries):
        try:
            req = urllib.request.Request(
                url,
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )

            with urllib.request.urlopen(req, timeout=20) as resp:
                body = resp.read().decode("utf-8")
                response_data = json.loads(body)
                
                if not response_data.get("ok", False):
                    error_desc = response_data.get("description", "Unknown error")
                    error_code = response_data.get("error_code", "N/A")
                    
                    logger.error(f"Telegram API error {error_code}: {error_desc}")
                    
                    # Check if retryable (rate limit, server error)
                    if "retry after" in error_desc.lower() or "too many requests" in error_desc.lower():
                        if attempt < max_retries - 1:
                            import time
                            wait_time = 2 ** attempt
                            logger.info(f"Rate limited, waiting {wait_time}s before retry...")
                            time.sleep(wait_time)
                            continue
                    
                    return False
                
                # Success!
                logger.debug(f"Telegram message sent successfully to chat_id {chat_id}")
                return True
                
        except urllib.error.HTTPError as e:
            error_body = e.read().decode("utf-8") if hasattr(e, 'read') else str(e)
            logger.error(
                "Telegram sendMessage HTTPError",
                extra={"code": e.code, "reason": e.reason, "body": error_body},
            )
            
            if e.code == 429:  # Too Many Requests
                if attempt < max_retries - 1:
                    import time
                    wait_time = 2 ** attempt
                    logger.info(f"Rate limited (429), waiting {wait_time}s...")
                    time.sleep(wait_time)
                    continue
            
        except urllib.error.URLError as e:
            logger.error(
                "Telegram sendMessage URLError",
                extra={"reason": str(e.reason)},
            )
            
            if attempt < max_retries - 1:
                import time
                wait_time = 2 ** attempt
                logger.info(f"Network error, waiting {wait_time}s before retry...")
                time.sleep(wait_time)
                continue
                
        except Exception as exc:
            logger.exception(
                "Unexpected error calling Telegram",
                extra={"error": str(exc)},
            )
            
            if attempt < max_retries - 1:
                import time
                wait_time = 2 ** attempt
                logger.info(f"Unexpected error, waiting {wait_time}s before retry...")
                time.sleep(wait_time)
                continue

    return False


def test_bot_token(token: str) -> bool:
    """Test if bot token is valid."""
    try:
        bot_info = get_bot_info(token)
        if bot_info.get("is_bot", False):
            logger.info(f"Bot token valid: @{bot_info.get('username')} (ID: {bot_info.get('id')})")
            return True
        return False
    except Exception as e:
        logger.error(f"Bot token invalid: {e}")
        return False
