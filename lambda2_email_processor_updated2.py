"""
Lambda #2: Process emails from S3, summarize with OpenAI, send to Telegram.

This Lambda:
1. Triggered by S3 when new emails are saved
2. Parses the email content
3. Sends to OpenAI for summarization
4. Sends summary to Telegram user
5. Updates DynamoDB with processing status
"""

from __future__ import annotations

import datetime as dt
import email
import json
import logging
import os
import re
import time
from typing import Any, Dict, Optional

import boto3
import requests  # For OpenAI API calls

logger = logging.getLogger(__name__)
logger.setLevel(os.getenv("LOG_LEVEL", "INFO"))

# Initialize AWS clients
_s3 = boto3.client("s3")
_dynamodb = boto3.resource("dynamodb")

# Telegram API base URL
TELEGRAM_API_BASE = "https://api.telegram.org"

# OpenAI configuration
OPENAI_API_URL = os.getenv("OPENAI_API_URL", "https://api.openai.com/v1/chat/completions")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-3.5-turbo")
OPENAI_MAX_TOKENS = int(os.getenv("OPENAI_MAX_TOKENS", "300"))
OPENAI_TIMEOUT = int(os.getenv("OPENAI_TIMEOUT", "30"))

# Telegram message limits
TELEGRAM_MAX_LENGTH = 4096
TELEGRAM_SUMMARY_MAX_LENGTH = 3500  # Leave room for formatting


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """
    Main Lambda handler - process S3 emails and send to Telegram.
    """
    logger.info("🚀 Starting email processing")
    
    processed = 0
    failed = 0
    skipped = 0
    
    for record in event.get("Records", []):
        try:
            if record.get("eventSource") == "aws:s3":
                bucket = record["s3"]["bucket"]["name"]
                key = record["s3"]["object"]["key"]
                
                logger.info(f"📨 Processing S3 object: s3://{bucket}/{key}")
                
                result = _process_single_email(bucket, key)
                
                if result == "processed":
                    processed += 1
                elif result == "failed":
                    failed += 1
                elif result == "skipped":
                    skipped += 1
                    
        except Exception as exc:
            logger.exception(f"❌ Failed to process record", extra={"error": str(exc), "record": record})
            failed += 1
    
    logger.info(f"✅ Processing complete: {processed} processed, {skipped} skipped, {failed} failed")
    
    return {
        "statusCode": 200,
        "body": json.dumps({
            "processed": processed,
            "skipped": skipped,
            "failed": failed,
            "timestamp": dt.datetime.now(dt.timezone.utc).isoformat()
        })
    }


def _process_single_email(bucket: str, key: str) -> str:
    """
    Process a single email from S3.
    Returns: "processed", "skipped", or "failed"
    """
    try:
        # Parse S3 key to get message_id
        # Format: alias_id/YYYY/MM/DD/message_id-uuid.eml
        parts = key.split("/")
        if len(parts) < 4:
            logger.error(f"Invalid S3 key format: {key}")
            return "failed"
        
        filename = parts[-1]
        if not filename.endswith(".eml"):
            logger.error(f"Not an .eml file: {filename}")
            return "failed"
        
        # Extract message_id (remove .eml and any suffix after last dash)
        message_id = filename[:-4]  # Remove .eml
        
        logger.info(f"Processing email: {message_id}")
        
        # Get email metadata from DynamoDB
        emails_table = os.environ["EMAILS_TABLE"]
        email_record = _get_email_record(emails_table, message_id)
        
        if not email_record:
            logger.error(f"Email record not found in DynamoDB: {message_id}")
            return "failed"
        
        # Check if already processed
        if email_record.get("state") == "PROCESSED":
            logger.info(f"Email already processed: {message_id}")
            return "skipped"
        
        # Get Telegram chat ID
        telegram_chat_id = email_record.get("telegram_chat_id")
        if not telegram_chat_id:
            logger.error(f"No telegram_chat_id for email: {message_id}")
            return "failed"
        
        alias_id = email_record.get("alias_id", "unknown")
        
        # Read and parse email from S3
        email_content = _read_email_from_s3(bucket, key)
        if not email_content:
            return "failed"
        
        # Extract subject and body
        subject, body_text = _extract_email_content(email_content)
        
        # Summarize with OpenAI
        summary = _summarize_with_openai(subject, body_text)
        
        # Build download URL (optional)
        download_url = _build_download_url(alias_id, message_id)
        
        # Send to Telegram
        telegram_success = _send_to_telegram(
            telegram_chat_id=telegram_chat_id,
            alias_id=alias_id,
            subject=subject,
            summary=summary,
            download_url=download_url
        )
        
        if telegram_success:
            # Mark as processed
            _mark_email_processed(emails_table, message_id, summary)
            logger.info(f"✅ Successfully processed and sent email: {message_id}")
            return "processed"
        else:
            # Mark as failed
            _mark_email_failed(emails_table, message_id)
            logger.error(f"❌ Failed to send email to Telegram: {message_id}")
            return "failed"
            
    except Exception as exc:
        logger.exception(f"❌ Error processing email: {exc}")
        return "failed"


