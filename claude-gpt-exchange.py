#!/usr/bin/env python3
"""
Claude-GPT Exchange Server
Minimal durable message exchange for code review between Claude and GPT.

Usage:
  python3 claude-gpt-exchange.py [--port 9741] [--data-dir ./gpt-exchange-data]
                                 [--ntfy-topic <topic>] [--ntfy-token <token>]

Session URL format:
  http://localhost:9741/exchange/<session-token>?role=claude
  http://localhost:9741/exchange/<session-token>?role=gpt

API:
  GET  /exchange/<token>?role=<role>     - List messages (optional role filter)
  POST /exchange/<token>?role=<role>     - Append message (Bearer auth)
  GET  /health                           - Health check
"""

import argparse
import json
import os
import secrets
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Lock
from urllib.parse import parse_qs, urlparse


class ExchangeStore:
    """Thread-safe store for exchange messages, backed by JSON files."""

    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.lock = Lock()

    def _session_file(self, token: str) -> Path:
        return self.data_dir / f"{token}.json"

    def create_session(self, ttl_minutes: int = 120) -> tuple[str, str]:
        """Create a new session, return (token, expires_at)."""
        token = secrets.token_hex(32)  # 64-char hex = 256 bits
        expires_at = (
            datetime.now(timezone.utc) + timedelta(minutes=ttl_minutes)
        ).isoformat()

        session = {
            "token": token,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "expires_at": expires_at,
            "messages": [],
        }

        with self.lock:
            path = self._session_file(token)
            with open(path, "w") as f:
                json.dump(session, f, indent=2)
            os.chmod(path, 0o600)

        return token, expires_at

    def get_messages(self, token: str, role: str | None = None) -> list[dict] | None:
        """Get messages for a session, optionally filtered by role."""
        with self.lock:
            path = self._session_file(token)
            if not path.exists():
                return None

            with open(path) as f:
                session = json.load(f)

            # Check expiration
            expires = datetime.fromisoformat(session["expires_at"])
            if expires < datetime.now(timezone.utc):
                return None

            messages = session["messages"]
            if role:
                messages = [m for m in messages if m.get("role") == role]
            return messages

    def add_message(self, token: str, role: str, body: str) -> dict | None:
        """Add a message. Returns message dict or None if expired/invalid."""
        with self.lock:
            path = self._session_file(token)
            if not path.exists():
                return None

            with open(path) as f:
                session = json.load(f)

            # Check expiration
            expires = datetime.fromisoformat(session["expires_at"])
            if expires < datetime.now(timezone.utc):
                return None

            message = {
                "id": secrets.token_hex(12),
                "role": role,
                "body": body,
                "posted_at": datetime.now(timezone.utc).isoformat(),
            }
            session["messages"].append(message)

            with open(path, "w") as f:
                json.dump(session, f, indent=2)

            return message

    def list_sessions(self) -> list[dict]:
        """List sessions with short ids (no full tokens)."""
        sessions = []
        with self.lock:
            for path in sorted(self.data_dir.glob("*.json")):
                try:
                    with open(path) as f:
                        session = json.load(f)
                    expires = datetime.fromisoformat(session["expires_at"])
                    sessions.append(
                        {
                            "sid": session["token"][:12],
                            "created_at": session["created_at"],
                            "expires_at": session["expires_at"],
                            "expired": expires < datetime.now(timezone.utc),
                            "message_count": len(session.get("messages", [])),
                        }
                    )
                except (json.JSONDecodeError, KeyError, ValueError):
                    continue
        return sessions

    def resolve_sid(self, sid: str) -> str | None:
        """Resolve a 12-char short id to the full token, server-side only."""
        if len(sid) < 12:
            return None
        for path in self.data_dir.glob(f"{sid}*.json"):
            return path.stem
        return None

    def cleanup_expired(self) -> int:
        """Remove expired sessions. Returns count deleted."""
        now = datetime.now(timezone.utc)
        deleted = 0

        with self.lock:
            for path in self.data_dir.glob("*.json"):
                try:
                    with open(path) as f:
                        session = json.load(f)
                    if datetime.fromisoformat(session["expires_at"]) < now:
                        path.unlink()
                        deleted += 1
                except (json.JSONDecodeError, KeyError, ValueError):
                    path.unlink()
                    deleted += 1

        return deleted


