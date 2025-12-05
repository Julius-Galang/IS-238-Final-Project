"""
Lambda #3: Telegram Bot Webhook Handler.

This Lambda:
1. Serves as the Telegram webhook URL
2. Handles commands from users (/start, /register, /list, /deactivate)
3. Manages email alias creation and deactivation
4. Provides email download redirects
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import secrets
import string
from typing import Any, Dict, List, Optional
from urllib import parse, request

import boto3

logger = logging.getLogger(__name__)
logger.setLevel(os.getenv("LOG_LEVEL", "INFO"))

# Initialize AWS clients
_secrets = boto3.client("secretsmanager")
_dynamodb = boto3.resource("dynamodb")
_s3 = boto3.client("s3")

# Telegram API base URL
TELEGRAM_API_BASE = "https://api.telegram.org"

# Email domain (configure via environment variable)
EMAIL_DOMAIN = os.getenv("EMAIL_DOMAIN", "your-domain.com")


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """
    Main Lambda handler for Telegram webhook and email downloads.
    """
    # Parse request context
    request_ctx = event.get("requestContext", {})
    http_info = request_ctx.get("http", {})
    
    path = http_info.get("path", "")
    method = http_info.get("method", "GET").upper()
    
    logger.debug(f"Request: {method} {path}")
    
    # 1) Telegram webhook (POST from Telegram)
    if path.endswith("/telegram/webhook") and method == "POST":
        return _handle_telegram_webhook(event)
    
    # 2) Email download redirect (GET from user)
    if path.startswith("/email/") and method == "GET":
        return _handle_email_download(event)
    
    # 3) Health check (optional)
    if path == "/health" and method == "GET":
        return _handle_health_check()
    
    # Anything else → 404
    return {
        "statusCode": 404,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"error": "Not found"})
    }


def _handle_telegram_webhook(event: dict[str, Any]) -> dict[str, Any]:
    """Handle Telegram webhook updates."""
    body = event.get("body", "{}")
    
    try:
        update = json.loads(body)
    except json.JSONDecodeError:
        logger.warning("Invalid JSON in webhook payload")
        # Always return 200 to prevent Telegram from retrying
        return {"statusCode": 200, "body": "ok"}
    
    # Get Telegram bot token
    telegram_secret_arn = os.environ["TELEGRAM_SECRET_ARN"]
    bot_token = _get_telegram_token(telegram_secret_arn)
    
    if not bot_token:
        logger.error("Failed to get Telegram bot token")
        return {"statusCode": 500, "body": "Internal server error"}
    
    # Route the update
    if "message" in update:
        _handle_telegram_message(bot_token, update["message"])
    elif "callback_query" in update:
        _handle_callback_query(bot_token, update["callback_query"])
    
    # Always return 200 OK to Telegram
    return {"statusCode": 200, "body": "ok"}


def _handle_telegram_message(bot_token: str, message: Dict[str, Any]) -> None:
    """Handle incoming Telegram messages (commands)."""
    chat = message.get("chat", {})
    chat_id = chat.get("id")
    
    if not chat_id:
        logger.warning("No chat ID in message")
        return
    
    text = message.get("text", "").strip()
    if not text:
        return
    
    # Get user info
    from_user = message.get("from", {})
    username = from_user.get("username", "")
    first_name = from_user.get("first_name", "")
    
    logger.info(f"📨 Message from {username} ({chat_id}): {text[:50]}...")
    
    # Convert to lowercase for command matching
    lower_text = text.lower()
    
    # Handle commands
    if lower_text.startswith("/start"):
        _handle_start_command(bot_token, chat_id, username, first_name)
    
    elif lower_text.startswith("/help"):
        _handle_help_command(bot_token, chat_id)
    
    elif lower_text.startswith("/list") or lower_text.startswith("/aliases"):
        _handle_list_command(bot_token, chat_id)
    
    elif lower_text.startswith("/register") or lower_text.startswith("/newemail") or lower_text.startswith("/create"):
        _handle_register_command(bot_token, chat_id)
    
    elif lower_text.startswith("/deactivate") or lower_text.startswith("/disable"):
        _handle_deactivate_command(bot_token, chat_id, text)
    
    else:
        # Unknown command
        _send_telegram_message(
            bot_token=bot_token,
            chat_id=chat_id,
            text=(
                "❓ I don't understand that command.\n\n"
                "Available commands:\n"
                "• /start - Get started with the bot\n"
                "• /help - Show help information\n"
                "• /list - Show your email aliases\n"
                "• /register - Create a new email alias\n"
                "• /deactivate <alias> - Disable an alias\n\n"
                "Need help? Contact support."
            ),
            parse_mode="Markdown"
        )


def _handle_start_command(bot_token: str, chat_id: int, username: str, first_name: str) -> None:
    """Handle /start command."""
    # Ensure user exists in database
    _ensure_user_exists(chat_id, username, first_name)
    
    # Send welcome message
    welcome_text = f"""👋 Welcome {first_name or username}!

