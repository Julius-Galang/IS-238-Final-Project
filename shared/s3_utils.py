# shared/s3_utils.py
"""Helpers for raw email storage and download links in S3."""

from __future__ import annotations

import logging
from typing import Any, Dict

import boto3

logger = logging.getLogger(__name__)

_s3 = boto3.client("s3")


def put_raw_email(bucket: str, key: str, body: bytes, metadata: Dict[str, Any] | None = None) -> None:
    """Store the raw email blob in S3 with optional metadata."""
    extra: Dict[str, Any] = {}
    if metadata:
        # S3 metadata must be strings
        extra["Metadata"] = {k: str(v) for k, v in metadata.items()}

    _s3.put_object(Bucket=bucket, Key=key, Body=body, **extra)


def get_raw_email(bucket: str, key: str) -> bytes:
    """Download and return the email bytes."""
    resp = _s3.get_object(Bucket=bucket, Key=key)
    return resp["Body"].read()


def generate_presigned_url(bucket: str, key: str, expires_in: int = 43200) -> str:
    """Generate temporary GET URL (default 12 hours = 43200 seconds)."""
    return _s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": bucket, "Key": key},
        ExpiresIn=expires_in,
    )
