"""
Scythe — Score Submission
POST /web/osu-submit-modular-selector.php

Reads multipart/form-data, AES-decrypts the encrypted `score` field
(modern osu! stable always encrypts it), parses with correct field
offsets, runs anti-cheat, and returns post-submission stat deltas.
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from http.server import BaseHTTPRequestHandler
import asyncio

from lib.db import (
    get_user_by_name, get_user_by_id, get_or_create_beatmap, submit_score,
    flag_score_db, update_user_stats, recalculate_rank,
    get_featured_map, fetchall, fetchval, execute,
)
from lib.pp import (
    calculate_pp, calculate_accuracy, get_rank_string, recalculate_user_pp,
)
from lib.scoresub import parse_multipart, decrypt_score, parse_score_string
from lib.skillset import compute_beatmap_skillset, compute_user_profile


class handler(BaseHTTPRequestHandler):

    def do_GET(self):
        self._respond(405, "Method Not Allowed")

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        ctype = self.headers.get("Content-Type", "")
        try:
            result = asyncio.run(self._handle(ctype, body))
        except Exception as e:
            result = f"error: {type(e).__name__}: {e}"
        self._respond(200, result)

    def _respond(self, code: int, body: str):
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(body.encode("utf-8", "ignore"))

    async def _handle(self, content_type: str, body: bytes) -> str:
        fields = parse_multipart(content_type, body)

        # The score field can be either:
        #   - encrypted (modern osu!): use 'score' (b64) + 'iv' (b64) + 'osuver'
        #   - raw text (older / unofficial clients): just 'score' or 'x'
        raw_score: str | None = None

        enc_score = fields.get("score")
        enc_iv    = fields.get("iv")
        osuver    = (fields.get("osuver") or b"").decode("utf-8", "ignore").strip()

        if enc_score and enc_iv and osuver:
            raw_score = decrypt_score(enc_score, enc_iv, osuver)

        if not raw_score:
            # Try legacy plaintext fallbacks
            legacy = fields.get("score") or fields.get("x")
            if legacy:
                raw_score = legacy.decode("utf-8", "ignore")

        if not raw_score:
            return "error: missing score data"

        parsed = parse_score_string(raw_score)
        if not parsed:
            return "error: malformed score string"

        # Authenticate using the password hash field 'pass' (sent by osu!)
        pw_hash = (fields.get("pass") or b"").decode("utf-8", "ignore").strip()
        user = await get_user_by_name(parsed["username"])
        if not user:
            return "error: user not found"
        if pw_hash and user["password_md5"] != pw_hash:
            return "error: bad credentials"

        # Beatmap (auto-loved so leaderboard shows up no matter what)
        beatmap = await get_or_create_beatmap(parsed["beatmap_md5"])
        stars = beatmap["diff_rating"] or 3.0
        ar    = beatmap["ar"] or 9.0
        od    = beatmap["od"] or 8.0
        bmc   = beatmap["max_combo"] or 0

        # Compute and cache beatmap skillset if not already stored
        beatmap_skillset = beatmap.get("skillset")
        if not beatmap_skillset or not isinstance(beatmap_skillset, dict):
            beatmap_skillset = compute_beatmap_skillset(dict(beatmap))
            try:
                await execute(
                    "UPDATE beatmaps SET skillset=$1::jsonb WHERE md5=$2",
                    beatmap_skillset, parsed["beatmap_md5"],
                )
            except Exception:
                pass  # non-critical

        accuracy = calculate_accuracy(
            parsed["count300"], parsed["count100"],
            parsed["count50"], parsed["countmiss"],
        )
        rank_str = get_rank_string(
            accuracy, parsed["countmiss"],
            parsed["count300"], parsed["count100"], parsed["count50"],
            parsed["mods"],
        )

        fm = await get_featured_map()
        is_featured = bool(fm and fm["beatmap_md5"] == parsed["beatmap_md5"])

        pp = 0.0
        if parsed["passed"]:
            pp = calculate_pp(
                stars=stars, accuracy=accuracy,
                max_combo=parsed["max_combo"], beatmap_max_combo=bmc,
                count300=parsed["count300"], count100=parsed["count100"],
                count50=parsed["count50"], countmiss=parsed["countmiss"],
                mods=parsed["mods"], ar=ar, od=od,
                is_featured_map=is_featured,
                hot_streak=user["hot_streak"] or 0,
            )

        # Snapshot "before" values BEFORE we run _update_stats
        before = {
            "ranked_score": user["ranked_score"] or 0,
            "total_score":  user["total_score"] or 0,
            "playcount":    user["playcount"] or 0,
            "rank":         user["rank"] or 0,
            "accuracy":     user["accuracy"] or 0.0,
            "pp":           user["pp"] or 0.0,
        }

        score_id = await submit_score(
            user["id"], parsed["beatmap_md5"], parsed["score"], pp, accuracy,
            parsed["max_combo"],
            parsed["count300"], parsed["count100"], parsed["count50"], parsed["countmiss"],
            parsed["mods"], rank_str, parsed["is_fc"], parsed["passed"],
        )

        # Anti-cheat
        flags = []
        if parsed["passed"]:
            if bmc > 0 and parsed["max_combo"] > bmc:
                flags.append(f"combo>{bmc}")
            if before["pp"] > 0 and pp > before["pp"] * 2.5:
                flags.append(f"pp_spike:{pp:.0f}vs{before['pp']:.0f}")
            if before["playcount"] < 5 and pp > 400:
                flags.append(f"new_acct_pp:{pp:.0f}")
        if flags:
            await flag_score_db(score_id, "; ".join(flags))

        if parsed["passed"]:
            await _update_stats(user["id"], parsed["is_fc"], parsed["countmiss"] > 0)
            # Recalculate user's skill profile from their top plays
            await _update_skill_profile(user["id"])

        # Re-fetch fresh stats so the response shows real deltas
        fresh = await get_user_by_id(user["id"])
        return _build_response(before, fresh, score_id)


def _build_response(before, after, score_id) -> str:
    lines = [
        "beatmapId:0", "beatmapSetId:0",
        "beatmapPlaycount:0", "beatmapPasscount:0", "approvedDate:", "",
        "chartId:overall", "chartName:Overall Ranking", "chartEndDate:",
        "beatmapRankingBefore:0", "beatmapRankingAfter:0",
        f"rankedScoreBefore:{before['ranked_score']}",
        f"rankedScoreAfter:{after['ranked_score']}",
        f"totalScoreBefore:{before['total_score']}",
        f"totalScoreAfter:{after['total_score']}",
        f"playCountBefore:{before['playcount']}",
        f"accuracyBefore:{(before['accuracy'] or 0)*100:.4f}",
        f"accuracyAfter:{(after['accuracy'] or 0)*100:.4f}",
        f"rankBefore:{before['rank']}",
        f"rankAfter:{after['rank']}",
        f"ppBefore:{before['pp']:.4f}",
        f"ppAfter:{after['pp']:.4f}",
        "toNextRank:1", "toNextRankUser:",
        "achievements:", "achievements-new:",
        f"onlineScoreId:{score_id}",
    ]
    return "\n".join(lines)


async def _update_stats(user_id: int, is_fc: bool, had_miss: bool):
    scores = await fetchall(
        "SELECT pp, accuracy FROM scores "
        "WHERE user_id=$1 AND passed=TRUE ORDER BY pp DESC LIMIT 200",
        user_id,
    )
    playcount = await fetchval("SELECT COUNT(*) FROM scores WHERE user_id=$1", user_id)
    ranked = await fetchval(
        "SELECT COALESCE(SUM(score),0) FROM scores WHERE user_id=$1 AND passed=TRUE",
        user_id,
    )
    total = await fetchval(
        "SELECT COALESCE(SUM(score),0) FROM scores WHERE user_id=$1",
        user_id,
    )
    max_combo = await fetchval(
        "SELECT COALESCE(MAX(max_combo),0) FROM scores WHERE user_id=$1 AND passed=TRUE",
        user_id,
    )

    pp, acc = recalculate_user_pp([dict(s) for s in scores])
    await update_user_stats(user_id, ranked, total, playcount, pp, acc, max_combo)
    await recalculate_rank(user_id)

    if is_fc:
        await execute("UPDATE users SET hot_streak=hot_streak+1 WHERE id=$1", user_id)
    elif had_miss:
        await execute("UPDATE users SET hot_streak=0 WHERE id=$1", user_id)



async def _update_skill_profile(user_id: int):
    """Recompute user skill profile from top 100 passed plays with beatmap skillsets."""
    try:
        rows = await fetchall(
            """
            SELECT s.accuracy, s.is_fc, s.mods, b.skillset
            FROM scores s
            JOIN beatmaps b ON b.md5 = s.beatmap_md5
            WHERE s.user_id=$1 AND s.passed=TRUE AND b.skillset IS NOT NULL
            ORDER BY s.pp DESC LIMIT 100
            """,
            user_id,
        )
        score_data = []
        for r in rows:
            sk = r["skillset"]
            if sk and isinstance(sk, dict):
                score_data.append({
                    "accuracy": r["accuracy"],
                    "is_fc": r["is_fc"],
                    "mods": r["mods"],
                    "skillset": sk,
                })
        if score_data:
            profile = compute_user_profile(score_data)
            await execute(
                "UPDATE users SET skill_profile=$1::jsonb WHERE id=$2",
                profile, user_id,
            )
    except Exception as e:
        print(f"[SCORE] skill profile update failed: {e}", flush=True)
