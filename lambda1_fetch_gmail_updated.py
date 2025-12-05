"""
Lambda #1: Fetch emails from Gmail and store in S3.

This Lambda:
1. Logs into Gmail using credentials from Secrets Manager
2. Checks for unread emails using IMAP
3. Extracts emails and stores raw .eml files in S3
4. Saves email metadata to DynamoDB for processing
"""

from __future__ import annotations

import datetime as dt
import email
import imaplib
import json
import logging
import os
import uuid
from email.utils import getaddresses, parsedate_to_datetime
from typing import Any, Dict, Optional, Tuple

import boto3

logger = logging.getLogger(__name__)
logger.setLevel(os.getenv("LOG_LEVEL", "INFO"))

# Initialize AWS clients
_s3 = boto3.client("s3")
_secrets = boto3.client("secretsmanager")
_dynamodb = boto3.resource("dynamodb")

# Email retention: 14 days
EMAIL_TTL_SECONDS = 14 * 24 * 60 * 60


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """
    Main Lambda handler to fetch and process unread Gmail emails.
    """
    try:
        logger.info("🚀 Starting Gmail email fetch")
        
        # Get configuration from environment
        bucket_name = os.environ["RAW_EMAIL_BUCKET"]
        emails_table = os.environ["EMAILS_TABLE"]
        aliases_table = os.environ["ALIASES_TABLE"]
        gmail_secret_arn = os.environ["GMAIL_SECRET_ARN"]
        
        # Get Gmail credentials from Secrets Manager
        gmail_user, gmail_pass = _get_gmail_credentials(gmail_secret_arn)
        
        # Connect to Gmail
        mail = imaplib.IMAP4_SSL("imap.gmail.com")
        mail.login(gmail_user, gmail_pass)
        mail.select("inbox")
        
        # Search for unread emails
        status, messages = mail.search(None, "UNSEEN")
        if status != "OK":
            logger.error("Failed to search emails")
            return {"processed": 0, "error": "Search failed"}
        
        email_ids = messages[0].split()
        logger.info(f"📨 Found {len(email_ids)} unread emails")
        
        processed = 0
        failed = 0
        
        # Process each unread email
        for e_id in email_ids:
            try:
                if _process_single_email(
                    mail=mail,
                    email_id=e_id,
                    bucket_name=bucket_name,
                    emails_table=emails_table,
                    aliases_table=aliases_table
                ):
                    processed += 1
                else:
                    failed += 1
                    
            except Exception as exc:
                logger.exception(f"Failed to process email {e_id}", extra={"error": str(exc)})
                failed += 1
        
        # Close connection
        mail.close()
        mail.logout()
        
        logger.info(f"✅ Email fetch completed: {processed} processed, {failed} failed")
        
        return {
            "statusCode": 200,
            "body": json.dumps({
                "processed": processed,
                "failed": failed,
                "total": len(email_ids),
                "timestamp": dt.datetime.now(dt.timezone.utc).isoformat()
            })
        }
        
    except Exception as exc:
        logger.exception("❌ Failed to fetch emails", extra={"error": str(exc)})
        return {
            "statusCode": 500,
            "body": json.dumps({"error": str(exc)})
        }


def _get_gmail_credentials(secret_arn: str) -> Tuple[str, str]:
    """
    Get Gmail credentials from AWS Secrets Manager.
    
    Secret format can be:
    1. JSON: {"email_user_name": "xxx", "email_password": "xxx"}
    2. Plain text: "user:password"
    """
    try:
        response = _secrets.get_secret_value(SecretId=secret_arn)
        secret_str = response.get("SecretString", "{}")
        
        # Try to parse as JSON
        try:
            data = json.loads(secret_str)
            email_user = data.get("email_user_name", "")
            email_pass = data.get("email_password", "")
        except json.JSONDecodeError:
            # Fallback: assume "user:password" format
            if ":" in secret_str:
                email_user, email_pass = secret_str.split(":", 1)
            else:
                raise RuntimeError("Invalid Gmail secret format")
        
        if not email_user or not email_pass:
            raise RuntimeError("Gmail credentials not found in secret")
        
        logger.info(f"✅ Retrieved Gmail credentials for user: {email_user}")
        return email_user, email_pass
        
    except Exception as exc:
        logger.exception("❌ Failed to get Gmail credentials")
        raise RuntimeError(f"Could not retrieve Gmail credentials: {exc}")