UI_HTML = """<!doctype html>
<html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>drop · exchange</title>
<style>
:root { --bg:#05070d; --panel:rgba(12,18,30,.88); --panel-strong:#101827;
  --text:#f4f8ff; --muted:#91a0b6; --muted-strong:#c4d0e3;
  --line:rgba(146,163,184,.18); --line-strong:rgba(108,228,255,.32);
  --accent:#38d9ff; --accent-soft:rgba(56,217,255,.13);
  --good:#19f28b; --good-soft:rgba(25,242,139,.12);
  --warning:#ffd166; --warning-soft:rgba(255,209,102,.14);
  --violet:#a96cff; --radius:18px; color-scheme:dark; }
*{box-sizing:border-box}
body{margin:0;min-height:100vh;color:var(--text);font-size:15px;
  font-family:Inter,ui-sans-serif,system-ui,sans-serif;
  background:linear-gradient(120deg,rgba(56,217,255,.10),transparent 28%),
    linear-gradient(240deg,rgba(25,242,139,.08),transparent 30%),
    linear-gradient(180deg,#070a12 0%,#05070d 55%,#03050a 100%)}
.app{max-width:1080px;margin:0 auto;padding:24px 16px 110px}
header{display:flex;align-items:center;gap:12px;flex-wrap:wrap;
  padding:16px 20px;border:1px solid var(--line-strong);border-radius:22px;
  background:linear-gradient(145deg,rgba(13,20,34,.96),rgba(8,12,22,.98));
  box-shadow:0 24px 90px rgba(0,0,0,.46),0 0 34px rgba(56,217,255,.15)}
.logo{font-weight:800;letter-spacing:.04em;font-size:17px}
.logo span{color:var(--accent)}
.chip{font-size:12px;padding:5px 12px;border-radius:999px;white-space:nowrap;
  border:1px solid var(--line);color:var(--muted-strong);background:var(--panel)}
.chip.live{color:var(--good);border-color:rgba(25,242,139,.4);
  background:var(--good-soft)}
.chip.ttl{color:var(--warning);border-color:rgba(255,209,102,.4);
  background:var(--warning-soft)}
.chip.user{margin-left:auto}
.sessions{display:flex;gap:10px;margin:16px 0 4px;flex-wrap:wrap}
.stab{font-size:13px;padding:8px 14px;border-radius:12px;cursor:pointer;
  border:1px solid var(--line);background:var(--panel);color:var(--muted-strong)}
.stab.active{border-color:var(--line-strong);color:var(--text);
  background:var(--accent-soft)}
.stab.expired{opacity:.45}
.stab .n{color:var(--accent);font-weight:700;margin-left:6px}
.thread{display:flex;flex-direction:column;gap:14px;margin-top:16px}
.msg{display:flex;flex-direction:column;max-width:74%}
.msg.claude{align-self:flex-start}
.msg.gpt,.msg.erik{align-self:flex-end}
.meta{display:flex;gap:8px;align-items:baseline;font-size:12px;
  color:var(--muted);margin:0 6px 4px}
.msg.gpt .meta,.msg.erik .meta{flex-direction:row-reverse}
.who{font-weight:700;letter-spacing:.03em}
.msg.claude .who{color:var(--accent)}
.msg.gpt .who{color:var(--violet)}
.msg.erik .who{color:var(--good)}
.bubble{padding:12px 16px;border-radius:var(--radius);line-height:1.55;
  border:1px solid var(--line);background:var(--panel-strong);
  box-shadow:0 8px 30px rgba(0,0,0,.35);white-space:pre-wrap;
  overflow-wrap:anywhere}
.msg.claude .bubble{border-color:rgba(56,217,255,.28);
  border-top-left-radius:6px}
.msg.gpt .bubble{border-color:rgba(169,108,255,.30);
  border-top-right-radius:6px}
.msg.erik .bubble{border-color:rgba(25,242,139,.30);
  border-top-right-radius:6px}
.empty{color:var(--muted);text-align:center;padding:40px 0}
.composer{position:fixed;bottom:0;left:0;right:0;
  background:linear-gradient(180deg,transparent,#03050a 40%);
  padding:26px 16px 18px}
.cinner{max-width:1080px;margin:0 auto;display:flex;gap:10px;
  border:1px solid var(--line-strong);border-radius:16px;padding:10px 14px;
  background:var(--panel-strong);box-shadow:0 24px 90px rgba(0,0,0,.46)}
.cinner textarea{flex:1;background:none;border:0;outline:0;resize:none;
  color:var(--text);font:inherit;max-height:120px}
.btn{border:0;border-radius:10px;padding:9px 16px;font-weight:700;
  cursor:pointer;background:linear-gradient(135deg,#38d9ff,#2f7cff);
  color:#04131c}
.hint{text-align:center;color:var(--muted);font-size:11.5px;margin-top:8px}
</style></head><body>
<div class="app">
  <header>
    <div class="logo">drop<span>·exchange</span></div>
    <span class="chip" id="status">cargando…</span>
    <span class="chip ttl" id="ttl" hidden></span>
    <span class="chip user" id="user"></span>
  </header>
  <div class="sessions" id="sessions"></div>
  <div class="thread" id="thread"><div class="empty">Sin sesión</div></div>
</div>
<div class="composer">
  <div class="cinner">
    <textarea id="box" rows="1"
      placeholder="Escribir como Erik (role=erik)…"></textarea>
    <button class="btn" id="send">Enviar</button>
  </div>
  <div class="hint">Autenticado vía Cloudflare Access · los agentes
    escriben por su propio canal</div>
</div>
<script>
let SID=null, TIMER=null;
const $=id=>document.getElementById(id);
async function j(u,o){const r=await fetch(u,o);
  if(!r.ok)throw new Error(r.status);return r.json()}
function fmt(iso){return new Date(iso).toLocaleTimeString("es-ES",
  {hour:"2-digit",minute:"2-digit"})}
async function loadSessions(){
  const d=await j("/ui/api/sessions");
  const el=$("sessions");el.innerHTML="";
  d.sessions.forEach(s=>{
    const t=document.createElement("div");
    t.className="stab"+(s.expired?" expired":"")+(s.sid===SID?" active":"");
    t.textContent=s.sid+"… ";
    const n=document.createElement("span");n.className="n";
    n.textContent=s.message_count;t.appendChild(n);
    t.onclick=()=>{SID=s.sid;loadSessions();loadThread()};
    el.appendChild(t)});
  if(!SID&&d.sessions.length){
    const act=d.sessions.filter(s=>!s.expired);
    SID=(act.length?act[act.length-1]:d.sessions[d.sessions.length-1]).sid;
    loadSessions();loadThread()}
  $("user").textContent=d.user||""}
async function loadThread(){
  if(!SID)return;
  const d=await j("/ui/api/thread?sid="+SID);
  $("status").textContent=d.expired?"expirada":"● activa";
  $("status").className="chip"+(d.expired?"":" live");
  if(!d.expired){const ms=new Date(d.expires_at)-Date.now();
    const h=Math.floor(ms/3.6e6),m=Math.floor(ms%3.6e6/6e4);
    $("ttl").hidden=false;$("ttl").textContent="TTL "+h+"h "+m+"m"}
  else $("ttl").hidden=true;
  const el=$("thread");el.innerHTML="";
  if(!d.messages.length)
    el.innerHTML='<div class="empty">Hilo vacío</div>';
  d.messages.forEach(m=>{
    const w=document.createElement("div");w.className="msg "+m.role;
    const meta=document.createElement("div");meta.className="meta";
    const who=document.createElement("span");who.className="who";
    who.textContent=m.role.toUpperCase();
    const ts=document.createElement("span");ts.textContent=fmt(m.posted_at);
    meta.append(who,ts);
    const b=document.createElement("div");b.className="bubble";
    b.textContent=m.body;w.append(meta,b);el.appendChild(w)});
  window.scrollTo(0,document.body.scrollHeight)}
$("send").onclick=async()=>{
  const t=$("box").value.trim();if(!t||!SID)return;
  await fetch("/ui/api/send?sid="+SID+"&role=erik",
    {method:"POST",body:t});
  $("box").value="";loadThread()};
$("box").addEventListener("keydown",e=>{
  if(e.key==="Enter"&&!e.shiftKey){e.preventDefault();$("send").click()}});
loadSessions();
TIMER=setInterval(()=>{loadSessions();if(SID)loadThread()},10000);
</script></body></html>
"""


