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
import requests  # REQUIRED for OpenAI

from shared import config, dynamodb, s3_utils, telegram

logger = logging.getLogger(__name__)
logger.setLevel(os.getenv("LOG_LEVEL", "INFO"))


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Process S3 emails and send summarized versions to Telegram."""
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
    
    logger.info(f"📧 Processing emails with bot: @{current_bot_username}")
    
    processed = 0
    failed = 0
    migrated = 0
    
    for record in event.get("Records", []):
        if record.get("eventSource") == "aws:s3":
            bucket = record["s3"]["bucket"]["name"]
            key = record["s3"]["object"]["key"]
            
            try:
                result = _process_email(cfg, bucket, key, bot_info)
                
                if result == "processed":
                    processed += 1
                elif result == "migrated":
                    migrated += 1
                elif result == "failed":
                    failed += 1
                    
            except Exception as exc:
                logger.exception(f"Failed to process {key}", extra={"error": str(exc)})
                failed += 1
    
    logger.info(f"✅ Complete: {processed} processed, {migrated} migrated, {failed} failed")
    
    return {
        "statusCode": 200,
        "body": json.dumps({
            "processed": processed,
            "migrated": migrated,
            "failed": failed,
            "bot": current_bot_username,
            "timestamp": dt.datetime.now(dt.timezone.utc).isoformat()
        })
    }


def _process_email(
    cfg: config.RuntimeConfig, 
    bucket: str, 
    key: str, 
    current_bot_info: dict
) -> str:
    """Process a single email from S3."""
    # Extract message_id from S3 key
    # Format: alias_id/YYYY/MM/DD/message_id-uuid.eml
    parts = key.split("/")
    if len(parts) < 4:
        logger.error(f"Invalid S3 key format: {key}")
        return "failed"
    
    filename = parts[-1]  # message_id-uuid.eml
    if not filename.endswith(".eml"):
        logger.error(f"Not an .eml file: {filename}")
        return "failed"
    
    # Remove .eml extension to get message_id
    message_id = filename[:-4]  # message_id-uuid
    
    logger.info(f"📨 Processing email: {message_id}")
    
    # Get email record from DynamoDB
    email_record = dynamodb.get_item(cfg.emails_table, {"message_id": message_id})
    
    # If not found, try alternative format (just the message_id part before dash)
    if not email_record and "-" in message_id:
        alt_message_id = message_id.split("-")[0]
        email_record = dynamodb.get_item(cfg.emails_table, {"message_id": alt_message_id})
        if email_record:
            logger.info(f"Found email with alternative ID: {alt_message_id}")
            message_id = alt_message_id
    
    if not email_record:
        logger.error(f"❌ No DynamoDB record found for {message_id}")
        return "failed"
    
    # Check if already processed
    if email_record.get("state") == "PROCESSED":
        logger.info(f"⏭️ Already processed: {message_id}")
        return "skipped"
    
    # Handle bot migration if needed
    email_bot_id = email_record.get("telegram_bot_id")
    current_bot_id = current_bot_info.get("id")
    needs_migration = email_record.get("needs_migration", False)
    
    if needs_migration or (email_bot_id and email_bot_id != current_bot_id):
        logger.info(f"🔄 Email {message_id} needs bot migration")
        migration_result = _handle_bot_migration(cfg, email_record, current_bot_info)
        
        if migration_result == "migrated":
            # Refresh email record after migration
            email_record = dynamodb.get_item(cfg.emails_table, {"message_id": message_id})
            logger.info(f"✅ Successfully migrated email {message_id}")
        elif migration_result == "queued":
            logger.info(f"⏳ Email queued for migration: {message_id}")
            return "queued"
        else:
            logger.error(f"❌ Migration failed for {message_id}")
            return "failed"
    
    # Now process the email content
    return _process_email_content(cfg, bucket, key, email_record)


def _handle_bot_migration(cfg, email_record, current_bot_info):
    """Handle migration of email to current bot."""
    message_id = email_record.get("message_id")
    alias_id = email_record.get("alias_id")
    
    # Get alias record
    alias_record = dynamodb.get_item(cfg.aliases_table, {"alias_id": alias_id})
    if not alias_record:
        logger.error(f"No alias record for {alias_id}")
        return "failed"
    
    # Check if alias uses current bot
    alias_bot_id = alias_record.get("telegram_bot_id")
    if alias_bot_id == current_bot_info.get("id"):
        # User has migrated to current bot
        new_chat_id = alias_record.get("telegram_chat_id")
        
        dynamodb.update_item(
            cfg.emails_table,
            {"message_id": message_id},
            UpdateExpression="""
                SET telegram_chat_id = :chat_id,
                    telegram_bot_id = :bot_id,
                    telegram_bot_username = :bot_username,
                    needs_migration = :false,
                    migrated_at = :now
            """,
            ExpressionAttributeValues={
                ":chat_id": str(new_chat_id),
                ":bot_id": current_bot_info.get("id"),
                ":bot_username": current_bot_info.get("username", ""),
                ":false": False,
                ":now": dt.datetime.now(dt.timezone.utc).isoformat()
            }
        )
        return "migrated"
    
    # User hasn't migrated yet
    return "queued"


def _process_email_content(cfg, bucket, key, email_record):
    """Process email content and send to Telegram."""
    message_id = email_record.get("message_id")
    telegram_chat_id = email_record.get("telegram_chat_id")
    alias_id = email_record.get("alias_id")
    
    if not telegram_chat_id:
        logger.error(f"❌ No telegram_chat_id for {message_id}")
        return "failed"
    
    try:
        # 1. Read email from S3
        raw_bytes = s3_utils.get_raw_email(bucket, key)
        msg = email.message_from_bytes(raw_bytes)
        
        # 2. Extract email content
        subject = msg.get("Subject", "(no subject)")
        body_text = _extract_email_body(msg)
        
        # 3. Summarize with OpenAI
        summary = _summarize_with_openai(subject, body_text)
        
        # 4. Build download URL
        base_url = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
        download_url = f"{base_url}/email/{alias_id}/{message_id}" if base_url else None
        
        # 5. Send to Telegram
        success = _send_to_telegram(
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
                        telegram_sent_at = :sent_ts,
                        summary = :summary
                """,
                ExpressionAttributeNames={"#state": "state"},
                ExpressionAttributeValues={
                    ":state": "PROCESSED",
                    ":ts": dt.datetime.now(dt.timezone.utc).isoformat(),
                    ":sent_ts": dt.datetime.now(dt.timezone.utc).isoformat(),
                    ":summary": summary[:500]  # Store truncated summary
                }
            )
            
            logger.info(f"✅ Successfully sent email {message_id} to Telegram")
            return "processed"
        else:
            logger.error(f"❌ Failed to send email {message_id} to Telegram")
            
            # Mark as failed
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
            
            return "failed"
            
    except Exception as e:
        logger.exception(f"❌ Error processing email {message_id}: {e}")
        return "failed"


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
                            # Remove script/style tags
                            html = re.sub(r'<(script|style).*?>.*?</\1>', '', html, flags=re.DOTALL)
                            # Replace <br> with newlines
                            html = re.sub(r'<br\s*/?>', '\n', html, flags=re.IGNORECASE)
                            # Remove all remaining tags
                            body = re.sub(r'<[^>]+>', '', html)
                            body = re.sub(r'\n{3,}', '\n\n', body)
                            break
                    except:
                        pass
    
    # Clean up
    body = body.strip()
    
    # Truncate if too long for OpenAI
    if len(body) > 12000:
        body = body[:12000] + "...\n[Email truncated for summarization]"
    
    return body


