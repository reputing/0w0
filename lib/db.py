"""
Scythe — Supabase DB client
Uses asyncpg with per-request connections (no global pool) to avoid
event loop issues in Vercel's threaded serverless environment.
"""
import os
import json
import time
import asyncpg

DATABASE_URL = os.environ.get("SUPABASE_DB_URL") or os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("SUPABASE_DB_URL is not set")


async def _connect():
    conn = await asyncpg.connect(DATABASE_URL, statement_cache_size=0)
    # Decode jsonb/json columns as Python objects, not raw strings.
    for typename in ("jsonb", "json"):
        await conn.set_type_codec(
            typename,
            encoder=json.dumps,
            decoder=json.loads,
            schema="pg_catalog",
        )
    return conn


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
        uid = await fetchval(
            """
            INSERT INTO users (username, username_safe, password_md5, email, country)
            VALUES ($1,$2,$3,$4,$5) RETURNING id
            """,
            username, safe, password_md5, email, country,
        )
        return uid, None
    except asyncpg.UniqueViolationError as e:
        return None, str(e)


async def update_user_stats(user_id, ranked_score, total_score, playcount, pp, accuracy, max_combo):
    await execute(
        """
        UPDATE users SET ranked_score=$1, total_score=$2, playcount=$3,
        pp=$4, accuracy=$5, max_combo=$6, last_seen=$7 WHERE id=$8
        """,
        ranked_score, total_score, playcount, pp, accuracy, max_combo,
        int(time.time()), user_id,
    )


async def recalculate_rank(user_id: int):
    await execute(
        """
        UPDATE users SET rank = (
            SELECT COUNT(*)+1 FROM users u2
            WHERE u2.pp > users.pp AND u2.status != 1
        ) WHERE id=$1
        """,
        user_id,
    )


# ── Score helpers ─────────────────────────────────────────────────────────────

async def submit_score(user_id, beatmap_md5, score, pp, accuracy, max_combo,
                       count300, count100, count50, countmiss, mods, rank, is_fc, passed):
    return await fetchval(
        """
        INSERT INTO scores
        (user_id, beatmap_md5, score, pp, accuracy, max_combo,
         count300, count100, count50, countmiss, mods, rank, is_fc, passed)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)
        RETURNING id
        """,
        user_id, beatmap_md5, score, pp, accuracy, max_combo,
        count300, count100, count50, countmiss, mods, rank, is_fc, passed,
    )


async def get_leaderboard(beatmap_md5: str, limit=50,
                          requesting_user_id=None,
                          friends_only=False, friends_list=None):
    if friends_only and friends_list:
        rows = await fetchall(
            """
            SELECT s.*, u.username, u.country, u.status as user_status
            FROM scores s JOIN users u ON s.user_id=u.id
            WHERE s.beatmap_md5=$1 AND s.passed=TRUE AND u.status!=1
              AND s.user_id=ANY($2)
            ORDER BY (COALESCE(s.paused, FALSE)) ASC, s.pp DESC LIMIT $3
            """,
            beatmap_md5, friends_list, limit,
        )
    else:
        rows = await fetchall(
            """
            SELECT s.*, u.username, u.country, u.status as user_status
            FROM scores s JOIN users u ON s.user_id=u.id
            WHERE s.beatmap_md5=$1 AND s.passed=TRUE AND u.status!=1
            ORDER BY (COALESCE(s.paused, FALSE)) ASC, s.pp DESC LIMIT $2
            """,
            beatmap_md5, limit,
        )

    ghost = None
    if requesting_user_id:
        user_ids = [r["user_id"] for r in rows]
        if requesting_user_id not in user_ids:
            ghost = await fetchone(
                """
                SELECT s.*, u.username, u.country, u.status as user_status
                FROM scores s JOIN users u ON s.user_id=u.id
                WHERE s.beatmap_md5=$1 AND s.user_id=$2 AND s.passed=TRUE
                ORDER BY s.pp DESC LIMIT 1
                """,
                beatmap_md5, requesting_user_id,
            )

    return rows, ghost


async def flag_score_db(score_id: int, reason: str):
    row = await fetchone("SELECT user_id FROM scores WHERE id=$1", score_id)
    await execute(
        "UPDATE scores SET ac_flagged=TRUE, ac_flag_reason=$1 WHERE id=$2",
        reason, score_id,
    )
    if row:
        await execute(
            "UPDATE users SET status=2 WHERE id=$1 AND status=0", row["user_id"],
        )
        await execute(
            """
            INSERT INTO ac_log (user_id, score_id, flag_type, flag_detail, action_taken)
            VALUES ($1,$2,'auto',$3,'shadowban')
            """,
            row["user_id"], score_id, reason,
        )


