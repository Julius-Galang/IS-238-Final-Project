# shared/dynamodb.py
"""Small helpers, boto3 DynamoDB for the bot MVP."""

from __future__ import annotations

import logging
from typing import Any, Dict, List

import boto3

logger = logging.getLogger(__name__)

_dynamodb = boto3.resource("dynamodb")


def _table(name: str):
    return _dynamodb.Table(name)


def get_item(table_name: str, key: Dict[str, Any]) -> Dict[str, Any] | None:
    resp = _table(table_name).get_item(Key=key)
    return resp.get("Item")


def upsert_item(table_name: str, item: Dict[str, Any]) -> None:
    _table(table_name).put_item(Item=item)


def update_item(table_name: str, key: Dict[str, Any], **kwargs: Any) -> None:
    """Thin wrapper over Table.update_item.
    """
    _table(table_name).update_item(Key=key, **kwargs)


def query_aliases_by_chat(table_name: str, chat_id: str) -> List[Dict[str, Any]]:
    """Return all aliases owned by a Telegram chat id.

    SCAN the table and filter in code.
    Avoids needing to create a GSI for the assignment.
    """
    table = _table(table_name)
    items: List[Dict[str, Any]] = []
    scan_kwargs: Dict[str, Any] = {}
    while True:
        resp = table.scan(**scan_kwargs)
        batch = resp.get("Items", [])
        items.extend(batch)
        last_key = resp.get("LastEvaluatedKey")
        if not last_key:
            break
        scan_kwargs["ExclusiveStartKey"] = last_key

    chat_id_str = str(chat_id)
    return [it for it in items if str(it.get("telegram_chat_id")) == chat_id_str]
