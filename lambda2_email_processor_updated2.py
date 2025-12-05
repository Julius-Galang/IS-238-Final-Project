"""Lambda #2 – Process emails with OpenAI summarization and Telegram notifications."""

from __future__ import annotations

import datetime as dt
import email
import json
import logging
import os
import time
from typing import Any

import boto3
import requests

from shared import config, dynamodb, s3_utils, telegram

logger = logging.getLogger(__name__)
logger.setLevel(os.getenv("LOG_LEVEL", "INFO"))


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Process S3 emails and send to Telegram."""
    cfg = config.get_config()
    
    # Get current bot info
    try:
        token = telegram.get_bot_token(cfg.telegram_secret_arn)
        bot_info = telegram.get_bot_info(token)
        current_bot_id = bot_info.get("id")
        current_bot_username = bot_info.get("username", "unknown")
    except Exception as e:
        logger.error(f"Failed to get bot info: {e}")
        current_bot_id = "unknown"
        current_bot_username = "unknown"
        bot_info = {"id": current_bot_id, "username": current_bot_username}
    
    logger.info(f"Processing with bot: @{current_bot_username} (ID: {current_bot_id})")
    
    processed = []
    migrated = []
    failed = []
    
    for record in event.get("Records", []):
        if record.get("eventSource") == "aws:s3":
            bucket = record["s3"]["bucket"]["name"]
            key = record["s3"]["object"]["key"]
            
            try:
                result = _process_s3_email(cfg, bucket, key, bot_info)
                
                if result["status"] == "processed":
                    processed.append(result["message_id"])
                elif result["status"] == "migrated":
                    migrated.append(result["message_id"])
                elif result["status"] == "failed":
                    failed.append(result["message_id"])
                    
            except Exception as exc:
                logger.exception(f"Failed to process {key}", extra={"error": str(exc)})
                failed.append(key)
    
    return {
        "statusCode": 200,
        "body": json.dumps({
            "processed": len(processed),
            "migrated": len(migrated),
            "failed": len(failed),
            "bot": current_bot_username,
            "details": {
                "processed": processed[:10],  # Limit output
                "migrated": migrated[:10],
                "failed": failed[:10]
            }
        })
    }


def _process_s3_email(
    cfg: config.RuntimeConfig, 
    bucket: str, 
    key: str, 
    current_bot_info: dict
) -> dict:
    """Process a single S3 email file."""
    # Parse S3 key to get message_id
    message_id = _extract_message_id_from_key(key)
    if not message_id:
        return {"status": "failed", "message_id": key, "reason": "invalid_key"}
    
    logger.info(f"Processing email: {message_id} from {key}")
    
    # Get email record from DynamoDB
    email_record = dynamodb.get_item(cfg.emails_table, {"message_id": message_id})
    if not email_record:
        # Try alternative message_id format
        alt_message_id = _try_alternative_message_id(key)
        if alt_message_id:
            email_record = dynamodb.get_item(cfg.emails_table, {"message_id": alt_message_id})
        
        if not email_record:
            logger.error(f"No email record found for {message_id}")
            return {"status": "failed", "message_id": message_id, "reason": "not_found"}
    
    # Check if already processed
    if email_record.get("state") == "PROCESSED":
        logger.info(f"Email already processed: {message_id}")
        return {"status": "skipped", "message_id": message_id}
    
    # Handle bot migration if needed
    email_bot_id = email_record.get("telegram_bot_id")
    current_bot_id = current_bot_info.get("id")
    needs_migration = email_record.get("needs_migration", False)
    
    if needs_migration or (email_bot_id and email_bot_id != current_bot_id):
        # Try to migrate email to current bot
        migrated = _migrate_email_to_current_bot(cfg, email_record, current_bot_info)
        if not migrated:
            logger.warning(f"Email {message_id} needs migration but user hasn't migrated yet")
            return {"status": "queued", "message_id": message_id, "reason": "needs_migration"}
        
        # Refresh email record after migration
        email_record = dynamodb.get_item(cfg.emails_table, {"message_id": message_id})
    
    # Process the email
    return _process_email_content(cfg, bucket, key, email_record)


def _migrate_email_to_current_bot(
    cfg: config.RuntimeConfig, 
    email_record: dict, 
    current_bot_info: dict
) -> bool:
    """Migrate email to use current bot."""
    message_id = email_record.get("message_id")
    alias_id = email_record.get("alias_id")
    
    # Get alias record
    alias_record = dynamodb.get_item(cfg.aliases_table, {"alias_id": alias_id})
    if not alias_record:
        logger.error(f"No alias record for {alias_id}")
        return False
    
    # Check if alias has migrated to current bot
    alias_bot_id = alias_record.get("telegram_bot_id")
    if alias_bot_id == current_bot_info.get("id"):
        # User has migrated! Update email record
        new_chat_id = alias_record.get("telegram_chat_id")
        
        dynamodb.update_item(
            cfg.emails_table,
            {"message_id": message_id},
            UpdateExpression="""
                SET telegram_chat_id = :chat_id,
                    telegram_bot_id = :bot_id,
                    telegram_bot_username = :bot_username,
                    needs_migration = :no_migration,
                    migrated_at = :now
            """,
            ExpressionAttributeValues={
                ":chat_id": str(new_chat_id),
                ":bot_id": current_bot_info.get("id"),
                ":bot_username": current_bot_info.get("username", ""),
                ":no_migration": False,
                ":now": dt.datetime.now(dt.timezone.utc).isoformat()
            }
        )
        
        logger.info(f"✅ Migrated email {message_id} to new bot")
        return True
    
    return False


def _process_email_content(
    cfg: config.RuntimeConfig, 
    bucket: str, 
    key: str, 
    email_record: dict
) -> dict:
    """Process email content and send to Telegram."""
    message_id = email_record.get("message_id")
    telegram_chat_id = email_record.get("telegram_chat_id")
    alias_id = email_record.get("alias_id")
    
    if not telegram_chat_id:
        logger.error(f"No telegram_chat_id for {message_id}")
        return {"status": "failed", "message_id": message_id, "reason": "no_chat_id"}
    
    try:
        # Read email from S3
        raw_bytes = s3_utils.get_raw_email(bucket, key)
        msg = email.message_from_bytes(raw_bytes)
        
        # Extract subject and body
        subject = msg.get("Subject", "(no subject)")
        body_text = _extract_email_body(msg)
        
        # Summarize with OpenAI
        summary = _summarize_email(subject, body_text)
        
        # Build download URL
        base_url = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
        download_url = f"{base_url}/email/{alias_id}/{message_id}" if base_url else None
        
        # Send to Telegram
        success = _send_telegram_message(
            cfg, telegram_chat_id, alias_id, subject, summary, download_url
        )
        
        if success:
            # Mark as processed
            dynamodb.update_item(
                cfg.emails_table,
                {"message_id": message_id},
                UpdateExpression="""
                    SET #state = :state, 
                        processed_at = :ts,
                        telegram_sent_at = :sent_ts
                """,
                ExpressionAttributeNames={"#state": "state"},
                ExpressionAttributeValues={
                    ":state": "PROCESSED",
                    ":ts": dt.datetime.now(dt.timezone.utc).isoformat(),
                    ":sent_ts": dt.datetime.now(dt.timezone.utc).isoformat()
                }
            )
            
            logger.info(f"✅ Successfully sent email {message_id} to Telegram")
            return {"status": "processed", "message_id": message_id}
        else:
            logger.error(f"❌ Failed to send email {message_id} to Telegram")
            
            # Mark as failed after retries
            dynamodb.update_item(
                cfg.emails_table,
                {"message_id": message_id},
                UpdateExpression="SET #state = :state, failed_at = :ts",
                ExpressionAttributeNames={"#state": "state"},
                ExpressionAttributeValues={
                    ":state": "FAILED",
                    ":ts": dt.datetime.now(dt.timezone.utc).isoformat()
                }
            )
            
            return {"status": "failed", "message_id": message_id, "reason": "telegram_failed"}
            
    except Exception as e:
        logger.exception(f"Error processing email {message_id}: {e}")
        return {"status": "failed", "message_id": message_id, "reason": str(e)}


def _extract_message_id_from_key(key: str) -> str:
    """Extract message_id from S3 key."""
    # Key format: alias_id/YYYY/MM/DD/message_id-uuid.eml
    parts = key.split("/")
    if len(parts) < 4:
        return ""
    
    filename = parts[-1]  # message_id-uuid.eml
    if not filename.endswith(".eml"):
        return ""
    
    # Remove .eml extension
    filename_no_ext = filename[:-4]
    
    # Return full filename without extension as message_id
    return filename_no_ext


def _try_alternative_message_id(key: str) -> str:
    """Try alternative message_id extraction."""
    parts = key.split("/")
    filename = parts[-1] if parts else ""
    
    if not filename.endswith(".eml"):
        return ""
    
    filename_no_ext = filename[:-4]
    
    # Try splitting by dash and taking first part
    if "-" in filename_no_ext:
        return filename_no_ext.split("-")[0]
    
    return filename_no_ext


def _extract_email_body(msg: email.message.EmailMessage) -> str:
    """Extract text body from email."""
    body = ""
    
    # Try to get plain text first
    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            content_disposition = str(part.get("Content-Disposition", "")).lower()
            
            # Skip attachments
            if "attachment" in content_disposition:
                continue
            
            if content_type == "text/plain":
                try:
                    payload = part.get_payload(decode=True)
                    if payload:
                        charset = part.get_content_charset() or "utf-8"
                        body = payload.decode(charset, errors="ignore")
                        break
                except:
                    pass
    else:
        try:
            payload = msg.get_payload(decode=True)
            if payload:
                charset = msg.get_content_charset() or "utf-8"
                body = payload.decode(charset, errors="ignore")
        except:
            pass
    
    # If no plain text, try HTML
    if not body.strip():
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/html":
                    try:
                        payload = part.get_payload(decode=True)
                        if payload:
                            charset = part.get_content_charset() or "utf-8"
                            html = payload.decode(charset, errors="ignore")
                            # Simple HTML to text conversion
                            import re
                            body = re.sub(r'<[^>]+>', '', html)
                            body = re.sub(r'\n{3,}', '\n\n', body)
                            break
                    except:
                        pass
    
    # Truncate if too long
    if len(body) > 15000:
        body = body[:15000] + "...\n[Email truncated]"
    
    return body.strip()


def _summarize_email(subject: str, body: str) -> str:
    """Summarize email using OpenAI."""
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key or not body.strip():
        # Fallback: return truncated body
        if len(body) > 1000:
            return body[:1000] + "..."
        return body
    
    # Prepare prompt
    prompt = f"""Please summarize this email in 2-3 concise sentences:

Subject: {subject}

Email content:
{body[:12000]}"""
    
    payload = {
        "model": os.environ.get("OPENAI_MODEL", "gpt-3.5-turbo"),
        "messages": [
            {
                "role": "system", 
                "content": "You are a helpful assistant that summarizes emails concisely. Focus on key points and actions needed."
            },
            {"role": "user", "content": prompt}
        ],
        "max_tokens": 300,
        "temperature": 0.3,
    }
    
    try:
        response = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json"
            },
            json=payload,
            timeout=30
        )
        
        if response.status_code == 200:
            data = response.json()
            summary = data["choices"][0]["message"]["content"].strip()
            
            # Validate summary
            if summary and len(summary) > 10:
                return summary
        
        logger.warning(f"OpenAI returned non-200: {response.status_code}")
        
    except requests.exceptions.Timeout:
        logger.warning("OpenAI request timed out")
    except Exception as e:
        logger.error(f"OpenAI error: {e}")
    
    # Fallback: return truncated body
    if len(body) > 1000:
        return body[:1000] + "..."
    return body


def _send_telegram_message(
    cfg: config.RuntimeConfig,
    telegram_chat_id: str,
    alias_id: str,
    subject: str,
    summary: str,
    download_url: str | None,
    max_retries: int = 3
) -> bool:
    """Send message to Telegram with retry logic."""
    try:
        token = telegram.get_bot_token(cfg.telegram_secret_arn)
        chat_id = int(telegram_chat_id)
        
        # Build message
        message_lines = [
            "📧 *New Email Summary*",
            "",
            f"*Subject:* {subject}",
            "",
            "*Summary:*",
            summary
        ]
        
        if download_url:
            message_lines.extend(["", f"[📎 Download Original Email]({download_url})"])
        
        message_text = "\n".join(message_lines)
        
        # Check length limit (Telegram max: 4096 chars)
        if len(message_text) > 4000:
            # Truncate summary
            available = 4000 - len("\n".join(message_lines[:5])) - 50
            if available > 100:
                summary = summary[:available] + "..."
                message_lines[4] = summary
                message_text = "\n".join(message_lines)
            else:
                # Very long subject, truncate it
                subject = subject[:100] + "..."
                message_lines[2] = f"*Subject:* {subject}"
                message_text = "\n".join(message_lines)[:4000]
        
        # Build inline keyboard
        keyboard = {
            "inline_keyboard": [[
                {
                    "text": "🚫 Disable this address",
                    "callback_data": f"disable:{alias_id}"
                }
            ]]
        }
        
        # Send with retry
        for attempt in range(max_retries):
            try:
                logger.info(f"Sending to Telegram (attempt {attempt + 1}/{max_retries})")
                
                success = telegram.send_message(
                    token=token,
                    chat_id=chat_id,
                    text=message_text,
                    parse_mode="Markdown",
                    reply_markup=keyboard
                )
                
                if success:
                    logger.info(f"✅ Telegram message sent to {chat_id}")
                    return True
                else:
                    logger.warning(f"Telegram send failed (attempt {attempt + 1})")
                    
                    if attempt < max_retries - 1:
                        wait_time = 2 ** attempt  # 1, 2, 4 seconds
                        time.sleep(wait_time)
                        
            except Exception as e:
                logger.error(f"Telegram attempt {attempt + 1} error: {e}")
                if attempt < max_retries - 1:
                    time.sleep(2 ** attempt)
        
        logger.error(f"All Telegram attempts failed for chat {chat_id}")
        return False
        
    except ValueError:
        logger.error(f"Invalid chat_id (not a number): {telegram_chat_id}")
        return False
    except Exception as e:
        logger.exception(f"Telegram send error: {e}")
        return False
