#!/usr/bin/env python3
"""
Claude-GPT Exchange Server
Minimal durable message exchange for code review between Claude and GPT.

Usage:
  python3 claude-gpt-exchange.py [--port 9741] [--data-dir ./gpt-exchange-data]
                                 [--ntfy-topic <topic>] [--ntfy-token <token>]

Delivery URL format:
  https://drop.krinekk.dev/exchange/<one-time-token>

API:
  GET  /exchange/<one-time-token>  - Redeem one GPT thread snapshot
  POST /ui/api/send                         - Append as the authenticated UI user
  GET  /health                           - Health check
"""

import argparse
import fcntl
import hashlib
import json
import os
import secrets
import sys
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock
from urllib.parse import parse_qs, urlparse


class ExchangeStore:
    """Durable threads plus one-time, read-only delivery capabilities."""

    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.lock = Lock()
        self.lock_path = self.data_dir / ".exchange.lock"
        self.lock_path.touch(exist_ok=True)
        os.chmod(self.lock_path, 0o600)

    @contextmanager
    def _locked(self):
        """Serialize mutations across threads and independent server processes."""
        with self.lock:
            with open(self.lock_path, "a+") as lock_file:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _thread_file(self, thread_id: str) -> Path:
        return self.data_dir / f"thread_{thread_id}.json"

    def _delivery_file(self, token: str) -> Path:
        return self.data_dir / f"delivery_{token}.json"

    @staticmethod
    def _read_json(path: Path) -> dict | None:
        try:
            with open(path) as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return None

    @staticmethod
    def _write_json(path: Path, data: dict) -> None:
        temp_path = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
        with open(temp_path, "w") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(temp_path, 0o600)
        os.replace(temp_path, path)

    def create_thread(self) -> str:
        """Create a durable exchange thread; it has no delivery credential."""
        with self._locked():
            while True:
                thread_id = secrets.token_hex(16)
                path = self._thread_file(thread_id)
                if not path.exists():
                    break
            self._write_json(
                path,
                {
                    "thread_id": thread_id,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "messages": [],
                },
            )
        return thread_id

    def get_thread(self, thread_id: str) -> dict | None:
        """Return a durable thread without any delivery credentials."""
        with self._locked():
            thread = self._read_json(self._thread_file(thread_id))
            return thread

    def add_message(self, thread_id: str, role: str, body: str) -> dict | None:
        """Append a message to a durable thread."""
        with self._locked():
            path = self._thread_file(thread_id)
            thread = self._read_json(path)
            if thread is None:
                return None
            message = {
                "id": secrets.token_hex(12),
                "role": role,
                "body": body,
                "posted_at": datetime.now(timezone.utc).isoformat(),
            }
            thread["messages"].append(message)
            self._write_json(path, thread)
            return message

    def issue_delivery(
        self, thread_id: str, role: str = "gpt", ttl_minutes: int = 15
    ) -> tuple[str, str]:
        """Issue a single-use, read-only delivery capability for a thread."""
        if role != "gpt":
            raise ValueError("only the gpt delivery role is supported")
        with self._locked():
            if self._read_json(self._thread_file(thread_id)) is None:
                raise ValueError("unknown thread")
            token, expires_at = self._issue_delivery_locked(
                thread_id, role, ttl_minutes
            )
        return token, expires_at

    def accept_gpt_drop(
        self, thread_id: str, drop_id: str, body: str, ttl_minutes: int = 15
    ) -> tuple[dict | None, tuple[str, str] | None, bool]:
        """Mirror one KOS drop and issue the next delivery exactly once."""
        with self._locked():
            path = self._thread_file(thread_id)
            thread = self._read_json(path)
            if thread is None:
                raise ValueError("unknown thread")
            processed = thread.setdefault("processed_drop_ids", [])
            if drop_id in processed:
                delivery = self._find_or_recover_drop_delivery_locked(
                    thread_id, drop_id, ttl_minutes
                )
                return None, delivery, True
            message = {
                "id": secrets.token_hex(12),
                "role": "gpt",
                "body": body,
                "posted_at": datetime.now(timezone.utc).isoformat(),
            }
            thread["messages"].append(message)
            processed.append(drop_id)
            self._write_json(path, thread)
            token, expires_at = self._issue_delivery_locked(
                thread_id, "gpt", ttl_minutes, source_drop_id=drop_id
            )
            return message, (token, expires_at), False

    def redeem_delivery(
        self, token: str, role: str
    ) -> tuple[dict | None, str | None]:
        """Atomically consume a delivery capability and return its thread."""
        with self._locked():
            path = self._delivery_file(token)
            delivery = self._read_json(path)
            if delivery is None:
                return None, "unknown"
            if role != delivery.get("role"):
                return None, "role"
            if delivery.get("consumed_at") is not None:
                return None, "consumed"
            expires_at = datetime.fromisoformat(delivery["expires_at"])
            if expires_at < datetime.now(timezone.utc):
                return None, "expired"
            delivery["consumed_at"] = datetime.now(timezone.utc).isoformat()
            self._write_json(path, delivery)
            thread = self._read_json(self._thread_file(delivery["thread_id"]))
            if thread is None:
                return None, "unknown"
            return thread, None

    def _issue_delivery_locked(
        self,
        thread_id: str,
        role: str,
        ttl_minutes: int,
        source_drop_id: str | None = None,
    ) -> tuple[str, str]:
        token = secrets.token_hex(32)
        expires_at = (
            datetime.now(timezone.utc) + timedelta(minutes=ttl_minutes)
        ).isoformat()
        delivery = {
            "token": token,
            "thread_id": thread_id,
            "role": role,
            "issued_at": datetime.now(timezone.utc).isoformat(),
            "expires_at": expires_at,
            "consumed_at": None,
        }
        if source_drop_id is not None:
            delivery["source_drop_id"] = source_drop_id
        self._write_json(self._delivery_file(token), delivery)
        return token, expires_at

    def _find_or_recover_drop_delivery_locked(
        self, thread_id: str, drop_id: str, ttl_minutes: int
    ) -> tuple[str, str]:
        """Return the delivery for a processed drop, recovering an interrupted emit.

        The thread marker is written before its delivery so a crash cannot duplicate
        a GPT message. If that crash happens, a retry creates the missing delivery.
        A consumed delivery similarly gets a fresh capability on retry.
        """
        now = datetime.now(timezone.utc)
        for path in self.data_dir.glob("delivery_*.json"):
            delivery = self._read_json(path)
            if (
                delivery is None
                or delivery.get("thread_id") != thread_id
                or delivery.get("source_drop_id") != drop_id
            ):
                continue
            try:
                unexpired = datetime.fromisoformat(delivery["expires_at"]) >= now
            except (KeyError, ValueError):
                unexpired = False
            if delivery.get("consumed_at") is None and unexpired:
                return delivery["token"], delivery["expires_at"]
        return self._issue_delivery_locked(
            thread_id, "gpt", ttl_minutes, source_drop_id=drop_id
        )

    def list_threads(self) -> list[dict]:
        """List durable threads for the UI without delivery tokens."""
        threads = []
        with self._locked():
            for path in sorted(self.data_dir.glob("thread_*.json")):
                thread = self._read_json(path)
                if thread is None:
                    continue
                threads.append(
                    {
                        "sid": thread["thread_id"][:12],
                        "created_at": thread["created_at"],
                        "message_count": len(thread.get("messages", [])),
                    }
                )
        return threads

    def resolve_sid(self, sid: str) -> str | None:
        """Resolve a UI short id to a durable thread id, server-side only."""
        if len(sid) < 12:
            return None
        with self._locked():
            for path in self.data_dir.glob(f"thread_{sid}*.json"):
                thread = self._read_json(path)
                if thread is not None:
                    return thread["thread_id"]
        return None

    def cleanup_expired(self) -> int:
        """Remove expired delivery capabilities but retain durable threads."""
        now = datetime.now(timezone.utc)
        deleted = 0
        with self._locked():
            for path in self.data_dir.glob("delivery_*.json"):
                delivery = self._read_json(path)
                if delivery is None:
                    path.unlink(missing_ok=True)
                    deleted += 1
                    continue
                try:
                    expired = datetime.fromisoformat(delivery["expires_at"]) < now
                except (KeyError, ValueError):
                    expired = True
                if expired:
                    path.unlink(missing_ok=True)
                    deleted += 1
        return deleted

    def migrate_legacy_sessions(self, dry_run: bool = False) -> dict[str, int]:
        """Move legacy token-named sessions into durable threads safely.

        The old credential is never reused as a thread id. Original JSON is
        moved into a private backup directory only after its replacement thread
        is durable, making retries safe after an interrupted migration.
        """
        results = {"migrated": 0, "already_migrated": 0, "skipped": 0}
        backup_dir = self.data_dir / "legacy-backups"
        with self._locked():
            legacy_paths = [
                path
                for path in self.data_dir.glob("*.json")
                if not path.name.startswith(("thread_", "delivery_"))
            ]
            for path in legacy_paths:
                legacy = self._read_json(path)
                if not self._is_legacy_session(legacy):
                    results["skipped"] += 1
                    continue
                assert legacy is not None
                digest = hashlib.sha256(
                    json.dumps(legacy, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest()
                existing = self._find_migrated_thread(digest)
                if existing is not None:
                    results["already_migrated"] += 1
                    if not dry_run:
                        self._archive_legacy(path, backup_dir, digest)
                    continue
                if dry_run:
                    results["migrated"] += 1
                    continue
                thread_id = secrets.token_hex(16)
                while self._thread_file(thread_id).exists():
                    thread_id = secrets.token_hex(16)
                self._write_json(
                    self._thread_file(thread_id),
                    {
                        "thread_id": thread_id,
                        "created_at": legacy["created_at"],
                        "messages": legacy["messages"],
                        "migrated_from_sha256": digest,
                    },
                )
                self._archive_legacy(path, backup_dir, digest)
                results["migrated"] += 1
        return results

    @staticmethod
    def _is_legacy_session(value: dict | None) -> bool:
        return bool(
            isinstance(value, dict)
            and isinstance(value.get("token"), str)
            and isinstance(value.get("created_at"), str)
            and isinstance(value.get("expires_at"), str)
            and isinstance(value.get("messages"), list)
        )

    def _find_migrated_thread(self, digest: str) -> dict | None:
        for path in self.data_dir.glob("thread_*.json"):
            thread = self._read_json(path)
            if thread and thread.get("migrated_from_sha256") == digest:
                return thread
        return None

    def _archive_legacy(self, path: Path, backup_dir: Path, digest: str) -> None:
        backup_dir.mkdir(mode=0o700, exist_ok=True)
        os.chmod(backup_dir, 0o700)
        backup_path = backup_dir / f"legacy-{digest[:16]}.json"
        os.replace(path, backup_path)
        os.chmod(backup_path, 0o600)


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
    t.className="stab"+(s.sid===SID?" active":"");
    t.textContent=s.sid+"… ";
    const n=document.createElement("span");n.className="n";
    n.textContent=s.message_count;t.appendChild(n);
    t.onclick=()=>{SID=s.sid;loadSessions();loadThread()};
    el.appendChild(t)});
  if(!SID&&d.sessions.length){
    SID=d.sessions[d.sessions.length-1].sid;
    loadSessions();loadThread()}
  $("user").textContent=d.user||""}
