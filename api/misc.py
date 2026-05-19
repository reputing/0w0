"""
Scythe — Misc osu! client endpoints + website API + avatars

Handles:
  - Tiny stub endpoints osu! pings on startup (bancho_connect, lastfm, ...)
  - Avatars on a.<domain>/<userid> and /avatar/<userid>
    Serves a per-user DiceBear pixel-art image so every player has a custom
    profile picture the moment they connect. Supports user-uploaded URLs
    via users.avatar_url override.
  - Public website APIs: /api/v1/leaderboard, /api/v1/online
  - Public profile JSON at /u/<userid>
  - Map voting via POST /web/osu-rate.php
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from http.server import BaseHTTPRequestHandler
import urllib.parse
import json
import asyncio

from lib.db import (
    get_user_by_name, get_user_by_id, get_avatar_url,
    fetchall, online_count, vote_map,
)


# Default avatar generator. DiceBear gives a unique deterministic image
# per-seed with no auth, no rate limit issues for our scale.
def _default_avatar_url(seed: int | str) -> str:
    return f"https://api.dicebear.com/9.x/pixel-art/png?seed={seed}&size=256&backgroundType=gradientLinear"


class handler(BaseHTTPRequestHandler):

    # ── Routing ────────────────────────────────────────────────────────────
    def do_GET(self):
        try:
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            params = dict(urllib.parse.parse_qsl(parsed.query))
            host = (self.headers.get("Host") or "").split(":")[0].lower()
            asyncio.run(self._dispatch_get(host, path, params))
        except Exception as e:
            self._text(500, f"error: {e}")

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length).decode("utf-8", errors="ignore")
            data = dict(urllib.parse.parse_qsl(raw))
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            host = (self.headers.get("Host") or "").split(":")[0].lower()
            asyncio.run(self._dispatch_post(host, path, data))
        except Exception as e:
            self._text(500, f"error: {e}")

    # ── GET dispatch ───────────────────────────────────────────────────────
    async def _dispatch_get(self, host: str, path: str, params: dict):
        # ── Avatars: a.<domain>/<userid> or /avatar/<userid> ──
        if host.startswith("a.") or path.startswith("/avatar/"):
            if path.startswith("/avatar/"):
                ident = path.split("/avatar/", 1)[1]
            else:
                ident = path.lstrip("/")
            ident = ident.split("/")[0].split(".")[0]  # strip ".jpg" etc

            # Try to resolve user_id
            url = None
            if ident.isdigit():
                custom = await get_avatar_url(int(ident))
                url = custom or _default_avatar_url(ident)
            else:
                url = _default_avatar_url(ident or "default")

            self.send_response(302)
            self.send_header("Location", url)
            self.send_header("Cache-Control", "public, max-age=3600")
            self.end_headers()
            return

        # ── Website APIs ──
        if path == "/api/v1/online":
            count = await online_count()
            self._json({"online": count})
            return

        if path == "/api/v1/leaderboard":
            rows = await fetchall(
                """
                SELECT id, username, pp, rank, accuracy, playcount,
                       ranked_score, country, status
                FROM users WHERE status != 1
                ORDER BY pp DESC LIMIT 50
                """
            )
            data = [
                {
                    "rank": i + 1,
                    "id": r["id"],
                    "username": r["username"],
                    "pp": r["pp"],
                    "accuracy": r["accuracy"],
                    "playcount": r["playcount"],
                    "country": r["country"],
                }
                for i, r in enumerate(rows)
            ]
            self._json(data)
            return

        # ── User profile JSON ──
        if path.startswith("/u/"):
            try:
                uid = int(path.split("/u/", 1)[1].split("/")[0])
                user = await get_user_by_id(uid)
                if user:
                    self._json({
                        "id": user["id"],
                        "username": user["username"],
                        "pp": user["pp"],
                        "rank": user["rank"],
                        "accuracy": user["accuracy"],
                        "playcount": user["playcount"],
                        "ranked_score": user["ranked_score"],
                        "hot_streak": user["hot_streak"],
                        "country": user["country"],
                        "avatar_url": user["avatar_url"] or _default_avatar_url(user["id"]),
                    })
                    return
            except Exception:
                pass
            self._json({"error": "not_found"}, 404)
            return

        # ── osu! client stub endpoints ──
        if "bancho_connect" in path:
            return self._text(200, "scythe")
        if "osu-seasonal" in path or "getseasonal" in path:
            return self._text(200, "[]")
        if "checktweets" in path:
            return self._text(200, "0")
        if "lastfm" in path:
            return self._text(200, "-3")
        if "osu-error" in path:
            return self._text(200, "")
        if "peppy" in path:
            return self._text(200, "Welcome to Scythe.")
        if "difficulty-rating" in path:
            return self._text(200, "0")
        if "osu-search" in path:
            return self._text(200, "-1\nUse the osu! website to find maps.")
        if "check-updates" in path:
            return self._text(200, "[]")

        self._text(200, "")

    # ── POST dispatch ──────────────────────────────────────────────────────
    async def _dispatch_post(self, host: str, path: str, data: dict):
        if "getbeatmapinfo" in path:
            return self._text(200, "")

        if "osu-rate" in path or "vote" in path:
            username = data.get("us", "")
            password = data.get("ha", "")
            md5      = data.get("c", "")
            try:
                v = int(data.get("v", 0))
            except (TypeError, ValueError):
                v = 0
            user = await get_user_by_name(username)
            if user and user["password_md5"] == password and md5:
                await vote_map(user["id"], md5, v)
                return self._text(200, "ok")
            return self._text(200, "auth fail")

        return self._text(200, "")

    # ── Response helpers ───────────────────────────────────────────────────
    def _text(self, code: int, body: str):
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body.encode("utf-8", "ignore"))

    def _json(self, obj, code: int = 200):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(obj).encode("utf-8"))
