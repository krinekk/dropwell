#!/usr/bin/env python3
"""Pure bridge from KOS GPT drops to durable exchange threads.

The process wrapper is deliberately separate: it may fetch/archive drops with
KOS credentials, while this module never reads credentials or talks to KOS.
"""

import importlib.util
import json
from pathlib import Path

PROTOCOL = "kos-gpt-exchange/v1"
TOPIC = "gpt-exchange"


def _load_store():
    module_path = Path(__file__).parent / "claude-gpt-exchange.py"
    spec = importlib.util.spec_from_file_location("claude_gpt_exchange", module_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.ExchangeStore


def process_inbound_drop(
    store, drop: dict, base_url: str, ttl_minutes: int = 15
) -> dict:
    """Validate, mirror, and prepare the next one-time delivery URL."""
    if drop.get("topic") != TOPIC or drop.get("status") != "inbound":
        raise ValueError("drop is not an inbound gpt-exchange message")
    try:
        payload = json.loads(drop["body"])
    except (KeyError, TypeError, json.JSONDecodeError) as error:
        raise ValueError("drop body must be JSON") from error
    if payload.get("protocol") != PROTOCOL:
        raise ValueError("unsupported exchange protocol")
    sid = payload.get("thread_sid")
    body = payload.get("body")
    drop_id = drop.get("id")
    if not isinstance(sid, str) or not isinstance(body, str) or not body.strip():
        raise ValueError("thread_sid and non-empty body are required")
    if not isinstance(drop_id, str) or not drop_id:
        raise ValueError("drop id is required")
    thread_id = store.resolve_sid(sid)
    if not thread_id:
        raise ValueError("unknown thread sid")
    message, delivery, duplicate = store.accept_gpt_drop(
        thread_id, drop_id, body, ttl_minutes
    )
    assert delivery is not None
    token, expires_at = delivery
    result = {
        "duplicate": duplicate,
        "thread_sid": sid,
        "expires_at": expires_at,
        "delivery_url": f"{base_url.rstrip('/')}/exchange/{token}?role=gpt",
    }
    if message is not None:
        result["message_id"] = message["id"]
    return result