def _summarize_with_openai(subject: str, body: str) -> str:
    """
    Summarize email using OpenAI API.
    
    Returns: Summary if successful, fallback text if not.
    """
    # Check if OpenAI is configured
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        logger.warning("⚠️ OPENAI_API_KEY not set, using fallback")
        return _create_fallback_summary(subject, body)
    
    # Don't summarize very short emails
    if len(body) < 100:
        logger.info("Email too short for summarization")
        return f"Short email: {body}"
    
    # Prepare prompt
    prompt = f"""Please summarize this email in 2-3 concise sentences for a busy Telegram user.

Email Subject: {subject}

Email Content:
{body}

Provide a clear, concise summary focusing on:
1. The main purpose of the email
2. Any important details or actions needed
3. Key takeaways

Summary:"""
    
    payload = {
        "model": os.environ.get("OPENAI_MODEL", "gpt-3.5-turbo"),
        "messages": [
            {
                "role": "system", 
                "content": "You are a helpful assistant that creates concise, clear email summaries."
            },
            {"role": "user", "content": prompt}
        ],
        "max_tokens": 300,
        "temperature": 0.3,
    }
    
    try:
        logger.info(f"🤖 Calling OpenAI API for summarization (body length: {len(body)} chars)")
        
        response = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json"
            },
            json=payload,
            timeout=30  # 30 second timeout
        )
        
        if response.status_code == 200:
            data = response.json()
            summary = data["choices"][0]["message"]["content"].strip()
            
            # Validate summary
            if summary and len(summary) > 20:
                logger.info(f"✅ OpenAI summary successful ({len(summary)} chars)")
                return summary
            else:
                logger.warning("OpenAI returned empty summary")
                return _create_fallback_summary(subject, body)
        
        else:
            logger.error(f"❌ OpenAI API error: {response.status_code} - {response.text}")
            return _create_fallback_summary(subject, body)
            
    except requests.exceptions.Timeout:
        logger.warning("⏱️ OpenAI request timed out")
        return _create_fallback_summary(subject, body)
    except Exception as e:
        logger.error(f"❌ OpenAI error: {e}")
        return _create_fallback_summary(subject, body)


