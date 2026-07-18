#!/usr/bin/env python3
"""
Claude-GPT Exchange Server
Minimal durable message exchange for code review between Claude and GPT.

Usage:
  python3 claude-gpt-exchange.py [--port 9741] [--data-dir ./gpt-exchange-data]
                                 [--ntfy-topic <topic>] [--ntfy-token <token>]

Delivery URL format:
  https://drop.krinekk.dev/exchange/<one-time-token>

API:
  GET  /exchange/<one-time-token>  - Redeem one GPT thread snapshot
  GET  /compatibility/<token>      - Replay an explicit compatibility snapshot
  POST /ui/api/send                         - Append as the authenticated UI user
  GET  /health                           - Health check
"""

import argparse
import fcntl
import hashlib
import json
import os
import re
import secrets
import sys
import urllib.request
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock
from urllib.parse import parse_qs, urlparse

COMPATIBILITY_MODE_LABEL = "COMPATIBILITY MODE — REPLAYABLE UNTIL EXPIRY"
COMPATIBILITY_MODE = "compatibility"
COMPATIBILITY_CACHE_NOTICE = (
    "REVOCATION MAY TAKE UP TO 60 SECONDS IN SHARED CACHES"
)
COMPATIBILITY_SCHEMA = "drop-exchange/compatibility-grant-v1"
COMPATIBILITY_DEFAULT_TTL_MINUTES = 5
COMPATIBILITY_MAX_TTL_MINUTES = 15
COMPATIBILITY_CACHE_MAX_AGE_SECONDS = 60
COMPATIBILITY_MAX_MESSAGES = 64
COMPATIBILITY_MAX_MESSAGE_BYTES = 32 * 1024
COMPATIBILITY_MAX_TOTAL_BYTES = 128 * 1024
COMPATIBILITY_ALLOWED_READER_ROLES = ("gpt",)
COMPATIBILITY_METRIC_NAMES = (
    "compatibility_grants_issued_total",
    "compatibility_first_reads_total",
    "compatibility_replays_total",
    "compatibility_role_denied_total",
    "compatibility_revocations_total",
    "compatibility_revoked_reads_total",
    "compatibility_expired_reads_total",
    "compatibility_unknown_reads_total",
    "compatibility_content_rejections_total",
)
COMPATIBILITY_SENSITIVE_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\bauthorization\s*:\s*bearer\s+\S+", re.IGNORECASE),
    re.compile(
        r"\b(?:api[_-]?key|access[_-]?token|client[_-]?secret|password)"
        r"\s*[:=]\s*\S+",
        re.IGNORECASE,
    ),
    re.compile(r"\b(?:sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,})\b"),
)


