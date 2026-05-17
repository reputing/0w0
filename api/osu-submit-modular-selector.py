"""
Scythe — Score Submission
POST /api/osu-submit-modular-selector.php
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from http.server import BaseHTTPRequestHandler
import json, time, urllib.parse, hashlib, math

from lib.db import (
    get_user_by_name, get_or_create_beatmap, submit_score,
    flag_score_db, update_user_stats, recalculate_rank,
    get_featured_map, fetchall, fetchval
)
from lib.pp import (
    calculate_pp, calculate_accuracy, get_rank_string, recalculate_user_pp
)


class handler(BaseHTTPRequestHandler):

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8", errors="ignore")
        data = dict(urllib.parse.parse_qsl(body))

        import asyncio
        result = asyncio.run(self._handle(data))
        self._respond(200, result)

    async def _handle(self, data: dict) -> str:
        score_str = data.get("x", "") or data.get("score", "")
        parts = score_str.split(":")
        if len(parts) < 14:
            return "error: malformed score"

        beatmap_md5 = parts[0].strip()
        username    = parts[1].strip()
        count300    = int(parts[2])
        count100    = int(parts[3])
        count50     = int(parts[4])
        countmiss   = int(parts[7])
        score_val   = int(parts[8])
        max_combo   = int(parts[9])
        is_fc       = parts[10] in ("True", "1")
        mods        = int(parts[12])
        passed      = parts[13] in ("True", "1")

        user = await get_user_by_name(username)
        if not user:
            return "error: user not found"

        beatmap = await get_or_create_beatmap(beatmap_md5)
        stars    = beatmap["diff_rating"] or 3.0
        ar       = beatmap["ar"] or 9.0
        od       = beatmap["od"] or 8.0
        bmc      = beatmap["max_combo"] or 0

        accuracy = calculate_accuracy(count300, count100, count50, countmiss)
        rank_str = get_rank_string(accuracy, countmiss, count300, count100, count50, mods)

        fm = await get_featured_map()
        is_featured = fm and fm["beatmap_md5"] == beatmap_md5

        pp = calculate_pp(
            stars=stars, accuracy=accuracy,
            max_combo=max_combo, beatmap_max_combo=bmc,
            count300=count300, count100=count100,
            count50=count50, countmiss=countmiss,
            mods=mods, ar=ar, od=od,
            is_featured_map=bool(is_featured),
            hot_streak=user["hot_streak"] or 0
        ) if passed else 0.0

        score_id = await submit_score(
            user["id"], beatmap_md5, score_val, pp, accuracy,
            max_combo, count300, count100, count50, countmiss,
            mods, rank_str, is_fc, passed
        )

        # Basic anti-cheat checks
        flags = []
        if passed:
            if bmc > 0 and max_combo > bmc:
                flags.append(f"combo exceeds max: {max_combo}>{bmc}")
            if user["pp"] > 0 and pp > user["pp"] * 2.5:
                flags.append(f"pp spike: {pp:.0f} vs {user['pp']:.0f}")
            if (user["playcount"] or 0) < 5 and pp > 400:
                flags.append(f"new acct high pp: {pp:.0f}")

        if flags:
            await flag_score_db(score_id, "; ".join(flags))

        # Update stats
        if passed:
            await _update_stats(user["id"], is_fc, countmiss > 0)

        return _build_response(user, score_id)

    def _respond(self, code: int, body: str):
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(body.encode())

    def do_GET(self):
        self._respond(405, "Method Not Allowed")


def _build_response(user, score_id) -> str:
    lines = [
        "beatmapId:0", "beatmapSetId:0",
        "beatmapPlaycount:0", "beatmapPasscount:0", "approvedDate:", "",
        "chartId:overall", "chartName:Overall Ranking", "chartEndDate:",
        f"beatmapRankingBefore:0", f"beatmapRankingAfter:0",
        f"rankedScoreBefore:{user['ranked_score']}",
        f"rankedScoreAfter:{user['ranked_score']}",
        f"totalScoreBefore:{user['total_score']}",
        f"totalScoreAfter:{user['total_score']}",
        f"playCountBefore:{user['playcount']}",
        f"accuracyBefore:{(user['accuracy'] or 0)*100:.4f}",
        f"accuracyAfter:{(user['accuracy'] or 0)*100:.4f}",
        f"rankBefore:{user['rank']}", f"rankAfter:{user['rank']}",
        "toNextRank:1", "toNextRankUser:",
        "achievements:", "achievements-new:",
        f"onlineScoreId:{score_id}",
    ]
    return "\n".join(lines)


async def _update_stats(user_id: int, is_fc: bool, had_miss: bool):
    scores = await fetchall("""
        SELECT pp, accuracy FROM scores
        WHERE user_id=$1 AND passed=TRUE ORDER BY pp DESC LIMIT 200
    """, user_id)
    playcount = await fetchval("SELECT COUNT(*) FROM scores WHERE user_id=$1", user_id)
    ranked    = await fetchval("SELECT COALESCE(SUM(score),0) FROM scores WHERE user_id=$1 AND passed=TRUE", user_id)
    total     = await fetchval("SELECT COALESCE(SUM(score),0) FROM scores WHERE user_id=$1", user_id)
    max_combo = await fetchval("SELECT COALESCE(MAX(max_combo),0) FROM scores WHERE user_id=$1 AND passed=TRUE", user_id)

    score_list = [dict(s) for s in scores]
    pp, acc = recalculate_user_pp(score_list)

    await update_user_stats(user_id, ranked, total, playcount, pp, acc, max_combo)
    await recalculate_rank(user_id)

    # Hot streak
    if is_fc:
        from lib.db import execute
        await execute("UPDATE users SET hot_streak=hot_streak+1 WHERE id=$1", user_id)
    elif had_miss:
        from lib.db import execute
        await execute("UPDATE users SET hot_streak=0 WHERE id=$1", user_id)