# ── Beatmap helpers ───────────────────────────────────────────────────────────

async def get_or_create_beatmap(md5: str):
    row = await fetchone("SELECT * FROM beatmaps WHERE md5=$1", md5)
    if not row:
        await execute(
            "INSERT INTO beatmaps (md5, status) VALUES ($1, 5) ON CONFLICT DO NOTHING",
            md5,
        )
        row = await fetchone("SELECT * FROM beatmaps WHERE md5=$1", md5)
    return row


async def vote_map(user_id: int, beatmap_md5: str, vote: int):
    await execute(
        """
        INSERT INTO map_votes (user_id, beatmap_md5, vote) VALUES ($1,$2,$3)
        ON CONFLICT (user_id, beatmap_md5) DO UPDATE SET vote=$3
        """,
        user_id, beatmap_md5, vote,
    )
    await execute(
        """
        UPDATE beatmaps SET
        vote_love=(SELECT COUNT(*) FROM map_votes WHERE beatmap_md5=$1 AND vote=1),
        vote_hate=(SELECT COUNT(*) FROM map_votes WHERE beatmap_md5=$1 AND vote=-1)
        WHERE md5=$1
        """,
        beatmap_md5,
    )


async def get_featured_map():
    return await fetchone("SELECT * FROM featured_map WHERE id=1")


async def set_featured_map(beatmap_md5: str):
    import datetime
    today = datetime.date.today().isoformat()
    await execute(
        """
        INSERT INTO featured_map (id, beatmap_md5, date) VALUES (1,$1,$2)
        ON CONFLICT (id) DO UPDATE SET beatmap_md5=$1, date=$2
        """,
        beatmap_md5, today,
    )


# ── Online count (last_seen heuristic) ────────────────────────────────────────

async def online_count(window_seconds: int = 300) -> int:
    now = int(time.time())
    return await fetchval(
        "SELECT COUNT(*) FROM users WHERE last_seen > $1", now - window_seconds,
    ) or 0


# ── Avatar ────────────────────────────────────────────────────────────────────

async def get_avatar_url(user_id: int) -> str | None:
    """Return per-user avatar_url if set, else None (caller should fall back)."""
    row = await fetchone("SELECT avatar_url FROM users WHERE id=$1", user_id)
    if row and row["avatar_url"]:
        return row["avatar_url"]
    return None


# ── Web sessions (cookie auth) ────────────────────────────────────────────────

async def create_web_session(user_id: int, token: str, ttl_seconds: int = 30 * 86400,
                             ip: str = "", user_agent: str = "") -> int:
    expires = int(time.time()) + ttl_seconds
    await execute(
        """
        INSERT INTO web_sessions (token, user_id, expires_at, ip, user_agent)
        VALUES ($1, $2, $3, $4, $5)
        """,
        token, user_id, expires, ip, user_agent[:500],
    )
    return expires


async def get_user_by_session(token: str):
    if not token:
        return None
    row = await fetchone(
        """
        SELECT u.* FROM web_sessions s
        JOIN users u ON u.id = s.user_id
        WHERE s.token = $1 AND s.expires_at > $2
        """,
        token, int(time.time()),
    )
    return row


async def delete_web_session(token: str):
    if not token:
        return
    await execute("DELETE FROM web_sessions WHERE token=$1", token)


async def purge_expired_sessions():
    await execute("DELETE FROM web_sessions WHERE expires_at < $1", int(time.time()))


# ── Settings / profile updates ────────────────────────────────────────────────

