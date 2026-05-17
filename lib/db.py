"""
Scythe — Supabase DB client
Uses asyncpg with per-request connections (no global pool) to avoid
event loop issues in Vercel's threaded serverless environment.
"""
import os
import asyncpg

DATABASE_URL = os.environ["SUPABASE_DB_URL"]

async def _connect():
    return await asyncpg.connect(DATABASE_URL)

async def fetchone(query: str, *args):
    conn = await _connect()
    try:
        return await conn.fetchrow(query, *args)
    finally:
        await conn.close()

async def fetchall(query: str, *args):
    conn = await _connect()
    try:
        return await conn.fetch(query, *args)
    finally:
        await conn.close()

async def execute(query: str, *args):
    conn = await _connect()
    try:
        return await conn.execute(query, *args)
    finally:
        await conn.close()

async def fetchval(query: str, *args):
    conn = await _connect()
    try:
        return await conn.fetchval(query, *args)
    finally:
        await conn.close()

# ── User helpers ──────────────────────────────────────────────────────────────

async def get_user_by_name(username: str):
    safe = username.lower().replace(" ", "_")
    return await fetchone("SELECT * FROM users WHERE username_safe=$1", safe)

async def get_user_by_id(user_id: int):
    return await fetchone("SELECT * FROM users WHERE id=$1", user_id)

async def create_user(username: str, password_md5: str, email: str, country="XX"):
    safe = username.lower().replace(" ", "_")
    try:
        uid = await fetchval("""
            INSERT INTO users (username, username_safe, password_md5, email, country)
            VALUES ($1,$2,$3,$4,$5) RETURNING id
        """, username, safe, password_md5, email, country)
        return uid, None
    except asyncpg.UniqueViolationError as e:
        return None, str(e)

async def update_user_stats(user_id, ranked_score, total_score, playcount, pp, accuracy, max_combo):
    import time
    await execute("""
        UPDATE users SET ranked_score=$1, total_score=$2, playcount=$3,
        pp=$4, accuracy=$5, max_combo=$6, last_seen=$7 WHERE id=$8
    """, ranked_score, total_score, playcount, pp, accuracy, max_combo,
         int(time.time()), user_id)

async def recalculate_rank(user_id: int):
    await execute("""
        UPDATE users SET rank = (
            SELECT COUNT(*)+1 FROM users u2
            WHERE u2.pp > users.pp AND u2.status != 1
        ) WHERE id=$1
    """, user_id)

# ── Score helpers ─────────────────────────────────────────────────────────────

async def submit_score(user_id, beatmap_md5, score, pp, accuracy, max_combo,
                       count300, count100, count50, countmiss, mods, rank, is_fc, passed):
    return await fetchval("""
        INSERT INTO scores
        (user_id, beatmap_md5, score, pp, accuracy, max_combo,
         count300, count100, count50, countmiss, mods, rank, is_fc, passed)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)
        RETURNING id
    """, user_id, beatmap_md5, score, pp, accuracy, max_combo,
         count300, count100, count50, countmiss, mods, rank, is_fc, passed)

async def get_leaderboard(beatmap_md5: str, limit=50,
                          requesting_user_id=None,
                          friends_only=False, friends_list=None):
    if friends_only and friends_list:
        rows = await fetchall("""
            SELECT s.*, u.username, u.country, u.status as user_status
            FROM scores s JOIN users u ON s.user_id=u.id
            WHERE s.beatmap_md5=$1 AND s.passed=TRUE AND u.status!=1
            AND s.user_id=ANY($2)
            ORDER BY s.pp DESC LIMIT $3
        """, beatmap_md5, friends_list, limit)
    else:
        rows = await fetchall("""
            SELECT s.*, u.username, u.country, u.status as user_status
            FROM scores s JOIN users u ON s.user_id=u.id
            WHERE s.beatmap_md5=$1 AND s.passed=TRUE AND u.status!=1
            ORDER BY s.pp DESC LIMIT $2
        """, beatmap_md5, limit)

    ghost = None
    if requesting_user_id:
        user_ids = [r["user_id"] for r in rows]
        if requesting_user_id not in user_ids:
            ghost = await fetchone("""
                SELECT s.*, u.username, u.country, u.status as user_status
                FROM scores s JOIN users u ON s.user_id=u.id
                WHERE s.beatmap_md5=$1 AND s.user_id=$2 AND s.passed=TRUE
                ORDER BY s.pp DESC LIMIT 1
            """, beatmap_md5, requesting_user_id)

    return rows, ghost

async def flag_score_db(score_id: int, reason: str):
    row = await fetchone("SELECT user_id FROM scores WHERE id=$1", score_id)
    await execute("UPDATE scores SET ac_flagged=TRUE, ac_flag_reason=$1 WHERE id=$2",
                  reason, score_id)
    if row:
        await execute("UPDATE users SET status=2 WHERE id=$1 AND status=0", row["user_id"])
        await execute("""
            INSERT INTO ac_log (user_id, score_id, flag_type, flag_detail, action_taken)
            VALUES ($1,$2,'auto',$3,'shadowban')
        """, row["user_id"], score_id, reason)

# ── Beatmap helpers ───────────────────────────────────────────────────────────

async def get_or_create_beatmap(md5: str):
    row = await fetchone("SELECT * FROM beatmaps WHERE md5=$1", md5)
    if not row:
        await execute("INSERT INTO beatmaps (md5, status) VALUES ($1, 5) ON CONFLICT DO NOTHING", md5)
        row = await fetchone("SELECT * FROM beatmaps WHERE md5=$1", md5)
    return row

async def vote_map(user_id: int, beatmap_md5: str, vote: int):
    await execute("""
        INSERT INTO map_votes (user_id, beatmap_md5, vote) VALUES ($1,$2,$3)
        ON CONFLICT (user_id, beatmap_md5) DO UPDATE SET vote=$3
    """, user_id, beatmap_md5, vote)
    await execute("""
        UPDATE beatmaps SET
        vote_love=(SELECT COUNT(*) FROM map_votes WHERE beatmap_md5=$1 AND vote=1),
        vote_hate=(SELECT COUNT(*) FROM map_votes WHERE beatmap_md5=$1 AND vote=-1)
        WHERE md5=$1
    """, beatmap_md5)

async def get_featured_map():
    return await fetchone("SELECT * FROM featured_map WHERE id=1")

async def set_featured_map(beatmap_md5: str):
    import datetime
    today = datetime.date.today().isoformat()
    await execute("""
        INSERT INTO featured_map (id, beatmap_md5, date) VALUES (1,$1,$2)
        ON CONFLICT (id) DO UPDATE SET beatmap_md5=$1, date=$2
    """, beatmap_md5, today)
