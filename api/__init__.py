"""
Scythe — Web API functions used by both Vercel handlers AND the integrated
bancho server (when running locally). These use the bancho DB pool directly.
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import json
import time

from lib.db import (
    get_user_by_name, get_user_by_id, get_or_create_beatmap, submit_score,
    flag_score_db, update_user_stats, recalculate_rank,
    get_featured_map, fetchall, fetchval, fetchone, execute, get_leaderboard,
)
from lib.pp import (
    calculate_pp, calculate_accuracy, get_rank_string, recalculate_user_pp,
)
from lib.scoresub import parse_multipart, decrypt_score, parse_score_string
from lib.skillset import compute_beatmap_skillset, compute_user_profile


async def _getscores(params: dict) -> str:
    """Core leaderboard logic — used by both Vercel handler and bancho."""
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

    friends_only = (lb_type == 4)
    friends_list = None
    if friends_only:
        f = user["friends"]
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

    out = []
    out.append(f"2|false|{bm_id}|{bs_id}|{len(rows)}")
    out.append("0")
    out.append(f"{artist} - {title} [{version}]")
    out.append("10.0")

    pb = next((r for r in rows if r["user_id"] == user["id"]), ghost)
    out.append(_fmt_score(pb, 0) if pb else "")

    for i, row in enumerate(rows[:50]):
        out.append(_fmt_score(row, i + 1))

    return "\n".join(out)


async def _submit_score(content_type: str, body: bytes) -> str:
    """Core score submission logic."""
    fields = parse_multipart(content_type, body)

    raw_score = None
    enc_score = fields.get("score")
    enc_iv    = fields.get("iv")
    osuver    = (fields.get("osuver") or b"").decode("utf-8", "ignore").strip()

    if enc_score and enc_iv and osuver:
        raw_score = decrypt_score(enc_score, enc_iv, osuver)

    if not raw_score:
        legacy = fields.get("score") or fields.get("x")
        if legacy:
            raw_score = legacy.decode("utf-8", "ignore")

    if not raw_score:
        return "error: missing score data"

    parsed = parse_score_string(raw_score)
    if not parsed:
        return "error: malformed score string"

    pw_hash = (fields.get("pass") or b"").decode("utf-8", "ignore").strip()
    user = await get_user_by_name(parsed["username"])
    if not user:
        return "error: user not found"
    if pw_hash and user["password_md5"] != pw_hash:
        return "error: bad credentials"

    beatmap = await get_or_create_beatmap(parsed["beatmap_md5"])

    # Try to fetch beatmap metadata from osu! API if we have a placeholder
    if not beatmap.get("title") or beatmap["title"] == "":
        try:
            from lib.osu_api import fetch_and_update_beatmap
            await fetch_and_update_beatmap(parsed["beatmap_md5"], execute, fetchone)
            beatmap = await fetchone("SELECT * FROM beatmaps WHERE md5=$1", parsed["beatmap_md5"]) or beatmap
        except Exception:
            pass

    stars = beatmap["diff_rating"] or 3.0
    ar    = beatmap["ar"] or 9.0
    od    = beatmap["od"] or 8.0
    bmc   = beatmap["max_combo"] or 0

    # Compute beatmap skillset if needed
    beatmap_skillset = beatmap.get("skillset")
    if not beatmap_skillset or not isinstance(beatmap_skillset, dict):
        beatmap_skillset = compute_beatmap_skillset(dict(beatmap))
        try:
            await execute("UPDATE beatmaps SET skillset=$1::jsonb WHERE md5=$2", beatmap_skillset, parsed["beatmap_md5"])
        except Exception:
            pass

    accuracy = calculate_accuracy(
        parsed["count300"], parsed["count100"], parsed["count50"], parsed["countmiss"],
    )
    rank_str = get_rank_string(
        accuracy, parsed["countmiss"],
        parsed["count300"], parsed["count100"], parsed["count50"], parsed["mods"],
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

    before = {
        "ranked_score": user["ranked_score"] or 0,
        "total_score":  user["total_score"] or 0,
        "playcount":    user["playcount"] or 0,
        "rank":         user["rank"] or 0,
        "accuracy":     user["accuracy"] or 0.0,
        "pp":           user["pp"] or 0.0,
    }

    # Pause detection
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

    if paused and score_id:
        try:
            await execute("UPDATE scores SET paused=TRUE WHERE id=$1", score_id)
        except Exception:
            pass

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
        await _update_stats_inline(user["id"], parsed["is_fc"], parsed["countmiss"] > 0)

    fresh = await get_user_by_id(user["id"])
    return _build_response(before, fresh, score_id)


async def _update_stats_inline(user_id: int, is_fc: bool, had_miss: bool):
    scores = await fetchall(
        "SELECT pp, accuracy FROM scores WHERE user_id=$1 AND passed=TRUE ORDER BY pp DESC LIMIT 200",
        user_id,
    )
    playcount = await fetchval("SELECT COUNT(*) FROM scores WHERE user_id=$1", user_id)
    ranked = await fetchval("SELECT COALESCE(SUM(score),0) FROM scores WHERE user_id=$1 AND passed=TRUE", user_id)
    total = await fetchval("SELECT COALESCE(SUM(score),0) FROM scores WHERE user_id=$1", user_id)
    max_combo = await fetchval("SELECT COALESCE(MAX(max_combo),0) FROM scores WHERE user_id=$1 AND passed=TRUE", user_id)

    pp, acc = recalculate_user_pp([dict(s) for s in scores])
    await update_user_stats(user_id, ranked, total, playcount, pp, acc, max_combo)
    await recalculate_rank(user_id)

    if is_fc:
        await execute("UPDATE users SET hot_streak=hot_streak+1 WHERE id=$1", user_id)
    elif had_miss:
        await execute("UPDATE users SET hot_streak=0 WHERE id=$1", user_id)


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


def _fmt_score(row, pos: int) -> str:
    name = row["username"]
    if row.get("paused"):
        name += " [P]"
    return (
        f"{row['id']}|{name}|{row['score']}|{row['max_combo']}|"
        f"{row['count50']}|{row['count100']}|{row['count300']}|"
        f"{row['countmiss']}|0|0|"
        f"{1 if row['is_fc'] else 0}|{row['mods']}|"
        f"{row['user_id']}|{pos}|{row['submitted_at']}|0"
    )
