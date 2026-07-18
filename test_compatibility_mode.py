#!/usr/bin/env python3
"""Contract tests for the explicit replayable compatibility channel."""

import contextlib
import hashlib
import http.client
import importlib.util
import io
import json
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


def load_module():
    module_path = Path(__file__).parent / "claude-gpt-exchange.py"
    spec = importlib.util.spec_from_file_location("claude_gpt_exchange", module_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_cli_module():
    module_path = Path(__file__).parent / "exchange-cli.py"
    spec = importlib.util.spec_from_file_location("exchange_cli", module_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


exchange_module = load_module()
cli_module = load_cli_module()
ExchangeStore = exchange_module.ExchangeStore
CompatibilityGrantStore = exchange_module.CompatibilityGrantStore
MODE_LABEL = exchange_module.COMPATIBILITY_MODE_LABEL


class CompatibilityGrantTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmpdir.name)
        self.thread_store = ExchangeStore(self.data_dir)
        self.thread_id = self.thread_store.create_thread()
        self.thread_store.add_message(self.thread_id, "claude", "Review this patch")
        self.now = datetime(2026, 7, 18, 0, 0, tzinfo=timezone.utc)
        self.store = CompatibilityGrantStore(
            self.data_dir / "compatibility",
            self.thread_store,
            now_fn=lambda: self.now,
        )

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_replay_succeeds_before_expiry(self):
        token, grant = self.store.issue(self.thread_id, ttl_minutes=5)

        first, first_error = self.store.read(token, "gpt")
        second, second_error = self.store.read(token, "gpt")

        self.assertIsNone(first_error)
        self.assertIsNone(second_error)
        self.assertEqual(first, second)
        self.assertEqual(first["mode"], "compatibility")
        self.assertEqual(first["warning"], MODE_LABEL)
        self.assertTrue(first["revocable"])
        self.assertEqual(
            first["cache_notice"],
            "REVOCATION MAY TAKE UP TO 60 SECONDS IN SHARED CACHES",
        )
        self.assertEqual(first["thread_id"], self.thread_id)
        self.assertRegex(first["snapshot_hash"], r"^[0-9a-f]{64}$")
        self.assertEqual(
            first["scope"],
            {"type": "single_thread", "thread_id": self.thread_id},
        )
        self.assertEqual(first["allowed_reader_roles"], ["gpt"])
        self.assertEqual(grant.read_count, 0)

    def test_snapshot_is_immutable_and_hash_verified(self):
        token, grant = self.store.issue(self.thread_id, ttl_minutes=5)
        self.thread_store.add_message(self.thread_id, "claude", "Later message")

        snapshot, error = self.store.read(token, "gpt")

        self.assertIsNone(error)
        self.assertNotIn("Later message", str(snapshot))
        canonical_snapshot = json.dumps(
            grant.snapshot,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
        self.assertEqual(
            snapshot["snapshot_hash"], hashlib.sha256(canonical_snapshot).hexdigest()
        )

        grant_path = self.store._grant_file(token)
        persisted = json.loads(grant_path.read_text())
        persisted["snapshot"]["messages"][0]["body"] = "tampered"
        self.store._write_json(grant_path, persisted)
        snapshot, error = self.store.read(token, "gpt")
        self.assertIsNone(snapshot)
        self.assertEqual(error, "unknown")

    def test_expired_grant_rejects_read(self):
        token, _ = self.store.issue(self.thread_id, ttl_minutes=1)
        self.now += timedelta(minutes=1, microseconds=1)

        snapshot, error = self.store.read(token, "gpt")

        self.assertIsNone(snapshot)
        self.assertEqual(error, "expired")

    def test_revoked_grant_rejects_read(self):
        token, _ = self.store.issue(self.thread_id, ttl_minutes=5)

        self.assertTrue(self.store.revoke(token))
        snapshot, error = self.store.read(token, "gpt")

        self.assertIsNone(snapshot)
        self.assertEqual(error, "revoked")

    def test_role_and_scope_are_explicit_and_narrow(self):
        other_thread = self.thread_store.create_thread()
        self.thread_store.add_message(other_thread, "claude", "Other thread secret")
        token, _ = self.store.issue(self.thread_id, allowed_roles=("gpt",))

        snapshot, error = self.store.read(token, "claude")
        self.assertIsNone(snapshot)
        self.assertEqual(error, "role")

        snapshot, error = self.store.read(token, "gpt")
        self.assertIsNone(error)
        self.assertEqual(snapshot["scope"]["thread_id"], self.thread_id)
        self.assertNotIn("Other thread secret", str(snapshot))

        with self.assertRaisesRegex(ValueError, "only the gpt reader role"):
            self.store.issue(self.thread_id, allowed_roles=("claude",))

    def test_persistence_is_separate_and_does_not_store_raw_capability(self):
        token, _ = self.store.issue(self.thread_id)

        compatibility_files = list(
            (self.data_dir / "compatibility").glob("grant_*.json")
        )
        self.assertEqual(len(compatibility_files), 1)
        self.assertNotIn(token, compatibility_files[0].name)
        self.assertNotIn(token, compatibility_files[0].read_text())
        self.assertEqual(list(self.data_dir.glob("delivery_*.json")), [])

    def test_persisted_role_allowlist_cannot_be_broadened(self):
        token, _ = self.store.issue(self.thread_id)
        grant_path = self.store._grant_file(token)
        grant = json.loads(grant_path.read_text())
        grant["allowed_reader_roles"] = ["gpt", "claude"]
        self.store._write_json(grant_path, grant)

        snapshot, error = self.store.read(token, "claude")

        self.assertIsNone(snapshot)
        self.assertEqual(error, "unknown")

    def test_capability_input_cannot_escape_compatibility_storage(self):
        malicious_token = "../../outside"

        grant_path = self.store._grant_file(malicious_token)
        snapshot, error = self.store.read(malicious_token, "gpt")

        self.assertEqual(grant_path.parent, self.data_dir / "compatibility")
        self.assertIsNone(snapshot)
        self.assertEqual(error, "unknown")

    def test_secret_and_attachment_content_is_rejected(self):
        secret_thread = self.thread_store.create_thread()
        self.thread_store.add_message(
            secret_thread,
            "claude",
            "Authorization: Bearer this-is-a-sensitive-token",
        )
        with self.assertRaisesRegex(ValueError, "sensitive content"):
            self.store.issue(secret_thread)

        thread = self.thread_store.get_thread(self.thread_id)
        thread["messages"][0]["attachments"] = [{"name": "private.txt"}]
        self.thread_store._write_json(
            self.thread_store._thread_file(self.thread_id), thread
        )
        with self.assertRaisesRegex(ValueError, "attachments are not supported"):
            self.store.issue(self.thread_id)

    def test_metrics_are_persisted_under_compatibility_namespace(self):
        token, _ = self.store.issue(self.thread_id)
        self.store.read(token, "gpt")
        self.store.read(token, "gpt")
        self.store.read(token, "claude")
        self.store.revoke(token)
        self.store.read(token, "gpt")

        metrics = self.store.metrics()

        self.assertEqual(metrics["compatibility_grants_issued_total"], 1)
        self.assertEqual(metrics["compatibility_first_reads_total"], 1)
        self.assertEqual(metrics["compatibility_replays_total"], 1)
        self.assertEqual(metrics["compatibility_role_denied_total"], 1)
        self.assertEqual(metrics["compatibility_revocations_total"], 1)
        self.assertEqual(metrics["compatibility_revoked_reads_total"], 1)
        self.assertTrue((self.data_dir / "compatibility" / "metrics.json").exists())


class CompatibilityHttpTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmpdir.name)
        self.thread_store = ExchangeStore(self.data_dir)
        self.thread_id = self.thread_store.create_thread()
        self.thread_store.add_message(self.thread_id, "claude", "Cache-safe review")
        self.one_time_token, _ = self.thread_store.issue_delivery(self.thread_id)
        self.compatibility_store = CompatibilityGrantStore(
            self.data_dir / "compatibility", self.thread_store
        )
        self.compatibility_token, _ = self.compatibility_store.issue(self.thread_id)
        exchange_module.ExchangeHandler.store = self.thread_store
        exchange_module.ExchangeHandler.compatibility_store = self.compatibility_store
        exchange_module.ExchangeHandler.ntfy_config = None
        exchange_module.ExchangeHandler.ui_email = "operator@example.test"
        self.server = exchange_module.ThreadingHTTPServer(
            ("127.0.0.1", 0), exchange_module.ExchangeHandler
        )
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=2)
        self.server.server_close()
        self.tmpdir.cleanup()

    def request(self, method, path, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port)
        try:
            connection.request(method, path, headers=headers or {})
            response = connection.getresponse()
            body = response.read().decode()
            return response.status, dict(response.getheaders()), body
        finally:
            connection.close()

    def test_compatibility_replays_without_one_time_regression(self):
        compatibility_path = f"/compatibility/{self.compatibility_token}"

        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            first_status, first_headers, first_body = self.request(
                "GET", compatibility_path
            )
            replay_status, _, replay_body = self.request("GET", compatibility_path)

        self.assertEqual(first_status, 200)
        self.assertEqual(replay_status, 200)
        first_payload = json.loads(first_body)
        self.assertEqual(first_payload["mode"], "compatibility")
        self.assertEqual(first_payload["warning"], MODE_LABEL)
        self.assertTrue(first_payload["revocable"])
        self.assertEqual(
            first_payload["cache_notice"],
            "REVOCATION MAY TAKE UP TO 60 SECONDS IN SHARED CACHES",
        )
        self.assertRegex(first_payload["snapshot_hash"], r"^[0-9a-f]{64}$")
        self.assertEqual(first_body, replay_body)
        self.assertRegex(first_headers["Cache-Control"], r"^public, max-age=\d+")
        self.assertNotIn("ETag", first_headers)
        self.assertNotIn("Vary", first_headers)
        events = [json.loads(line) for line in stderr.getvalue().splitlines()]
        self.assertEqual([event["mode"] for event in events], [MODE_LABEL, MODE_LABEL])
        self.assertEqual([event["event"] for event in events], ["first_read", "replay"])
        self.assertNotIn(self.compatibility_token, stderr.getvalue())
        self.assertNotIn(
            hashlib.sha256(self.compatibility_token.encode()).hexdigest(),
            stderr.getvalue(),
        )

        one_time_path = f"/exchange/{self.one_time_token}"
        status, headers, _ = self.request("GET", one_time_path)
        self.assertEqual(status, 200)
        self.assertEqual(headers["Cache-Control"], "no-store")
        status, headers, _ = self.request("GET", one_time_path)
        self.assertEqual(status, 410)
        self.assertEqual(headers["Cache-Control"], "no-store")

    def test_cache_identity_cannot_mix_distinct_capability_paths(self):
        other_thread = self.thread_store.create_thread()
        self.thread_store.add_message(other_thread, "claude", "Distinct snapshot")
        other_token, _ = self.compatibility_store.issue(other_thread)

        first_status, first_headers, first_body = self.request(
            "GET", f"/compatibility/{self.compatibility_token}"
        )
        other_status, other_headers, other_body = self.request(
            "GET", f"/compatibility/{other_token}"
        )

        self.assertEqual((first_status, other_status), (200, 200))
        self.assertNotEqual(
            json.loads(first_body)["thread_id"], json.loads(other_body)["thread_id"]
        )
        self.assertNotEqual(
            json.loads(first_body)["snapshot_hash"],
            json.loads(other_body)["snapshot_hash"],
        )
        for headers in (first_headers, other_headers):
            self.assertNotIn("ETag", headers)
            self.assertNotIn("Vary", headers)

    def test_explicit_role_rejection_and_no_post_or_cors_shortcut(self):
        path = f"/compatibility/{self.compatibility_token}"

        status, headers, body = self.request("GET", path + "?role=claude")
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(body)["mode"], "compatibility")
        self.assertEqual(json.loads(body)["warning"], MODE_LABEL)
        self.assertEqual(headers["Cache-Control"], "no-store")

        status, headers, body = self.request("POST", path)
        self.assertEqual(status, 405)
        self.assertEqual(json.loads(body)["mode"], "compatibility")
        self.assertEqual(json.loads(body)["warning"], MODE_LABEL)
        self.assertEqual(headers["Cache-Control"], "no-store")
        status, headers, _ = self.request("OPTIONS", path)
        self.assertEqual(status, 204)
        self.assertNotIn("Access-Control-Allow-Origin", headers)

    def test_authenticated_metrics_are_separate(self):
        self.request("GET", f"/compatibility/{self.compatibility_token}")
        headers = {"Cf-Access-Authenticated-User-Email": "operator@example.test"}

        status, _, body = self.request(
            "GET", "/ui/api/compatibility/metrics", headers=headers
        )

        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["mode"], "compatibility")
        self.assertEqual(payload["warning"], MODE_LABEL)
        self.assertEqual(payload["metrics"]["compatibility_first_reads_total"], 1)
        self.assertNotIn(self.compatibility_token, body)
        self.assertNotIn(
            hashlib.sha256(self.compatibility_token.encode()).hexdigest(), body
        )
        self.assertNotIn("Cache-safe review", body)


class CompatibilityCliTests(unittest.TestCase):
    def test_cli_issue_and_revoke_use_compatibility_namespace(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            thread_store = ExchangeStore(data_dir)
            thread_id = thread_store.create_thread()
            thread_store.add_message(thread_id, "claude", "CLI canary")

            token, grant = cli_module.issue_compatibility(
                data_dir, thread_id[:12], ttl_minutes=5
            )

            self.assertEqual(grant.mode, MODE_LABEL)
            self.assertIn(
                "/compatibility/",
                cli_module._compatibility_url("https://drop.example", token),
            )
            self.assertTrue(cli_module.revoke_compatibility(data_dir, token))
            self.assertEqual(list(data_dir.glob("delivery_*.json")), [])


if __name__ == "__main__":
    unittest.main()