def _get_email_record(emails_table: str, message_id: str) -> Optional[Dict[str, Any]]:
    """Get email record from DynamoDB."""
    try:
        table = _dynamodb.Table(emails_table)
        response = table.get_item(Key={"message_id": message_id})
        return response.get("Item")
    except Exception as exc:
        logger.error(f"Failed to get email record {message_id}: {exc}")
        return None


def _read_email_from_s3(bucket: str, key: str) -> Optional[email.message.EmailMessage]:
    """Read email from S3 and parse it."""
    try:
        response = _s3.get_object(Bucket=bucket, Key=key)
        raw_email = response["Body"].read()
        
        # Parse email
        msg = email.message_from_bytes(raw_email)
        return msg
        
    except Exception as exc:
        logger.error(f"Failed to read email from S3: {exc}")
        return None


def _extract_email_content(msg: email.message.EmailMessage) -> tuple[str, str]:
    """Extract subject and plain text body from email."""
    # Get subject
    subject = msg.get("Subject", "(no subject)")
    
    # Extract plain text body
    body_text = ""
    
    if msg.is_multipart():
        # Walk through all parts
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
                        body_text = payload.decode(charset, errors="ignore")
                        break
                except Exception:
                    pass
    else:
        # Single part email
        try:
            payload = msg.get_payload(decode=True)
            if payload:
                charset = msg.get_content_charset() or "utf-8"
                body_text = payload.decode(charset, errors="ignore")
        except Exception:
            pass
    
    # If no plain text, try to extract from HTML
    if not body_text.strip():
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/html":
                    try:
                        payload = part.get_payload(decode=True)
                        if payload:
                            charset = part.get_content_charset() or "utf-8"
                            html = payload.decode(charset, errors="ignore")
                            # Simple HTML to text conversion
                            body_text = _html_to_text(html)
                            break
                    except Exception:
                        pass
    
    # Clean up body text
    body_text = body_text.strip()
    
    # Truncate if too long for OpenAI
    if len(body_text) > 10000:
        body_text = body_text[:10000] + "\n\n[Email truncated for summarization]"
    
    return subject, body_text


def _html_to_text(html: str) -> str:
    """Convert HTML to plain text."""
    # Remove script and style tags
    html = re.sub(r'<(script|style).*?>.*?</\1>', '', html, flags=re.DOTALL | re.IGNORECASE)
    
    # Replace common HTML elements with newlines
    html = re.sub(r'<br\s*/?>', '\n', html, flags=re.IGNORECASE)
    html = re.sub(r'</(p|div|tr|li)>', '\n', html, flags=re.IGNORECASE)
    
    # Remove all remaining tags
    html = re.sub(r'<[^>]+>', '', html)
    
    # Decode HTML entities
    try:
        import html as html_module
        html = html_module.unescape(html)
    except:
        # Basic entity decoding
        html = html.replace('&nbsp;', ' ')
        html = html.replace('&amp;', '&')
        html = html.replace('&lt;', '<')
        html = html.replace('&gt;', '>')
        html = html.replace('&quot;', '"')
    
    # Normalize whitespace
    html = re.sub(r'[ \t]+', ' ', html)
    html = re.sub(r'\n{3,}', '\n\n', html)
    
    return html.strip()