I'm your Email Summarizer bot. Here's what I can do:

📧 **Receive Emails**
• Create unique email aliases
• Forward emails to your aliases
• Get AI-powered summaries in Telegram

🛠️ **Commands**
• /list - Show your email aliases
• /register - Create a new alias
• /deactivate <alias> - Disable an alias
• /help - Get help

🚀 **Get Started**
1. Use /register to create your first email alias
2. Send emails to that address
3. Receive AI-summarized versions here!

Need help? Just ask!"""
    
    _send_telegram_message(bot_token, chat_id, welcome_text)


def _handle_help_command(bot_token: str, chat_id: int) -> None:
    """Handle /help command."""
    help_text = """📚 **Help & Usage Guide**

**How It Works**
1. Create email aliases using /register
2. Send emails to your alias addresses
3. Receive AI-summarized versions in Telegram
4. Download original emails when needed

**Available Commands**
• /start - Welcome message
• /list - Show your email aliases
• /register - Create new email alias
• /deactivate <alias> - Disable an alias

**Creating Aliases**
Use /register to get a new email address like:
  `abc123@{domain}`

You can use this address anywhere! Emails sent to it will be:
1. Securely stored
2. Summarized by AI
3. Sent to you in Telegram

**Managing Aliases**
• See all aliases: /list
• Disable an alias: /deactivate abc123
• Disabled aliases stop receiving emails

**Privacy & Security**
• Your emails are encrypted
• Aliases can be disabled anytime
• No permanent storage

Need more help? Contact support."""
    
    help_text = help_text.replace("{domain}", EMAIL_DOMAIN)
    
    _send_telegram_message(bot_token, chat_id, help_text, parse_mode="Markdown")


def _handle_list_command(bot_token: str, chat_id: int) -> None:
    """Handle /list command - show user's email aliases."""
    aliases_table = os.environ["ALIASES_TABLE"]
    
    # Get user's aliases from DynamoDB
    aliases = _get_user_aliases(aliases_table, str(chat_id))
    
    if not aliases:
        _send_telegram_message(
            bot_token=bot_token,
            chat_id=chat_id,
            text=(
                "📭 You don't have any email aliases yet.\n\n"
                "Use /register to create your first email alias!"
            )
        )
        return
    
    # Format aliases list
    lines = ["📧 **Your Email Aliases**\n"]
    
    for alias in aliases:
        alias_id = alias.get("alias_id", "unknown")
        email_address = alias.get("email_address", f"{alias_id}@{EMAIL_DOMAIN}")
        status = alias.get("status", "UNKNOWN")
        
        # Add emoji based on status
        if status == "ACTIVE":
            status_emoji = "✅"
        elif status == "DISABLED":
            status_emoji = "❌"
        else:
            status_emoji = "❓"
        
        lines.append(f"{status_emoji} `{email_address}`")
        
        # Add creation date if available
        created_at = alias.get("created_at")
        if created_at:
            try:
                dt_obj = dt.datetime.fromisoformat(created_at.replace('Z', '+00:00'))
                date_str = dt_obj.strftime("%b %d, %Y")
                lines.append(f"   Created: {date_str}")
            except:
                pass
        
        lines.append("")  # Empty line between aliases
    
    lines.append("\n💡 Use /deactivate <alias> to disable an alias.")
    
    _send_telegram_message(
        bot_token=bot_token,
        chat_id=chat_id,
        text="\n".join(lines),
        parse_mode="Markdown"
    )


