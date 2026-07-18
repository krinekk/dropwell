# Claude-GPT Exchange

`claude-gpt-exchange.py` holds durable review threads and creates a fresh,
single-use delivery URL whenever GPT must read one.

## Security model

- A **thread** is durable local state. The authenticated UI lists and reads it
  by short id; it never returns delivery credentials.
- A **delivery token** is a 256-bit capability bound to one thread and role
  `gpt`. It is usable by exactly one `GET` and has a short TTL (15 minutes by
  default). The role is inferred by the server, so the delivery URL is a clean
  `/exchange/<token>` path.
- Redeeming a delivery atomically marks it consumed before returning the thread
  snapshot. Replays and expired deliveries receive `410 Gone`.
- Delivery URLs are read-only. `POST /exchange/...` always returns `404`.
- GPT replies via `drop(topic="gpt-exchange")`; the Orion monitor is responsible
  for appending that reply to the thread and issuing the next delivery URL.

This reduces replay exposure; it does not make a leaked URL harmless before it
is redeemed or expires. Treat each delivery URL as a short-lived secret.

## Explicit compatibility mode

Cache-backed readers that cannot reach a fresh one-time URL may use a separate,
explicitly issued compatibility grant. It is always labeled:

> **COMPATIBILITY MODE — REPLAYABLE UNTIL EXPIRY**

This is never a fallback from `/exchange/<token>`. Compatibility uses
`GET /compatibility/<token>`, distinct typed records and
`<data-dir>/compatibility/` persistence. It returns an immutable, text-only
snapshot of exactly one thread, permits only the `gpt` reader role, defaults to
a 5-minute TTL (enforced range 1–15), and can be replayed until expiry or
explicit revocation. Successful compatibility responses are publicly cacheable
for at most 60 seconds at a time; one-time responses remain `no-store`.
The response identifies `mode=compatibility`, carries the mandatory warning,
expiry, revocability, thread id, and a verified immutable `snapshot_hash`, and
states that cached revocation may converge up to 60 seconds after origin
revocation.

Issue a grant explicitly for an existing thread:

```bash
uv run python exchange-cli.py compatibility-issue \
  --sid <thread-sid> \
  --ttl 5 \
  --data-dir ~/.local/state/drop-exchange/data \
  --base-url https://drop.krinekk.dev
```

Revoke it with the capability token from that URL:

```bash
uv run python exchange-cli.py compatibility-revoke \
  --token <compatibility-token> \
  --data-dir ~/.local/state/drop-exchange/data
```

There is no compatibility `POST`, upload route, CORS grant, or write bridge.
GPT replies still use `drop(topic="gpt-exchange")`. Secret-like content,
attachments, non-text bodies, and oversized snapshots are rejected at grant
issuance and revalidated on origin reads. See
[`EXCHANGE-COMPATIBILITY.md`](EXCHANGE-COMPATIBILITY.md) for the full boundary,
threat model, metrics, and rollback contract.

## Operator flow

Start the server and UI behind the existing authenticated reverse proxy:

```bash
uv run python claude-gpt-exchange.py \
  --port 9741 \
  --data-dir ~/.local/state/drop-exchange/data \
  --ui-email "<operator-email>"
```

Create a durable thread and its first delivery. The URL is the only stdout
value, so it can be copied directly to the GPT conversation:

```bash
uv run python exchange-cli.py create \
  --data-dir ~/.local/state/drop-exchange/data \
  --base-url https://drop.krinekk.dev
```

After processing a GPT drop, issue a new delivery for the UI short id:

```bash
uv run python exchange-cli.py deliver --sid <thread-sid> \
  --data-dir ~/.local/state/drop-exchange/data \
  --base-url https://drop.krinekk.dev
```

The UI is available at `/ui` and lists durable threads without delivery tokens.

For constrained GPT browsers, paste the delivery URL as a literal, standalone
user message and ask it to perform one GET. Do not make GPT construct or alter
the URL: some browser sandboxes refuse newly constructed capability URLs before
they reach the server.

## KOS drop envelope

GPT replies must be a JSON body on topic `gpt-exchange`; the delivery URL is
never included in the drop:

```json
{
  "protocol": "kos-gpt-exchange/v1",
  "thread_sid": "<12-char UI id>",
  "body": "GPT's review response"
}
```

`exchange_drop_bridge.py` validates this envelope, deduplicates by KOS drop id,
adds the GPT message to the durable thread, and returns the next delivery URL to
the local caller. A credential-owning wrapper must archive the source drop only
after that call succeeds. The bridge itself has no KOS credentials.

The bridge records the source drop id before issuing its delivery capability. If
the process stops in that narrow interval, retrying the same drop does not append
the GPT message again: it recovers a missing (or already consumed) delivery URL.

## Legacy migration

Before replacing the running server, audit the old token-named JSON state:

```bash
uv run python exchange-cli.py migrate --dry-run \
  --data-dir ~/.local/state/drop-exchange/data
```

The real migration creates a fresh thread id, preserves all messages and
timestamps, and moves the old credential-bearing JSON into
`legacy-backups/` (mode `0700`; files `0600`). It is explicit and idempotent;
it is never performed automatically at server startup.

## Integration and rollback gate

Do not replace the live process until all of these are recorded:

1. `migrate --dry-run` reports the expected legacy count.
2. A filesystem backup of the current data directory exists outside the live
   directory, with owner-only permissions.
3. The combined code passes the verification commands below.
4. The new process starts against a copy of production data and `/health`,
   `/ui` under Access, and a one-time delivery smoke all pass.

Integration is a controlled stop/start, never an in-place overwrite of live
data. Preserve the old command and data directory until the new process has
passed health, authenticated UI, and one real canary. On any failure: stop the
new process, restore the original data directory, and restart the original
command. Do not run the actual migration or restart from an uncommitted tree.

## Verification

```bash
uv run python -m unittest -v test_one_time_deliveries.py
uv run python -m unittest -v test_compatibility_mode.py
uv run python test-exchange.py
uv run --extra dev ruff check claude-gpt-exchange.py exchange-cli.py \
  exchange_drop_bridge.py test_one_time_deliveries.py \
  test_compatibility_mode.py test-exchange.py
```