async function loadThread(){
  if(!SID)return;
  const d=await j("/ui/api/thread?sid="+SID);
  $("status").textContent="● hilo activo";
  $("status").className="chip live";
  $("ttl").hidden=true;
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
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(json.dumps(data, indent=2).encode() + b"\n")

    def _send_text(self, code: int, text: str):
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(text.encode() + b"\n")

    def _content_length(self) -> int | None:
        """Parse Content-Length; reject malformed, negative, or oversized values."""
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            return None
        if length < 0 or length > 50_000_000:
            return None
        return length

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
                    {"sessions": self.store.list_threads(), "user": email},
                )
                return
            if parsed.path == "/ui/api/thread":
                query = parse_qs(parsed.query)
                sid = query.get("sid", [""])[0]
                token = self.store.resolve_sid(sid)
                if not token:
                    self._send_json(404, {"error": "unknown session"})
                    return
                thread = self.store.get_thread(token)
                assert thread is not None
                self._send_json(
                    200,
                    {
                        "messages": thread["messages"],
                        "created_at": thread["created_at"],
                    },
                )
                return
            self._send_json(404, {"error": "not found"})
            return

        if len(path_parts) == 2 and path_parts[0] == "exchange":
            token = path_parts[1]
            query = parse_qs(parsed.query)
            # The sole delivery role is GPT. Keeping it out of the capability
            # URL makes the literal handoff compatible with constrained readers.
            role = query.get("role", ["gpt"])[0]
            assert self.store is not None
            thread, error = self.store.redeem_delivery(token, role or "")
            if error:
                status = 410 if error in ("consumed", "expired") else 404
                self._send_json(status, {"error": "delivery unavailable"})
                return
            assert thread is not None
            self._send_json(200, {"messages": thread["messages"]})
            return

        self._send_json(404, {"error": "not found"})

    def do_POST(self):
        """Only the authenticated UI may write to durable threads over HTTP."""
        parsed = urlparse(self.path)

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
            content_length = self._content_length()
            if content_length is None:
                self._send_json(413, {"error": "body too large or malformed"})
                return
            try:
                body = self.rfile.read(content_length).decode("utf-8")
            except UnicodeDecodeError:
                self._send_json(400, {"error": "invalid utf-8"})
                return
            message = self.store.add_message(token, role, body)
            if message is None:
                self._send_json(404, {"error": "unknown thread"})
                return
            self._notify(role, body)
            self._send_json(201, message)
            return

        self._send_json(404, {"error": "not found"})

    def do_OPTIONS(self):
        """No cross-origin clients exist; answer preflights without CORS grants."""
        self.send_response(204)
        self.send_header("Allow", "GET, POST, OPTIONS")
        self.end_headers()

def main():
    parser = argparse.ArgumentParser(
        description=(
            "Claude-GPT Exchange Server: durable threads and one-time delivery URLs."
        )
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
        print(f"Cleaned up {deleted} expired delivery capability(s)", file=sys.stderr)
    ExchangeHandler.store = store
    ExchangeHandler.ntfy_config = ntfy_config
    ExchangeHandler.ui_email = args.ui_email
    if args.ui_email:
        print(f"UI enabled for {args.ui_email} at /ui", file=sys.stderr)

    # Server
    server = ThreadingHTTPServer(("127.0.0.1", args.port), ExchangeHandler)
    server.daemon_threads = True
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
