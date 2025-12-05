"""Lambda #1 – Fetch emails from Gmail, parse recipients, and route to S3 + DynamoDB."""

from __future__ import annotations

import datetime as dt
import email
import imaplib
import json
import logging
import os
import re
import uuid
from email.utils import parseaddr
from typing import Any, Dict, List, Optional, Tuple

import boto3

from shared import config, dynamodb, s3_utils

logger = logging.getLogger(__name__)
logger.setLevel(os.getenv("LOG_LEVEL", "INFO"))


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Main Lambda handler to fetch and process unread Gmail emails."""
    cfg = config.get_config()
    
    logger.info("Starting Gmail email fetch")
    
    try:
        # Fetch and process emails
        results = _fetch_and_process_emails(cfg)
        
        logger.info("Email fetch completed", extra=results)
        return {
            "statusCode": 200,
            "body": json.dumps(results)
        }
        
    except Exception as exc:
        logger.exception("Failed to fetch emails", extra={"error": str(exc)})
        return {
            "statusCode": 500,
            "body": json.dumps({"error": str(exc)})
        }


def _fetch_and_process_emails(cfg: config.RuntimeConfig) -> Dict[str, Any]:
    """Fetch unread emails and process each one."""
    # Get Gmail credentials from Secrets Manager
    gmail_user, gmail_pass = _get_gmail_credentials(cfg.gmail_secret_arn)
    
    # Connect to Gmail
    mail = imaplib.IMAP4_SSL("imap.gmail.com")
    
    try:
        mail.login(gmail_user, gmail_pass)
        mail.select("inbox")
        
        # Search for unread emails
        status, messages = mail.search(None, 'UNSEEN')
        if status != 'OK':
            logger.error("Failed to search emails")
            return {"processed": 0, "error": "Search failed"}
        
        email_ids = messages[0].split()
        logger.info(f"Found {len(email_ids)} unread emails")
        
        processed = 0
        failed = 0
        
        for e_id in email_ids:
            try:
                if _process_single_email(cfg, mail, e_id):
                    processed += 1
                else:
                    failed += 1
                    
            except Exception as exc:
                logger.exception(f"Failed to process email {e_id}", extra={"error": str(exc)})
                failed += 1
        
        return {
            "processed": processed,
            "failed": failed,
            "total": len(email_ids)
        }
        
    finally:
        try:
            mail.close()
        except:
            pass
        mail.logout()


def _get_gmail_credentials(secret_arn: str) -> Tuple[str, str]:
    """Get Gmail credentials from Secrets Manager."""
    client = boto3.client('secretsmanager')
    response = client.get_secret_value(SecretId=secret_arn)
    secret_str = response.get('SecretString', '{}')
    
    try:
        data = json.loads(secret_str)
        email_user = data.get('email_user_name', '')
        email_pass = data.get('email_password', '')
    except json.JSONDecodeError:
        # Fallback: assume format is "email:password"
        if ':' in secret_str:
            email_user, email_pass = secret_str.split(':', 1)
        else:
            raise RuntimeError("Invalid Gmail secret format")
    
    if not email_user or not email_pass:
        raise RuntimeError("Gmail credentials not found in secret")
    
    return email_user, email_pass


def _process_single_email(cfg: config.RuntimeConfig, mail: imaplib.IMAP4_SSL, email_id: bytes) -> bool:
    """Process a single email: parse, find recipient, save to S3 and DynamoDB."""
    # Fetch email
    status, msg_data = mail.fetch(email_id, "(RFC822)")
    if status != 'OK':
        logger.error(f"Failed to fetch email {email_id}")
        return False
    
    raw_email = msg_data[0][1]
    
    # Parse email to get headers
    msg = email.message_from_bytes(raw_email)
    
    # Extract recipients (To, CC, BCC)
    recipients = _extract_recipients(msg)
    logger.info(f"Email {email_id} recipients: {recipients}")
    
    # Find which alias(es) this email is for
    aliases_found = []
    for recipient in recipients:
        alias_match = _find_alias_for_email(cfg, recipient)
        if alias_match:
            aliases_found.append((alias_match, recipient))
    
    if not aliases_found:
        logger.warning(f"No matching alias found for recipients: {recipients}")
        # Mark as seen anyway to avoid infinite retry
        mail.store(email_id, '+FLAGS', '\\Seen')
        return False
    
    # Process for each matching alias (forwarded emails might go to multiple aliases)
    success_count = 0
    for alias_record, recipient_email in aliases_found:
        try:
            if _save_email_for_alias(cfg, email_id, raw_email, msg, alias_record, recipient_email):
                success_count += 1
        except Exception as exc:
            logger.exception(f"Failed to save email for alias {alias_record.get('alias_id')}", 
                           extra={"error": str(exc)})
    
    # Mark email as seen in Gmail
    mail.store(email_id, '+FLAGS', '\\Seen')
    
    # Optional: Apply label if configured
    if cfg.gmail_processed_label:
        try:
            _apply_gmail_label(mail, email_id, cfg.gmail_processed_label)
        except Exception as exc:
            logger.warning(f"Failed to apply label: {exc}")
    
    return success_count > 0


def _extract_recipients(msg: email.message.EmailMessage) -> List[str]:
    """Extract all recipient email addresses from email headers."""
    recipients = []
    
    # Extract from To, CC, BCC headers
    for header in ['To', 'Cc', 'Bcc']:
        header_value = msg.get(header, '')
        if header_value:
            # Parse email addresses (could be multiple, could have names)
            addresses = email.utils.getaddresses([header_value])
            for _, addr in addresses:
                if addr and '@' in addr:
                    recipients.append(addr.lower())
    
    return list(set(recipients))  # Remove duplicates


def _find_alias_for_email(cfg: config.RuntimeConfig, recipient_email: str) -> Optional[Dict[str, Any]]:
    """
    Find which alias this email is addressed to.
    
    Checks if recipient_email matches any alias's email_address in DynamoDB.
    Also checks if alias is ACTIVE and not DISABLED.
    """
    if not cfg.aliases_table:
        return None
    
    # Extract domain and local-part
    if '@' not in recipient_email:
        return None
    
    local_part, domain = recipient_email.lower().split('@', 1)
    
    # Query by email_address (full email)
    aliases = dynamodb.query_by_email_address(cfg.aliases_table, recipient_email)
    
    # Also try by alias_id (local-part) in case of different domain
    if not aliases:
        aliases = dynamodb.query_aliases_by_id(cfg.aliases_table, local_part)
    
    # Filter for ACTIVE aliases
    active_aliases = [a for a in aliases if a.get('status') == 'ACTIVE']
    
    if not active_aliases:
        logger.debug(f"No active alias found for {recipient_email}")
        return None
    
    if len(active_aliases) > 1:
        logger.warning(f"Multiple active aliases found for {recipient_email}")
    
    return active_aliases[0]


def _save_email_for_alias(
    cfg: config.RuntimeConfig,
    email_id: bytes,
    raw_email: bytes,
    msg: email.message.EmailMessage,
    alias_record: Dict[str, Any],
    recipient_email: str
) -> bool:
    """Save email to S3 and create metadata record in DynamoDB."""
    alias_id = alias_record.get('alias_id')
    telegram_chat_id = alias_record.get('telegram_chat_id')
    
    if not alias_id:
        logger.error("Alias record has no alias_id")
        return False
    
    if not telegram_chat_id:
        logger.error(f"Alias {alias_id} has no telegram_chat_id")
        return False
    
    # Generate unique message ID
    message_id = f"{str(uuid.uuid4())[:8]}_{email_id.decode()}"
    
    # Create S3 key with structure: alias_id/YYYY/MM/DD/message_id.eml
    now = dt.datetime.now(dt.timezone.utc)
    s3_key = f"{alias_id}/{now.year}/{now.month:02d}/{now.day:02d}/{message_id}.eml"
    
    # Get bot username for tracking (optional)
    telegram_bot_username = _get_current_bot_username(cfg)
    
    # Save to S3
    try:
        s3_utils.save_raw_email(cfg.raw_email_bucket, s3_key, raw_email)
        logger.info(f"Saved email to S3: {s3_key}")
    except Exception as exc:
        logger.error(f"Failed to save to S3: {exc}")
        return False
    
    # Extract email metadata
    subject = msg.get('Subject', '(no subject)')
    from_addr = msg.get('From', '')
    date_str = msg.get('Date', now.isoformat())
    
    # Create email record in DynamoDB
    email_item = {
        "message_id": message_id,
        "alias_id": alias_id,
        "telegram_chat_id": str(telegram_chat_id),  # Ensure string
        "telegram_bot_username": telegram_bot_username,
        "recipient_email": recipient_email,
        "from_email": from_addr,
        "subject": subject[:500],  # Limit length
        "s3_key": s3_key,
        "s3_bucket": cfg.raw_email_bucket,
        "email_date": date_str,
        "state": "PENDING",
        "created_at": now.isoformat(),
    }
    
    try:
        dynamodb.upsert_item(cfg.emails_table, email_item)
        logger.info(f"Created email record: {message_id} for chat {telegram_chat_id}")
        return True
    except Exception as exc:
        logger.error(f"Failed to save to DynamoDB: {exc}")
        # Try to delete the S3 object since we failed
        try:
            s3 = boto3.client('s3')
            s3.delete_object(Bucket=cfg.raw_email_bucket, Key=s3_key)
        except:
            pass
        return False


def _get_current_bot_username(cfg: config.RuntimeConfig) -> str:
    """Get current bot username (optional, for tracking)."""
    try:
        from shared import telegram
        token = telegram.get_bot_token(cfg.telegram_secret_arn)
        
        # Simple HTTP request to get bot info
        import urllib.request
        import json as json_module
        
        url = f"https://api.telegram.org/bot{token}/getMe"
        req = urllib.request.Request(url, method='GET')
        
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json_module.loads(resp.read().decode('utf-8'))
            if data.get('ok'):
                return data['result'].get('username', '')
    except Exception:
        pass
    
    return ""


def _apply_gmail_label(mail: imaplib.IMAP4_SSL, email_id: bytes, label: str) -> None:
    """Apply a label to the email in Gmail."""
    # Create label if it doesn't exist
    status, response = mail.list()
    
    # Apply label using Gmail's X-GM-LABELS
    mail.store(email_id, '+X-GM-LABELS', f'({label})')


# Helper function for DynamoDB queries (add to shared/dynamodb.py)
"""
# Add to shared/dynamodb.py:

def query_by_email_address(table_name: str, email_address: str) -> List[Dict[str, Any]]:
    \"\"\"Query aliases by email_address.\"\"\"
    dynamodb = boto3.resource('dynamodb')
    table = dynamodb.Table(table_name)
    
    response = table.query(
        IndexName='EmailAddressIndex',  # You need to create this GSI
        KeyConditionExpression=boto3.dynamodb.conditions.Key('email_address').eq(email_address)
    )
    return response.get('Items', [])

def query_aliases_by_id(table_name: str, alias_id: str) -> List[Dict[str, Any]]:
    \"\"\"Query aliases by alias_id.\"\"\"
    dynamodb = boto3.resource('dynamodb')
    table = dynamodb.Table(table_name)
    
    response = table.get_item(Key={'alias_id': alias_id})
    return [response['Item']] if 'Item' in response else []
"""