def _handle_register_command(bot_token: str, chat_id: int) -> None:
    """Handle /register command - create new email alias."""
    aliases_table = os.environ["ALIASES_TABLE"]
    cloudflare_secret_arn = os.environ["CLOUDFLARE_SECRET_ARN"]
    
    # Generate unique alias ID
    alias_id = _generate_alias_id()
    
    # Check for collisions (unlikely but possible)
    existing = _get_alias_record(aliases_table, alias_id)
    if existing:
        # Try one more time
        alias_id = _generate_alias_id()
        existing = _get_alias_record(aliases_table, alias_id)
        if existing:
            _send_telegram_message(
                bot_token=bot_token,
                chat_id=chat_id,
                text="❌ Could not generate unique alias. Please try again."
            )
            return
    
    # Create Cloudflare email routing rule
    try:
        # Import here to avoid dependency if not used
        from shared import cloudflare
        
        # Create Cloudflare rule
        cf_result = cloudflare.create_alias(cloudflare_secret_arn, alias_id)
        
        if not cf_result or "error" in cf_result:
            logger.error(f"Failed to create Cloudflare rule: {cf_result}")
            _send_telegram_message(
                bot_token=bot_token,
                chat_id=chat_id,
                text="❌ Failed to create email routing rule. Please try again later."
            )
            return
        
        # Extract email address from Cloudflare response
        email_address = cf_result.get("name", f"{alias_id}@{EMAIL_DOMAIN}")
        rule_id = cf_result.get("id", "")
        
    except Exception as exc:
        logger.exception(f"Failed to create Cloudflare alias: {exc}")
        _send_telegram_message(
            bot_token=bot_token,
            chat_id=chat_id,
            text="❌ Failed to configure email routing. Please try again later."
        )
        return
    
    # Save alias to DynamoDB
    try:
        table = _dynamodb.Table(aliases_table)
        
        alias_data = {
            "alias_id": alias_id,
            "email_address": email_address,
            "telegram_chat_id": str(chat_id),
            "cloudflare_rule_id": rule_id,
            "status": "ACTIVE",
            "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        }
        
        table.put_item(Item=alias_data)
        
        # Send success message
        success_text = f"""🎉 **New Email Alias Created!**

**Email Address:**
`{email_address}`

**How to use it:**
1. Use this address anywhere you need an email
2. Forward emails to this address
3. Receive AI-summarized versions here

**Features:**
• AI-powered summaries
• Secure email storage
• One-click downloads
• Easy to disable

**To disable this alias:**
Use `/deactivate {alias_id}`

Start using your new email address right away! 📧"""
        
        _send_telegram_message(
            bot_token=bot_token,
            chat_id=chat_id,
            text=success_text,
            parse_mode="Markdown"
        )
        
        logger.info(f"Created new alias {alias_id} for chat {chat_id}")
        
    except Exception as exc:
        logger.exception(f"Failed to save alias to DynamoDB: {exc}")
        
        # Try to clean up Cloudflare rule
        try:
            from shared import cloudflare
            if rule_id:
                cloudflare.disable_alias(cloudflare_secret_arn, rule_id)
        except:
            pass
        
        _send_telegram_message(
            bot_token=bot_token,
            chat_id=chat_id,
            text="❌ Failed to save alias. Please try again."
        )


def _handle_deactivate_command(bot_token: str, chat_id: int, text: str) -> None:
    """Handle /deactivate command - disable an email alias."""
    aliases_table = os.environ["ALIASES_TABLE"]
    cloudflare_secret_arn = os.environ["CLOUDFLARE_SECRET_ARN"]
    
    # Parse alias from command
    parts = text.split()
    if len(parts) < 2:
        _send_telegram_message(
            bot_token=bot_token,
            chat_id=chat_id,
            text=(
                "❓ Usage: /deactivate <alias>\n\n"
                "Example:\n"
                "• /deactivate abc123\n"
                "• /deactivate abc123@domain.com"
            )
        )
        return
    
    alias_input = parts[1].strip()
    
    # Extract alias ID (remove @domain if present)
    if "@" in alias_input:
        alias_id = alias_input.split("@")[0]
    else:
        alias_id = alias_input
    
    # Get alias record
    alias_record = _get_alias_record(aliases_table, alias_id)
    
    if not alias_record:
        _send_telegram_message(
            bot_token=bot_token,
            chat_id=chat_id,
            text=f"❌ Alias `{alias_id}` not found."
        )
        return
    
    # Check ownership
    if alias_record.get("telegram_chat_id") != str(chat_id):
        _send_telegram_message(
            bot_token=bot_token,
            chat_id=chat_id,
            text="❌ You don't own this alias."
        )
        return
    
    # Check if already disabled
    if alias_record.get("status") == "DISABLED":
        _send_telegram_message(
            bot_token=bot_token,
            chat_id=chat_id,
            text=f"ℹ️ Alias `{alias_id}` is already disabled."
        )
        return
    
    # Disable Cloudflare rule
    rule_id = alias_record.get("cloudflare_rule_id")
    if rule_id:
        try:
            from shared import cloudflare
            cloudflare.disable_alias(cloudflare_secret_arn, rule_id)
        except Exception as exc:
            logger.error(f"Failed to disable Cloudflare rule: {exc}")
            # Continue anyway, we'll still mark it as disabled in DB
    
    # Update alias status in DynamoDB
    try:
        table = _dynamodb.Table(aliases_table)
        
        table.update_item(
            Key={"alias_id": alias_id},
            UpdateExpression="""
                SET #status = :status,
                    disabled_at = :disabled_at
            """,
            ExpressionAttributeNames={
                "#status": "status"
            },
            ExpressionAttributeValues={
                ":status": "DISABLED",
                ":disabled_at": dt.datetime.now(dt.timezone.utc).isoformat()
            }
        )
        
        email_address = alias_record.get("email_address", f"{alias_id}@{EMAIL_DOMAIN}")
        
        _send_telegram_message(
            bot_token=bot_token,
            chat_id=chat_id,
            text=(
                f"✅ Alias disabled successfully!\n\n"
                f"`{email_address}`\n\n"
                f"This address will no longer receive emails.\n"
                f"You can create a new one anytime with /register."
            ),
            parse_mode="Markdown"
        )
        
        logger.info(f"Disabled alias {alias_id} for chat {chat_id}")
        
    except Exception as exc:
        logger.exception(f"Failed to disable alias: {exc}")
        _send_telegram_message(
            bot_token=bot_token,
            chat_id=chat_id,
            text="❌ Failed to disable alias. Please try again."
        )


def _handle_callback_query(bot_token: str, callback_query: Dict[str, Any]) -> None:
    """Handle callback queries from inline keyboards."""
    data = callback_query.get("data", "")
    chat_id = callback_query.get("message", {}).get("chat", {}).get("id")
    callback_id = callback_query.get("id", "")
    
    if not data or not chat_id or not callback_id:
        return
    
    # Answer the callback query first (stops loading indicator)
    _answer_callback_query(bot_token, callback_id)
    
    # Parse callback data (format: "action:value")
    if ":" in data:
        action, value = data.split(":", 1)
        
        if action == "disable":
            # Handle disable button
            aliases_table = os.environ["ALIASES_TABLE"]
            alias_record = _get_alias_record(aliases_table, value)
            
            if alias_record and alias_record.get("telegram_chat_id") == str(chat_id):
                # Send deactivation confirmation
                _send_telegram_message(
                    bot_token=bot_token,
                    chat_id=chat_id,
                    text=(
                        f"Are you sure you want to disable alias `{value}`?\n\n"
                        f"Use /deactivate {value} to confirm."
                    ),
                    parse_mode="Markdown"
                )


def _handle_email_download(event: dict[str, Any]) -> dict[str, Any]:
    """Handle email download redirects."""
    path_params = event.get("pathParameters", {})
    alias_id = path_params.get("aliasId")
    message_id = path_params.get("messageId")
    
    if not alias_id or not message_id:
        return {
            "statusCode": 400,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"error": "Missing aliasId or messageId"})
        }
    
    emails_table = os.environ["EMAILS_TABLE"]
    raw_email_bucket = os.environ["RAW_EMAIL_BUCKET"]
    
    # Get email record
    email_record = _get_email_record(emails_table, message_id)
    
    if not email_record or email_record.get("alias_id") != alias_id:
        return {
            "statusCode": 404,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"error": "Email not found"})
        }
    
    s3_key = email_record.get("s3_key")
    if not s3_key:
        return {
            "statusCode": 500,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"error": "Missing S3 key"})
        }
    
    # Generate pre-signed URL for S3 object (valid for 5 minutes)
    try:
        url = _s3.generate_presigned_url(
            ClientMethod='get_object',
            Params={
                'Bucket': raw_email_bucket,
                'Key': s3_key
            },
            ExpiresIn=300  # 5 minutes
        )
        
        # Redirect to the pre-signed URL
        return {
            "statusCode": 302,
            "headers": {"Location": url},
            "body": ""
        }
        
    except Exception as exc:
        logger.error(f"Failed to generate pre-signed URL: {exc}")
        return {
            "statusCode": 500,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"error": "Failed to generate download link"})
        }


