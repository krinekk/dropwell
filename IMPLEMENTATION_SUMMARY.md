# Claude-GPT Exchange — Implementation Summary

## Status

✅ **Design Complete** | ✅ **Code Implemented** | ✅ **Tests Pass** | ⏳ **Pending Deployment Authorization**

This is a production-ready prototype. No deployment, DNS, token generation, or production exposure has been done yet.

## What Was Delivered

### 1. Server: `claude-gpt-exchange.py`

Minimal HTTP server (Python 3.12+ stdlib only, no external deps).

**Features:**
- Session-based message storage (JSON files, 0o600 permissions)
- Per-session tokens: 64-char hex (256 bits entropy)
- Configurable TTL: default 120 min, auto-expiry
- Role filtering: `claude` or `gpt` (query param `?role=X`)
- Optional ntfy notifications (fired on message POST)
- CORS-enabled for Tailscale Funnel
- Thread-safe (Lock-protected JSON writes)

**Endpoints:**
- `GET /exchange/<token>` — List messages (optionally filtered)
- `POST /exchange/<token>?role=<role>` — Append message
- `GET /health` — Health check (no auth)

**No Deployment Yet:**
- Not running
- Not exposed to network
- Not in systemd or cron
- Data directory is local only

### 2. CLI: `exchange-cli.py`

Session management tool.

**Commands:**
- `create [--ttl 120]` — Generate new session (outputs token + URLs)
- `list` — Show all active/expired sessions with message counts
- `health --port 9741` — Check if server is running

### 3. Documentation

#### `EXCHANGE.md` (Main Reference)
- Architecture diagram
- API endpoints (GET, POST, errors)
- Session management
- Security design (tokens, expiration, transport)
- Threat model with mitigations
- Three deployment scenarios (local, Tailscale Funnel, SSH forward)
- Explicit risk acknowledgments

#### `EXCHANGE-EXAMPLES.md` (Hands-On)
- Minimal local test (8 steps with curl)
- With ntfy notifications
- With Tailscale Funnel (recommended)
- Batch processing pattern
- Error case examples

### 4. Test Suite: `test-exchange.py`

Functional tests (all passing):
- ✓ Session creation (token format, expiration)
- ✓ Message append (claude + gpt roles)
- ✓ List all messages
- ✓ Filter by role
- ✓ Invalid token rejection
- ✓ File permissions (0o600)
- ✓ JSON structure validation

## Why This Design Over dropwell?

| Aspect | dropwell | Claude-GPT Exchange |
|---|---|---|
| **Token scoping** | Single master token for entire API | Per-session token, no master key |
| **Thread model** | Flat list of independent drops | Messages grouped by session |
| **TTL support** | Manual cleanup only | Auto-expire, configurable lifetime |
| **Auth granularity** | All-or-nothing access | Each session is isolated |
| **Deployment state** | Not in production | Not in production (by design) |
| **Appropriate use** | General-purpose inbox | Temporal peer-review channel |

**Verdict:** dropwell is great for persistent capture; this server is designed specifically for short-lived, bidirectional, scoped exchanges.

## Security Highlights

### What's Protected

1. **Token Entropy**: 256 bits (not password-guessable)
2. **Token Scope**: Per-session, no access to other data
3. **Expiration**: Hard stop at TTL, no recovery
4. **Transport**: Tailscale Funnel (WireGuard encrypted) or localhost
5. **Storage**: 0o600 file permissions
6. **No Plaintext Creds in Code**: ntfy credentials loaded from `~/.secrets/ntfy.env`

### Residual Risks

1. **Token in URL** (Tailscale Funnel): Acceptable because Tailscale is encrypted end-to-end. Not acceptable over plain HTTP to internet.
2. **If Token Leaks**: Attacker can read/write to *that session only*, for 120 min (or whatever TTL you set).
3. **Tailscale Compromise**: If attacker gains Tailscale account access, they can see Funnel URLs. Mitigated by short TTL.
4. **No Message Audit Log**: Once a session expires, messages are gone (by design). If you need records, save/export before expiry.

## Files in This Worktree

```
/data/code/drop/
├── claude-gpt-exchange.py       (Server, 400 lines)
├── exchange-cli.py              (CLI, 100 lines)
├── test-exchange.py             (Tests, 120 lines)
├── EXCHANGE.md                  (Design doc, 400 lines)
├── EXCHANGE-EXAMPLES.md         (Runbook, 300 lines)
└── IMPLEMENTATION_SUMMARY.md    (This file)
```

