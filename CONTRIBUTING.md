# Contributing

`drop` is primarily a personal infrastructure project, but small focused
improvements are welcome.

## Principles

- Keep the service small and understandable.
- Prefer boring backend primitives over clever automation.
- Do not add AI processing inside the capture API.
- Do not add multi-user account logic without an explicit design discussion.
- Keep examples free of real endpoints, tokens, hostnames, and private data.

## Development

```bash
uv sync --extra dev
uv run ruff check .
uv run pytest
```

Tests require PostgreSQL. Set `TEST_DROP_DATABASE_URL` when the default local
test database is not available.

## Pull Requests

Good pull requests usually include:

- a small behavior or documentation change
- updated tests when behavior changes
- updated README/API notes when public behavior changes
- no secrets or personal deployment details

Before opening a pull request:

```bash
uv run ruff check .
uv run pytest
```