def _handle_health_check() -> dict[str, Any]:
    """Handle health check requests."""
    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({
            "status": "healthy",
            "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
            "service": "telegram-webhook"
        })
    }


# Helper functions

def _get_telegram_token(secret_arn: str) -> Optional[str]:
    """Get Telegram bot token from Secrets Manager."""
    try:
        response = _secrets.get_secret_value(SecretId=secret_arn)
        secret_str = response.get("SecretString", "{}")
        
        # Try to parse as JSON
        try:
            data = json.loads(secret_str)
            token = data.get("bot_token", "")
        except json.JSONDecodeError:
            # Assume the entire string is the token
            token = secret_str
        
        return token.strip() if token else None
        
    except Exception as exc:
        logger.error(f"Failed to get Telegram token: {exc}")
        return None


def _send_telegram_message(
    bot_token: str,
    chat_id: int,
    text: str,
    parse_mode: Optional[str] = None,
    reply_markup: Optional[Dict] = None
) -> bool:
    """Send message to Telegram."""
    try:
        url = f"{TELEGRAM_API_BASE}/bot{bot_token}/sendMessage"
        
        payload = {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": True
        }
        
        if parse_mode:
            payload["parse_mode"] = parse_mode
        
        if reply_markup:
            payload["reply_markup"] = reply_markup
        
        data = json.dumps(payload).encode("utf-8")
        req = request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        
        with request.urlopen(req, timeout=10) as resp:
            resp.read()  # Read response to complete request
        
        return True
        
    except Exception as exc:
        logger.error(f"Failed to send Telegram message: {exc}")
        return False


