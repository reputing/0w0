"""
Scythe — Leaderboard
GET /api/osu-osz2-getscores.php
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from http.server import BaseHTTPRequestHandler
import urllib.parse, json

from lib.db import get_user_by_name, get_or_create_beatmap, get_leaderboard, get_featured_map


class handler(BaseHTTPRequestHandler):

    def do_GET(self):
        params = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(self.path).query))
        import asyncio
        result = asyncio.run(self._handle(params))
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(result.encode())

    async def _handle(self, params: dict) -> str:
        beatmap_md5 = params.get("c", "")
        username    = params.get("us", "")
        password_md5 = params.get("ha", "")
        lb_type     = int(params.get("v", 1))

        if not beatmap_md5:
            return "-1"

        user = await get_user_by_name(username)
        if not user or user["password_md5"] != password_md5:
            return "-1"

        beatmap = await get_or_create_beatmap(beatmap_md5)

        # Always return status 5 (Loved) — forces leaderboard on every map
        status = 5

        friends_only = lb_type == 4
        friends_list = None
        if friends_only:
            friends_list = user["friends"] or []

        rows, ghost = await get_leaderboard(
            beatmap_md5,
            requesting_user_id=user["id"],
            friends_only=friends_only,
            friends_list=friends_list
        )

        lines = []
        lines.append(f"{status}|false|{beatmap['beatmap_id']}|{beatmap['beatmapset_id']}|{len(rows)}")
        artist  = beatmap["artist"] or "Unknown"
        title   = beatmap["title"] or "Unknown"
        version = beatmap["version"] or "Unknown"
        lines.append(f"0\n{artist} - {title} [{version}]\n10.0")

        # Personal best
        pb = next((r for r in rows if r["user_id"] == user["id"]), ghost)
        lines.append(_fmt(pb, 0) if pb else "")

        # Global scores
        for i, row in enumerate(rows[:50]):
            lines.append(_fmt(row, i + 1))

        # Ghost score appended at end if not in top 50
        if ghost:
            lines.append(_fmt(ghost, 0, ghost=True))

        return "\n".join(lines)


def _fmt(row, pos: int, ghost=False) -> str:
    name = ("[Ghost] " + row["username"]) if ghost else row["username"]
    return (
        f"{row['id']}|{name}|{row['score']}|{row['max_combo']}|"
        f"{row['count50']}|{row['count100']}|{row['count300']}|"
        f"{row['countmiss']}|0|0|"
        f"{1 if row['is_fc'] else 0}|{row['mods']}|"
        f"{row['user_id']}|{pos}|{row['submitted_at']}|1"
    )
