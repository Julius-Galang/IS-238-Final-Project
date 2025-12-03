from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass
class RuntimeConfig:
    # Core data stores
    aliases_table: str
    emails_table: str
    users_table: str
    raw_email_bucket: str

    # External integrations (stored in Secrets Manager)
    gmail_secret_arn: str
    gmail_processed_label: str
    telegram_secret_arn: str
    cloudflare_secret_arn: str


def get_config() -> RuntimeConfig:
    """
    Runtime config for all Lambdas.

    Values, from Lambda environment variables,
    same code can run in different AWS accounts / stages.
    """
    return RuntimeConfig(
        # DynamoDB + S3
        aliases_table=os.environ["ALIASES_TABLE"],
        emails_table=os.environ["EMAILS_TABLE"],
        users_table=os.environ["USERS_TABLE"],
        raw_email_bucket=os.environ["RAW_EMAIL_BUCKET"],

        # Gmail
        gmail_secret_arn=os.environ["GMAIL_SECRET_ARN"],
        # If not set, fall back to reasonable default label name
        gmail_processed_label=os.environ.get("GMAIL_PROCESSED_LABEL", "botsum-processed"),

        # Telegram + Cloudflare
        telegram_secret_arn=os.environ["TELEGRAM_SECRET_ARN"],
        cloudflare_secret_arn=os.environ["CLOUDFLARE_SECRET_ARN"],
    )
