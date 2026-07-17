# Claude-GPT Exchange

Temporal, authenticated message exchange between Claude Code and ChatGPT for collaborative code review.

## What It Is

A minimal HTTP server that manages **session-based message threads** between two roles (Claude, GPT). Each session has:

- **Token**: 64-character hex string (256 bits entropy) generated on creation
- **TTL**: Configurable lifetime (default 120 minutes) — expires automatically
- **Messages**: Append-only list, each with sender role, timestamp, and body
- **Storage**: JSON files on disk, encrypted by Tailscale Funnel transport layer

## Why Not dropwell?

`dropwell` uses a **single master token** with no scoping mechanism. Using it for GPT would require:
1. Sharing the production token (insecure — GPT would have full API access)
2. Creating a new dropwell instance just for this session (adds deployment complexity)

This server solves both: token-per-session, short lifetime, no access to other data.

## Architecture

```
┌─────────────┐
│ Claude      │  http://localhost:9741/exchange/<token>?role=claude
│ (Claude Code)─────────────────────────────────────────────────────────┐
└─────────────┘                                                         │
                                                                        │
┌─────────────┐                                                        ▼
│ GPT         │  http://localhost:9741/exchange/<token>?role=gpt   Exchange Server
│ (Web)       ─────────────────────────────────────────────────────────┤  (python3)
└─────────────┘                                                        │
       │                                                               │
       └──────────── (Tailscale Funnel URL) ───────────────────────────┘
```

- **No internet exposure**: Tailscale Funnel bridges local 127.0.0.1:9741 to an authenticated URL only you can share.
- **Optional ntfy**: Notifications when GPT responds (uses `~/.secrets/ntfy.env` if available).

## API

### GET /exchange/<token>

List all messages for a session (optionally filter by role).

```bash
curl http://localhost:9741/exchange/<token>?role=claude \
  -H "Authorization: Bearer <token>"
```

Response:
```json
{
  "messages": [
    {
      "id": "a1b2c3d4e5f6g7h8",
      "role": "claude",
      "body": "Here's my initial analysis...",
      "posted_at": "2026-07-17T11:30:00+00:00"
    },
    {
      "id": "z9y8x7w6v5u4t3s2",
      "role": "gpt",
      "body": "Thanks, I see two issues...",
      "posted_at": "2026-07-17T11:31:45+00:00"
    }
  ]
}
```

### POST /exchange/<token>

Append a message to the session. Role is required in query string.

```bash
curl -X POST "http://localhost:9741/exchange/<token>?role=claude" \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: text/plain" \
  --data "Here is my analysis..."
```

Response:
```json
{
  "id": "a1b2c3d4e5f6g7h8",
  "role": "claude",
  "body": "Here is my analysis...",
  "posted_at": "2026-07-17T11:30:00+00:00"
}
```

### GET /health

Health check (no auth required).

```bash
curl http://localhost:9731/health
{"status": "ok"}
```

## Session Management

### Create a Session

```bash
python3 exchange-cli.py create --ttl 120 --data-dir ./gpt-exchange-data
```

Output:
```
✓ Session created
Token:     a1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6q7r8s9t0u1v2w3x4y5z6a7b8c9d0e1f
Expires:   2026-07-17T13:31:00+00:00
```

### Expose via Tailscale Funnel (Temporary)

```bash
# Start server in background
python3 claude-gpt-exchange.py --port 9741 &

# Create Funnel endpoint (temporary, auto-revoked when process stops)
tailscale funnel 9741 --bg

# Copy the Funnel URL and share with GPT
# URL format: https://<node>.tail<...>.ts.net/exchange/<token>?role=gpt
```

For testing locally without Tailscale:
```bash
# Just use http://localhost:9741/exchange/<token> directly
# But then GPT needs network access to your machine
```

### List Sessions

```bash
python3 exchange-cli.py list --data-dir ./gpt-exchange-data
```

Output:
```
3 session(s):

  a1b2c3d4e5...     2026-07-17T10:00:00+00:00 → 2026-07-17T12:00:00+00:00 (EXPIRED, 2 msgs)
  f6g7h8i9j0...     2026-07-17T11:00:00+00:00 → 2026-07-17T13:00:00+00:00 (ACTIVE, 5 msgs)
  z9x8w7v6u5...     2026-07-17T11:30:00+00:00 → 2026-07-17T13:30:00+00:00 (ACTIVE, 0 msgs)
```

## Usage Example

### 1. In Claude Code (this terminal)

```bash
# Start server (foreground, so you can stop it easily)
python3 claude-gpt-exchange.py --port 9741 \
  --ntfy-topic "https://ntfy.sh/your-high-entropy-topic" \
  --ntfy-token "$NTFY_TOKEN"

# In another terminal, create a session
python3 exchange-cli.py create --ttl 120
# → Token: f6g7h8i9j0k1l2m3n4o5p6q7r8s9t0u1v2w3x4y5z6a7b8c9d0e1f...
```

### 2. Share URL with GPT

Copy this Funnel URL to GPT:
```
https://<your-node>.tail<...>.ts.net/exchange/f6g7h8i9j0k1l2m3n4o5p6q7r8s9t0u1v2w3x4y5z6a7b8c9d0e1f
```

