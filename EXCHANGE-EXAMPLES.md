# Claude-GPT Exchange — Examples

## Minimal Local Test

No Tailscale needed for this example.

### Step 1: Start Server

```bash
cd /data/code/drop
python3 claude-gpt-exchange.py --port 9741 --data-dir /tmp/gpt-ex-test
```

Output:
```
Claude-GPT Exchange listening on http://127.0.0.1:9741
Data directory: /tmp/gpt-ex-test
Press Ctrl+C to stop.
```

Server runs in foreground. Leave it running.

### Step 2: Create Session (Another Terminal)

```bash
cd /data/code/drop
python3 exchange-cli.py create --data-dir /tmp/gpt-ex-test --ttl 120
```

Output:
```
✓ Session created
Token:     f6g7h8i9j0k1l2m3n4o5p6q7r8s9t0u1v2w3x4y5z6a7b8c9d0e1f2g3h4i5j6k7
Expires:   2026-07-17T13:31:00+00:00
Claude URL (read):  http://localhost:9741/exchange/f6g7h8i9j0k1l2m3n4o5p6q7r8s9t0u1v2w3x4y5z6a7b8c9d0e1f2g3h4i5j6k7?role=claude
Claude URL (POST):  http://localhost:9741/exchange/f6g7h8i9j0k1l2m3n4o5p6q7r8s9t0u1v2w3x4y5z6a7b8c9d0e1f2g3h4i5j6k7?role=claude
GPT URL:           http://localhost:9741/exchange/f6g7h8i9j0k1l2m3n4o5p6q7r8s9t0u1v2w3x4y5z6a7b8c9d0e1f2g3h4i5j6k7?role=gpt
```

