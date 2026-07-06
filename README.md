# dropwell

[English](README.md) | [Español](README.es.md)

[![CI](https://github.com/krinekk/dropwell/actions/workflows/test.yml/badge.svg)](https://github.com/krinekk/dropwell/actions/workflows/test.yml)
[![Python](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-005571?logo=fastapi)](https://fastapi.tiangolo.com/)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-4169E1?logo=postgresql&logoColor=white)](https://www.postgresql.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

`dropwell` is an authenticated capture API for durable notes, automation
events, and low-friction inbox workflows.

It accepts raw UTF-8 payloads at topic-based endpoints, stores them durably in
PostgreSQL, and exposes a focused review surface for listing, updating,
archiving, and deleting captured drops. It is boring on purpose: no feed
ranking, no AI claims, no background magic — a primitive you rely on and build
the interesting parts on top of.

## Why It Exists

Most personal automation systems need one boring primitive:

1. receive something quickly
2. store it durably
3. review or archive it later
4. avoid coupling producers to the rest of the system

`dropwell` is that primitive. Producers only need HTTP and a bearer token. The
classification, enrichment, memory, and agent layers can live elsewhere.

## Features

- Authenticated write endpoint: `POST /drop/{topic}`
- Authenticated read endpoint: `GET /drops`
- Authenticated update endpoint: `PATCH /drops/{id}`
- Authenticated delete endpoint: `DELETE /drops/{id}`
- PostgreSQL persistence
- CI with ruff and pytest against a real PostgreSQL service
- Vercel serverless adapter
- Optional local `systemd` service example

## Non-goals

dropwell stays deliberately focused. By design it does not do:

- Multi-user accounts or OAuth
- Public unauthenticated ingestion
- UI / dashboard
- AI processing or background workers

Classification, enrichment, memory, and agent layers belong elsewhere —
dropwell is the primitive they build on.

## API

| Method | Path | Auth | Description |
|---|---|---|---|
| `GET` | `/health` | No | Health check and version |
| `POST` | `/drop/{topic}` | Yes | Store a raw UTF-8 payload under a topic |
| `GET` | `/drops` | Yes | List captured drops |
| `PATCH` | `/drops/{id}` | Yes | Update `status` and/or `body` |
| `DELETE` | `/drops/{id}` | Yes | Delete a drop |

Topics must match:

```text
[a-z0-9][a-z0-9-]{0,63}
```

Statuses:

- `inbound`
- `archived`

Default max body size:

- `10 MiB`, configurable with `DROPWELL_MAX_BODY_BYTES`

## Quickstart

Requirements:

- Python 3.12+
- `uv`
- PostgreSQL

```bash
git clone https://github.com/krinekk/dropwell
cd dropwell
cp .env.example .env
docker compose up -d postgres
uv sync --extra dev
uv run uvicorn dropwell.app:app --host 127.0.0.1 --port 9731
```

`docker compose up -d postgres` starts a local PostgreSQL 16 container and
creates both the `drop` and `drop_test` databases used by the app and the
test suite. No local PostgreSQL install is required.

If port `5432` is already taken by another PostgreSQL instance on your host,
remap it: change the `5432:5432` port mapping in `docker-compose.yml` (e.g. to
`15432:5432`) and update the port in the `*_DATABASE_URL` values in `.env`
to match.

Edit `.env` before starting the service:

```env
DROPWELL_TOKEN=replace-with-a-long-random-token
DROPWELL_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/drop
DROPWELL_PORT=9731
DROPWELL_MAX_BODY_BYTES=10485760
DROPWELL_CORS_ORIGINS=http://localhost:3000
```

### All-in-Docker

To run both the app and the database in containers, with no local Python
setup required:

```bash
git clone https://github.com/krinekk/dropwell
cd dropwell
cp .env.example .env
docker compose up --build
```

This builds the `app` image (multi-stage, `uv`-managed, reproducible via
`uv.lock`) and starts it alongside `postgres` on a dedicated Docker network.
The app connects to `postgres` using its service name, overriding the
`DROPWELL_DATABASE_URL` host from `.env`. The app port is published to the
host as `127.0.0.1:9731` only, not `0.0.0.0`. Stop and remove everything,
including the database volume, with:

```bash
docker compose down -v
```

## Usage

Use `http://127.0.0.1:9731` locally or replace it with your own deployment URL.

```bash
export DROPWELL_URL="http://127.0.0.1:9731"
export DROPWELL_TOKEN="replace-with-a-long-random-token"  # match the value in .env
```

Health check:

```bash
curl "$DROPWELL_URL/health"
```

Capture a note:

```bash
curl -X POST "$DROPWELL_URL/drop/note" \
  -H "Authorization: Bearer $DROPWELL_TOKEN" \
  --data "remember to review the API boundary"
```

Capture JSON as raw body:

```bash
curl -X POST "$DROPWELL_URL/drop/github-event" \
  -H "Authorization: Bearer $DROPWELL_TOKEN" \
  -H "Content-Type: application/json" \
  --data '{"action":"push","repo":"dropwell"}'
```

List drops:

```bash
curl "$DROPWELL_URL/drops?limit=10" \
  -H "Authorization: Bearer $DROPWELL_TOKEN"
```

Example list response:

```json
[
  {
    "id": "65cc274b-a368-455b-a6c1-cf3a3f9d5b81",
    "topic": "note",
    "body": "remember to review the API boundary",
    "received_at": "2026-05-28T00:00:00+00:00",
    "updated_at": "2026-05-28T00:00:00+00:00",
    "status": "inbound"
  }
]
```

Filter by topic or status:

```bash
curl "$DROPWELL_URL/drops?topic=note&status=inbound" \
  -H "Authorization: Bearer $DROPWELL_TOKEN"
```

Archive a drop:

```bash
curl -X PATCH "$DROPWELL_URL/drops/<id>" \
  -H "Authorization: Bearer $DROPWELL_TOKEN" \
  -H "Content-Type: application/json" \
  --data '{"status":"archived"}'
```

Update body text:

```bash
curl -X PATCH "$DROPWELL_URL/drops/<id>" \
  -H "Authorization: Bearer $DROPWELL_TOKEN" \
  -H "Content-Type: application/json" \
  --data '{"body":"cleaned up note body"}'
```

Delete a drop:

```bash
curl -X DELETE "$DROPWELL_URL/drops/<id>" \
  -H "Authorization: Bearer $DROPWELL_TOKEN"
```

Successful capture response:

```json
{
  "id": "65cc274b-a368-455b-a6c1-cf3a3f9d5b81",
  "topic": "note",
  "received_at": "2026-05-28T00:00:00+00:00",
  "updated_at": "2026-05-28T00:00:00+00:00"
}
```

## Configuration

| Variable | Required | Default | Description |
|---|---:|---|---|
| `DROPWELL_TOKEN` | Yes | - | Bearer token for all non-health endpoints |
| `DROPWELL_DATABASE_URL` | Yes | - | PostgreSQL connection string |
| `DROPWELL_HOST` | No | `127.0.0.1` | Host used by local process helpers |
| `DROPWELL_PORT` | No | `9731` | Port used by local process helpers |
| `DROPWELL_MAX_BODY_BYTES` | No | `10485760` | Maximum accepted request body size |
| `DROPWELL_CORS_ORIGINS` | No | empty | Comma-separated browser origins allowed by CORS |

## Development

Install dependencies:

```bash
uv sync --extra dev
```

Run the API:

```bash
uv run uvicorn dropwell.app:app --host 127.0.0.1 --port 9731 --reload
```

Run linting:

```bash
uv run ruff check .
```

Run tests:

```bash
uv run pytest
```

Tests use PostgreSQL. By default they expect:

```text
postgresql://postgres:postgres@localhost:5432/drop_test
```

`docker compose up -d postgres` (see Quickstart) provisions this database
automatically. Override with:

```bash
export TEST_DROPWELL_DATABASE_URL="postgresql://user:password@host:5432/drop_test"
uv run pytest
```

## Deployment

The repo includes two deployment-oriented paths:

- `api/index.py` for Vercel-style serverless deployment through Mangum.
- `deploy/dropwell.service` for a local user `systemd` service.

Set these environment variables in the target deployment environment:

```bash
DROPWELL_TOKEN=<long-random-token>
DROPWELL_DATABASE_URL=<postgres-url>
DROPWELL_MAX_BODY_BYTES=10485760
DROPWELL_CORS_ORIGINS=https://your-ui.example.com
```

For Vercel:

```bash
vercel env add DROPWELL_TOKEN production
vercel env add DROPWELL_DATABASE_URL production
vercel --prod
```

Use your own deployment URL in examples and docs. Do not commit real tokens,
database URLs, local hostnames, or production endpoints.

## Architecture

```text
producer scripts / webhooks / tools
        |
        | HTTP + bearer token
        v
FastAPI app
        |
        v
PostgreSQL table: drop
        |
        v
review / archive / downstream automation
```

Design choices:

- Use one simple authenticated HTTP boundary.
- Store raw UTF-8 body text without trying to infer meaning.
- Keep producer integration cheap.
- Keep downstream enrichment outside this service.
- Prefer boring operational primitives over clever automation.

## Security And Privacy

- All non-health endpoints require a bearer token.
- The token must be provided through environment configuration.
- The project does not include account management or per-topic permissions.
- Payloads are stored as raw text. Do not send secrets unless your deployment,
  database, backups, and retention policy are designed for that.
- Keep `.env`, database files, logs, and deployment metadata out of Git.
- Review `SECURITY.md` before exposing an instance beyond localhost.

## Relationship To KOS

`dropwell` can be used as an ingestion primitive for KOS or other personal
automation systems, but it is intentionally independent.

KOS is an experimental long-term personal project. `dropwell` should not imply that
KOS is a commercial product, employer project, or market-ready system.

## Roadmap

Possible next steps:

- Optional pagination cursor
- Optional topic allowlist
- Basic metrics endpoint
- Minimal OpenAPI examples
- More explicit retention/export story

Non-goals unless the project direction changes:

- Turning `dropwell` into a SaaS
- Adding AI summarization inside the capture API
- Building a social or collaborative inbox
- Replacing a full task manager

## Contributing

This is primarily a personal infrastructure project, but issues and small pull
requests are welcome if they keep the project simple, secure, and boring.

See `CONTRIBUTING.md`.

## License

MIT. See `LICENSE`. Project ownership and affiliation: see [`DISCLAIMER.md`](DISCLAIMER.md).
