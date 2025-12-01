import pytest

# NOTE:
#
# This is about the email parser behavior for Lambda #2
#
# Tests are marked as "skipped" so they will not fail CI.
# When the real parser function exists in src/lambda2/, remove the skip
# and call the real function instead of the placeholders.
#
# Suggested future import (adjust path as needed):
# from src.lambda2.email_parser import parse_email_body

# Skip all tests in this file until Lambda #2's parser is ready.
pytestmark = pytest.mark.skip(
    reason=(
        "Email parsing for Lambda #2 is not implemented yet. "
        "Remove this skip when the real parser is available."
    )
)


def test_parse_email_simple_text():
    """
    Parser should take simple HTML email and return text.

    Requirement link (from Problem Specs + Test Plan):
    - HTML body must be converted to readable text.
    - No HTML tags should appear in the final summary input.
    """
    html = "<html><body>Hello, this is a test email.</body></html>"

    # Replace this placeholder when parse_email_body exists
    # text = parse_email_body(html)
    text = "Hello, this is a test email."  # placeholder for result

    assert "Hello, this is a test email." in text
    # No raw HTML tags in the final text
    assert "<html>" not in text
    assert "<body>" not in text


def test_parse_email_ignores_basic_tags_and_keeps_content():
    """
    Parser should remove markup tags but keep human-readable content.

    Requirement link:
    - Summaries must be based on actual body text.
    - Markup (p, b, i, etc.) is not needed in the text passed to OpenAI.
    """
    html = "<html><body><p>Hello <b>world</b>!</p></body></html>"

    # Replace this placeholder when parse_email_body exists
    # text = parse_email_body(html)
    text = "Hello world!"  # placeholder idea of the expected result

    assert "Hello" in text
    assert "world" in text
    # Again, no raw HTML tags
    assert "<p>" not in text
    assert "<b>" not in text
    assert "<html>" not in text
    assert "<body>" not in text
