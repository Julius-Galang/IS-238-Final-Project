from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional


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

    # OpenAI Configuration
    openai_api_key: Optional[str]
    openai_api_url: str
    openai_model: str
    openai_enabled: bool

    # Optional features
    public_base_url: Optional[str]


def get_config() -> RuntimeConfig:
    """
    Runtime config for all Lambdas.

    Values, from Lambda environment variables,
    same code can run in different AWS accounts / stages.
    """
    # Check if OpenAI is enabled
    openai_api_key = os.environ.get("OPENAI_API_KEY")
    openai_enabled = bool(openai_api_key)
    
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

        # OpenAI Configuration
        openai_api_key=openai_api_key,
        openai_api_url=os.environ.get(
            "OPENAI_API_URL", 
            "https://api.openai.com/v1/chat/completions"
        ),
        openai_model=os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
        openai_enabled=openai_enabled,

        # Optional: Public base URL for download links
        public_base_url=os.environ.get("PUBLIC_BASE_URL"),
    )


def validate_config(cfg: RuntimeConfig) -> bool:
    """Validate that required configuration is present."""
    errors = []
    
    # Required configurations
    required = [
        ("ALIASES_TABLE", cfg.aliases_table),
        ("EMAILS_TABLE", cfg.emails_table),
        ("USERS_TABLE", cfg.users_table),
        ("RAW_EMAIL_BUCKET", cfg.raw_email_bucket),
        ("GMAIL_SECRET_ARN", cfg.gmail_secret_arn),
        ("TELEGRAM_SECRET_ARN", cfg.telegram_secret_arn),
        ("CLOUDFLARE_SECRET_ARN", cfg.cloudflare_secret_arn),
    ]
    
    for name, value in required:
        if not value:
            errors.append(f"{name} is required but not set")
    
    # OpenAI warnings (optional but recommended)
    if not cfg.openai_enabled:
        print("WARNING: OpenAI is disabled. Summarization will use fallback truncation.")
        print("Set OPENAI_API_KEY environment variable to enable AI summaries.")
    
    if cfg.openai_enabled:
        # Validate OpenAI URL format if set
        if cfg.openai_api_url and not (
            cfg.openai_api_url.startswith("http://") or 
            cfg.openai_api_url.startswith("https://")
        ):
            errors.append("OPENAI_API_URL must be a valid URL starting with http:// or https://")
    
    if errors:
        print("Configuration errors:")
        for error in errors:
            print(f"  - {error}")
        return False
    
    return True


# Optional: Helper to get config with validation
def get_validated_config() -> RuntimeConfig:
    """Get and validate configuration."""
    cfg = get_config()
    if not validate_config(cfg):
        raise ValueError("Invalid configuration. Check environment variables.")
    return cfg