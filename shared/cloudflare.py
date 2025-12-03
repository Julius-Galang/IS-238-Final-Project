# shared/cloudflare.py
"""Cloudflare Email Routing integration (catch-all stub).

Updated specs: Cloudflare with a catch-all rule,
so every alias like `abcd1234@your-domain.com', automatically
forwarded to Gmail. These helpers just construct the address.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict

logger = logging.getLogger(__name__)


def create_alias(secret_arn: str, alias_id: str) -> Dict[str, Any]:
    """Return a fake Cloudflare rule record.

    Assuming Cloudflare Email Routing catch-all, already set up.
    We only need to construct the email address based on EMAIL_DOMAIN.
    """
    domain = os.environ.get("EMAIL_DOMAIN")
    if not domain:
        raise RuntimeError("EMAIL_DOMAIN env var must be set (e.g. example.com)")
    email_address = f"{alias_id}@{domain}"
    # We pretend there is a "rule id" for compatibility with the rest of the code.
    return {"id": alias_id, "name": email_address}


def disable_alias(secret_arn: str, rule_id: str) -> None:
    """No-op in catch-all mode; we just log."""
    logger.info("disable_alias noop for rule_id=%s (catch-all mode)", rule_id)