def _process_single_email(
    mail: imaplib.IMAP4_SSL,
    email_id: bytes,
    bucket_name: str,
    emails_table: str,
    aliases_table: str
) -> bool:
    """
    Process a single email:
    1. Fetch email from Gmail
    2. Parse to find recipient (alias)
    3. Save raw .eml to S3
    4. Save metadata to DynamoDB
    """
    try:
        # Fetch the email
        status, msg_data = mail.fetch(email_id, "(RFC822)")
        if status != "OK":
            logger.error(f"Failed to fetch email {email_id}")
            return False
        
        raw_email = msg_data[0][1]
        
        # Parse email to extract headers
        msg = email.message_from_bytes(raw_email)
        
        # Find which alias this email is for
        alias_id, recipient_email = _extract_alias_from_email(msg)
        if not alias_id:
            logger.warning(f"Could not determine alias for email {email_id}")
            # Mark as seen anyway to avoid infinite retry
            mail.store(email_id, "+FLAGS", "\\Seen")
            return False
        
        logger.info(f"📧 Email for alias: {alias_id} ({recipient_email})")
        
        # Check if alias exists and is active
        alias_record = _get_alias_record(aliases_table, alias_id)
        if not alias_record:
            logger.warning(f"Alias {alias_id} not found in database")
            mail.store(email_id, "+FLAGS", "\\Seen")
            return False
        
        if alias_record.get("status") == "DISABLED":
            logger.info(f"Alias {alias_id} is disabled, skipping")
            mail.store(email_id, "+FLAGS", "\\Seen")
            return False
        
        telegram_chat_id = alias_record.get("telegram_chat_id")
        if not telegram_chat_id:
            logger.error(f"Alias {alias_id} has no telegram_chat_id")
            mail.store(email_id, "+FLAGS", "\\Seen")
            return False
        
        # Generate unique message ID
        message_id = _generate_message_id(msg, email_id)
        
        # Check for duplicates
        if _email_exists(emails_table, message_id):
            logger.info(f"Duplicate email detected: {message_id}")
            mail.store(email_id, "+FLAGS", "\\Seen")
            return False
        
        # Parse timestamp
        received_at = _parse_email_timestamp(msg)
        
        # Save raw email to S3
        s3_key = _build_s3_key(alias_id, received_at, message_id)
        _save_to_s3(bucket_name, s3_key, raw_email)
        
        # Save metadata to DynamoDB
        _save_to_dynamodb(
            emails_table=emails_table,
            aliases_table=aliases_table,
            message_id=message_id,
            alias_id=alias_id,
            telegram_chat_id=telegram_chat_id,
            recipient_email=recipient_email,
            s3_key=s3_key,
            s3_bucket=bucket_name,
            email_msg=msg,
            received_at=received_at
        )
        
        # Mark email as seen in Gmail
        mail.store(email_id, "+FLAGS", "\\Seen")
        
        logger.info(f"✅ Saved email {message_id} for alias {alias_id}")
        return True
        
    except Exception as exc:
        logger.exception(f"❌ Failed to process email {email_id}", extra={"error": str(exc)})
        return False


def _extract_alias_from_email(msg: email.message.EmailMessage) -> Tuple[Optional[str], Optional[str]]:
    """
    Extract alias from email headers.
    Looks for the recipient in various headers.
    """
    # Headers to check (in order of priority)
    headers_to_check = [
        "X-Original-To",      # Cloudflare Email Routing
        "Delivered-To",       # Standard SMTP
        "Envelope-To",        # SMTP envelope
        "To",                 # Primary recipient
        "CC",                 # Carbon copy
    ]
    
    for header in headers_to_check:
        header_value = msg.get(header, "")
        if not header_value:
            continue
        
        # Parse email addresses from header
        addresses = getaddresses([header_value])
        for _, email_addr in addresses:
            if not email_addr or "@" not in email_addr:
                continue
            
            email_addr_lower = email_addr.lower()
            # Extract local part (before @)
            local_part = email_addr_lower.split("@")[0]
            
            # Basic validation
            if local_part and len(local_part) >= 3:
                logger.debug(f"Found alias in {header}: {local_part} ({email_addr_lower})")
                return local_part, email_addr_lower
    
    logger.warning("No alias found in email headers")
    return None, None


def _get_alias_record(aliases_table: str, alias_id: str) -> Optional[Dict[str, Any]]:
    """Get alias record from DynamoDB."""
    try:
        table = _dynamodb.Table(aliases_table)
        response = table.get_item(Key={"alias_id": alias_id})
        return response.get("Item")
    except Exception as exc:
        logger.error(f"Failed to get alias {alias_id}: {exc}")
        return None


def _email_exists(emails_table: str, message_id: str) -> bool:
    """Check if email already exists in DynamoDB."""
    try:
        table = _dynamodb.Table(emails_table)
        response = table.get_item(Key={"message_id": message_id})
        return "Item" in response
    except Exception:
        return False