def _compatibility_snapshot_hash(snapshot: dict) -> str:
    canonical = json.dumps(
        snapshot,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()
    return hashlib.sha256(canonical).hexdigest()


@dataclass(frozen=True)
class CompatibilityGrant:
    """Persisted compatibility grant, distinct from one-time deliveries."""

    schema: str
    mode: str
    grant_id: str
    thread_id: str
    allowed_reader_roles: list[str]
    issued_at: str
    expires_at: str
    revoked_at: str | None
    read_count: int
    snapshot: dict
    snapshot_hash: str

    @classmethod
    def from_dict(cls, value: dict) -> "CompatibilityGrant":
        grant = cls(**value)
        if grant.schema != COMPATIBILITY_SCHEMA:
            raise ValueError("invalid compatibility schema")
        if grant.mode != COMPATIBILITY_MODE_LABEL:
            raise ValueError("invalid compatibility mode")
        if grant.allowed_reader_roles != list(COMPATIBILITY_ALLOWED_READER_ROLES):
            raise ValueError("invalid compatibility reader roles")
        if re.fullmatch(r"[0-9a-f]{24}", grant.grant_id) is None:
            raise ValueError("invalid compatibility grant id")
        if re.fullmatch(r"[0-9a-f]{32}", grant.thread_id) is None:
            raise ValueError("invalid compatibility thread scope")
        if type(grant.read_count) is not int or grant.read_count < 0:
            raise ValueError("invalid compatibility read count")
        issued_at = datetime.fromisoformat(grant.issued_at)
        expires_at = datetime.fromisoformat(grant.expires_at)
        if issued_at.tzinfo is None or expires_at.tzinfo is None:
            raise ValueError("compatibility timestamps must be timezone-aware")
        if expires_at <= issued_at:
            raise ValueError("invalid compatibility expiry")
        if grant.revoked_at is not None:
            revoked_at = datetime.fromisoformat(grant.revoked_at)
            if revoked_at.tzinfo is None:
                raise ValueError("compatibility timestamps must be timezone-aware")
        if not isinstance(grant.snapshot, dict):
            raise ValueError("invalid compatibility snapshot")
        if re.fullmatch(r"[0-9a-f]{64}", grant.snapshot_hash) is None:
            raise ValueError("invalid compatibility snapshot hash")
        if not secrets.compare_digest(
            grant.snapshot_hash, _compatibility_snapshot_hash(grant.snapshot)
        ):
            raise ValueError("compatibility snapshot integrity check failed")
        return grant


class ExchangeStore:
    """Durable threads plus one-time, read-only delivery capabilities."""

    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.lock = Lock()
        self.lock_path = self.data_dir / ".exchange.lock"
        self.lock_path.touch(exist_ok=True)
        os.chmod(self.lock_path, 0o600)

    @contextmanager
    def _locked(self):
        """Serialize mutations across threads and independent server processes."""
        with self.lock:
            with open(self.lock_path, "a+") as lock_file:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _thread_file(self, thread_id: str) -> Path:
        return self.data_dir / f"thread_{thread_id}.json"

    def _delivery_file(self, token: str) -> Path:
        return self.data_dir / f"delivery_{token}.json"

    @staticmethod
    def _read_json(path: Path) -> dict | None:
        try:
            with open(path) as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return None

    @staticmethod
    def _write_json(path: Path, data: dict) -> None:
        temp_path = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
        with open(temp_path, "w") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(temp_path, 0o600)
        os.replace(temp_path, path)

    def create_thread(self) -> str:
        """Create a durable exchange thread; it has no delivery credential."""
        with self._locked():
            while True:
                thread_id = secrets.token_hex(16)
                path = self._thread_file(thread_id)
                if not path.exists():
                    break
            self._write_json(
                path,
                {
                    "thread_id": thread_id,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "messages": [],
                },
            )
        return thread_id

    def get_thread(self, thread_id: str) -> dict | None:
        """Return a durable thread without any delivery credentials."""
        with self._locked():
            thread = self._read_json(self._thread_file(thread_id))
            return thread

    def add_message(self, thread_id: str, role: str, body: str) -> dict | None:
        """Append a message to a durable thread."""
        with self._locked():
            path = self._thread_file(thread_id)
            thread = self._read_json(path)
            if thread is None:
                return None
            message = {
                "id": secrets.token_hex(12),
                "role": role,
                "body": body,
                "posted_at": datetime.now(timezone.utc).isoformat(),
            }
            thread["messages"].append(message)
            self._write_json(path, thread)
            return message

    def issue_delivery(
        self, thread_id: str, role: str = "gpt", ttl_minutes: int = 15
    ) -> tuple[str, str]:
        """Issue a single-use, read-only delivery capability for a thread."""
        if role != "gpt":
            raise ValueError("only the gpt delivery role is supported")
        with self._locked():
            if self._read_json(self._thread_file(thread_id)) is None:
                raise ValueError("unknown thread")
            token, expires_at = self._issue_delivery_locked(
                thread_id, role, ttl_minutes
            )
        return token, expires_at

    def accept_gpt_drop(
        self, thread_id: str, drop_id: str, body: str, ttl_minutes: int = 15
    ) -> tuple[dict | None, tuple[str, str] | None, bool]:
        """Mirror one KOS drop and issue the next delivery exactly once."""
        with self._locked():
            path = self._thread_file(thread_id)
            thread = self._read_json(path)
            if thread is None:
                raise ValueError("unknown thread")
            processed = thread.setdefault("processed_drop_ids", [])
            if drop_id in processed:
                delivery = self._find_or_recover_drop_delivery_locked(
                    thread_id, drop_id, ttl_minutes
                )
                return None, delivery, True
            message = {
                "id": secrets.token_hex(12),
                "role": "gpt",
                "body": body,
                "posted_at": datetime.now(timezone.utc).isoformat(),
            }
            thread["messages"].append(message)
            processed.append(drop_id)
            self._write_json(path, thread)
            token, expires_at = self._issue_delivery_locked(
                thread_id, "gpt", ttl_minutes, source_drop_id=drop_id
            )
            return message, (token, expires_at), False

    def redeem_delivery(
        self, token: str, role: str
    ) -> tuple[dict | None, str | None]:
        """Atomically consume a delivery capability and return its thread."""
        with self._locked():
            path = self._delivery_file(token)
            delivery = self._read_json(path)
            if delivery is None:
                return None, "unknown"
            if role != delivery.get("role"):
                return None, "role"
            if delivery.get("consumed_at") is not None:
                return None, "consumed"
            expires_at = datetime.fromisoformat(delivery["expires_at"])
            if expires_at < datetime.now(timezone.utc):
                return None, "expired"
            delivery["consumed_at"] = datetime.now(timezone.utc).isoformat()
            self._write_json(path, delivery)
            thread = self._read_json(self._thread_file(delivery["thread_id"]))
            if thread is None:
                return None, "unknown"
            return thread, None

    def _issue_delivery_locked(
        self,
        thread_id: str,
        role: str,
        ttl_minutes: int,
        source_drop_id: str | None = None,
    ) -> tuple[str, str]:
        token = secrets.token_hex(32)
        expires_at = (
            datetime.now(timezone.utc) + timedelta(minutes=ttl_minutes)
        ).isoformat()
        delivery = {
            "token": token,
            "thread_id": thread_id,
            "role": role,
            "issued_at": datetime.now(timezone.utc).isoformat(),
            "expires_at": expires_at,
            "consumed_at": None,
        }
        if source_drop_id is not None:
            delivery["source_drop_id"] = source_drop_id
        self._write_json(self._delivery_file(token), delivery)
        return token, expires_at

    def _find_or_recover_drop_delivery_locked(
        self, thread_id: str, drop_id: str, ttl_minutes: int
    ) -> tuple[str, str]:
        """Return the delivery for a processed drop, recovering an interrupted emit.

        The thread marker is written before its delivery so a crash cannot duplicate
        a GPT message. If that crash happens, a retry creates the missing delivery.
        A consumed delivery similarly gets a fresh capability on retry.
        """
        now = datetime.now(timezone.utc)
        for path in self.data_dir.glob("delivery_*.json"):
            delivery = self._read_json(path)
            if (
                delivery is None
                or delivery.get("thread_id") != thread_id
                or delivery.get("source_drop_id") != drop_id
            ):
                continue
            try:
                unexpired = datetime.fromisoformat(delivery["expires_at"]) >= now
            except (KeyError, ValueError):
                unexpired = False
            if delivery.get("consumed_at") is None and unexpired:
                return delivery["token"], delivery["expires_at"]
        return self._issue_delivery_locked(
            thread_id, "gpt", ttl_minutes, source_drop_id=drop_id
        )

    def list_threads(self) -> list[dict]:
        """List durable threads for the UI without delivery tokens."""
        threads = []
        with self._locked():
            for path in sorted(self.data_dir.glob("thread_*.json")):
                thread = self._read_json(path)
                if thread is None:
                    continue
                threads.append(
                    {
                        "sid": thread["thread_id"][:12],
                        "created_at": thread["created_at"],
                        "message_count": len(thread.get("messages", [])),
                    }
                )
        return threads

    def resolve_sid(self, sid: str) -> str | None:
        """Resolve a UI short id to a durable thread id, server-side only."""
        if len(sid) < 12:
            return None
        with self._locked():
            for path in self.data_dir.glob(f"thread_{sid}*.json"):
                thread = self._read_json(path)
                if thread is not None:
                    return thread["thread_id"]
        return None

    def cleanup_expired(self) -> int:
        """Remove expired delivery capabilities but retain durable threads."""
        now = datetime.now(timezone.utc)
        deleted = 0
        with self._locked():
            for path in self.data_dir.glob("delivery_*.json"):
                delivery = self._read_json(path)
                if delivery is None:
                    path.unlink(missing_ok=True)
                    deleted += 1
                    continue
                try:
                    expired = datetime.fromisoformat(delivery["expires_at"]) < now
                except (KeyError, ValueError):
                    expired = True
                if expired:
                    path.unlink(missing_ok=True)
                    deleted += 1
        return deleted

    def migrate_legacy_sessions(self, dry_run: bool = False) -> dict[str, int]:
        """Move legacy token-named sessions into durable threads safely.

        The old credential is never reused as a thread id. Original JSON is
        moved into a private backup directory only after its replacement thread
        is durable, making retries safe after an interrupted migration.
        """
        results = {"migrated": 0, "already_migrated": 0, "skipped": 0}
        backup_dir = self.data_dir / "legacy-backups"
        with self._locked():
            legacy_paths = [
                path
                for path in self.data_dir.glob("*.json")
                if not path.name.startswith(("thread_", "delivery_"))
            ]
            for path in legacy_paths:
                legacy = self._read_json(path)
                if not self._is_legacy_session(legacy):
                    results["skipped"] += 1
                    continue
                assert legacy is not None
                digest = hashlib.sha256(
                    json.dumps(legacy, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest()
                existing = self._find_migrated_thread(digest)
                if existing is not None:
                    results["already_migrated"] += 1
                    if not dry_run:
                        self._archive_legacy(path, backup_dir, digest)
                    continue
                if dry_run:
                    results["migrated"] += 1
                    continue
                thread_id = secrets.token_hex(16)
                while self._thread_file(thread_id).exists():
                    thread_id = secrets.token_hex(16)
                self._write_json(
                    self._thread_file(thread_id),
                    {
                        "thread_id": thread_id,
                        "created_at": legacy["created_at"],
                        "messages": legacy["messages"],
                        "migrated_from_sha256": digest,
                    },
                )
                self._archive_legacy(path, backup_dir, digest)
                results["migrated"] += 1
        return results

    @staticmethod
    def _is_legacy_session(value: dict | None) -> bool:
        return bool(
            isinstance(value, dict)
            and isinstance(value.get("token"), str)
            and isinstance(value.get("created_at"), str)
            and isinstance(value.get("expires_at"), str)
            and isinstance(value.get("messages"), list)
        )

    def _find_migrated_thread(self, digest: str) -> dict | None:
        for path in self.data_dir.glob("thread_*.json"):
            thread = self._read_json(path)
            if thread and thread.get("migrated_from_sha256") == digest:
                return thread
        return None

    def _archive_legacy(self, path: Path, backup_dir: Path, digest: str) -> None:
        backup_dir.mkdir(mode=0o700, exist_ok=True)
        os.chmod(backup_dir, 0o700)
        backup_path = backup_dir / f"legacy-{digest[:16]}.json"
        os.replace(path, backup_path)
        os.chmod(backup_path, 0o600)


class CompatibilityGrantStore:
    """Replayable, expiring grants stored outside one-time delivery state."""

    def __init__(self, data_dir: Path, thread_store: ExchangeStore, now_fn=None):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.data_dir, 0o700)
        self.thread_store = thread_store
        self.now_fn = now_fn or (lambda: datetime.now(timezone.utc))
        self.lock = Lock()
        self.lock_path = self.data_dir / ".compatibility.lock"
        self.lock_path.touch(exist_ok=True)
        os.chmod(self.lock_path, 0o600)
        self.metrics_path = self.data_dir / "metrics.json"

    @contextmanager
    def _locked(self):
        with self.lock:
            with open(self.lock_path, "a+") as lock_file:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _read_json(path: Path) -> dict | None:
        try:
            with open(path) as file:
                return json.load(file)
        except (FileNotFoundError, json.JSONDecodeError):
            return None

    @staticmethod
    def _write_json(path: Path, data: dict) -> None:
        temp_path = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
        with open(temp_path, "w") as file:
            json.dump(data, file, indent=2)
            file.flush()
            os.fsync(file.fileno())
        os.chmod(temp_path, 0o600)
        os.replace(temp_path, path)

    def _grant_file(self, token: str) -> Path:
        digest = hashlib.sha256(token.encode()).hexdigest()
        return self.data_dir / f"grant_{digest}.json"

    def _empty_metrics(self) -> dict[str, int]:
        return {name: 0 for name in COMPATIBILITY_METRIC_NAMES}

    def _metrics_locked(self) -> dict[str, int]:
        metrics = self._read_json(self.metrics_path)
        if not isinstance(metrics, dict):
            return self._empty_metrics()
        return {
            name: int(metrics.get(name, 0)) for name in COMPATIBILITY_METRIC_NAMES
        }

    def _increment_locked(self, metric: str) -> None:
        metrics = self._metrics_locked()
        metrics[metric] += 1
        self._write_json(self.metrics_path, metrics)

    def _increment(self, metric: str) -> None:
        with self._locked():
            self._increment_locked(metric)

    def metrics(self) -> dict[str, int]:
        with self._locked():
            return self._metrics_locked()

    def issue(
        self,
        thread_id: str,
        ttl_minutes: int = COMPATIBILITY_DEFAULT_TTL_MINUTES,
        allowed_roles: tuple[str, ...] = COMPATIBILITY_ALLOWED_READER_ROLES,
    ) -> tuple[str, CompatibilityGrant]:
        if not 1 <= ttl_minutes <= COMPATIBILITY_MAX_TTL_MINUTES:
            raise ValueError("compatibility TTL must be between 1 and 15 minutes")
        if tuple(allowed_roles) != COMPATIBILITY_ALLOWED_READER_ROLES:
            raise ValueError("only the gpt reader role is supported")
        thread = self.thread_store.get_thread(thread_id)
        if thread is None:
            raise ValueError("unknown thread")
        try:
            snapshot = self._validated_snapshot(thread)
        except ValueError:
            self._increment("compatibility_content_rejections_total")
            raise

        now = self.now_fn()
        token = secrets.token_hex(32)
        grant = CompatibilityGrant(
            schema=COMPATIBILITY_SCHEMA,
            mode=COMPATIBILITY_MODE_LABEL,
            grant_id=secrets.token_hex(12),
            thread_id=thread_id,
            allowed_reader_roles=list(allowed_roles),
            issued_at=now.isoformat(),
            expires_at=(now + timedelta(minutes=ttl_minutes)).isoformat(),
            revoked_at=None,
            read_count=0,
            snapshot=snapshot,
            snapshot_hash=_compatibility_snapshot_hash(snapshot),
        )
        with self._locked():
            self._write_json(self._grant_file(token), asdict(grant))
            self._increment_locked("compatibility_grants_issued_total")
        return token, grant

    def read(self, token: str, role: str) -> tuple[dict | None, str | None]:
        snapshot, error, _event, _grant_id = self.read_for_http(token, role)
        return snapshot, error

    def read_for_http(
        self, token: str, role: str
    ) -> tuple[dict | None, str | None, str, str | None]:
        with self._locked():
            path = self._grant_file(token)
            raw_grant = self._read_json(path)
            if raw_grant is None:
                self._increment_locked("compatibility_unknown_reads_total")
                return None, "unknown", "denied", None
            try:
                grant = CompatibilityGrant.from_dict(raw_grant)
            except (TypeError, ValueError):
                self._increment_locked("compatibility_unknown_reads_total")
                return None, "unknown", "denied", None
            if role not in grant.allowed_reader_roles:
                self._increment_locked("compatibility_role_denied_total")
                return None, "role", "denied", grant.grant_id
            if grant.revoked_at is not None:
                self._increment_locked("compatibility_revoked_reads_total")
                return None, "revoked", "denied", grant.grant_id
            if self.now_fn() >= datetime.fromisoformat(grant.expires_at):
                self._increment_locked("compatibility_expired_reads_total")
                return None, "expired", "denied", grant.grant_id
            try:
                snapshot = self._validated_snapshot(grant.snapshot)
            except ValueError:
                self._increment_locked("compatibility_content_rejections_total")
                return None, "content", "denied", grant.grant_id

            event = "first_read" if grant.read_count == 0 else "replay"
            metric = (
                "compatibility_first_reads_total"
                if grant.read_count == 0
                else "compatibility_replays_total"
            )
            updated = CompatibilityGrant(
                **{**asdict(grant), "read_count": grant.read_count + 1}
            )
            self._write_json(path, asdict(updated))
            self._increment_locked(metric)
            payload = {
                "schema": grant.schema,
                "mode": COMPATIBILITY_MODE,
                "warning": COMPATIBILITY_MODE_LABEL,
                "revocable": True,
                "cache_notice": COMPATIBILITY_CACHE_NOTICE,
                "thread_id": grant.thread_id,
                "snapshot_hash": grant.snapshot_hash,
                "scope": {"type": "single_thread", "thread_id": grant.thread_id},
                "allowed_reader_roles": grant.allowed_reader_roles,
                "issued_at": grant.issued_at,
                "expires_at": grant.expires_at,
                "messages": snapshot["messages"],
            }
            return payload, None, event, grant.grant_id

    def revoke(self, token: str) -> bool:
        with self._locked():
            path = self._grant_file(token)
            raw_grant = self._read_json(path)
            if raw_grant is None:
                return False
            grant = CompatibilityGrant.from_dict(raw_grant)
            if grant.revoked_at is None:
                updated = CompatibilityGrant(
                    **{**asdict(grant), "revoked_at": self.now_fn().isoformat()}
                )
                self._write_json(path, asdict(updated))
                self._increment_locked("compatibility_revocations_total")
            return True

    @staticmethod
    def _validated_snapshot(thread: dict) -> dict:
        messages = thread.get("messages")
        if not isinstance(messages, list):
            raise ValueError("compatibility snapshot requires text messages")
        if len(messages) > COMPATIBILITY_MAX_MESSAGES:
            raise ValueError("compatibility snapshot has too many messages")

        clean_messages = []
        total_bytes = 0
        attachment_fields = {"attachment", "attachments", "file", "files"}
        for message in messages:
            if not isinstance(message, dict):
                raise ValueError("compatibility snapshot requires text messages")
            if attachment_fields.intersection(message):
                raise ValueError("attachments are not supported in compatibility mode")
            body = message.get("body")
            if not isinstance(body, str):
                raise ValueError("compatibility snapshot requires text messages")
            body_bytes = len(body.encode("utf-8"))
            if body_bytes > COMPATIBILITY_MAX_MESSAGE_BYTES:
                raise ValueError("compatibility message exceeds content limit")
            total_bytes += body_bytes
            if total_bytes > COMPATIBILITY_MAX_TOTAL_BYTES:
                raise ValueError("compatibility snapshot exceeds content limit")
            if any(
                pattern.search(body) for pattern in COMPATIBILITY_SENSITIVE_PATTERNS
            ):
                raise ValueError(
                    "sensitive content is not accepted in compatibility mode"
                )
            clean_messages.append(
                {
                    key: message[key]
                    for key in ("id", "role", "body", "posted_at")
                    if key in message
                }
            )
        return {"messages": clean_messages}


UI_HTML = """<!doctype html>
<html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>drop · exchange</title>
<style>
:root { --bg:#05070d; --panel:rgba(12,18,30,.88); --panel-strong:#101827;
  --text:#f4f8ff; --muted:#91a0b6; --muted-strong:#c4d0e3;
  --line:rgba(146,163,184,.18); --line-strong:rgba(108,228,255,.32);
  --accent:#38d9ff; --accent-soft:rgba(56,217,255,.13);
  --good:#19f28b; --good-soft:rgba(25,242,139,.12);
  --warning:#ffd166; --warning-soft:rgba(255,209,102,.14);
  --violet:#a96cff; --radius:18px; color-scheme:dark; }
*{box-sizing:border-box}
body{margin:0;min-height:100vh;color:var(--text);font-size:15px;
  font-family:Inter,ui-sans-serif,system-ui,sans-serif;
  background:linear-gradient(120deg,rgba(56,217,255,.10),transparent 28%),
    linear-gradient(240deg,rgba(25,242,139,.08),transparent 30%),
    linear-gradient(180deg,#070a12 0%,#05070d 55%,#03050a 100%)}
.app{max-width:1080px;margin:0 auto;padding:24px 16px 110px}
header{display:flex;align-items:center;gap:12px;flex-wrap:wrap;
  padding:16px 20px;border:1px solid var(--line-strong);border-radius:22px;
  background:linear-gradient(145deg,rgba(13,20,34,.96),rgba(8,12,22,.98));
  box-shadow:0 24px 90px rgba(0,0,0,.46),0 0 34px rgba(56,217,255,.15)}
.logo{font-weight:800;letter-spacing:.04em;font-size:17px}
.logo span{color:var(--accent)}
.chip{font-size:12px;padding:5px 12px;border-radius:999px;white-space:nowrap;
  border:1px solid var(--line);color:var(--muted-strong);background:var(--panel)}
.chip.live{color:var(--good);border-color:rgba(25,242,139,.4);
  background:var(--good-soft)}
.chip.ttl{color:var(--warning);border-color:rgba(255,209,102,.4);
  background:var(--warning-soft)}
.chip.user{margin-left:auto}
.sessions{display:flex;gap:10px;margin:16px 0 4px;flex-wrap:wrap}
.stab{font-size:13px;padding:8px 14px;border-radius:12px;cursor:pointer;
  border:1px solid var(--line);background:var(--panel);color:var(--muted-strong)}
.stab.active{border-color:var(--line-strong);color:var(--text);
  background:var(--accent-soft)}
.stab.expired{opacity:.45}
.stab .n{color:var(--accent);font-weight:700;margin-left:6px}
.thread{display:flex;flex-direction:column;gap:14px;margin-top:16px}
.msg{display:flex;flex-direction:column;max-width:74%}
.msg.claude{align-self:flex-start}
.msg.gpt,.msg.erik{align-self:flex-end}
.meta{display:flex;gap:8px;align-items:baseline;font-size:12px;
  color:var(--muted);margin:0 6px 4px}
.msg.gpt .meta,.msg.erik .meta{flex-direction:row-reverse}
.who{font-weight:700;letter-spacing:.03em}
.msg.claude .who{color:var(--accent)}
.msg.gpt .who{color:var(--violet)}
.msg.erik .who{color:var(--good)}
.bubble{padding:12px 16px;border-radius:var(--radius);line-height:1.55;
  border:1px solid var(--line);background:var(--panel-strong);
  box-shadow:0 8px 30px rgba(0,0,0,.35);white-space:pre-wrap;
  overflow-wrap:anywhere}
.msg.claude .bubble{border-color:rgba(56,217,255,.28);
  border-top-left-radius:6px}
.msg.gpt .bubble{border-color:rgba(169,108,255,.30);
  border-top-right-radius:6px}
.msg.erik .bubble{border-color:rgba(25,242,139,.30);
  border-top-right-radius:6px}
.empty{color:var(--muted);text-align:center;padding:40px 0}
.composer{position:fixed;bottom:0;left:0;right:0;
  background:linear-gradient(180deg,transparent,#03050a 40%);
  padding:26px 16px 18px}
.cinner{max-width:1080px;margin:0 auto;display:flex;gap:10px;
  border:1px solid var(--line-strong);border-radius:16px;padding:10px 14px;
  background:var(--panel-strong);box-shadow:0 24px 90px rgba(0,0,0,.46)}
.cinner textarea{flex:1;background:none;border:0;outline:0;resize:none;
  color:var(--text);font:inherit;max-height:120px}
.btn{border:0;border-radius:10px;padding:9px 16px;font-weight:700;
  cursor:pointer;background:linear-gradient(135deg,#38d9ff,#2f7cff);
  color:#04131c}
.hint{text-align:center;color:var(--muted);font-size:11.5px;margin-top:8px}
</style></head><body>
<div class="app">
  <header>
    <div class="logo">drop<span>·exchange</span></div>
    <span class="chip" id="status">cargando…</span>
    <span class="chip ttl" id="ttl" hidden></span>
    <span class="chip user" id="user"></span>
  </header>
  <div class="sessions" id="sessions"></div>
  <div class="thread" id="thread"><div class="empty">Sin sesión</div></div>
</div>
<div class="composer">
  <div class="cinner">
    <textarea id="box" rows="1"
      placeholder="Escribir como Erik (role=erik)…"></textarea>
    <button class="btn" id="send">Enviar</button>
  </div>
  <div class="hint">Autenticado vía Cloudflare Access · los agentes
    escriben por su propio canal</div>
</div>
<script>
let SID=null, TIMER=null;
const $=id=>document.getElementById(id);
async function j(u,o){const r=await fetch(u,o);
  if(!r.ok)throw new Error(r.status);return r.json()}
function fmt(iso){return new Date(iso).toLocaleTimeString("es-ES",
  {hour:"2-digit",minute:"2-digit"})}
async function loadSessions(){
  const d=await j("/ui/api/sessions");
  const el=$("sessions");el.innerHTML="";
  d.sessions.forEach(s=>{
    const t=document.createElement("div");
    t.className="stab"+(s.sid===SID?" active":"");
    t.textContent=s.sid+"… ";
    const n=document.createElement("span");n.className="n";
    n.textContent=s.message_count;t.appendChild(n);
    t.onclick=()=>{SID=s.sid;loadSessions();loadThread()};
    el.appendChild(t)});
  if(!SID&&d.sessions.length){
    SID=d.sessions[d.sessions.length-1].sid;
    loadSessions();loadThread()}
  $("user").textContent=d.user||""}
async function loadThread(){
  if(!SID)return;
  const d=await j("/ui/api/thread?sid="+SID);
  $("status").textContent="● hilo activo";
  $("status").className="chip live";
  $("ttl").hidden=true;
  const el=$("thread");el.innerHTML="";
  if(!d.messages.length)
    el.innerHTML='<div class="empty">Hilo vacío</div>';
  d.messages.forEach(m=>{
    const w=document.createElement("div");w.className="msg "+m.role;
    const meta=document.createElement("div");meta.className="meta";
    const who=document.createElement("span");who.className="who";
    who.textContent=m.role.toUpperCase();
    const ts=document.createElement("span");ts.textContent=fmt(m.posted_at);
    meta.append(who,ts);
    const b=document.createElement("div");b.className="bubble";
    b.textContent=m.body;w.append(meta,b);el.appendChild(w)});
  window.scrollTo(0,document.body.scrollHeight)}
$("send").onclick=async()=>{
  const t=$("box").value.trim();if(!t||!SID)return;
  await fetch("/ui/api/send?sid="+SID+"&role=erik",
    {method:"POST",body:t});
  $("box").value="";loadThread()};
$("box").addEventListener("keydown",e=>{
  if(e.key==="Enter"&&!e.shiftKey){e.preventDefault();$("send").click()}});
loadSessions();
TIMER=setInterval(()=>{loadSessions();if(SID)loadThread()},10000);
</script></body></html>
"""


class ExchangeHandler(BaseHTTPRequestHandler):
    store: "ExchangeStore | None" = None
    compatibility_store: "CompatibilityGrantStore | None" = None
    ntfy_config: "dict | None" = None
    ui_email: "str | None" = None

    def _ui_authorized(self) -> bool:
        """UI requires the identity header injected by Cloudflare Access."""
        if not self.ui_email:
            return False
        header = self.headers.get("Cf-Access-Authenticated-User-Email", "")
        return secrets.compare_digest(header, self.ui_email)

    def log_message(self, format, *args):
        """Suppress default HTTP logging; we log selectively."""
        del format, args

    def _send_json(self, code: int, data: dict):
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(json.dumps(data, indent=2).encode() + b"\n")

    def _send_compatibility_json(
        self, code: int, data: dict, cache_max_age: int = 0
    ) -> None:
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        if code == 200:
            self.send_header(
                "Cache-Control", f"public, max-age={cache_max_age}, must-revalidate"
            )
        else:
            self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Drop-Exchange-Mode", "compatibility-replayable")
        self.end_headers()
        self.wfile.write(json.dumps(data, indent=2).encode() + b"\n")

    @staticmethod
    def _compatibility_log(
        event: str,
        outcome: str,
        grant_id: str | None = None,
        thread_id: str | None = None,
    ) -> None:
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "mode": COMPATIBILITY_MODE_LABEL,
            "event": event,
            "outcome": outcome,
            "grant_id": grant_id or "unknown",
        }
        if thread_id:
            record["thread_id"] = thread_id
        print(json.dumps(record, separators=(",", ":")), file=sys.stderr)

    def _send_text(self, code: int, text: str):
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(text.encode() + b"\n")

    def _content_length(self) -> int | None:
        """Parse Content-Length; reject malformed, negative, or oversized values."""
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            return None
        if length < 0 or length > 50_000_000:
            return None
        return length

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

        if parsed.path == "/ui" or parsed.path.startswith("/ui/"):
            if not self._ui_authorized():
                self._send_json(403, {"error": "ui requires authentication"})
                return
            assert self.store is not None
            if parsed.path == "/ui":
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(UI_HTML.encode())
                return
            if parsed.path == "/ui/api/sessions":
                email = self.headers.get(
                    "Cf-Access-Authenticated-User-Email", ""
                )
                self._send_json(
                    200,
                    {"sessions": self.store.list_threads(), "user": email},
                )
                return
            if parsed.path == "/ui/api/thread":
                query = parse_qs(parsed.query)
                sid = query.get("sid", [""])[0]
                token = self.store.resolve_sid(sid)
                if not token:
                    self._send_json(404, {"error": "unknown session"})
                    return
                thread = self.store.get_thread(token)
                assert thread is not None
                self._send_json(
                    200,
                    {
                        "messages": thread["messages"],
                        "created_at": thread["created_at"],
                    },
                )
                return
            if parsed.path == "/ui/api/compatibility/metrics":
                assert self.compatibility_store is not None
                self._send_json(
                    200,
                    {
                        "mode": COMPATIBILITY_MODE,
                        "warning": COMPATIBILITY_MODE_LABEL,
                        "metrics": self.compatibility_store.metrics(),
                    },
                )
                return
            self._send_json(404, {"error": "not found"})
            return

        if len(path_parts) == 2 and path_parts[0] == "compatibility":
            token = path_parts[1]
            role = parse_qs(parsed.query, keep_blank_values=True).get(
                "role", ["gpt"]
            )[0]
            assert self.compatibility_store is not None
            payload, error, event, grant_id = (
                self.compatibility_store.read_for_http(token, role or "")
            )
            if error:
                status = 403 if error == "role" else 410
                if error == "unknown":
                    status = 404
                self._compatibility_log(event, error, grant_id)
                self._send_compatibility_json(
                    status,
                    {
                        "mode": COMPATIBILITY_MODE,
                        "warning": COMPATIBILITY_MODE_LABEL,
                        "revocable": True,
                        "error": "compatibility grant unavailable",
                        "reason": error,
                    },
                )
                return
            assert payload is not None
            remaining = int(
                (
                    datetime.fromisoformat(payload["expires_at"])
                    - datetime.now(timezone.utc)
                ).total_seconds()
            )
            cache_max_age = max(
                1, min(COMPATIBILITY_CACHE_MAX_AGE_SECONDS, remaining)
            )
            self._compatibility_log(
                event,
                "allowed",
                grant_id,
                payload["thread_id"],
            )
            self._send_compatibility_json(200, payload, cache_max_age)
            return

        if len(path_parts) == 2 and path_parts[0] == "exchange":
            token = path_parts[1]
            query = parse_qs(parsed.query)
            # The sole delivery role is GPT. Keeping it out of the capability
            # URL makes the literal handoff compatible with constrained readers.
            role = query.get("role", ["gpt"])[0]
            assert self.store is not None
            thread, error = self.store.redeem_delivery(token, role or "")
            if error:
                status = 410 if error in ("consumed", "expired") else 404
                self._send_json(status, {"error": "delivery unavailable"})
                return
            assert thread is not None
            self._send_json(200, {"messages": thread["messages"]})
            return

        self._send_json(404, {"error": "not found"})

    def do_POST(self):
        """Only the authenticated UI may write to durable threads over HTTP."""
        parsed = urlparse(self.path)

        path_parts = parsed.path.strip("/").split("/")
        if len(path_parts) == 2 and path_parts[0] == "compatibility":
            self._compatibility_log("denied", "method_not_allowed")
            self._send_compatibility_json(
                405,
                {
                    "mode": COMPATIBILITY_MODE,
                    "warning": COMPATIBILITY_MODE_LABEL,
                    "revocable": True,
                    "error": "compatibility mode is read-only",
                },
            )
            return

        if parsed.path == "/ui/api/send":
            if not self._ui_authorized():
                self._send_json(403, {"error": "ui requires authentication"})
                return
            query = parse_qs(parsed.query)
            sid = query.get("sid", [""])[0]
            role = query.get("role", ["erik"])[0]
            if role not in ("claude", "gpt", "erik"):
                self._send_json(400, {"error": "invalid role"})
                return
            assert self.store is not None
            token = self.store.resolve_sid(sid)
            if not token:
                self._send_json(404, {"error": "unknown session"})
                return
            content_length = self._content_length()
            if content_length is None:
                self._send_json(413, {"error": "body too large or malformed"})
                return
            try:
                body = self.rfile.read(content_length).decode("utf-8")
            except UnicodeDecodeError:
                self._send_json(400, {"error": "invalid utf-8"})
                return
            message = self.store.add_message(token, role, body)
            if message is None:
                self._send_json(404, {"error": "unknown thread"})
                return
            self._notify(role, body)
            self._send_json(201, message)
            return

        self._send_json(404, {"error": "not found"})

    def do_OPTIONS(self):
        """No cross-origin clients exist; answer preflights without CORS grants."""
        self.send_response(204)
        self.send_header("Allow", "GET, POST, OPTIONS")
        self.end_headers()

def main():
    parser = argparse.ArgumentParser(
        description=(
            "Claude-GPT Exchange Server: durable threads and one-time delivery URLs."
        )
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
    parser.add_argument(
        "--ui-email",
        type=str,
        help=(
            "Enable /ui for this email (must match the "
            "Cf-Access-Authenticated-User-Email header injected by "
            "Cloudflare Access). UI is disabled if omitted."
        ),
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
        print(f"Cleaned up {deleted} expired delivery capability(s)", file=sys.stderr)
    ExchangeHandler.store = store
    ExchangeHandler.compatibility_store = CompatibilityGrantStore(
        args.data_dir / "compatibility", store
    )
    ExchangeHandler.ntfy_config = ntfy_config
    ExchangeHandler.ui_email = args.ui_email
    if args.ui_email:
        print(f"UI enabled for {args.ui_email} at /ui", file=sys.stderr)

    # Server
    server = ThreadingHTTPServer(("127.0.0.1", args.port), ExchangeHandler)
    server.daemon_threads = True
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