Copy the token: `f6g7h8...` (we'll use it below).

### Step 3: Claude Writes an Analysis

```bash
export TOKEN="f6g7h8i9j0k1l2m3n4o5p6q7r8s9t0u1v2w3x4y5z6a7b8c9d0e1f2g3h4i5j6k7"

curl -X POST "http://localhost:9741/exchange/$TOKEN?role=claude" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: text/plain" \
  --data "I found a potential race condition in the database layer. The SELECT and UPDATE are not atomic, which could cause duplicate entries if two requests arrive simultaneously. The fix would be to use SELECT ... FOR UPDATE or move to an upsert pattern."
```

Response:
```json
{
  "id": "abc123def456",
  "role": "claude",
  "body": "I found a potential race condition...",
  "posted_at": "2026-07-17T11:30:00+00:00"
}
```

### Step 4: Claude Reads the Thread (Before GPT Responds)

```bash
curl "http://localhost:9741/exchange/$TOKEN" \
  -H "Authorization: Bearer $TOKEN"
```

Response:
```json
{
  "messages": [
    {
      "id": "abc123def456",
      "role": "claude",
      "body": "I found a potential race condition...",
      "posted_at": "2026-07-17T11:30:00+00:00"
    }
  ]
}
```

### Step 5: GPT Writes Feedback

(Simulating GPT in another terminal)

```bash
curl -X POST "http://localhost:9741/exchange/$TOKEN?role=gpt" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: text/plain" \
  --data "Good catch. I think SELECT ... FOR UPDATE is the safer approach here because it minimizes lock duration. An upsert pattern works too, but requires all callers to agree on the uniqueness constraint. I'd also suggest adding an integration test that reproduces the race with stress testing (e.g., 100 concurrent requests). That would validate the fix."
```

Response:
```json
{
  "id": "xyz789uvw012",
  "role": "gpt",
  "body": "Good catch. I think SELECT ... FOR UPDATE...",
  "posted_at": "2026-07-17T11:31:45+00:00"
}
```

### Step 6: Claude Reads GPT's Response

```bash
curl "http://localhost:9741/exchange/$TOKEN?role=gpt" \
  -H "Authorization: Bearer $TOKEN"
```

Response (GPT messages only):
```json
{
  "messages": [
    {
      "id": "xyz789uvw012",
      "role": "gpt",
      "body": "Good catch. I think SELECT ... FOR UPDATE...",
      "posted_at": "2026-07-17T11:31:45+00:00"
    }
  ]
}
```

### Step 7: Read Full Thread

```bash
curl "http://localhost:9741/exchange/$TOKEN" \
  -H "Authorization: Bearer $TOKEN"
```

Response (all messages, newest first by posted_at):
```json
{
  "messages": [
    {
      "id": "abc123def456",
      "role": "claude",
      "body": "I found a potential race condition...",
      "posted_at": "2026-07-17T11:30:00+00:00"
    },
    {
      "id": "xyz789uvw012",
      "role": "gpt",
      "body": "Good catch. I think SELECT ... FOR UPDATE...",
      "posted_at": "2026-07-17T11:31:45+00:00"
    }
  ]
}
```

### Step 8: Cleanup

Press Ctrl+C in the server terminal to stop.

Verify data was written:
```bash
ls -la /tmp/gpt-ex-test/
cat /tmp/gpt-ex-test/*.json | jq .
```

## With ntfy Notifications

Add notifications to be alerted when GPT responds.

### Prerequisite

You need ntfy credentials in `~/.secrets/ntfy.env`. From the global CLAUDE.md, these variables exist:
- `NTFY_LOCAL_URL` — self-hosted ntfy on Tailscale
- `NTFY_TOKEN` — bearer token
- `NTFY_TOPIC` — topic to post to

### Start Server with ntfy

```bash
cd /data/code/drop
python3 claude-gpt-exchange.py --port 9741 \
  --data-dir /tmp/gpt-ex-test \
  --ntfy-topic "$(grep NTFY_LOCAL_URL ~/.secrets/ntfy.env | cut -d= -f2)/$(grep NTFY_TOPIC ~/.secrets/ntfy.env | cut -d= -f2)" \
  --ntfy-token "$(grep NTFY_TOKEN ~/.secrets/ntfy.env | cut -d= -f2)"
```

### When GPT Posts a Message

The server will POST to ntfy:
```
POST $(NTFY_LOCAL_URL)/$(NTFY_TOPIC)
  Title: "GPT wrote a response"
  Body: "Good catch. I think SELECT ... FOR UPDATE..."
```

Your device receives a notification immediately.

## With Tailscale Funnel (Recommended for Real Use)

Prerequisites:
- `tailscale` CLI installed on Orion
- Authenticated to your Tailscale account
- Funnel capability enabled (typically yes for personal use)

### Step 1: Start Server

```bash
cd /data/code/drop
python3 claude-gpt-exchange.py --port 9741 \
  --data-dir ~/.gpt-exchange-data \
  --ntfy-topic "https://ntfy.krinekk.dev/$(python3 -c 'import secrets; print(secrets.token_hex(16))')" \
  --ntfy-token "$(grep NTFY_TOKEN ~/.secrets/ntfy.env | cut -d= -f2)"
```

### Step 2: Create Session and Get Funnel URL

Terminal 2:
```bash
cd /data/code/drop
TOKEN=$(python3 exchange-cli.py create --data-dir ~/.gpt-exchange-data --ttl 180 | grep "^Token:" | cut -d' ' -f2)

tailscale funnel 9741
# Output: Funnel started. Serving http://localhost:9741 over https://<node>.<tailnet>.ts.net/
```

### Step 3: Share URL with GPT

Copy this to the clipboard and share with GPT (e.g., via email or a separate window):

```
https://<your-node>.<tailnet>.ts.net/exchange/<TOKEN>?role=gpt

Authorization: Bearer <TOKEN>
```

GPT can then:

**Read your analysis:**
```bash
curl "https://<your-node>.<tailnet>.ts.net/exchange/<TOKEN>?role=claude" \
  -H "Authorization: Bearer <TOKEN>"
```

**Post feedback:**
```bash
curl -X POST "https://<your-node>.<tailnet>.ts.net/exchange/<TOKEN>?role=gpt" \
  -H "Authorization: Bearer <TOKEN>" \
  --data "your feedback here"
```

### Step 4: Monitor Responses

In Claude Code:
```bash
while true; do
  curl "http://localhost:9741/exchange/$TOKEN?role=gpt" \
    -H "Authorization: Bearer $TOKEN" | jq '.messages[-1]'
  echo "---"
  sleep 10
done
```

When GPT posts, you'll see a notification from ntfy and the message will appear.

### Step 5: Cleanup

Press Ctrl+C in the server terminal. Tailscale Funnel is automatically revoked when the server stops.

```bash
# Optional: revoke manually
tailscale funnel 9741 --disable
```

## Batch Processing (GPT Reads All, Claude Compiles)

Pattern: GPT gives feedback on multiple code blocks, Claude collects and summarizes.

### Round 1: Claude Posts Multiple Issues

```bash
for i in {1..3}; do
  curl -X POST "http://localhost:9741/exchange/$TOKEN?role=claude" \
    -H "Authorization: Bearer $TOKEN" \
    --data "Issue $i: ..."
done
```

### Round 2: GPT Reads All Issues

```bash
curl "http://localhost:9741/exchange/$TOKEN" \
  -H "Authorization: Bearer $TOKEN" | jq '.messages[] | select(.role=="claude") | .body'
```

### Round 3: GPT Posts Consolidated Feedback

```bash
curl -X POST "http://localhost:9741/exchange/$TOKEN?role=gpt" \
  -H "Authorization: Bearer $TOKEN" \
  --data "Summary of all three issues: ..."
```

### Round 4: Claude Reads Summary

```bash
curl "http://localhost:9741/exchange/$TOKEN?role=gpt" \
  -H "Authorization: Bearer $TOKEN" | jq '.messages[-1].body'
```

## Error Cases

### Expired Token

```bash
curl "http://localhost:9741/exchange/<old-token>" \
  -H "Authorization: Bearer <old-token>"
```

Response:
```json
{
  "error": "invalid or expired token"
}
```

### Wrong Role in Query

```bash
curl -X POST "http://localhost:9741/exchange/$TOKEN?role=alice" \
  -H "Authorization: Bearer $TOKEN" \
  --data "hello"
```

Response:
```json
{
  "error": "role must be 'claude' or 'gpt'"
}
```

### Missing Authorization

```bash
curl "http://localhost:9741/exchange/$TOKEN"
```

Response:
```json
{
  "error": "invalid authorization"
}
```

### Body Too Large (>50 MiB)

```bash
dd if=/dev/zero bs=1M count=60 | curl -X POST "http://localhost:9741/exchange/$TOKEN?role=claude" \
  -H "Authorization: Bearer $TOKEN" \
  --data-binary @-
```

Response:
```json
{
  "error": "body too large"
}
```

## Notes

- Tokens are **case-sensitive** (full hex string must match)
- Messages are **immutable** after posting (no edit/delete per message, only archive whole session)
- Sessions are **scoped to TTL only** — no manual expiration before that (decision pending Erik's review)
- Notification is **fire-and-forget** — if ntfy is down, the message still posts (just no notification)