def _summarize_with_openai(subject: str, body: str) -> str:
    """
    Summarize email content using OpenAI API.
    Returns summary or fallback text if OpenAI fails.
    """
    # Check if OpenAI is configured
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        logger.warning("⚠️ OPENAI_API_KEY not set, using fallback summary")
        return _create_fallback_summary(body)
    
    # Don't summarize very short emails
    if len(body) < 100:
        logger.info("Email too short for summarization")
        return body
    
    # Prepare the prompt
    prompt = f"""Please summarize this email in 2-3 concise sentences:

Subject: {subject}

Email Content:
{body}

Focus on:
1. The main purpose or key message
2. Any important details or requests
3. Action items if mentioned

Summary:"""
    
    payload = {
        "model": OPENAI_MODEL,
        "messages": [
            {
                "role": "system",
                "content": "You are a helpful assistant that summarizes emails clearly and concisely."
            },
            {
                "role": "user",
                "content": prompt
            }
        ],
        "max_tokens": OPENAI_MAX_TOKENS,
        "temperature": 0.3,
    }
    
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    
    try:
        logger.info(f"🤖 Calling OpenAI API (model: {OPENAI_MODEL})")
        
        response = requests.post(
            OPENAI_API_URL,
            headers=headers,
            json=payload,
            timeout=OPENAI_TIMEOUT
        )
        
        if response.status_code == 200:
            data = response.json()
            summary = data["choices"][0]["message"]["content"].strip()
            
            if summary and len(summary) > 10:
                logger.info(f"✅ OpenAI summary successful ({len(summary)} chars)")
                return summary
            else:
                logger.warning("OpenAI returned empty summary")
                return _create_fallback_summary(body)
        else:
            logger.error(f"❌ OpenAI API error: {response.status_code} - {response.text}")
            return _create_fallback_summary(body)
            
    except requests.exceptions.Timeout:
        logger.warning("⏱️ OpenAI request timed out")
        return _create_fallback_summary(body)
    except Exception as exc:
        logger.error(f"❌ OpenAI error: {exc}")
        return _create_fallback_summary(body)


def _create_fallback_summary(body: str) -> str:
    """Create a fallback summary when OpenAI is unavailable."""
    # Simple truncation
    if len(body) <= 500:
        return body
    
    # Try to extract first few sentences
    sentences = re.split(r'[.!?]+', body)
    
    summary_sentences = []
    char_count = 0
    
    for sentence in sentences:
        sentence = sentence.strip()
        if sentence and len(sentence) > 10:
            summary_sentences.append(sentence)
            char_count += len(sentence) + 1  # +1 for space/punctuation
            
            if len(summary_sentences) >= 3 or char_count > 400:
                break
    
    if summary_sentences:
        summary = '. '.join(summary_sentences) + '.'
        if len(summary) > 500:
            summary = summary[:500] + "..."
        return summary
    
    # Fallback: just truncate
    return body[:500] + "..."


def _build_download_url(alias_id: str, message_id: str) -> Optional[str]:
    """Build download URL for the original email."""
    base_url = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
    if not base_url:
        return None
    
    return f"{base_url}/email/{alias_id}/{message_id}"


