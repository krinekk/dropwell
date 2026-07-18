# Drop Exchange Compatibility Mode Contract

Status: implementation contract for controlled integration only.

Visible label: **COMPATIBILITY MODE — REPLAYABLE UNTIL EXPIRY**.

## Boundary and data model

Compatibility mode is an explicit, opt-in read channel for cache-backed GPT
readers. It is not a fallback from one-time delivery. A compatibility grant is
a distinct typed record with its own schema and storage directory. It contains:

- a cryptographically random capability represented on disk only by its hash;
- one immutable snapshot scoped to exactly one durable exchange thread;
- an explicit allowlist of reader roles (v1 permits only `gpt`);
- issue, expiry, revocation, and read-count fields;
- the visible compatibility label and schema version.

Compatibility records and metrics live below `<data-dir>/compatibility/` and
must never use `delivery_*.json` or alter one-time delivery records.

## Endpoint and lifecycle

- `GET /compatibility/<capability>` returns the immutable snapshot and mode
  metadata. Repeated reads are allowed only before expiry and revocation.
- All compatibility responses identify the mode. Successful responses permit
  short public caching; error responses are `no-store`.
- There is no compatibility `POST`, upload, or CORS grant. GPT writes continue
  exclusively through `drop(topic="gpt-exchange")`.
- Operators issue and revoke grants explicitly through separate CLI commands.
  Revocation prevents new origin reads; an already cached response can remain
  readable only for its bounded cache age.

TTL is 5 minutes by default, with an enforced range of 1–15 minutes. Shared
cache freshness is capped at 60 seconds so revocation convergence is bounded.
Every successful response repeats that cache-revocation caveat in
`cache_notice`, so callers do not have to infer it from this contract.

Successful responses include unambiguous transport metadata:

```json
{
  "mode": "compatibility",
  "warning": "COMPATIBILITY MODE — REPLAYABLE UNTIL EXPIRY",
  "revocable": true,
  "cache_notice": "REVOCATION MAY TAKE UP TO 60 SECONDS IN SHARED CACHES",
  "expires_at": "...",
  "thread_id": "...",
  "snapshot_hash": "..."
}
```

`snapshot_hash` is SHA-256 over canonical JSON for the immutable snapshot and
is revalidated by the origin on every read.

## Content limits

The grant captures text messages only: at most 64 messages, 32 KiB per message,
and 128 KiB total UTF-8 body content. Any attachment/file field, non-text body,
or recognized credential/private-key pattern rejects issuance. Validation runs
both when the grant is issued and before every origin response. These guards
reduce accidental disclosure but do not replace operator review or a secret
scanner.

## Logs and metrics

Compatibility origin reads emit structured stderr events containing the mode,
event, outcome, thread scope, and a non-capability grant id. Raw capabilities
and message bodies are never logged.

Separate persisted counters cover grants issued, successful first reads,
replays, revoked/expired/role-denied reads, explicit revocations, and content
rejections. Authenticated operators can read them at
`GET /ui/api/compatibility/metrics`; one-time metrics and behavior are not
changed.

## Threats and rollback

Primary threats are capability leakage, cache persistence after revocation,
over-broad thread scope, accidental secret inclusion, and confusion with the
one-time channel. Mitigations are short TTL, 60-second cache freshness,
single-thread immutable snapshots, explicit `gpt` role allowlisting, defensive
content validation, separate paths/storage/telemetry, and the mandatory label.

Rollback is to stop issuing compatibility grants and remove the compatibility
route, CLI commands, and `<data-dir>/compatibility/` state. One-time delivery
files, endpoints, `Cache-Control: no-store`, and replay semantics remain valid
throughout and need no migration or rollback.
