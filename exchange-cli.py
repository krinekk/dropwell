#!/usr/bin/env python3
"""
CLI tool to create and manage Claude-GPT exchange sessions.

Usage:
  python3 exchange-cli.py create [--ttl 120] [--data-dir ./gpt-exchange-data]
  python3 exchange-cli.py list [--data-dir ./gpt-exchange-data]
  python3 exchange-cli.py health [--port 9741]
"""

import argparse
import importlib.util
import json
import sys
import urllib.request
from pathlib import Path


def _load_exchange_store():
    """Load ExchangeStore from claude-gpt-exchange.py (hyphenated filename)."""
    module_path = Path(__file__).parent / "claude-gpt-exchange.py"
    spec = importlib.util.spec_from_file_location("claude_gpt_exchange", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.ExchangeStore


def create_session(data_dir: Path, ttl_minutes: int) -> tuple[str, str]:
    """Create a new session using the store directly (no server required)."""
    ExchangeStore = _load_exchange_store()
    store = ExchangeStore(data_dir)
    token, expires_at = store.create_session(ttl_minutes=ttl_minutes)
    return token, expires_at


def list_sessions(data_dir: Path) -> list[dict]:
    """List all active sessions."""
    from datetime import datetime, timezone

    data_dir = Path(data_dir)
    sessions = []

    for path in sorted(data_dir.glob("*.json")):
        try:
            with open(path) as f:
                session = json.load(f)

            expires_at = datetime.fromisoformat(session["expires_at"])
            is_expired = expires_at < datetime.now(timezone.utc)
            msg_count = len(session.get("messages", []))

            sessions.append(
                {
                    "token": session["token"][:16] + "...",
                    "created_at": session["created_at"],
                    "expires_at": session["expires_at"],
                    "is_expired": is_expired,
                    "message_count": msg_count,
                }
            )
        except (json.JSONDecodeError, KeyError):
            pass

    return sessions


def health_check(port: int) -> bool:
    """Check if server is running."""
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/health")
        with urllib.request.urlopen(req, timeout=2) as f:
            data = json.loads(f.read())
            return data.get("status") == "ok"
    except Exception:
        return False


def main():
    parser = argparse.ArgumentParser(description="Manage Claude-GPT exchange sessions")
    subparsers = parser.add_subparsers(dest="command", help="Command")

    # create
    create_parser = subparsers.add_parser("create", help="Create a new session")
    create_parser.add_argument(
        "--ttl", type=int, default=120, help="TTL in minutes (default 120)"
    )
    create_parser.add_argument(
        "--data-dir", type=Path, default=Path("./gpt-exchange-data")
    )

    # list
    list_parser = subparsers.add_parser("list", help="List active sessions")
    list_parser.add_argument(
        "--data-dir", type=Path, default=Path("./gpt-exchange-data")
    )

    # health
    health_parser = subparsers.add_parser("health", help="Check server health")
    health_parser.add_argument("--port", type=int, default=9741)

    args = parser.parse_args()

    if args.command == "create":
        token, expires_at = create_session(args.data_dir, args.ttl)
        print("✓ Session created", file=sys.stderr)
        print(f"Token:     {token}")
        print(f"Expires:   {expires_at}")
        base = f"http://localhost:9741/exchange/{token}"
        print(f"Claude URL:  {base}?role=claude", file=sys.stderr)
        print(f"GPT URL:     {base}?role=gpt", file=sys.stderr)
        return

    if args.command == "list":
        sessions = list_sessions(args.data_dir)
        if not sessions:
            print("No sessions found")
            return
        print(f"\n{len(sessions)} session(s):\n")
        for s in sessions:
            status = "EXPIRED" if s["is_expired"] else "ACTIVE"
            print(
                f"  {s['token']:20} {s['created_at']} → {s['expires_at']} "
                f"({status}, {s['message_count']} msgs)"
            )
        return

    if args.command == "health":
        ok = health_check(args.port)
        if ok:
            print(f"✓ Server ok (http://127.0.0.1:{args.port})")
            return
        print(f"✗ Server not responding on port {args.port}")
        sys.exit(1)

    parser.print_help()


if __name__ == "__main__":
    main()
