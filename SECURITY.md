# Security Policy

`drop` is a small personal capture API. Treat every deployment as private
infrastructure unless you have explicitly designed and reviewed it for public
exposure.

## Supported Versions

This repository currently tracks a single active development line on `main`.

## Reporting A Vulnerability

Please open a private disclosure channel if available, or contact the
maintainer directly. Do not publish working exploits, real tokens, database
URLs, private hostnames, or captured payloads in public issues.

## Deployment Checklist

Before exposing an instance beyond localhost:

- Use a long random `DROP_TOKEN`.
- Store `DROP_TOKEN` and `DROP_DATABASE_URL` only in the deployment environment.
- Confirm `.env`, database files, logs, and provider metadata are not tracked.
- Use HTTPS at the public boundary.
- Restrict database access to the minimum required network surface.
- Decide whether request bodies may contain sensitive data.
- Define retention and backup behavior for captured payloads.
- Rotate the bearer token if it is ever logged, committed, shared, or suspected
  to be exposed.

## Known Limits

- `drop` uses one bearer token for all non-health endpoints.
- There are no per-topic permissions.
- There is no built-in rate limiting.
- There is no built-in payload encryption layer.
- Payloads are stored as raw text.

Those trade-offs are intentional for a small personal ingestion primitive. Add
more controls before using it in a broader or higher-risk environment.
