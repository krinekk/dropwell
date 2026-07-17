#!/usr/bin/env python3
"""
Claude-GPT Exchange Server
Minimal durable message exchange for code review between Claude and GPT.

Usage:
  python3 claude-gpt-exchange.py [--port 9741] [--data-dir ./gpt-exchange-data]
                                 [--ntfy-topic <topic>] [--ntfy-token <token>]

Session URL format:
  http://localhost:9741/exchange/<session-token>?role=claude
  http://localhost:9741/exchange/<session-token>?role=gpt

API:
  GET  /exchange/<token>?role=<role>     - List messages (optional role filter)
  POST /exchange/<token>?role=<role>     - Append message (Bearer auth)
  GET  /health                           - Health check
"""

import argparse
import json
import os
import secrets
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Lock
from urllib.parse import parse_qs, urlparse


class ExchangeStore:
    """Thread-safe store for exchange messages, backed by JSON files."""

    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.lock = Lock()

    def _session_file(self, token: str) -> Path:
        return self.data_dir / f"{token}.json"

    def create_session(self, ttl_minutes: int = 120) -> tuple[str, str]:
        """Create a new session, return (token, expires_at)."""
        token = secrets.token_hex(32)  # 64-char hex = 256 bits
        expires_at = (
            datetime.now(timezone.utc) + timedelta(minutes=ttl_minutes)
        ).isoformat()

        session = {
            "token": token,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "expires_at": expires_at,
            "messages": [],
        }

        with self.lock:
            path = self._session_file(token)
            with open(path, "w") as f:
                json.dump(session, f, indent=2)
            os.chmod(path, 0o600)

        return token, expires_at

    def get_messages(self, token: str, role: str | None = None) -> list[dict] | None:
        """Get messages for a session, optionally filtered by role."""
        with self.lock:
            path = self._session_file(token)
            if not path.exists():
                return None

            with open(path) as f:
                session = json.load(f)

            # Check expiration
            expires = datetime.fromisoformat(session["expires_at"])
            if expires < datetime.now(timezone.utc):
                return None

            messages = session["messages"]
            if role:
                messages = [m for m in messages if m.get("role") == role]
            return messages

    def add_message(self, token: str, role: str, body: str) -> dict | None:
        """Add a message. Returns message dict or None if expired/invalid."""
        with self.lock:
            path = self._session_file(token)
            if not path.exists():
                return None

            with open(path) as f:
                session = json.load(f)

            # Check expiration
            expires = datetime.fromisoformat(session["expires_at"])
            if expires < datetime.now(timezone.utc):
                return None

            message = {
                "id": secrets.token_hex(12),
                "role": role,
                "body": body,
                "posted_at": datetime.now(timezone.utc).isoformat(),
            }
            session["messages"].append(message)

            with open(path, "w") as f:
                json.dump(session, f, indent=2)

            return message

    def cleanup_expired(self) -> int:
        """Remove expired sessions. Returns count deleted."""
        now = datetime.now(timezone.utc)
        deleted = 0

        with self.lock:
            for path in self.data_dir.glob("*.json"):
                try:
                    with open(path) as f:
                        session = json.load(f)
                    if datetime.fromisoformat(session["expires_at"]) < now:
                        path.unlink()
                        deleted += 1
                except (json.JSONDecodeError, KeyError, ValueError):
                    path.unlink()
                    deleted += 1

        return deleted


