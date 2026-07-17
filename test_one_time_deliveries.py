#!/usr/bin/env python3
"""Contract tests for one-time GPT delivery URLs."""

import http.client
import importlib.util
import json
import multiprocessing
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


def load_module():
    module_path = Path(__file__).parent / "claude-gpt-exchange.py"
    spec = importlib.util.spec_from_file_location("claude_gpt_exchange", module_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


exchange_module = load_module()
ExchangeStore = exchange_module.ExchangeStore


def load_bridge():
    module_path = Path(__file__).parent / "exchange_drop_bridge.py"
    spec = importlib.util.spec_from_file_location("exchange_drop_bridge", module_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bridge_module = load_bridge()


def redeem_in_process(data_dir, token, queue):
    """Redeem a delivery from an independent Python process."""
    store = ExchangeStore(Path(data_dir))
    _thread, error = store.redeem_delivery(token, "gpt")
    queue.put(error)


class OneTimeDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.store = ExchangeStore(Path(self.tmpdir.name))
        self.thread_id = self.store.create_thread()
        self.store.add_message(self.thread_id, "claude", "M4 contract ready")

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_delivery_redeems_once_and_returns_thread_snapshot(self):
        token, _ = self.store.issue_delivery(self.thread_id, ttl_minutes=15)

        snapshot, error = self.store.redeem_delivery(token, "gpt")

        self.assertIsNone(error)
        self.assertEqual(snapshot["thread_id"], self.thread_id)
        self.assertEqual(snapshot["messages"][0]["body"], "M4 contract ready")

        snapshot, error = self.store.redeem_delivery(token, "gpt")
        self.assertIsNone(snapshot)
        self.assertEqual(error, "consumed")

    def test_expired_delivery_never_reveals_thread(self):
        token, _ = self.store.issue_delivery(self.thread_id, ttl_minutes=-1)

        snapshot, error = self.store.redeem_delivery(token, "gpt")

        self.assertIsNone(snapshot)
        self.assertEqual(error, "expired")

    def test_wrong_role_does_not_consume_delivery(self):
        token, _ = self.store.issue_delivery(self.thread_id, ttl_minutes=15)

        snapshot, error = self.store.redeem_delivery(token, "claude")
        self.assertIsNone(snapshot)
        self.assertEqual(error, "role")

        snapshot, error = self.store.redeem_delivery(token, "gpt")
        self.assertIsNone(error)
        self.assertEqual(snapshot["thread_id"], self.thread_id)

    def test_concurrent_redemption_has_exactly_one_winner(self):
        token, _ = self.store.issue_delivery(self.thread_id, ttl_minutes=15)
        barrier = threading.Barrier(2)

        def redeem():
            barrier.wait()
            return self.store.redeem_delivery(token, "gpt")

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: redeem(), range(2)))

        successes = [snapshot for snapshot, error in results if error is None]
        errors = [error for snapshot, error in results if snapshot is None]
        self.assertEqual(len(successes), 1)
        self.assertEqual(errors, ["consumed"])

    def test_redemption_has_exactly_one_winner_across_processes(self):
        token, _ = self.store.issue_delivery(self.thread_id, ttl_minutes=15)
        context = multiprocessing.get_context("spawn")
        queue = context.Queue()
        processes = [
            context.Process(
                target=redeem_in_process, args=(self.tmpdir.name, token, queue)
            )
            for _ in range(2)
        ]
        for process in processes:
            process.start()
        for process in processes:
            process.join(timeout=10)
            self.assertEqual(process.exitcode, 0)
        results = [queue.get(timeout=2) for _ in processes]
        self.assertCountEqual(results, [None, "consumed"])

    def test_thread_and_consumption_survive_store_reopen(self):
        token, _ = self.store.issue_delivery(self.thread_id, ttl_minutes=15)
        reopened = ExchangeStore(Path(self.tmpdir.name))

        thread, error = reopened.redeem_delivery(token, "gpt")
        self.assertIsNone(error)
        self.assertEqual(thread["messages"][0]["body"], "M4 contract ready")

        restarted = ExchangeStore(Path(self.tmpdir.name))
        thread, error = restarted.redeem_delivery(token, "gpt")
        self.assertIsNone(thread)
        self.assertEqual(error, "consumed")

    def test_gpt_drop_is_mirrored_once_and_issues_fresh_delivery(self):
        sid = self.thread_id[:12]
        drop = {
            "id": "canary-drop-1",
            "topic": "gpt-exchange",
            "status": "inbound",
            "body": json.dumps(
                {
                    "protocol": "kos-gpt-exchange/v1",
                    "thread_sid": sid,
                    "body": "GPT canary reply",
                }
            ),
        }

        result = bridge_module.process_inbound_drop(
            self.store, drop, "https://drop.krinekk.dev"
        )

        self.assertFalse(result["duplicate"])
        self.assertIn("/exchange/", result["delivery_url"])
        mirrored = self.store.get_thread(self.thread_id)
        self.assertEqual(mirrored["messages"][-1]["body"], "GPT canary reply")
        retry = bridge_module.process_inbound_drop(
            self.store, drop, "https://drop.krinekk.dev"
        )
        self.assertTrue(retry["duplicate"])
        self.assertEqual(retry["thread_sid"], sid)
        self.assertEqual(retry["delivery_url"], result["delivery_url"])

    def test_gpt_drop_retry_recovers_missing_delivery_after_interruption(self):
        drop_id = "interrupted-drop"
        thread = self.store.get_thread(self.thread_id)
        thread["processed_drop_ids"] = [drop_id]
        self.store._write_json(self.store._thread_file(self.thread_id), thread)
        drop = {
            "id": drop_id,
            "topic": "gpt-exchange",
            "status": "inbound",
            "body": json.dumps(
                {
                    "protocol": "kos-gpt-exchange/v1",
                    "thread_sid": self.thread_id[:12],
                    "body": "already persisted before a crash",
                }
            ),
        }

        result = bridge_module.process_inbound_drop(
            self.store, drop, "https://drop.krinekk.dev"
        )

        self.assertTrue(result["duplicate"])
        self.assertIn("/exchange/", result["delivery_url"])
        thread = self.store.get_thread(self.thread_id)
        self.assertEqual(
            [message["body"] for message in thread["messages"]].count(
                "already persisted before a crash"
            ),
            0,
        )

    def test_bridge_rejects_unaddressable_drop(self):
        drop = {
            "topic": "gpt-exchange",
            "status": "inbound",
            "body": json.dumps(
                {
                    "protocol": "kos-gpt-exchange/v1",
                    "thread_sid": self.thread_id[:12],
                    "body": "missing id",
                }
            ),
        }
        with self.assertRaisesRegex(ValueError, "drop id is required"):
            bridge_module.process_inbound_drop(
                self.store, drop, "https://drop.krinekk.dev"
            )

    def test_ui_listing_contains_threads_but_never_delivery_tokens(self):
        token, _ = self.store.issue_delivery(self.thread_id, ttl_minutes=15)

        listing = self.store.list_threads()
        thread = self.store.get_thread(self.thread_id)

        self.assertEqual(listing[0]["sid"], self.thread_id[:12])
        self.assertNotIn(token, str(listing))
        self.assertNotIn(token, str(thread))

    def test_legacy_migration_preserves_messages_without_reusing_token(self):
        token = "a" * 64
        legacy_path = Path(self.tmpdir.name) / f"{token}.json"
        legacy = {
            "token": token,
            "created_at": "2026-07-17T12:00:00+00:00",
            "expires_at": "2026-07-17T14:00:00+00:00",
            "messages": [{"id": "old", "role": "claude", "body": "keep me"}],
        }
        with open(legacy_path, "w") as file:
            json.dump(legacy, file)

        result = self.store.migrate_legacy_sessions()

        self.assertEqual(result, {"migrated": 1, "already_migrated": 0, "skipped": 0})
        self.assertFalse(legacy_path.exists())
        threads = self.store.list_threads()
        migrated = [
            self.store.get_thread(self.store.resolve_sid(item["sid"]))
            for item in threads
        ]
        thread = next(
            item for item in migrated if item["messages"][0]["body"] == "keep me"
        )
        self.assertEqual(thread["created_at"], legacy["created_at"])
        self.assertEqual(thread["messages"][0]["body"], "keep me")
        self.assertNotEqual(thread["thread_id"], token)
        self.assertNotIn(token, str(thread))
        self.assertEqual(
            len(list((Path(self.tmpdir.name) / "legacy-backups").glob("*.json"))), 1
        )

        retry = self.store.migrate_legacy_sessions()
        self.assertEqual(retry, {"migrated": 0, "already_migrated": 0, "skipped": 0})


class OneTimeDeliveryHttpTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.store = ExchangeStore(Path(self.tmpdir.name))
        thread_id = self.store.create_thread()
        self.store.add_message(thread_id, "claude", "Read-only handoff")
        self.token, _ = self.store.issue_delivery(thread_id, ttl_minutes=15)
        exchange_module.ExchangeHandler.store = self.store
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

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            response_body = response.read().decode()
            return response.status, response_body
        finally:
            connection.close()

    def response_headers(self, method, path):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port)
        try:
            connection.request(method, path)
            response = connection.getresponse()
            response.read()
            return response.status, dict(response.getheaders())
        finally:
            connection.close()

    def test_get_consumes_delivery_and_post_cannot_write(self):
        path = f"/exchange/{self.token}"

        status, body = self.request("GET", path)
        self.assertEqual(status, 200)
        self.assertIn("Read-only handoff", body)

        status, _ = self.request("GET", path)
        self.assertEqual(status, 410)

        status, _ = self.request("POST", path)
        self.assertEqual(status, 404)

    def test_ui_exposes_thread_but_not_delivery_capability(self):
        headers = {"Cf-Access-Authenticated-User-Email": "operator@example.test"}

        status, body = self.request("GET", "/ui/api/sessions", headers=headers)
        self.assertEqual(status, 200)
        self.assertIn("message_count", body)
        self.assertNotIn(self.token, body)

    def test_ui_rejects_malformed_length(self):
        headers = {
            "Cf-Access-Authenticated-User-Email": "operator@example.test",
            "Content-Length": "not-a-number",
        }
        path = "/ui/api/send?sid=" + self.store.list_threads()[0]["sid"]
        status, _ = self.request("POST", path, body="x", headers=headers)
        self.assertEqual(status, 413)

    def test_options_has_no_cors_grant(self):
        status, headers = self.response_headers("OPTIONS", "/exchange/anything")
        self.assertEqual(status, 204)
        self.assertEqual(headers["Allow"], "GET, POST, OPTIONS")
        self.assertNotIn("Access-Control-Allow-Origin", headers)


if __name__ == "__main__":
    unittest.main()