Instruct GPT:
- To **read** your analysis: `GET /exchange/<token>?role=claude` with header `Authorization: Bearer <token>`
- To **write** feedback: `POST /exchange/<token>?role=gpt` with header `Authorization: Bearer <token>` and body containing their response
- Query param `?role=gpt` filters to only GPT's messages when reading

### 3. In Claude Code, Periodically Read GPT's Response

```bash
# Read all messages
curl http://localhost:9741/exchange/f6g7h8... \
  -H "Authorization: Bearer f6g7h8..."

# Read only GPT's messages
curl "http://localhost:9741/exchange/f6g7h8...?role=gpt" \
  -H "Authorization: Bearer f6g7h8..."
```

## Security Design

### Token

- **64-character hex**, generated by `secrets.token_hex(32)` (256 bits entropy)
- **Per-session**: Each review gets a unique token
- **Bearer header only**: Not in query string to avoid logs/history
- **Limited scope**: Can only access messages in that session

### Expiration

- **TTL configurable**: Default 120 minutes, customizable at creation
- **Automatic cleanup**: Expired sessions are pruned on server startup
- **No persistence after expiry**: Session file is deleted, messages are lost

### Transport

- **Tailscale Funnel**: End-to-end encrypted by Tailscale (WireGuard)
- **No internet routing**: Private tailnet only
- **Token in Bearer header**: Not exposed in URLs or query strings

### File Permissions

- Session JSON files: `chmod 0o600` (readable/writable by owner only)
- Data directory: owner-only access
- No world-readable files

### Threats Mitigated

| Threat | Mitigation |
|---|---|
| Token leakage to GPT's API logs | Bearer header (not query string) |
| Token reuse for other sessions | Per-session token, short lifetime |
| Token bruteforce | 256-bit entropy, not a weak password |
| Network interception | Tailscale Funnel encryption |
| Unauthorized access to messages | Bearer token auth on every request |
| Long-term data exposure | Auto-expiration (default 2 hours) |

### Explicit Risks

1. **If token is compromised** before expiry: Attacker can read/write to that session. Mitigation: shorter TTL, revoke and create new session immediately.
2. **If Tailscale account is compromised**: Attacker could see the Funnel URL. Mitigation: Don't deploy to production without reviewing Tailscale security docs.
3. **If server is deployed to internet without Tailscale**: You're exposing auth tokens to anyone who guesses the URL pattern. **Never do this.**

## Deployment Scenarios

### Scenario 1: Local Testing (No Tailscale)

Easiest for development, only works if GPT has access to your localhost:

```bash
# Terminal 1: Start server
python3 claude-gpt-exchange.py --port 9741

# Terminal 2: Create session
python3 exchange-cli.py create

# Share: http://localhost:9741/exchange/<token>
# (Works only if GPT is on the same network or you forward port 9741)
```

**Risk**: Localhost address only works on your machine. GPT on the web can't reach it.

### Scenario 2: Tailscale Funnel (Recommended)

Requires: Tailscale installed and authenticated on Orion.

```bash
# Terminal 1: Start server
python3 claude-gpt-exchange.py --port 9741 \
  --ntfy-topic "$(grep NTFY_LOCAL_URL ~/.secrets/ntfy.env | cut -d= -f2)" \
  --ntfy-token "$(grep NTFY_TOKEN ~/.secrets/ntfy.env | cut -d= -f2)"

# Terminal 2: Create session and expose via Funnel
python3 exchange-cli.py create
# Token: <TOKEN>

tailscale funnel 9741
# Output: https://<node>.tail<...>.ts.net/

# Share: https://<node>.tail<...>.ts.net/exchange/<TOKEN>?role=gpt
```

**Advantages**:
- Encrypted by Tailscale
- GPT can access from the web
- Token is part of URL (acceptable over Tailscale's encrypted link)
- Auto-revoked when Funnel terminates

**Disadvantages**:
- Requires Tailscale account with Funnel capability
- Exposes Tailscale identity in URL

### Scenario 3: Temporary SSH Forward (Advanced)

If you want to avoid Tailscale:

```bash
# On Orion, start server and forward to a remote machine
ssh -R 9741:localhost:9741 user@remote-machine

# Then GPT connects to user@remote-machine:9741
# Still secure if you trust the remote machine and use short TTL
```

**Risk**: Exposes token to remote machine's logs and network stack.

## No Deployment Required (Yet)

⚠️ **This code is in `/data/code/drop` but NOT deployed to production.**

Before production use, you must:

1. **Decide on exposure method**: Tailscale Funnel, SSH forward, or other
2. **Generate a new token** (not reusing development ones)
3. **Set ntfy credentials** from `~/.secrets/ntfy.env` (optional but recommended)
4. **Test with real GPT** (this is a prototype design)
5. **Document runbook** for creating sessions and sharing URLs

All decisions pending Erik's explicit approval.

## Implementation Notes

- **No external dependencies**: Python 3.12+ stdlib only (http.server, urllib, json)
- **Synchronous**: Not async — simpler for low-frequency, short-lived sessions
- **Thread-safe**: Lock protects concurrent reads/writes to JSON files
- **Cleanup**: Expired sessions are pruned when listed or on server startup

## Quick Integration (Claude Code Hooks)

Future: Add a Claude Code hook to auto-generate and display session URLs:

```bash
# .claude/hooks/post-code-review
#!/bin/bash
# Auto-create exchange session on code review start
python3 /path/to/exchange-cli.py create --ttl 180 | tail -1
```

Not implemented yet — design only.
