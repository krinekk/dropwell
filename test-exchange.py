#!/usr/bin/env python3
"""Quick functional test of durable exchange thread components."""

import importlib.util
import shutil
import sys
import tempfile
from pathlib import Path


def _load_exchange_store():
    module_path = Path(__file__).parent / "claude-gpt-exchange.py"
    spec = importlib.util.spec_from_file_location("claude_gpt_exchange", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.ExchangeStore


def test():
    tmpdir = Path(tempfile.mkdtemp())
    store = _load_exchange_store()(tmpdir)
    try:
        print("Testing durable Claude-GPT exchange thread...")
        thread_id = store.create_thread()
        assert len(thread_id) == 32
        print(f"Thread created: {thread_id[:12]}...")

        msg1 = store.add_message(thread_id, "claude", "Initial analysis")
        msg2 = store.add_message(thread_id, "gpt", "Recommend a lock")
        assert msg1 is not None and msg2 is not None

        token, _ = store.issue_delivery(thread_id, ttl_minutes=15)
        thread, error = store.redeem_delivery(token, "gpt")
        assert error is None and thread is not None
        assert len(thread["messages"]) == 2
        print("One-time delivery redeemed")

        thread, error = store.redeem_delivery(token, "gpt")
        assert thread is None and error == "consumed"
        print("Replay rejected")

        assert store.resolve_sid(thread_id[:12]) == thread_id
        assert token not in str(store.list_threads())
        print("UI listing contains no delivery credential")
    finally:
        shutil.rmtree(tmpdir)
    print("All tests passed")


if __name__ == "__main__":
    try:
        test()
    except Exception as error:
        print(f"Test failed: {error}", file=sys.stderr)
        raise