class ExchangeHandler(BaseHTTPRequestHandler):
    store: "ExchangeStore | None" = None
    ntfy_config: "dict | None" = None
    ui_email: "str | None" = None

    def _ui_authorized(self) -> bool:
        """UI requires the identity header injected by Cloudflare Access."""
        if not self.ui_email:
            return False
        header = self.headers.get("Cf-Access-Authenticated-User-Email", "")
        return secrets.compare_digest(header, self.ui_email)

    def log_message(self, format, *args):
        """Suppress default HTTP logging; we log selectively."""
        del format, args

    def _send_json(self, code: int, data: dict):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data, indent=2).encode() + b"\n")

    def _send_text(self, code: int, text: str):
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(text.encode() + b"\n")

    def _get_bearer_token(self) -> str | None:
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            return auth[7:]
        return None

    def _notify(self, role: str, message_snippet: str):
        """Send ntfy notification if configured."""
        if not self.ntfy_config or not self.ntfy_config.get("url"):
            return

        try:
            title = f"{role.upper()} wrote a response"
            msg_short = message_snippet[:100]
            if len(message_snippet) > 100:
                msg_short += "..."

            headers = {"Title": title}
            if self.ntfy_config.get("token"):
                headers["Authorization"] = f"Bearer {self.ntfy_config['token']}"

            req = urllib.request.Request(
                url=self.ntfy_config["url"],
                data=f"{title}\n{msg_short}".encode(),
                headers=headers,
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=3):
                pass
        except Exception as e:
            print(f"[WARN] ntfy notification failed: {e}", file=sys.stderr)

    def do_GET(self):
        """GET /exchange/<token> - List messages (optionally filtered by role)."""
        parsed = urlparse(self.path)
        path_parts = parsed.path.strip("/").split("/")

        if parsed.path == "/health":
            self._send_json(200, {"status": "ok"})
            return

        if parsed.path == "/ui" or parsed.path.startswith("/ui/"):
            if not self._ui_authorized():
                self._send_json(403, {"error": "ui requires authentication"})
                return
            assert self.store is not None
            if parsed.path == "/ui":
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(UI_HTML.encode())
                return
            if parsed.path == "/ui/api/sessions":
                email = self.headers.get(
                    "Cf-Access-Authenticated-User-Email", ""
                )
                self._send_json(
                    200,
                    {"sessions": self.store.list_sessions(), "user": email},
                )
                return
            if parsed.path == "/ui/api/thread":
                query = parse_qs(parsed.query)
                sid = query.get("sid", [""])[0]
                token = self.store.resolve_sid(sid)
                if not token:
                    self._send_json(404, {"error": "unknown session"})
                    return
                path = self.store._session_file(token)
                with open(path) as f:
                    session = json.load(f)
                expires = datetime.fromisoformat(session["expires_at"])
                self._send_json(
                    200,
                    {
                        "messages": session["messages"],
                        "expires_at": session["expires_at"],
                        "expired": expires < datetime.now(timezone.utc),
                    },
                )
                return
            self._send_json(404, {"error": "not found"})
            return

        if len(path_parts) == 2 and path_parts[0] == "exchange":
            token = path_parts[1]
            query = parse_qs(parsed.query)
            role = query.get("role", [None])[0]

            # The 256-bit path token is the credential. A Bearer header, if
            # present, must match it; browser-only clients may omit it.
            auth_token = self._get_bearer_token()
            if auth_token and not secrets.compare_digest(auth_token, token):
                self._send_json(401, {"error": "invalid authorization"})
                return

            assert self.store is not None
            messages = self.store.get_messages(token, role=role)
            if messages is None:
                self._send_json(401, {"error": "invalid or expired token"})
                return

            self._send_json(200, {"messages": messages})
            return

        # GET /exchange/<token>/post?role=<r>&body=<text> — write fallback for
        # browser-only clients that cannot send POST or custom headers.
        if (
            len(path_parts) == 3
            and path_parts[0] == "exchange"
            and path_parts[2] == "post"
        ):
            token = path_parts[1]
            query = parse_qs(parsed.query)
            role = query.get("role", [None])[0]
            body = query.get("body", [None])[0]

            if not role or role not in ("claude", "gpt", "erik"):
                self._send_json(
                    400, {"error": "role must be 'claude', 'gpt' or 'erik'"}
                )
                return
            if not body:
                self._send_json(400, {"error": "body query param required"})
                return

            assert self.store is not None
            message = self.store.add_message(token, role, body)
            if message is None:
                self._send_json(401, {"error": "invalid or expired token"})
                return

            self._notify(role, body)
            self._send_json(201, message)
            return

        self._send_json(404, {"error": "not found"})

    def do_POST(self):
        """POST /exchange/<token> - Append message (raw text body, role in query)."""
        parsed = urlparse(self.path)
        path_parts = parsed.path.strip("/").split("/")

        if parsed.path == "/ui/api/send":
            if not self._ui_authorized():
                self._send_json(403, {"error": "ui requires authentication"})
                return
            query = parse_qs(parsed.query)
            sid = query.get("sid", [""])[0]
            role = query.get("role", ["erik"])[0]
            if role not in ("claude", "gpt", "erik"):
                self._send_json(400, {"error": "invalid role"})
                return
            assert self.store is not None
            token = self.store.resolve_sid(sid)
            if not token:
                self._send_json(404, {"error": "unknown session"})
                return
            content_length = int(self.headers.get("Content-Length", 0))
            if content_length > 50_000_000:
                self._send_json(413, {"error": "body too large"})
                return
            try:
                body = self.rfile.read(content_length).decode("utf-8")
            except UnicodeDecodeError:
                self._send_json(400, {"error": "invalid utf-8"})
                return
            message = self.store.add_message(token, role, body)
            if message is None:
                self._send_json(401, {"error": "invalid or expired token"})
                return
            self._notify(role, body)
            self._send_json(201, message)
            return

        if len(path_parts) == 2 and path_parts[0] == "exchange":
            token = path_parts[1]
            query = parse_qs(parsed.query)
            role = query.get("role", [None])[0]

            if not role or role not in ("claude", "gpt", "erik"):
                self._send_json(
                    400, {"error": "role must be 'claude', 'gpt' or 'erik'"}
                )
                return

            auth_token = self._get_bearer_token()
            if not auth_token or not secrets.compare_digest(auth_token, token):
                self._send_json(401, {"error": "invalid authorization"})
                return

            content_length = int(self.headers.get("Content-Length", 0))
            if content_length > 50_000_000:  # 50 MiB limit
                self._send_json(413, {"error": "body too large"})
                return

            try:
                body = self.rfile.read(content_length).decode("utf-8")
            except UnicodeDecodeError:
                self._send_json(400, {"error": "invalid utf-8"})
                return

            assert self.store is not None
            message = self.store.add_message(token, role, body)
            if message is None:
                self._send_json(401, {"error": "invalid or expired token"})
                return

            self._notify(role, body)
            self._send_json(201, message)
            return

        self._send_json(404, {"error": "not found"})

    def do_OPTIONS(self):
        """Handle CORS preflight."""
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        self.end_headers()


