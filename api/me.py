"""
Scythe — Authenticated Web Settings & Friends
All endpoints require a valid `scythe_session` cookie.

  PATCH  /me/settings        — body: {avatar_url?, banner_url?, bio?, country?}
  POST   /me/password        — body: {old_password, new_password}
  POST   /me/friends         — body: {user_id}    add friend (idempotent)
  DELETE /me/friends/<id>    — remove friend
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from http.server import BaseHTTPRequestHandler
import json, hashlib, asyncio, urllib.parse
from http.cookies import SimpleCookie

from lib.db import (
    get_user_by_session,
    update_user_settings, update_user_password,
    add_friend, remove_friend,
)


COOKIE_NAME = "scythe_session"


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


def _is_valid_url(s: str | None) -> bool:
    if not s:
        return True  # allow clearing
    if len(s) > 500:
        return False
    return s.startswith("http://") or s.startswith("https://")


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

    def _json(self, obj, code=200):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(json.dumps(obj).encode("utf-8"))

    def _route(self) -> str:
        return urllib.parse.urlparse(self.path).path.rstrip("/") or "/"

    async def _require_user(self):
        token = _read_session_cookie(self.headers)
        if not token:
            self._json({"error": "not_authenticated"}, 401)
            return None
        user = await get_user_by_session(token)
        if not user:
            self._json({"error": "not_authenticated"}, 401)
            return None
        return user

    # ── HTTP verbs ────────────────────────────────────────────────────────
    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin",
                         self.headers.get("Origin", "*"))
        self.send_header("Access-Control-Allow-Credentials", "true")
        self.send_header("Access-Control-Allow-Methods",
                         "GET, POST, PATCH, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    # Vercel maps every method onto a single handler. We dispatch by method here.
    def do_PATCH(self): asyncio.run(self._dispatch("PATCH"))
    def do_POST(self):  asyncio.run(self._dispatch("POST"))
    def do_DELETE(self):asyncio.run(self._dispatch("DELETE"))
    def do_GET(self):   self._json({"error": "method_not_allowed"}, 405)

    async def _dispatch(self, method: str):
        path = self._route()
        body = self._read_body() if method in ("POST", "PATCH") else {}

        user = await self._require_user()
        if not user:
            return  # response already written

        # ── PATCH /me/settings ────────────────────────────────────────────
        if method == "PATCH" and path == "/me/settings":
            avatar = body.get("avatar_url")
            banner = body.get("banner_url")
            bio    = body.get("bio")
            country= body.get("country")
            if avatar is not None and not _is_valid_url(avatar):
                return self._json({"error": "bad_avatar_url"}, 400)
            if banner is not None and not _is_valid_url(banner):
                return self._json({"error": "bad_banner_url"}, 400)
            if bio is not None and len(str(bio)) > 500:
                return self._json({"error": "bio_too_long"}, 400)
            updated = await update_user_settings(
                user["id"],
                avatar_url=avatar,
                banner_url=banner,
                bio=bio,
                country=country,
            )
            return self._json({
                "ok": True,
                "user": {
                    "id": updated.get("id"),
                    "username": updated.get("username"),
                    "country": updated.get("country"),
                    "avatar_url": updated.get("avatar_url"),
                    "banner_url": updated.get("banner_url"),
                    "bio": updated.get("bio") or "",
                },
            })

        # ── POST /me/password ─────────────────────────────────────────────
        if method == "POST" and path == "/me/password":
            old_pw = body.get("old_password") or ""
            new_pw = body.get("new_password") or ""
            if not old_pw or not new_pw:
                return self._json({"error": "missing_fields"}, 400)
            if len(new_pw) < 6:
                return self._json({"error": "password_too_short"}, 400)
            ok, err = await update_user_password(
                user["id"],
                hashlib.md5(old_pw.encode()).hexdigest(),
                hashlib.md5(new_pw.encode()).hexdigest(),
            )
            if not ok:
                code = 401 if err == "bad_password" else 400
                return self._json({"error": err or "password_change_failed"}, code)
            return self._json({"ok": True, "logout_required": True})

        # ── POST /me/friends ──────────────────────────────────────────────
        if method == "POST" and path == "/me/friends":
            try:
                target = int(body.get("user_id"))
            except (TypeError, ValueError):
                return self._json({"error": "bad_user_id"}, 400)
            ok, err = await add_friend(user["id"], target)
            if not ok:
                code = 404 if err == "target_not_found" else 400
                return self._json({"error": err}, code)
            return self._json({"ok": True, "friend_id": target})

        # ── DELETE /me/friends/<id> ───────────────────────────────────────
        if method == "DELETE" and path.startswith("/me/friends/"):
            try:
                target = int(path.rsplit("/", 1)[1])
            except (TypeError, ValueError):
                return self._json({"error": "bad_user_id"}, 400)
            await remove_friend(user["id"], target)
            return self._json({"ok": True, "friend_id": target})

        return self._json({"error": "not_found"}, 404)
