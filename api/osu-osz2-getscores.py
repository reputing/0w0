"""
Scythe — Leaderboard
GET /web/osu-osz2-getscores.php

Always returns ranked_status=2 (Ranked) so osu! shows a leaderboard
on every map. (We force this regardless of the map's actual status.)
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from http.server import BaseHTTPRequestHandler
import urllib.parse
import asyncio
import json

from lib.db import get_user_by_name, get_or_create_beatmap, get_leaderboard


# osu! ranked-status codes that *show* a leaderboard:
#   2 = Ranked, 3 = Approved, 4 = Qualified, 5 = Loved
# We use 2 (Ranked) — it gives full leaderboard + PP submission UI without
# the "Loved" tag that osu! sometimes hides scores under.
LEADERBOARD_STATUS = 2


class handler(BaseHTTPRequestHandler):

    def do_GET(self):
        params = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(self.path).query))
        try:
            result = asyncio.run(self._handle(params))
        except Exception as e:
            import traceback
            traceback.print_exc()
            result = f"-1\nServer error: {e}"
        print(f"[GETSCORES] md5={params.get('c','?')[:8]} user={params.get('us','?')} → {len(result)} bytes, first_line={result.split(chr(10))[0]}", flush=True)
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(result.encode("utf-8", "ignore"))

    async def _handle(self, params: dict) -> str:
        beatmap_md5 = params.get("c", "")
        username    = params.get("us", "")
        password_md5 = params.get("ha", "")
        lb_type     = int(params.get("v", 1) or 1)

        if not beatmap_md5:
            return "-1|false"

        user = await get_user_by_name(username)
        if not user or user["password_md5"] != password_md5:
            return "-1|false"

        beatmap = await get_or_create_beatmap(beatmap_md5)

        # Friends-only filter: lb_type 4 = Friends
        friends_only = (lb_type == 4)
        friends_list = None
        if friends_only:
            f = user["friends"]
            # JSONB codec gives us a list, but be defensive.
            if isinstance(f, list):
                friends_list = [int(x) for x in f if isinstance(x, (int, str)) and str(x).isdigit()]
            elif isinstance(f, str):
                try:
                    friends_list = [int(x) for x in (json.loads(f) or [])]
                except Exception:
                    friends_list = []
            else:
                friends_list = []

        rows, ghost = await get_leaderboard(
            beatmap_md5,
            requesting_user_id=user["id"],
            friends_only=friends_only,
            friends_list=friends_list,
        )

        artist  = beatmap["artist"]  or "Unknown"
        title   = beatmap["title"]   or "Unknown"
        version = beatmap["version"] or "Unknown"
        bm_id   = beatmap["beatmap_id"] or 0
        bs_id   = beatmap["beatmapset_id"] or 0

        # Response format:
        #   line 0: status|server_has_osz|beatmap_id|beatmapset_id|count
        #   line 1: online_offset
        #   line 2: "Artist - Title [Version]"
        #   line 3: rating (float)
        #   line 4: personal best (or empty string if none)
        #   line 5..N: scores
        out = []
        out.append(f"{LEADERBOARD_STATUS}|false|{bm_id}|{bs_id}|{len(rows)}")
        out.append("0")
        out.append(f"{artist} - {title} [{version}]")
        out.append("10.0")

        # Personal best — find user's score in rows or use ghost
        pb = next((r for r in rows if r["user_id"] == user["id"]), ghost)
        out.append(_fmt(pb, 0) if pb else "")

        # Top scores
        for i, row in enumerate(rows[:50]):
            out.append(_fmt(row, i + 1))

        # Ghost score appended only if the user isn't in the top 50
        # AND we have a ghost (already covered by pb logic above, so skip here).

        return "\n".join(out)


def _fmt(row, pos: int, ghost: bool = False) -> str:
    name = ("[Ghost] " + row["username"]) if ghost else row["username"]
    # Mark paused scores with [P] suffix so players know
    if row.get("paused"):
        name += " [P]"
    # Last field is "has_replay" (1/0). We have no replays → 0.
    return (
        f"{row['id']}|{name}|{row['score']}|{row['max_combo']}|"
        f"{row['count50']}|{row['count100']}|{row['count300']}|"
        f"{row['countmiss']}|0|0|"
        f"{1 if row['is_fc'] else 0}|{row['mods']}|"
        f"{row['user_id']}|{pos}|{row['submitted_at']}|0"
    )