def _create_fallback_summary(subject: str, body: str) -> str:
    """Create a fallback summary when OpenAI is unavailable."""
    # Simple truncation for short emails
    if len(body) <= 500:
        return body
    
    # Try to extract first few sentences
    import re
    
    # Split into sentences
    sentences = re.split(r'[.!?]+', body)
    
    # Take first 3-5 sentences
    summary_sentences = []
    for sentence in sentences:
        sentence = sentence.strip()
        if sentence and len(sentence) > 10:
            summary_sentences.append(sentence)
            if len(summary_sentences) >= 5 or len(' '.join(summary_sentences)) > 400:
                break
    
    if summary_sentences:
        summary = ' '.join(summary_sentences)
        if len(summary) > 500:
            summary = summary[:500] + "..."
        return summary
    
    # Fallback: just truncate
    return body[:500] + "..."


def _send_to_telegram(
    cfg: config.RuntimeConfig,
    telegram_chat_id: str,
    alias_id: str,
    subject: str,
    summary: str,
    download_url: str | None,
    max_retries: int = 3
) -> bool:
    """Send summarized email to Telegram."""
    try:
        token = telegram.get_bot_token(cfg.telegram_secret_arn)
        chat_id = int(telegram_chat_id)
        
        # Build the Telegram message
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
        
        # Telegram has 4096 character limit
        if len(message_text) > 4000:
            # Truncate the summary
            available_chars = 4000 - len("\n".join(message_lines[:5])) - 100
            if available_chars > 100:
                summary = summary[:available_chars] + "..."
                message_lines[4] = summary
                message_text = "\n".join(message_lines)
            else:
                # Even subject is too long
                subject = subject[:100] + "..."
                message_lines[2] = f"*Subject:* {subject}"
                message_text = "\n".join(message_lines)[:4000]
        
        # Build inline keyboard
        keyboard = {
            "inline_keyboard": [[
                {
                    "text": "🚫 Disable this email address",
                    "callback_data": f"disable:{alias_id}"
                }
            ]]
        }
        
        # Send with retry logic
        for attempt in range(max_retries):
            try:
                logger.info(f"📤 Sending to Telegram (attempt {attempt + 1}/{max_retries})")
                
                success = telegram.send_message(
                    token=token,
                    chat_id=chat_id,
                    text=message_text,
                    parse_mode="Markdown",
                    reply_markup=keyboard
                )
                
                if success:
                    logger.info(f"✅ Telegram message sent successfully to chat {chat_id}")
                    return True
                else:
                    logger.warning(f"⚠️ Telegram send failed (attempt {attempt + 1})")
                    
                    if attempt < max_retries - 1:
                        wait_time = 2 ** attempt  # Exponential backoff: 1, 2, 4 seconds
                        logger.info(f"⏱️ Waiting {wait_time}s before retry...")
                        time.sleep(wait_time)
                        
            except Exception as e:
                logger.error(f"❌ Telegram attempt {attempt + 1} error: {e}")
                if attempt < max_retries - 1:
                    time.sleep(2 ** attempt)
        
        logger.error(f"❌ All Telegram attempts failed for chat {chat_id}")
        return False
        
    except ValueError:
        logger.error(f"❌ Invalid chat_id (not a number): {telegram_chat_id}")
        return False
    except Exception as e:
        logger.exception(f"❌ Telegram send error: {e}")
        return False