def _answer_callback_query(bot_token: str, callback_id: str) -> None:
    """Answer a callback query (stops loading indicator)."""
    try:
        url = f"{TELEGRAM_API_BASE}/bot{bot_token}/answerCallbackQuery"
        
        payload = {
            "callback_query_id": callback_id
        }
        
        data = json.dumps(payload).encode("utf-8")
        req = request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        
        with request.urlopen(req, timeout=5) as resp:
            resp.read()
            
    except Exception as exc:
        logger.error(f"Failed to answer callback query: {exc}")


def _ensure_user_exists(chat_id: int, username: str, first_name: str) -> None:
    """Ensure user exists in users table."""
    try:
        users_table = os.environ.get("USERS_TABLE")
        if not users_table:
            return
        
        table = _dynamodb.Table(users_table)
        
        # Check if user exists
        response = table.get_item(Key={"telegram_chat_id": str(chat_id)})
        
        if "Item" not in response:
            # Create new user
            user_data = {
                "telegram_chat_id": str(chat_id),
                "username": username or "",
                "first_name": first_name or "",
                "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "last_active_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "status": "ACTIVE"
            }
            
            table.put_item(Item=user_data)
            logger.info(f"Created new user: {chat_id} ({username})")
        else:
            # Update last active timestamp
            table.update_item(
                Key={"telegram_chat_id": str(chat_id)},
                UpdateExpression="SET last_active_at = :last_active",
                ExpressionAttributeValues={
                    ":last_active": dt.datetime.now(dt.timezone.utc).isoformat()
                }
            )
            
    except Exception as exc:
        logger.error(f"Failed to ensure user exists: {exc}")


def _get_user_aliases(aliases_table: str, chat_id: str) -> List[Dict[str, Any]]:
    """Get all aliases for a user."""
    try:
        table = _dynamodb.Table(aliases_table)
        
        # Scan for user's aliases (using filter)
        response = table.scan(
            FilterExpression="telegram_chat_id = :chat_id",
            ExpressionAttributeValues={":chat_id": chat_id}
        )
        
        return response.get("Items", [])
        
    except Exception as exc:
        logger.error(f"Failed to get user aliases: {exc}")
        return []


def _get_alias_record(aliases_table: str, alias_id: str) -> Optional[Dict[str, Any]]:
    """Get alias record by ID."""
    try:
        table = _dynamodb.Table(aliases_table)
        response = table.get_item(Key={"alias_id": alias_id})
        return response.get("Item")
    except Exception as exc:
        logger.error(f"Failed to get alias record: {exc}")
        return None


def _get_email_record(emails_table: str, message_id: str) -> Optional[Dict[str, Any]]:
    """Get email record by message ID."""
    try:
        table = _dynamodb.Table(emails_table)
        response = table.get_item(Key={"message_id": message_id})
        return response.get("Item")
    except Exception as exc:
        logger.error(f"Failed to get email record: {exc}")
        return None


def _generate_alias_id(length: int = 8) -> str:
    """Generate random alias ID."""
    # Use lowercase letters and numbers
    alphabet = string.ascii_lowercase + string.digits
    return ''.join(secrets.choice(alphabet) for _ in range(length))
