#!/usr/bin/env python3
"""Manage one-time deliveries and explicit compatibility grants."""

import argparse
import importlib.util
import json
import sys
import urllib.request
from pathlib import Path


def _load_exchange_module():
    """Load the hyphenated server module."""
    module_path = Path(__file__).parent / "claude-gpt-exchange.py"
    spec = importlib.util.spec_from_file_location("claude_gpt_exchange", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_exchange_store():
    return _load_exchange_module().ExchangeStore


def _store(data_dir: Path):
    return _load_exchange_store()(data_dir)


def _compatibility_store(data_dir: Path):
    module = _load_exchange_module()
    thread_store = module.ExchangeStore(data_dir)
    return module.CompatibilityGrantStore(
        data_dir / "compatibility", thread_store
    ), thread_store


def create_thread(data_dir: Path, ttl_minutes: int) -> tuple[str, str, str]:
    """Create a durable thread and its first GPT delivery capability."""
    store = _store(data_dir)
    thread_id = store.create_thread()
    token, expires_at = store.issue_delivery(thread_id, ttl_minutes=ttl_minutes)
    return thread_id, token, expires_at


def issue_delivery(data_dir: Path, sid: str, ttl_minutes: int) -> tuple[str, str]:
    """Issue a fresh GPT delivery URL for an existing durable thread."""
    store = _store(data_dir)
    thread_id = store.resolve_sid(sid) or sid
    token, expires_at = store.issue_delivery(thread_id, ttl_minutes=ttl_minutes)
    return token, expires_at


def issue_compatibility(data_dir: Path, sid: str, ttl_minutes: int):
    """Issue a replayable compatibility grant for exactly one thread."""
    store, thread_store = _compatibility_store(data_dir)
    thread_id = thread_store.resolve_sid(sid) or sid
    return store.issue(thread_id, ttl_minutes=ttl_minutes, allowed_roles=("gpt",))


def revoke_compatibility(data_dir: Path, token: str) -> bool:
    """Explicitly revoke one compatibility capability."""
    store, _thread_store = _compatibility_store(data_dir)
    return store.revoke(token)


def health_check(port: int) -> bool:
    """Check if server is running."""
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/health")
        with urllib.request.urlopen(req, timeout=2) as response:
            return json.loads(response.read()).get("status") == "ok"
    except Exception:
        return False


def _delivery_url(base_url: str, token: str) -> str:
    return f"{base_url.rstrip('/')}/exchange/{token}"


def _compatibility_url(base_url: str, token: str) -> str:
    return f"{base_url.rstrip('/')}/compatibility/{token}"


def main():
    parser = argparse.ArgumentParser(
        description="Manage one-time deliveries and explicit compatibility grants"
    )
    subparsers = parser.add_subparsers(dest="command", help="Command")

    create_parser = subparsers.add_parser(
        "create", help="Create a thread and delivery"
    )
    create_parser.add_argument(
        "--ttl", type=int, default=15, help="Delivery TTL in minutes"
    )
    create_parser.add_argument(
        "--data-dir", type=Path, default=Path("./gpt-exchange-data")
    )
    create_parser.add_argument("--base-url", default="https://drop.krinekk.dev")

    deliver_parser = subparsers.add_parser("deliver", help="Issue a fresh delivery")
    deliver_parser.add_argument(
        "--sid", required=True, help="UI short id or full thread id"
    )
    deliver_parser.add_argument(
        "--ttl", type=int, default=15, help="Delivery TTL in minutes"
    )
    deliver_parser.add_argument(
        "--data-dir", type=Path, default=Path("./gpt-exchange-data")
    )
    deliver_parser.add_argument("--base-url", default="https://drop.krinekk.dev")

    compatibility_issue_parser = subparsers.add_parser(
        "compatibility-issue",
        help="Issue an explicit replayable compatibility grant",
    )
    compatibility_issue_parser.add_argument(
        "--sid", required=True, help="UI short id or full thread id"
    )
    compatibility_issue_parser.add_argument(
        "--ttl", type=int, default=5, help="Compatibility TTL in minutes (1-15)"
    )
    compatibility_issue_parser.add_argument(
        "--role", choices=("gpt",), default="gpt", help="Allowed reader role"
    )
    compatibility_issue_parser.add_argument(
        "--data-dir", type=Path, default=Path("./gpt-exchange-data")
    )
    compatibility_issue_parser.add_argument(
        "--base-url", default="https://drop.krinekk.dev"
    )

    compatibility_revoke_parser = subparsers.add_parser(
        "compatibility-revoke", help="Revoke a compatibility grant"
    )
    compatibility_revoke_parser.add_argument("--token", required=True)
    compatibility_revoke_parser.add_argument(
        "--data-dir", type=Path, default=Path("./gpt-exchange-data")
    )

    list_parser = subparsers.add_parser("list", help="List durable threads")
    list_parser.add_argument(
        "--data-dir", type=Path, default=Path("./gpt-exchange-data")
    )

    migrate_parser = subparsers.add_parser(
        "migrate", help="Migrate token-named legacy sessions into durable threads"
    )
    migrate_parser.add_argument(
        "--data-dir", type=Path, default=Path("./gpt-exchange-data")
    )
    migrate_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report legacy sessions without changing data",
    )

    health_parser = subparsers.add_parser("health", help="Check server health")
    health_parser.add_argument("--port", type=int, default=9741)

    args = parser.parse_args()

    if args.command == "create":
        thread_id, token, expires_at = create_thread(args.data_dir, args.ttl)
        print(f"Thread sid: {thread_id[:12]}", file=sys.stderr)
        print(f"Delivery expires: {expires_at}", file=sys.stderr)
        print(_delivery_url(args.base_url, token))
        return

    if args.command == "deliver":
        try:
            token, expires_at = issue_delivery(args.data_dir, args.sid, args.ttl)
        except ValueError as error:
            print(f"Cannot issue delivery: {error}", file=sys.stderr)
            sys.exit(1)
        print(f"Delivery expires: {expires_at}", file=sys.stderr)
        print(_delivery_url(args.base_url, token))
        return

    if args.command == "compatibility-issue":
        try:
            token, grant = issue_compatibility(args.data_dir, args.sid, args.ttl)
        except ValueError as error:
            print(f"Cannot issue compatibility grant: {error}", file=sys.stderr)
            sys.exit(1)
        print(grant.mode, file=sys.stderr)
        print(f"Scope: single thread {grant.thread_id[:12]}", file=sys.stderr)
        print(
            f"Allowed reader roles: {','.join(grant.allowed_reader_roles)}",
            file=sys.stderr,
        )
        print(f"Compatibility grant expires: {grant.expires_at}", file=sys.stderr)
        print(_compatibility_url(args.base_url, token))
        return

    if args.command == "compatibility-revoke":
        if not revoke_compatibility(args.data_dir, args.token):
            print("Compatibility grant not found", file=sys.stderr)
            sys.exit(1)
        print("Compatibility grant revoked")
        return

    if args.command == "list":
        threads = _store(args.data_dir).list_threads()
        if not threads:
            print("No threads found")
            return
        for thread in threads:
            print(
                f"{thread['sid']}  {thread['created_at']} "
                f"({thread['message_count']} messages)"
            )
        return

    if args.command == "migrate":
        result = _store(args.data_dir).migrate_legacy_sessions(dry_run=args.dry_run)
        mode = "would migrate" if args.dry_run else "migrated"
        print(
            f"{mode}: {result['migrated']}; already migrated: "
            f"{result['already_migrated']}; skipped: {result['skipped']}"
        )
        return

    if args.command == "health":
        if health_check(args.port):
            print(f"Server ok (http://127.0.0.1:{args.port})")
            return
        print(f"Server not responding on port {args.port}")
        sys.exit(1)

    parser.print_help()


if __name__ == "__main__":
    main()
