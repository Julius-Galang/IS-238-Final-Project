# shared/gmail_client.py
"""Gmail IMAP client used by Lambda #1."""

from __future__ import annotations

import dataclasses
import imaplib
import json
import logging
from typing import List

import boto3

logger = logging.getLogger(__name__)

_sm = boto3.client("secretsmanager")


@dataclasses.dataclass
class GmailMessage:
    uid: str
    raw_email: bytes


class GmailClient:
    """Fetch unread messages from Gmail using IMAP + app password."""

    def __init__(self, secret_arn: str, processed_label: str | None = None) -> None:
        self.secret_arn = secret_arn
        self.processed_label = processed_label 

    def _get_credentials(self) -> tuple[str, str]:
        resp = _sm.get_secret_value(SecretId=self.secret_arn)
        secret_str = resp.get("SecretString") or ""
        data = json.loads(secret_str)
        user = data.get("email_user_name")
        password = data.get("email_password")
        if not user or not password:
            raise RuntimeError("Gmail credentials not found in secret")
        return user, password

    def fetch_unread(self) -> List[GmailMessage]:
        user, password = self._get_credentials()
        mail = imaplib.IMAP4_SSL("imap.gmail.com")
        try:
            mail.login(user, password)
            mail.select("inbox")
            status, data = mail.search(None, "UNSEEN")
            if status != "OK":
                logger.warning("Gmail search failed: %s", status)
                return []

            uids = data[0].split()
            messages: List[GmailMessage] = []

            for uid in uids:
                status, msg_data = mail.fetch(uid, "(RFC822)")
                if status != "OK":
                    logger.warning("Fetch failed for UID %s", uid)
                    continue
                raw_email = msg_data[0][1]
                messages.append(GmailMessage(uid=uid.decode(), raw_email=raw_email))
                # Mark as seen so no re-process
                mail.store(uid, "+FLAGS", "\\Seen")

            return messages
        finally:
            try:
                mail.close()
            except Exception:
                pass
            mail.logout()
