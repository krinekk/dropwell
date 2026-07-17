#!/usr/bin/env python3
"""Quick functional test of exchange server components."""

import importlib.util
import sys
import tempfile
from pathlib import Path


def _load_exchange_store():
    """Load ExchangeStore from claude-gpt-exchange.py (hyphenated filename)."""
    module_path = Path(__file__).parent / "claude-gpt-exchange.py"
    spec = importlib.util.spec_from_file_location("claude_gpt_exchange", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.ExchangeStore


ExchangeStore = _load_exchange_store()


def test():
    tmpdir = Path(tempfile.mkdtemp())
    store = ExchangeStore(tmpdir)

    print("Testing Claude-GPT Exchange Store...")

    # Test 1: Create session
    token, _expires = store.create_session(ttl_minutes=120)
    assert len(token) == 64, f"Token should be 64 chars, got {len(token)}"
    print(f"✓ Session created: {token[:16]}...")

    # Test 2: Add message from claude
    msg1 = store.add_message(token, "claude", "Initial analysis: found race condition")
    assert msg1 is not None
    assert msg1["role"] == "claude"
    assert "race condition" in msg1["body"]
    print(f"✓ Claude message added: {msg1['id'][:12]}...")

    # Test 3: Add message from gpt
    msg2 = store.add_message(
        token, "gpt", "Good catch! Recommend SELECT ... FOR UPDATE"
    )
    assert msg2 is not None
    assert msg2["role"] == "gpt"
    print(f"✓ GPT message added: {msg2['id'][:12]}...")

    # Test 4: List all messages
    all_msgs = store.get_messages(token)
    assert all_msgs is not None
    assert len(all_msgs) == 2
    print(f"✓ Total messages: {len(all_msgs)}")

    # Test 5: Filter by role
    claude_only = store.get_messages(token, role="claude")
    assert claude_only is not None
    assert len(claude_only) == 1
    assert claude_only[0]["role"] == "claude"
    print(f"✓ Claude messages only: {len(claude_only)}")

    gpt_only = store.get_messages(token, role="gpt")
    assert gpt_only is not None
    assert len(gpt_only) == 1
    assert gpt_only[0]["role"] == "gpt"
    print(f"✓ GPT messages only: {len(gpt_only)}")

    # Test 6: Invalid token
    invalid = store.get_messages("invalid-token-0123456789abcdef")
    assert invalid is None
    print("✓ Invalid token returns None")

    # Test 7: Check file permissions (must be 0o600)
    session_file = tmpdir / f"{token}.json"
    assert session_file.exists()
    perms = oct(session_file.stat().st_mode)[-3:]
    assert perms == "600", f"Expected 0o600, got 0o{perms}"
    print(f"✓ Session file permissions: 0o{perms}")

    # Test 8: Verify JSON structure
    import json

    with open(session_file) as f:
        data = json.load(f)
    assert data["token"] == token
    assert len(data["messages"]) == 2
    assert "created_at" in data
    assert "expires_at" in data
    print("✓ Session JSON structure valid")

    # Cleanup
    import shutil

    shutil.rmtree(tmpdir)
    print("\n✅ All tests passed!")
    return True


if __name__ == "__main__":
    try:
        test()
    except Exception as e:
        print(f"❌ Test failed: {e}", file=sys.stderr)
        import traceback

        traceback.print_exc()
        sys.exit(1)
