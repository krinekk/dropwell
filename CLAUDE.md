# CLAUDE.md - dropwell

This file gives agent-facing guidance for working in this repository.

## Project Summary

`dropwell` is a small authenticated FastAPI service for capturing raw UTF-8
payloads under topic-based endpoints and storing them in PostgreSQL.

It is deliberately narrow:

- receive a payload
- store it durably
- list/update/archive/delete captured rows
- leave classification, enrichment, memory, and agent behavior to downstream
  systems

Personal project. Not affiliated with my employer.

## Current Stack

- Python 3.12+
- `uv` for dependency and virtualenv management
- FastAPI + Uvicorn
- PostgreSQL via `psycopg2`
- Mangum adapter for Vercel-style serverless deployment
- pytest + httpx for tests
- ruff for linting

## Commands

```bash
uv sync --extra dev
uv run ruff check .
uv run pytest
uv run uvicorn dropwell.app:app --host 127.0.0.1 --port 9731 --reload
```

Tests expect a PostgreSQL database. Override the default test URL with
`TEST_DROPWELL_DATABASE_URL`.

## Boundaries

Do not add:

- plaintext credentials
- real production URLs
- private hostnames
- personal deployment metadata
- hardcoded CORS origins for personal deployments
- employer context
- AI processing inside the capture API
- multi-user account logic without an explicit design decision

Keep public examples deployment-neutral. Use placeholders such as
`https://drop.example.com` or local URLs.

## Documentation Rules

- Keep README examples accurate against the current API.
- Mention that all non-health endpoints require bearer auth.
- Keep the project English-first for public discoverability.
- A short Spanish section is fine, but technical API docs should remain
  English-first.
- Keep KOS references optional and clearly independent.

## Commit Conventions

- Prefer small commits.
- Use imperative English commit messages, for example:
  `docs: polish public README`.
- Update docs with behavior changes.
- Run ruff and tests before claiming the repo is ready.
