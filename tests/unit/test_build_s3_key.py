import datetime as dt
import pathlib
import sys

ROOT_DIR = pathlib.Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0.str(ROOT_DIR))

from lambda_functions import _build_s3_key


def test_build_s3_key_structure():
    """
    _build_s3_key produce keys of the form:

        <alias_id>/<YYYY>/<MM>/<DD>/<message_id>-<suffix>.eml

    Keeps emails partitioned by alias and grouped by day.
    """
    alias_id = "alias123"
    message_id = "msg-ABC_123"
    received_at = dt.datetime(2025, 1, 2, 3, 4, 5, tzinfo=dt.timezone.utc)

    key = _build_s3_key(alias_id, received_at, message_id)

    # Basic shape checks
    assert key.endswith(".eml")
    parts = key.split("/")

    # Expected segments: [alias_id, YYYY, MM, DD, last_part]
    assert len(parts) == 5
    assert parts[0] == alias_id
    assert parts[1] == "2025"
    assert parts[2] == "01"
    assert parts[3] == "02"

    last_part = parts[4]
    assert last_part.startswith(message_id + "-")
    assert last_part.endswith(".eml")


def test_build_s3_key_unique_suffix_differs_for_each_call():
    """
    Each call to _build_s3_key to produce a different key because of the
    random hex suffix, even with the same alias_id / message_id / received_at.
    """
    alias_id = "alias123"
    message_id = "msg-ABC_123"
    received_at = dt.datetime(2025, 1, 2, tzinfo=dt.timezone.utc)

    key1 = _build_s3_key(alias_id, received_at, message_id)
    key2 = _build_s3_key(alias_id, received_at, message_id)

    # They should not be identical, because of the uuid hex suffix
    assert key1 != key2

