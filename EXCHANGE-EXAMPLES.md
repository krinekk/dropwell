# One-time delivery example

Use a temporary directory for a local smoke test:

```bash
uv run python claude-gpt-exchange.py --port 9741 --data-dir /tmp/gpt-exchange
```

In another terminal create a thread. The command prints one URL, which is a
single-use GPT read capability:

```bash
uv run python exchange-cli.py create \
  --data-dir /tmp/gpt-exchange \
  --base-url http://127.0.0.1:9741
```

Opening that URL once returns the thread snapshot. A second GET returns `410`.
To inspect the durable thread, use the authenticated `/ui` endpoint. To deliver
an updated snapshot to GPT after its drop has been mirrored, create a new URL:

```bash
uv run python exchange-cli.py list --data-dir /tmp/gpt-exchange
uv run python exchange-cli.py deliver --sid <thread-sid> \
  --data-dir /tmp/gpt-exchange \
  --base-url http://127.0.0.1:9741
```

Do not put a delivery URL in logs, commits, tickets, or KOS drops.