VALID_COUNTRIES = {
    "AD","AE","AF","AG","AI","AL","AM","AO","AQ","AR","AS","AT","AU","AW","AX","AZ",
    "BA","BB","BD","BE","BF","BG","BH","BI","BJ","BL","BM","BN","BO","BQ","BR","BS",
    "BT","BV","BW","BY","BZ","CA","CC","CD","CF","CG","CH","CI","CK","CL","CM","CN",
    "CO","CR","CU","CV","CW","CX","CY","CZ","DE","DJ","DK","DM","DO","DZ","EC","EE",
    "EG","EH","ER","ES","ET","FI","FJ","FK","FM","FO","FR","GA","GB","GD","GE","GF",
    "GG","GH","GI","GL","GM","GN","GP","GQ","GR","GS","GT","GU","GW","GY","HK","HM",
    "HN","HR","HT","HU","ID","IE","IL","IM","IN","IO","IQ","IR","IS","IT","JE","JM",
    "JO","JP","KE","KG","KH","KI","KM","KN","KP","KR","KW","KY","KZ","LA","LB","LC",
    "LI","LK","LR","LS","LT","LU","LV","LY","MA","MC","MD","ME","MF","MG","MH","MK",
    "ML","MM","MN","MO","MP","MQ","MR","MS","MT","MU","MV","MW","MX","MY","MZ","NA",
    "NC","NE","NF","NG","NI","NL","NO","NP","NR","NU","NZ","OM","PA","PE","PF","PG",
    "PH","PK","PL","PM","PN","PR","PS","PT","PW","PY","QA","RE","RO","RS","RU","RW",
    "SA","SB","SC","SD","SE","SG","SH","SI","SJ","SK","SL","SM","SN","SO","SR","SS",
    "ST","SV","SX","SY","SZ","TC","TD","TF","TG","TH","TJ","TK","TL","TM","TN","TO",
    "TR","TT","TV","TW","TZ","UA","UG","UM","US","UY","UZ","VA","VC","VE","VG","VI",
    "VN","VU","WF","WS","YE","YT","ZA","ZM","ZW",
}


async def update_user_settings(user_id: int, *, avatar_url=None, banner_url=None,
                               bio=None, country=None) -> dict:
    """Patch any subset of profile settings. Returns the updated row."""
    updates: list[str] = []
    vals: list = []
    if avatar_url is not None:
        updates.append(f"avatar_url=${len(vals)+1}")
        vals.append(avatar_url[:500] if avatar_url else None)
    if banner_url is not None:
        updates.append(f"banner_url=${len(vals)+1}")
        vals.append(banner_url[:500] if banner_url else None)
    if bio is not None:
        updates.append(f"bio=${len(vals)+1}")
        vals.append(str(bio)[:500])
    if country is not None:
        cc = (country or "").strip().upper()
        if cc and cc in VALID_COUNTRIES:
            updates.append(f"country=${len(vals)+1}")
            vals.append(cc)
    if updates:
        vals.append(user_id)
        await execute(
            f"UPDATE users SET {', '.join(updates)} WHERE id=${len(vals)}",
            *vals,
        )
    row = await get_user_by_id(user_id)
    return dict(row) if row else {}


async def update_user_password(user_id: int, old_password_md5: str,
                               new_password_md5: str) -> tuple[bool, str | None]:
    row = await fetchone(
        "SELECT password_md5 FROM users WHERE id=$1", user_id,
    )
    if not row:
        return False, "user_not_found"
    if row["password_md5"] != old_password_md5:
        return False, "bad_password"
    await execute(
        "UPDATE users SET password_md5=$1 WHERE id=$2",
        new_password_md5, user_id,
    )
    # Invalidate ALL existing web sessions on password change.
    await execute("DELETE FROM web_sessions WHERE user_id=$1", user_id)
    return True, None


# ── Friends (web side, used by /me/friends) ───────────────────────────────────

async def add_friend(user_id: int, target_id: int) -> tuple[bool, str | None]:
    if user_id == target_id:
        return False, "cannot_friend_self"
    target = await fetchone("SELECT id FROM users WHERE id=$1", target_id)
    if not target:
        return False, "target_not_found"
    row = await fetchone("SELECT friends FROM users WHERE id=$1", user_id)
    if not row:
        return False, "user_not_found"
    friends = row["friends"] or []
    if isinstance(friends, str):
        try:
            friends = json.loads(friends)
        except Exception:
            friends = []
    friends = [int(f) for f in friends if str(f).isdigit()]
    if int(target_id) in friends:
        return True, None  # already a friend, idempotent
    friends.append(int(target_id))
    await execute(
        "UPDATE users SET friends=$1::jsonb WHERE id=$2", friends, user_id,
    )
    return True, None


async def remove_friend(user_id: int, target_id: int) -> bool:
    row = await fetchone("SELECT friends FROM users WHERE id=$1", user_id)
    if not row:
        return False
    friends = row["friends"] or []
    if isinstance(friends, str):
        try:
            friends = json.loads(friends)
        except Exception:
            friends = []
    friends = [int(f) for f in friends if str(f).isdigit() and int(f) != int(target_id)]
    await execute(
        "UPDATE users SET friends=$1::jsonb WHERE id=$2", friends, user_id,
    )
    return True