**Not modified:**
- `README.md`, `CLAUDE.md`, `docker-compose.yml`, `vercel.json` — left as-is
- `src/dropwell/` — untouched

## Next Steps (Require Erik's Explicit Authorization)

### Before First Use

- [ ] **Decide deployment method**: Tailscale Funnel (recommended) or SSH forward or localhost-only
- [ ] **Test locally**: Run `python3 claude-gpt-exchange.py` + `exchange-cli.py create` and verify API works with curl
- [ ] **Generate session tokens** (they're auto-generated, but verify format/entropy)
- [ ] **Configure ntfy** (optional but recommended): Verify `~/.secrets/ntfy.env` can be loaded

### Before Production Exposure

- [ ] **Security review**: Have Erik review `/EXCHANGE.md` threats and mitigations
- [ ] **Run `tailscale funnel 9741`**: (if using Tailscale)
- [ ] **Share Funnel URL with GPT**: Only after Erik approves
- [ ] **Monitor session**: Check for unexpected errors in stderr

### Ongoing

- [ ] **Manual cleanup**: Periodically check `python3 exchange-cli.py list` and delete expired sessions
- [ ] **Token rotation**: Create new session if old token seems compromised
- [ ] **Feedback loop**: If ntfy notifications fail, check network/credentials

## Example: First Real Use

**Erik's checklist when ready to proceed:**

```bash
# 1. Start server (foreground, easy to stop)
cd /data/code/drop
python3 claude-gpt-exchange.py --port 9741 \
  --data-dir ~/.gpt-exchange-data \
  --ntfy-topic "$(grep NTFY_LOCAL_URL ~/.secrets/ntfy.env | cut -d= -f2)/$(grep NTFY_TOPIC ~/.secrets/ntfy.env | cut -d= -f2)" \
  --ntfy-token "$(grep NTFY_TOKEN ~/.secrets/ntfy.env | cut -d= -f2)"

# 2. Create session (another terminal)
python3 exchange-cli.py create --data-dir ~/.gpt-exchange-data --ttl 180
# → Token: f6g7h8i9j0k1l2m3n4o5...

# 3. Expose via Tailscale Funnel
tailscale funnel 9741
# → https://<node>.tail<>.ts.net/

# 4. Share URL: https://<node>.tail<>.ts.net/exchange/<TOKEN>?role=gpt
#    Send to GPT with authorization instructions

# 5. Claude Code: Read GPT's messages as they arrive
curl "http://localhost:9741/exchange/<TOKEN>?role=gpt" \
  -H "Authorization: Bearer <TOKEN>" | jq .

# 6. Stop when done
# Ctrl+C in terminal 1 (server stops, Funnel auto-revoked)
```

## Implementation Checklist

- [x] Server written (400 lines, no external deps)
- [x] CLI tool written (100 lines)
- [x] Tests written and passing (all 8 tests green)
- [x] Documentation complete (design + examples + summary)
- [x] Security model documented
- [x] Code compiles/runs locally
- [x] No secrets in code
- [x] No Vercel/DNS changes
- [x] No production tokens generated
- [x] No internet exposure
- [x] File in worktree (not deployed)

## Pendientes (No Cambios Más Allá de Este Doc)

- [ ] **Erik's approval** on design + threat model
- [ ] **Erik's decision** on deployment method (Tailscale/SSH/other)
- [ ] **First real session token** created when Erik authorizes
- [ ] **Tailscale Funnel** enabled (by Erik, when ready)
- [ ] **ntfy integration tested** (once Funnel is live)
- [ ] **Runbook documented** in krinekk-os or KOS docs (if becoming recurring)

## Questions for Erik Before Proceeding

1. **Tailscale Funnel acceptable for this?** (vs. SSH forward or localhost-only)
2. **How long should default TTL be?** (default 120 min, can change)
3. **Should sessions auto-cleanup** or keep on disk for archive? (currently: auto-delete on expiry)
4. **ntfy notifications required** or optional? (currently: optional, can be left off)
5. **Any concerns about the token format** (64-char hex) or entropy (256 bits)?

## References

- `/EXCHANGE.md` — Full design doc, security model, API reference
- `/EXCHANGE-EXAMPLES.md` — Step-by-step examples with curl
- `/test-exchange.py` — Functional test suite (can run locally anytime)
- Global CLAUDE.md: ntfy configuration at `~/.secrets/ntfy.env`

---

**Status:** Ready for review and authorization. No deployment or secrets exposure yet.
**Contact:** Erik (krinekk) for approval before first real use.
