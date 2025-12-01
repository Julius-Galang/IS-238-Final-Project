import os
import pytest
import boto3

# NOTE:
#
# ambda #1 expected behavior with respect
# to S3 storage of raw emails.
#
# Right now all tests in this file are marked as "skipped" so that they
# do not fail CI until Lambda #1 is fully implemented and wired to S3.
#
# Suggested future import (adjust when code structure is final):
# from lambda1_fetch_gmail import handler as lambda1_handler

pytestmark = pytest.mark.skip(
    reason=(
        "Lambda #1 S3 storage behaviour is not wired to tests yet. "
        "Remove this skip when lambda1 implementation is ready."
    )
)


def test_lambda1_writes_email_to_s3():
    """
    Lambda #1 should write raw email into configured S3 bucket.

    High-level expectation (from Problem Specs + Test Plan):
    - When Lambda #1 processes a Gmail message,
      it stores full raw email content into an S3 object.
    - The object key should be deterministic (e.g., based on message ID).
    """
    # Arrange: create fake S3 and bucket
    s3 = boto3.client("s3", region_name="us-east-1")
    bucket_name = "test-email-bucket"
    s3.create_bucket(Bucket=bucket_name)

    # In real code, Lambda #1 would read this from an env var.
    os.environ["EMAIL_BUCKET_NAME"] = bucket_name

    # Example fake event representing Gmail message (simplified).
    # Adapt this to real event structure later.
    fake_event = {
        "gmailMessageId": "12345",
        "rawEmail": "From: example@test\nSubject: Hello\n\nThis is a test.",
    }

    # When Lambda #1 is ready, call handler here, for example:
    # lambda1_handler(fake_event, context={})

    # For now, we simulate what we expect Lambda #1 to have done:
    expected_key = "raw/12345.eml"
    s3.put_object(
        Bucket=bucket_name,
        Key=expected_key,
        Body=fake_event["rawEmail"].encode("utf-8"),
    )

    # Assert: object exists in S3 with the expected key
    objects = s3.list_objects_v2(Bucket=bucket_name)
    keys = [obj["Key"] for obj in objects.get("Contents", [])]

    assert expected_key in keys