class ExchangeHandler(BaseHTTPRequestHandler):
    store: "ExchangeStore | None" = None
    ntfy_config: "dict | None" = None

    def log_message(self, format, *args):
        """Suppress default HTTP logging; we log selectively."""
        del format, args

    def _send_json(self, code: int, data: dict):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data, indent=2).encode() + b"\n")

    def _send_text(self, code: int, text: str):
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(text.encode() + b"\n")

    def _get_bearer_token(self) -> str | None:
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            return auth[7:]
        return None

    def _notify(self, role: str, message_snippet: str):
        """Send ntfy notification if configured."""
        if not self.ntfy_config or not self.ntfy_config.get("url"):
            return

        try:
            title = f"{role.upper()} wrote a response"
            msg_short = message_snippet[:100]
            if len(message_snippet) > 100:
                msg_short += "..."

            headers = {"Title": title}
            if self.ntfy_config.get("token"):
                headers["Authorization"] = f"Bearer {self.ntfy_config['token']}"

            req = urllib.request.Request(
                url=self.ntfy_config["url"],
                data=f"{title}\n{msg_short}".encode(),
                headers=headers,
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=3):
                pass
        except Exception as e:
            print(f"[WARN] ntfy notification failed: {e}", file=sys.stderr)

    def do_GET(self):
        """GET /exchange/<token> - List messages (optionally filtered by role)."""
        parsed = urlparse(self.path)
        path_parts = parsed.path.strip("/").split("/")

        if parsed.path == "/health":
            self._send_json(200, {"status": "ok"})
            return

        if len(path_parts) == 2 and path_parts[0] == "exchange":
            token = path_parts[1]
            query = parse_qs(parsed.query)
            role = query.get("role", [None])[0]

            auth_token = self._get_bearer_token()
            if not auth_token or not secrets.compare_digest(auth_token, token):
                self._send_json(401, {"error": "invalid authorization"})
                return

            assert self.store is not None
            messages = self.store.get_messages(token, role=role)
            if messages is None:
                self._send_json(401, {"error": "invalid or expired token"})
                return

            self._send_json(200, {"messages": messages})
            return

        self._send_json(404, {"error": "not found"})

    def do_POST(self):
        """POST /exchange/<token> - Append message (raw text body, role in query)."""
        parsed = urlparse(self.path)
        path_parts = parsed.path.strip("/").split("/")

        if len(path_parts) == 2 and path_parts[0] == "exchange":
            token = path_parts[1]
            query = parse_qs(parsed.query)
            role = query.get("role", [None])[0]

            if not role or role not in ("claude", "gpt"):
                self._send_json(400, {"error": "role must be 'claude' or 'gpt'"})
                return

            auth_token = self._get_bearer_token()
            if not auth_token or not secrets.compare_digest(auth_token, token):
                self._send_json(401, {"error": "invalid authorization"})
                return

            content_length = int(self.headers.get("Content-Length", 0))
            if content_length > 50_000_000:  # 50 MiB limit
                self._send_json(413, {"error": "body too large"})
                return

            try:
                body = self.rfile.read(content_length).decode("utf-8")
            except UnicodeDecodeError:
                self._send_json(400, {"error": "invalid utf-8"})
                return

            assert self.store is not None
            message = self.store.add_message(token, role, body)
            if message is None:
                self._send_json(401, {"error": "invalid or expired token"})
                return

            self._notify(role, body)
            self._send_json(201, message)
            return

        self._send_json(404, {"error": "not found"})

    def do_OPTIONS(self):
        """Handle CORS preflight."""
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        self.end_headers()


def main():
    parser = argparse.ArgumentParser(
        description="Claude-GPT Exchange Server: session-based message exchange."
    )
    parser.add_argument(
        "--port", type=int, default=9741, help="HTTP port (default 9741)"
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path("./gpt-exchange-data"),
        help="Directory for session data (default ./gpt-exchange-data)",
    )
    parser.add_argument(
        "--ntfy-topic",
        type=str,
        help="ntfy topic URL (e.g. 'https://ntfy.sh/<topic>') for notifications",
    )
    parser.add_argument(
        "--ntfy-token",
        type=str,
        help="ntfy bearer token (if self-hosted and requires auth)",
    )

    args = parser.parse_args()

    # Prepare ntfy config
    ntfy_config = {}
    if args.ntfy_topic:
        ntfy_config["url"] = args.ntfy_topic
        ntfy_config["token"] = args.ntfy_token or ""

    # Set class-level config
    store = ExchangeStore(args.data_dir)
    deleted = store.cleanup_expired()
    if deleted:
        print(f"Cleaned up {deleted} expired session(s)", file=sys.stderr)
    ExchangeHandler.store = store
    ExchangeHandler.ntfy_config = ntfy_config

    # Server
    server = HTTPServer(("127.0.0.1", args.port), ExchangeHandler)
    print(
        f"Claude-GPT Exchange listening on http://127.0.0.1:{args.port}",
        file=sys.stderr,
    )
    print(f"Data directory: {args.data_dir.resolve()}", file=sys.stderr)
    if ntfy_config:
        print(f"Notifications enabled: {ntfy_config['url']}", file=sys.stderr)
    print("Press Ctrl+C to stop.", file=sys.stderr)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutdown.", file=sys.stderr)


if __name__ == "__main__":
    main()