def main():
    parser = argparse.ArgumentParser(
        description="Claude-GPT Exchange Server: session-based message exchange."
    )
    parser.add_argument(
        "--port", type=int, default=9741, help="HTTP port (default 9741)"
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path("./gpt-exchange-data"),
        help="Directory for session data (default ./gpt-exchange-data)",
    )
    parser.add_argument(
        "--ntfy-topic",
        type=str,
        help="ntfy topic URL (e.g. 'https://ntfy.sh/<topic>') for notifications",
    )
    parser.add_argument(
        "--ntfy-token",
        type=str,
        help="ntfy bearer token (if self-hosted and requires auth)",
    )
    parser.add_argument(
        "--ui-email",
        type=str,
        help=(
            "Enable /ui for this email (must match the "
            "Cf-Access-Authenticated-User-Email header injected by "
            "Cloudflare Access). UI is disabled if omitted."
        ),
    )

    args = parser.parse_args()

    # Prepare ntfy config
    ntfy_config = {}
    if args.ntfy_topic:
        ntfy_config["url"] = args.ntfy_topic
        ntfy_config["token"] = args.ntfy_token or ""

    # Set class-level config
    store = ExchangeStore(args.data_dir)
    deleted = store.cleanup_expired()
    if deleted:
        print(f"Cleaned up {deleted} expired session(s)", file=sys.stderr)
    ExchangeHandler.store = store
    ExchangeHandler.ntfy_config = ntfy_config
    ExchangeHandler.ui_email = args.ui_email
    if args.ui_email:
        print(f"UI enabled for {args.ui_email} at /ui", file=sys.stderr)

    # Server
    server = HTTPServer(("127.0.0.1", args.port), ExchangeHandler)
    print(
        f"Claude-GPT Exchange listening on http://127.0.0.1:{args.port}",
        file=sys.stderr,
    )
    print(f"Data directory: {args.data_dir.resolve()}", file=sys.stderr)
    if ntfy_config:
        print(f"Notifications enabled: {ntfy_config['url']}", file=sys.stderr)
    print("Press Ctrl+C to stop.", file=sys.stderr)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutdown.", file=sys.stderr)


if __name__ == "__main__":
    main()
