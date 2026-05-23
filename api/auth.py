"""
Scythe — Web Auth
  POST /login   — body: {username, password}; sets HttpOnly cookie
  POST /logout  — clears session
  GET  /me      — returns current user JSON (or 401)

Cookie:
  Name:   scythe_session
  Value:  64-hex token (random 32 bytes)
  Flags:  HttpOnly, Secure, SameSite=Lax, Path=/
  TTL:    30 days
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from http.server import BaseHTTPRequestHandler
import json, hashlib, asyncio, secrets, urllib.parse
from http.cookies import SimpleCookie

from lib.db import (
    get_user_by_name,
    create_web_session, get_user_by_session, delete_web_session,
)


COOKIE_NAME = "scythe_session"
COOKIE_TTL  = 30 * 86400  # 30 days


def _read_session_cookie(headers) -> str | None:
    raw = headers.get("Cookie") or headers.get("cookie")
    if not raw:
        return None
    try:
        c = SimpleCookie()
        c.load(raw)
        m = c.get(COOKIE_NAME)
        return m.value if m else None
    except Exception:
        return None


def _set_session_cookie(send_header, token: str, max_age: int = COOKIE_TTL):
    parts = [
        f"{COOKIE_NAME}={token}",
        f"Max-Age={max_age}",
        "Path=/",
        "HttpOnly",
        "Secure",
        "SameSite=Lax",
    ]
    send_header("Set-Cookie", "; ".join(parts))


def _clear_session_cookie(send_header):
    parts = [
        f"{COOKIE_NAME}=",
        "Max-Age=0",
        "Path=/",
        "HttpOnly",
        "Secure",
        "SameSite=Lax",
    ]
    send_header("Set-Cookie", "; ".join(parts))


def _public_user(user) -> dict:
    return {
        "id": user["id"],
        "username": user["username"],
        "email": user["email"],
        "country": user["country"],
        "pp": user["pp"],
        "rank": user["rank"],
        "accuracy": user["accuracy"],
        "playcount": user["playcount"],
        "avatar_url": user["avatar_url"],
        "banner_url": user.get("banner_url"),
        "bio": user.get("bio") or "",
        "status": user["status"],
        "friends": user.get("friends") or [],
    }


class handler(BaseHTTPRequestHandler):

    # ── helpers ───────────────────────────────────────────────────────────
    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length).decode("utf-8", errors="ignore") if length else ""
        ctype = (self.headers.get("Content-Type") or "").lower()
        if "application/json" in ctype:
            try:
                return json.loads(raw or "{}")
            except Exception:
                return {}
        return dict(urllib.parse.parse_qsl(raw))

    def _json(self, obj, code=200, set_cookie_token: str | None = None,
              clear_cookie: bool = False):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        if set_cookie_token:
            _set_session_cookie(self.send_header, set_cookie_token)
        if clear_cookie:
            _clear_session_cookie(self.send_header)
        self.end_headers()
        self.wfile.write(json.dumps(obj).encode("utf-8"))

    def _route(self) -> str:
        return urllib.parse.urlparse(self.path).path.rstrip("/") or "/"

    # ── HTTP verbs ────────────────────────────────────────────────────────
    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin",
                         self.headers.get("Origin", "*"))
        self.send_header("Access-Control-Allow-Credentials", "true")
        self.send_header("Access-Control-Allow-Methods",
                         "GET, POST, PATCH, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers",
                         "Content-Type")
        self.end_headers()

    def do_GET(self):
        path = self._route()
        if path == "/me":
            asyncio.run(self._me())
            return
        self._json({"error": "not_found"}, 404)

    def do_POST(self):
        path = self._route()
        body = self._read_body()
        if path == "/login":
            asyncio.run(self._login(body))
        elif path == "/logout":
            asyncio.run(self._logout())
        else:
            self._json({"error": "not_found"}, 404)

    # ── Handlers ──────────────────────────────────────────────────────────
    async def _login(self, body: dict):
        username = (body.get("username") or "").strip()
        password = (body.get("password") or "")
        if not username or not password:
            return self._json({"error": "missing_fields"}, 400)

        user = await get_user_by_name(username)
        if not user:
            return self._json({"error": "invalid_credentials"}, 401)

        pw_md5 = hashlib.md5(password.encode()).hexdigest()
        if user["password_md5"] != pw_md5:
            return self._json({"error": "invalid_credentials"}, 401)

        if user["status"] == 1:
            return self._json({"error": "account_restricted"}, 403)

        token = secrets.token_hex(32)
        ip = (
            self.headers.get("x-forwarded-for", "").split(",")[0].strip()
            or self.client_address[0]
        )
        ua = self.headers.get("User-Agent", "")
        await create_web_session(user["id"], token, COOKIE_TTL, ip, ua)
        return self._json({"user": _public_user(user)},
                          set_cookie_token=token)

    async def _logout(self):
        token = _read_session_cookie(self.headers)
        if token:
            await delete_web_session(token)
        return self._json({"ok": True}, clear_cookie=True)

    async def _me(self):
        token = _read_session_cookie(self.headers)
        if not token:
            return self._json({"error": "not_authenticated"}, 401)
        user = await get_user_by_session(token)
        if not user:
            # stale/expired cookie — clear it client-side
            return self._json({"error": "not_authenticated"}, 401,
                              clear_cookie=True)
        return self._json({"user": _public_user(user)})