def _generate_message_id(msg: email.message.EmailMessage, email_id: bytes) -> str:
    """Generate unique message ID for the email."""
    # Try to use Message-ID header first
    msg_id_header = msg.get("Message-ID", "")
    if msg_id_header:
        # Clean up the Message-ID
        msg_id = msg_id_header.strip().strip("<>")
        # Remove problematic characters
        msg_id = "".join(c for c in msg_id if c.isalnum() or c in ("-", "_", ".", "@"))
        if msg_id:
            return msg_id[:100]  # Limit length
    
    # Fallback: use Gmail UID with timestamp
    timestamp = dt.datetime.now().strftime("%Y%m%d%H%M%S")
    return f"gmail-{email_id.decode()}-{timestamp}"


def _parse_email_timestamp(msg: email.message.EmailMessage) -> dt.datetime:
    """Parse email timestamp from Date header."""
    date_header = msg.get("Date", "")
    if date_header:
        try:
            parsed = parsedate_to_datetime(date_header)
            if parsed:
                # Ensure timezone info
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=dt.timezone.utc)
                return parsed.astimezone(dt.timezone.utc)
        except (ValueError, TypeError) as exc:
            logger.warning(f"Failed to parse date header: {exc}")
    
    # Fallback to current time
    return dt.datetime.now(dt.timezone.utc)


def _build_s3_key(alias_id: str, received_at: dt.datetime, message_id: str) -> str:
    """
    Build S3 key path for storing email.
    Format: alias_id/YYYY/MM/DD/message_id.eml
    """
    # Sanitize message_id for filename
    safe_message_id = "".join(c for c in message_id if c.isalnum() or c in ("-", "_"))
    
    # Add random suffix to avoid collisions
    random_suffix = uuid.uuid4().hex[:8]
    
    # Create date-based folder structure
    date_folder = received_at.strftime("%Y/%m/%d")
    
    return f"{alias_id}/{date_folder}/{safe_message_id}-{random_suffix}.eml"


def _save_to_s3(bucket_name: str, key: str, raw_email: bytes) -> None:
    """Save raw email to S3."""
    try:
        _s3.put_object(
            Bucket=bucket_name,
            Key=key,
            Body=raw_email,
            ContentType="message/rfc822",
            Metadata={
                "processed": "false",
                "timestamp": dt.datetime.now(dt.timezone.utc).isoformat()
            }
        )
        logger.info(f"📁 Saved email to S3: s3://{bucket_name}/{key}")
    except Exception as exc:
        logger.error(f"❌ Failed to save to S3: {exc}")
        raise


def _save_to_dynamodb(
    emails_table: str,
    aliases_table: str,
    message_id: str,
    alias_id: str,
    telegram_chat_id: str,
    recipient_email: str,
    s3_key: str,
    s3_bucket: str,
    email_msg: email.message.EmailMessage,
    received_at: dt.datetime
) -> None:
    """Save email metadata to DynamoDB."""
    try:
        table = _dynamodb.Table(emails_table)
        
        # Extract email metadata
        subject = email_msg.get("Subject", "(no subject)")[:500]
        from_email = email_msg.get("From", "")[:200]
        
        # Calculate TTL (14 days from receipt)
        ttl_timestamp = int(received_at.timestamp()) + EMAIL_TTL_SECONDS
        
        # Create email record
        email_item = {
            "message_id": message_id,
            "alias_id": alias_id,
            "telegram_chat_id": str(telegram_chat_id),
            "recipient_email": recipient_email or "",
            "from_email": from_email,
            "subject": subject,
            "s3_key": s3_key,
            "s3_bucket": s3_bucket,
            "received_at": received_at.isoformat(),
            "state": "PENDING",  # Will be processed by Lambda2
            "ttl_expiry": ttl_timestamp,
            "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        }
        
        # Save to DynamoDB
        table.put_item(Item=email_item)
        logger.info(f"💾 Saved email metadata to DynamoDB: {message_id}")
        
        # Update alias last_message_at timestamp
        _update_alias_timestamp(aliases_table, alias_id, received_at)
        
    except Exception as exc:
        logger.error(f"❌ Failed to save to DynamoDB: {exc}")
        raise


def _update_alias_timestamp(aliases_table: str, alias_id: str, timestamp: dt.datetime) -> None:
    """Update last_message_at timestamp for alias."""
    try:
        table = _dynamodb.Table(aliases_table)
        table.update_item(
            Key={"alias_id": alias_id},
            UpdateExpression="SET last_message_at = :ts",
            ExpressionAttributeValues={
                ":ts": timestamp.isoformat()
            }
        )
    except Exception as exc:
        logger.warning(f"Failed to update alias timestamp: {exc}")
