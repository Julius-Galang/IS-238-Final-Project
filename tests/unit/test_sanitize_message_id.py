import pathlib
import sys
import types

# Repository root (where lambda_functions.py lives) is on sys.path
ROOT_DIR = pathlib.Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

# Fake "shared" module so that lambda_functions can import it.
fake_shared = types.ModuleType("shared")
fake_shared.config = types.SimpleNamespace()
fake_shared.dynamodb = types.SimpleNamespace()
fake_shared.gmail_client = types.SimpleNamespace()
fake_shared.s3_utils = types.SimpleNamespace()
sys.modules["shared"] = fake_shared

from lambda_functions import _sanitize_message_id


def test_sanitize_message_id_returns_none_for_none_input():
    assert _sanitize_message_id(None) is None


def test_sanitize_message_id_strips_angle_brackets_and_whitespace():
    raw = "  <msg-123@example.com>  "
    result = _sanitize_message_id(raw)
    # angle brackets + spaces removed
    assert result == "msg-123examplecom"


def test_sanitize_message_id_keeps_alnum_dash_underscore_only():
    raw = "<ID: A B_C-1.2@host.example>"
    result = _sanitize_message_id(raw)
    # allowed: letters, digits, -, _
    assert result == "IDAB_C-12hostexample"


def test_sanitize_message_id_returns_none_if_everything_is_removed():
    raw = "<<<>>>!!!@@@"
    result = _sanitize_message_id(raw)
    assert result is None
