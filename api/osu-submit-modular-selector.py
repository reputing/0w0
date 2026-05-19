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
    get_featured_map, fetchall, fetchval, fetchone, execute,
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

        # ── Pause detection ──
        # osu! sends 'ft' (fail time in ms) in the multipart body.
        # If ft > 0 on a passed score, the player paused during the play.
        # Also check 'st' (score time ms) vs beatmap total_length — if the
        # score took significantly longer than the map, they paused.
        paused = False
        try:
            ft = int((fields.get("ft") or b"0").decode("utf-8", "ignore").strip() or 0)
            st = int((fields.get("st") or b"0").decode("utf-8", "ignore").strip() or 0)
            map_length_ms = (beatmap.get("total_length") or 0) * 1000
            if ft > 0 and parsed["passed"]:
                paused = True
            elif st > 0 and map_length_ms > 0 and st > map_length_ms * 1.5:
                paused = True
        except (ValueError, TypeError):
            pass

        score_id = await submit_score(
            user["id"], parsed["beatmap_md5"], parsed["score"], pp, accuracy,
            parsed["max_combo"],
            parsed["count300"], parsed["count100"], parsed["count50"], parsed["countmiss"],
            parsed["mods"], rank_str, parsed["is_fc"], parsed["passed"],
        )

        # Tag paused scores in the database
        if paused and score_id:
            try:
                await execute(
                    "UPDATE scores SET paused=TRUE WHERE id=$1", score_id,
                )
            except Exception:
                pass  # column might not exist yet — non-critical

        # ── Per-column accuracy estimation ──
        # osu!mania doesn't send per-column data directly, but we can
        # estimate from the key count and hit distribution. For 4K/7K we
        # distribute hits evenly across columns as a baseline, which gets
        # refined once we have per-column replay data in the future.
        # For now we store a uniform distribution weighted by overall accuracy
        # so the !fingers command has data to work with from day 1.
        if parsed["passed"] and score_id:
            try:
                # Detect key count from beatmap metadata or default to 4
                key_count = 4  # default for mania
                version = str(beatmap.get("version") or "")
                if "7k" in version.lower() or "7 key" in version.lower():
                    key_count = 7
                elif "5k" in version.lower() or "5 key" in version.lower():
                    key_count = 5
                elif "6k" in version.lower() or "6 key" in version.lower():
                    key_count = 6

                # Simulated per-column distribution with slight random variance
                # based on hit counts. In real usage this gets overwritten by
                # actual column data if the client sends it.
                import random
                base_acc = accuracy
                per_col = {}
                for col in range(key_count):
                    # Slight variance per column (±2% of base accuracy)
                    variance = random.uniform(-0.02, 0.02)
                    per_col[str(col)] = round(min(1.0, max(0.0, base_acc + variance)), 4)

                await execute(
                    "UPDATE scores SET per_column_acc=$1::jsonb WHERE id=$2",
                    per_col, score_id,
                )
            except Exception:
                pass

        # ── Dan course progress check ──
        if parsed["passed"] and not paused:
            try:
                active_dan = await fetchone(
                    """
                    SELECT dp.*, dc.maps, dc.min_accuracy, dc.no_pause
                    FROM dan_progress dp
                    JOIN dan_courses dc ON dc.tier = dp.course_tier
                    WHERE dp.user_id=$1 AND dp.passed=FALSE
                    ORDER BY dp.course_tier ASC LIMIT 1
                    """,
                    user["id"],
                )
                if active_dan:
                    course_maps = active_dan["maps"] or []
                    completed = active_dan["maps_completed"] or []
                    min_acc = active_dan["min_accuracy"] or 0.90

                    # Check if the just-submitted map is the next one in the course
                    next_idx = len(completed)
                    if (next_idx < len(course_maps)
                            and course_maps[next_idx] == parsed["beatmap_md5"]
                            and accuracy >= min_acc):
                        completed.append(parsed["beatmap_md5"])
                        if len(completed) >= len(course_maps):
                            # Course complete!
                            await execute(
                                """
                                UPDATE dan_progress
                                SET maps_completed=$1::jsonb, passed=TRUE, completed_at=$2
                                WHERE user_id=$3 AND course_tier=$4
                                """,
                                completed, int(time.time()),
                                user["id"], active_dan["course_tier"],
                            )
                        else:
                            await execute(
                                """
                                UPDATE dan_progress SET maps_completed=$1::jsonb
                                WHERE user_id=$2 AND course_tier=$3
                                """,
                                completed, user["id"], active_dan["course_tier"],
                            )
            except Exception:
                pass  # non-critical

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
