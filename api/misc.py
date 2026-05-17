"""
Scythe — Misc osu! client endpoints
Handles all the small endpoints osu! hits on startup / during play.
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from http.server import BaseHTTPRequestHandler
import urllib.parse, json, asyncio

from lib.db import get_user_by_name, get_featured_map, vote_map


class handler(BaseHTTPRequestHandler):

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        params = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(self.path).query))
        body = asyncio.run(self._get(path, params))
        self._respond(200, body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode("utf-8", errors="ignore")
        data = dict(urllib.parse.parse_qsl(raw))
        path = urllib.parse.urlparse(self.path).path
        body = asyncio.run(self._post(path, data))
        self._respond(200, body)

    async def _get(self, path: str, params: dict) -> str:
        if "bancho_connect" in path:
            return "scythe"
        if "osu-seasonal" in path or "getseasonal" in path:
            return '["https://i.imgur.com/scythe_bg.jpg"]'
        if "checktweets" in path:
            return "0"
        if "lastfm" in path:
            return "-3"
        if "osu-error" in path:
            return ""
        if "peppy" in path:
            return "This is Scythe."
        if "difficulty-rating" in path:
            return "0"
        if "osu-search" in path:
            return "-1\nUse the osu! website to find maps."

        # Global leaderboard (public JSON)
        if path.endswith("/api/v1/leaderboard"):
            from lib.db import fetchall
            rows = await fetchall("""
                SELECT id, username, pp, rank, accuracy, playcount,
                       ranked_score, country, status
                FROM users WHERE status != 1
                ORDER BY pp DESC LIMIT 50
            """)
            result = []
            for i, r in enumerate(rows):
                result.append({
                    "rank": i + 1,
                    "id": r["id"],
                    "username": r["username"],
                    "pp": r["pp"],
                    "accuracy": r["accuracy"],
                    "playcount": r["playcount"],
                    "country": r["country"],
                })
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(result).encode())
            return None

        # User profile
        if "/u/" in path:
            try:
                uid = int(path.split("/u/")[1].split("/")[0])
                from lib.db import get_user_by_id
                user = await get_user_by_id(uid)
                if user:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps({
                        "id": user["id"],
                        "username": user["username"],
                        "pp": user["pp"],
                        "rank": user["rank"],
                        "accuracy": user["accuracy"],
                        "playcount": user["playcount"],
                        "ranked_score": user["ranked_score"],
                        "hot_streak": user["hot_streak"],
                        "country": user["country"],
                    }).encode())
                    return None
            except Exception:
                pass

        return ""

    async def _post(self, path: str, data: dict) -> str:
        if "getbeatmapinfo" in path:
            return ""
        if "vote" in path:
            username = data.get("us", "")
            password = data.get("ha", "")
            md5      = data.get("c", "")
            vote     = int(data.get("v", 0))
            user = await get_user_by_name(username)
            if user and user["password_md5"] == password and md5:
                await vote_map(user["id"], md5, vote)
                return "ok"
        return ""

    def _respond(self, code, body):
        if body is None:
            return
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(body.encode())