def _send_to_telegram(
    telegram_chat_id: str,
    alias_id: str,
    subject: str,
    summary: str,
    download_url: Optional[str],
    max_retries: int = 3
) -> bool:
    """Send email summary to Telegram."""
    # Get Telegram bot token from Secrets Manager
    telegram_secret_arn = os.environ["TELEGRAM_SECRET_ARN"]
    bot_token = _get_telegram_token(telegram_secret_arn)
    
    if not bot_token:
        logger.error("❌ Failed to get Telegram bot token")
        return False
    
    # Build the message
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
    
    # Ensure message doesn't exceed Telegram limits
    if len(message_text) > TELEGRAM_SUMMARY_MAX_LENGTH:
        # Calculate how much we need to truncate
        truncate_by = len(message_text) - TELEGRAM_SUMMARY_MAX_LENGTH + 100  # Add buffer
        
        # Try to truncate the summary
        if len(summary) > truncate_by:
            summary = summary[:-truncate_by] + "..."
            message_lines[4] = summary
            message_text = "\n".join(message_lines)
        else:
            # Even truncating summary isn't enough, truncate everything
            message_text = message_text[:TELEGRAM_SUMMARY_MAX_LENGTH] + "..."
    
    # Build inline keyboard for disabling the alias
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
            
            url = f"{TELEGRAM_API_BASE}/bot{bot_token}/sendMessage"
            
            payload = {
                "chat_id": int(telegram_chat_id),
                "text": message_text,
                "parse_mode": "Markdown",
                "reply_markup": keyboard,
                "disable_web_page_preview": True
            }
            
            response = requests.post(url, json=payload, timeout=10)
            
            if response.status_code == 200:
                data = response.json()
                if data.get("ok", False):
                    logger.info(f"✅ Telegram message sent to chat {telegram_chat_id}")
                    return True
                else:
                    error_msg = data.get("description", "Unknown error")
                    logger.error(f"Telegram API error: {error_msg}")
            else:
                logger.error(f"Telegram HTTP error: {response.status_code} - {response.text}")
            
            # If we get here, it failed
            if attempt < max_retries - 1:
                wait_time = 2 ** attempt  # Exponential backoff
                logger.info(f"⏱️ Waiting {wait_time}s before retry...")
                time.sleep(wait_time)
                
        except Exception as exc:
            logger.error(f"❌ Telegram send error (attempt {attempt + 1}): {exc}")
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
    
    logger.error(f"❌ All Telegram attempts failed for chat {telegram_chat_id}")
    return False


def _get_telegram_token(secret_arn: str) -> Optional[str]:
    """Get Telegram bot token from Secrets Manager."""
    try:
        secrets = boto3.client("secretsmanager")
        response = secrets.get_secret_value(SecretId=secret_arn)
        secret_str = response.get("SecretString", "{}")
        
        # Try to parse as JSON
        try:
            data = json.loads(secret_str)
            token = data.get("bot_token", "")
        except json.JSONDecodeError:
            # Assume the entire string is the token
            token = secret_str
        
        if token:
            return token.strip()
        else:
            logger.error("No Telegram token found in secret")
            return None
            
    except Exception as exc:
        logger.error(f"Failed to get Telegram token: {exc}")
        return None


def _mark_email_processed(emails_table: str, message_id: str, summary: str) -> None:
    """Mark email as processed in DynamoDB."""
    try:
        table = _dynamodb.Table(emails_table)
        
        table.update_item(
            Key={"message_id": message_id},
            UpdateExpression="""
                SET #state = :state,
                    processed_at = :processed_at,
                    summary = :summary
            """,
            ExpressionAttributeNames={
                "#state": "state"
            },
            ExpressionAttributeValues={
                ":state": "PROCESSED",
                ":processed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                ":summary": summary[:500]  # Store truncated summary
            }
        )
    except Exception as exc:
        logger.error(f"Failed to mark email as processed: {exc}")


def _mark_email_failed(emails_table: str, message_id: str) -> None:
    """Mark email as failed in DynamoDB."""
    try:
        table = _dynamodb.Table(emails_table)
        
        table.update_item(
            Key={"message_id": message_id},
            UpdateExpression="""
                SET #state = :state,
                    failed_at = :failed_at
            """,
            ExpressionAttributeNames={
                "#state": "state"
            },
            ExpressionAttributeValues={
                ":state": "FAILED",
                ":failed_at": dt.datetime.now(dt.timezone.utc).isoformat()
            }
        )
    except Exception as exc:
        logger.error(f"Failed to mark email as failed: {exc}")
