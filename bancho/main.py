"""
Scythe — Bancho HTTP Server (Railway deployment)
osu! stable uses HTTP POST to c.<domain> for all bancho communication.
"""
import asyncio
import json
import os
import struct
import sys
import time
import traceback
import uuid
from aiohttp import web

sys.path.insert(0, os.path.dirname(__file__))

# ── Config ────────────────────────────────────────────────────────────────────
SERVER_NAME   = os.environ.get("SERVER_NAME", "Scythe")
SERVER_DOMAIN = os.environ.get("SERVER_DOMAIN", "0w0.fit")
BANCHO_PORT   = int(os.environ.get("PORT", 13381))

# ── Database ──────────────────────────────────────────────────────────────────
import asyncpg

DATABASE_URL = os.environ.get("SUPABASE_DB_URL") or os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    print("[BANCHO][FATAL] SUPABASE_DB_URL is not set — aborting.", flush=True)
    sys.exit(1)

_pool: asyncpg.Pool | None = None
_pool_lock = asyncio.Lock()


async def _init_conn(conn: asyncpg.Connection):
    # Make jsonb / json round-trip as Python objects instead of raw strings.
    # Without this, user["friends"] returns the literal string '[]'.
    for typename in ("jsonb", "json"):
        await conn.set_type_codec(
            typename,
            encoder=json.dumps,
            decoder=json.loads,
            schema="pg_catalog",
        )


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        async with _pool_lock:
            if _pool is None:
                print(f"[BANCHO] Connecting to Postgres…", flush=True)
                _pool = await asyncpg.create_pool(
                    DATABASE_URL,
                    min_size=1,
                    max_size=10,
                    statement_cache_size=0,  # required for pgbouncer / supavisor
                    init=_init_conn,
                    timeout=15,
                    command_timeout=30,
                )
                print(f"[BANCHO] Postgres pool ready.", flush=True)
    return _pool


async def db_fetchrow(q, *a):
    p = await get_pool()
    async with p.acquire() as c:
        return await c.fetchrow(q, *a)


async def db_fetch(q, *a):
    p = await get_pool()
    async with p.acquire() as c:
        return await c.fetch(q, *a)


async def db_execute(q, *a):
    p = await get_pool()
    async with p.acquire() as c:
        return await c.execute(q, *a)


# ── Online sessions: token → session dict ─────────────────────────────────────
sessions: dict[str, dict] = {}  # token → {"user", "queue", "channels", "tz"}

# ── Packet helpers ────────────────────────────────────────────────────────────
def uleb128(v: int) -> bytes:
    r = b""
    while True:
        b = v & 0x7F
        v >>= 7
        if v:
            b |= 0x80
        r += bytes([b])
        if not v:
            break
    return r


def w_str(s: str) -> bytes:
    if not s:
        return b"\x00"
    e = s.encode("utf-8")
    return b"\x0b" + uleb128(len(e)) + e


def w_i32(v):  return struct.pack("<i", int(v))
def w_u32(v):  return struct.pack("<I", int(v))
def w_i64(v):  return struct.pack("<q", int(v))
def w_f32(v):  return struct.pack("<f", float(v))
def w_i16(v):  return struct.pack("<h", int(v))
def w_u16(v):  return struct.pack("<H", int(v))
def w_i8(v):   return struct.pack("<b", int(v))
def w_u8(v):   return struct.pack("<B", int(v) & 0xFF)


def pkt(pid: int, data: bytes = b"") -> bytes:
    # Bancho packet header: u16 packetId + u8 compression(0) + u32 length
    return w_u16(pid) + b"\x00" + w_u32(len(data)) + data


def r_str(data: bytes, off: int):
    if off >= len(data) or data[off] == 0:
        return "", off + 1
    off += 1
    length = shift = 0
    while True:
        byte = data[off]
        off += 1
        length |= (byte & 0x7F) << shift
        shift += 7
        if not (byte & 0x80):
            break
    return data[off:off + length].decode("utf-8", "ignore"), off + length


def r_i32(data, off):
    return struct.unpack_from("<i", data, off)[0], off + 4


# ── Bancho server→client packet IDs (verified against osu! stable) ────────────
P_USER_ID            = 5
P_SEND_MESSAGE       = 7
P_PONG               = 8
P_USER_STATS         = 11
P_USER_LOGOUT        = 12
P_NOTIFICATION       = 24
P_CHANNEL_JOIN_OK    = 64
P_CHANNEL_INFO       = 65
P_LOGIN_PERMISSIONS  = 71
P_FRIENDS_LIST       = 72
P_PROTOCOL_VERSION   = 75
P_MAIN_MENU_ICON     = 76
P_USER_PRESENCE      = 83
P_CHANNEL_INFO_END   = 89


# ── Packet builders ───────────────────────────────────────────────────────────
def pkt_login_reply(uid):       return pkt(P_USER_ID, w_i32(uid))
def pkt_notification(msg):      return pkt(P_NOTIFICATION, w_str(msg))
def pkt_protocol(v=19):         return pkt(P_PROTOCOL_VERSION, w_i32(v))
def pkt_pong():                 return pkt(P_PONG)
def pkt_login_perms(perms):     return pkt(P_LOGIN_PERMISSIONS, w_i32(perms))
def pkt_channel_info_end():     return pkt(P_CHANNEL_INFO_END)
def pkt_channel_join_ok(name):  return pkt(P_CHANNEL_JOIN_OK, w_str(name))
def pkt_menu_icon(icon, url):   return pkt(P_MAIN_MENU_ICON, w_str(f"{icon}|{url}"))


def pkt_logout(uid):
    # Logout packet: i32 userId + u8 quitState
    return pkt(P_USER_LOGOUT, w_i32(uid) + w_u8(0))


def pkt_channel_info(name, topic, count):
    return pkt(P_CHANNEL_INFO, w_str(name) + w_str(topic) + w_i16(count))


def pkt_message(sender, text, channel, sid):
    return pkt(P_SEND_MESSAGE,
               w_str(sender) + w_str(text) + w_str(channel) + w_i32(sid))


def pkt_friends(ids):
    d = w_i16(len(ids))
    for i in ids:
        d += w_i32(int(i))
    return pkt(P_FRIENDS_LIST, d)


def pkt_user_stats(u):
    # action(u8) + info_text(str) + map_md5(str) + mods(u32) + mode(u8) + map_id(i32)
    status = w_u8(0) + w_str("") + w_str("") + w_u32(0) + w_u8(0) + w_i32(0)
    d = (
        w_i32(u["id"]) + status
        + w_i64(int(u["ranked_score"] or 0))
        + w_f32(float(u["accuracy"] or 0.0))
        + w_i32(int(u["playcount"] or 0))
        + w_i64(int(u["total_score"] or 0))
        + w_i32(int(u["rank"] or 0))
        + w_i16(min(int(u["pp"] or 0), 32767))
    )
    return pkt(P_USER_STATS, d)


def pkt_user_presence(u, tz=0):
    d = (
        w_i32(u["id"]) + w_str(u["username"])
        + w_u8((tz + 24) & 0xFF)  # timezone (offset+24)
        + w_u8(0)                 # country code (245 = unknown)
        + w_u8(0b11111)           # bancho privileges
        + w_f32(0.0) + w_f32(0.0) # longitude / latitude
        + w_i32(int(u["rank"] or 0))
    )
    return pkt(P_USER_PRESENCE, d)


# ── Helpers ───────────────────────────────────────────────────────────────────
def coerce_friends(val) -> list[int]:
    """Defensive: handle list, JSON string, None, or asyncpg jsonb."""
    if val is None:
        return []
    if isinstance(val, list):
        out = []
        for x in val:
            try: out.append(int(x))
            except (TypeError, ValueError): pass
        return out
    if isinstance(val, str):
        try:
            parsed = json.loads(val)
            return coerce_friends(parsed)
        except Exception:
            return []
    return []


def enqueue(token, data):
    if token in sessions:
        sessions[token]["queue"] += data


def broadcast(data, exclude_token=None):
    for tok, s in sessions.items():
        if tok != exclude_token:
            s["queue"] += data


# ── Login handler ─────────────────────────────────────────────────────────────
async def handle_login(body_bytes: bytes):
    try:
        text = body_bytes.decode("utf-8", "ignore")
        lines = text.split("\n")
        if len(lines) < 3:
            return None, pkt_login_reply(-1) + pkt_notification("Bad login request."), "bad_request"
        username = lines[0].strip()
        pw_md5   = lines[1].strip()
        info     = lines[2].strip().split("|")
        osu_ver  = info[0] if len(info) > 0 else ""
        tz       = int(info[1]) if len(info) > 1 else 0
    except Exception as e:
        print(f"[BANCHO] login parse error: {e}", flush=True)
        return None, pkt_login_reply(-1) + pkt_notification("Parse error."), "parse_error"

    safe = username.lower().replace(" ", "_")

    try:
        user = await db_fetchrow("SELECT * FROM users WHERE username_safe=$1", safe)
    except Exception as e:
        traceback.print_exc()
        return None, pkt_login_reply(-5) + pkt_notification("Server error (DB unreachable)."), "db_err"

    if not user:
        return None, pkt_login_reply(-1) + pkt_notification("User not found."), "no_user"

    if user["password_md5"] != pw_md5:
        return None, pkt_login_reply(-1) + pkt_notification("Wrong password."), "bad_pw"

    if user["status"] == 1:
        return None, pkt_login_reply(-3) + pkt_notification("Your account is restricted."), "restricted"

    token = str(uuid.uuid4())
    sessions[token] = {
        "user":     dict(user),
        "queue":    bytearray(),
        "channels": {"#osu", "#announce"},
        "token":    token,
        "tz":       tz,
    }

    try:
        await db_execute("UPDATE users SET last_seen=$1 WHERE id=$2",
                         int(time.time()), user["id"])
    except Exception as e:
        print(f"[BANCHO] last_seen update failed: {e}", flush=True)

    print(f"[BANCHO] {username} logged in (id={user['id']}, ver={osu_ver}, tz={tz})", flush=True)

    friends = coerce_friends(user["friends"])

    # Build login response. Order matters for the osu! client.
    resp = bytearray()
    resp += pkt_protocol(19)
    resp += pkt_login_reply(user["id"])
    resp += pkt_login_perms(1 << 4 if user["status"] == 3 else 1)  # admin or normal
    resp += pkt_notification(f"Welcome to {SERVER_NAME}!")
    resp += pkt_menu_icon("", f"https://{SERVER_DOMAIN}")
    resp += pkt_friends(friends)
    resp += pkt_user_presence(dict(user), tz)
    resp += pkt_user_stats(user)

    # Channel listings
    resp += pkt_channel_info("#osu", "Main chat", max(1, len(sessions)))
    resp += pkt_channel_info("#announce", "Announcements", max(1, len(sessions)))
    resp += pkt_channel_info_end()
    resp += pkt_channel_join_ok("#osu")
    resp += pkt_channel_join_ok("#announce")

    # Send already-online users to the new player
    for tok, s in sessions.items():
        if tok != token:
            resp += pkt_user_presence(s["user"], s.get("tz", 0))
            resp += pkt_user_stats(s["user"])

    # Announce new player to everyone else
    join = pkt_user_presence(dict(user), tz) + pkt_user_stats(user)
    broadcast(join, exclude_token=token)

    return token, bytes(resp), "ok"


# ── Packet loop ───────────────────────────────────────────────────────────────
async def handle_packets(token: str, body: bytes) -> bytes:
    if token not in sessions:
        # Unknown token — tell client to log out and reconnect
        return pkt_login_reply(-1) + pkt_notification("Session expired — please reconnect.")

    s    = sessions[token]
    user = s["user"]
    off  = 0

    while off + 7 <= len(body):
        pid  = struct.unpack_from("<H", body, off)[0]; off += 2
        off += 1  # compression flag
        plen = struct.unpack_from("<I", body, off)[0]; off += 4
        if off + plen > len(body):
            break
        pkt_body = body[off:off + plen]; off += plen

        try:
            if pid == 4:        # OsuPing
                s["queue"] += pkt_pong()
            elif pid == 2:      # OsuLogout
                await disconnect(token)
                return bytes(s["queue"])
            elif pid == 0:      # OsuChangeAction (status update)
                pass
            elif pid == 3:      # OsuRequestStatusUpdate
                u = await db_fetchrow("SELECT * FROM users WHERE id=$1", user["id"])
                if u:
                    s["user"] = dict(u)
                    s["queue"] += pkt_user_stats(u)
            elif pid == 1:      # OsuSendPublicMessage
                await handle_chat(token, pkt_body, public=True)
            elif pid == 25:     # OsuSendPrivateMessage
                await handle_chat(token, pkt_body, public=False)
            elif pid == 63:     # OsuChannelJoin
                ch, _ = r_str(pkt_body, 0)
                s["channels"].add(ch)
                s["queue"] += pkt_channel_join_ok(ch)
            elif pid == 78:     # OsuChannelPart
                ch, _ = r_str(pkt_body, 0)
                s["channels"].discard(ch)
            elif pid == 73:     # OsuFriendAdd
                tid = struct.unpack_from("<i", pkt_body, 0)[0]
                await friend_add(user["id"], tid)
            elif pid == 74:     # OsuFriendRemove
                tid = struct.unpack_from("<i", pkt_body, 0)[0]
                await friend_remove(user["id"], tid)
            elif pid == 85:     # OsuUserStatsRequest
                count = struct.unpack_from("<h", pkt_body, 0)[0]
                for i in range(count):
                    uid = struct.unpack_from("<i", pkt_body, 2 + i * 4)[0]
                    u = await db_fetchrow("SELECT * FROM users WHERE id=$1", uid)
                    if u:
                        s["queue"] += pkt_user_stats(u) + pkt_user_presence(u)
            elif pid == 97:     # OsuUserPresenceRequest
                count = struct.unpack_from("<h", pkt_body, 0)[0]
                for i in range(count):
                    uid = struct.unpack_from("<i", pkt_body, 2 + i * 4)[0]
                    u = await db_fetchrow("SELECT * FROM users WHERE id=$1", uid)
                    if u:
                        s["queue"] += pkt_user_presence(u)
            # ignored: spectate / multiplayer (16,17,18,29-56,...)
        except Exception:
            print(f"[BANCHO] error handling packet {pid}:", flush=True)
            traceback.print_exc()

    out = bytes(s["queue"])
    s["queue"] = bytearray()
    return out


# ── Chat ──────────────────────────────────────────────────────────────────────
async def handle_chat(token, body, public=True):
    s = sessions.get(token)
    if not s:
        return
    try:
        off = 0
        _,   off = r_str(body, off)  # sender (ignored, we trust session)
        msg, off = r_str(body, off)
        ch,  off = r_str(body, off)
        if msg.startswith("!"):
            await bot_cmd(token, msg, ch)
            return
        mpkt = pkt_message(s["user"]["username"], msg, ch, s["user"]["id"])
        for tok, other in sessions.items():
            if tok != token and ch in other["channels"]:
                other["queue"] += mpkt
    except Exception as e:
        print(f"[BANCHO][CHAT] {e}", flush=True)


async def bot_cmd(token, cmd, ch):
    s = sessions.get(token)
    if not s:
        return
    parts = cmd.strip().split()
    c = parts[0].lower()
    u = await db_fetchrow("SELECT * FROM users WHERE id=$1", s["user"]["id"])

    def reply(msg):
        return pkt_message(f"{SERVER_NAME}Bot", msg, ch, 0)

    if c == "!help":
        s["queue"] += reply("Commands: !rank !pp !online")
    elif c == "!rank":
        s["queue"] += reply(
            f"{u['username']} — Rank #{u['rank']} | "
            f"{u['pp'] or 0:.0f}pp | Acc {(u['accuracy'] or 0)*100:.2f}%"
        )
    elif c == "!pp":
        s["queue"] += reply(f"{u['username']}: {u['pp'] or 0:.2f}pp")
    elif c == "!online":
        s["queue"] += reply(f"{len(sessions)} player(s) online on {SERVER_NAME}.")


# ── Friend helpers ────────────────────────────────────────────────────────────
async def friend_add(uid, tid):
    u = await db_fetchrow("SELECT friends FROM users WHERE id=$1", uid)
    if not u:
        return
    friends = coerce_friends(u["friends"])
    if tid not in friends:
        friends.append(int(tid))
        # JSONB codec is registered → asyncpg will encode list → JSON automatically.
        await db_execute("UPDATE users SET friends=$1::jsonb WHERE id=$2", friends, uid)


async def friend_remove(uid, tid):
    u = await db_fetchrow("SELECT friends FROM users WHERE id=$1", uid)
    if not u:
        return
    friends = [f for f in coerce_friends(u["friends"]) if f != int(tid)]
    await db_execute("UPDATE users SET friends=$1::jsonb WHERE id=$2", friends, uid)


# ── Disconnect ────────────────────────────────────────────────────────────────
async def disconnect(token):
    s = sessions.pop(token, None)
    if not s:
        return
    uid = s["user"]["id"]
    print(f"[BANCHO] {s['user']['username']} disconnected. Online: {len(sessions)}", flush=True)
    broadcast(pkt_logout(uid))


# ── HTTP handler ──────────────────────────────────────────────────────────────
async def bancho_handler(request: web.Request):
    try:
        token = request.headers.get("osu-token")
        body  = await request.read()

        if not token:
            # Login request
            tok, resp, status = await handle_login(body)
            print(f"[BANCHO] login attempt → {status}", flush=True)
            headers = {
                "cho-token": tok if tok else "no",
                "cho-protocol": "19",
                "Content-Type": "application/octet-stream",
                "Connection": "keep-alive",
            }
            return web.Response(body=resp, headers=headers)
        else:
            resp = await handle_packets(token, body)
            return web.Response(
                body=resp,
                headers={
                    "Content-Type": "application/octet-stream",
                    "Connection": "keep-alive",
                },
            )
    except Exception:
        print("[BANCHO][FATAL] unhandled exception in bancho_handler:", flush=True)
        traceback.print_exc()
        # Always return SOMETHING the client can parse instead of a 500 with HTML.
        return web.Response(
            body=pkt_login_reply(-5) + pkt_notification("Server error — check Railway logs."),
            headers={"cho-token": "no", "cho-protocol": "19",
                     "Content-Type": "application/octet-stream"},
        )


async def health_handler(request: web.Request):
    """GET / — diagnostic page so you can verify Railway is routing to bancho."""
    pool_ok = "ok"
    try:
        await db_fetchrow("SELECT 1")
    except Exception as e:
        pool_ok = f"FAIL: {e!r}"
    body = (
        f"{SERVER_NAME} bancho alive\n"
        f"domain: {SERVER_DOMAIN}\n"
        f"online: {len(sessions)}\n"
        f"db: {pool_ok}\n"
    )
    return web.Response(text=body, content_type="text/plain")


# ── Background tasks ──────────────────────────────────────────────────────────
async def background_tasks():
    import datetime
    while True:
        try:
            await asyncio.sleep(300)
            await db_execute("""
                UPDATE users SET rank=(
                    SELECT COUNT(*)+1 FROM users u2
                    WHERE u2.pp > users.pp AND u2.status != 1
                ) WHERE status != 1
            """)
            await db_execute("UPDATE users SET rank=0 WHERE status=1")

            today = datetime.date.today().isoformat()
            fm = await db_fetchrow("SELECT date FROM featured_map WHERE id=1")
            if not fm or fm["date"] != today:
                candidate = await db_fetchrow("""
                    SELECT beatmap_md5 FROM scores WHERE passed=TRUE
                    ORDER BY RANDOM() LIMIT 1
                """)
                if candidate:
                    md5 = candidate["beatmap_md5"]
                    await db_execute("""
                        INSERT INTO featured_map (id, beatmap_md5, date) VALUES (1,$1,$2)
                        ON CONFLICT(id) DO UPDATE SET beatmap_md5=$1, date=$2
                    """, md5, today)
                    print(f"[BANCHO] Featured map → {md5}", flush=True)
        except Exception as e:
            print(f"[BANCHO][BG] Error: {e}", flush=True)


# ── Entry point ───────────────────────────────────────────────────────────────
async def main():
    print(f"""
╔══════════════════════════════════════════╗
║    SCYTHE BANCHO — Railway Deployment    ║
║    Port {BANCHO_PORT:<5} | HTTP mode | Supabase    ║
╚══════════════════════════════════════════╝
    """, flush=True)

    # Eager pool init so DB problems show in startup logs, not silently on first login.
    try:
        await get_pool()
        await db_fetchrow("SELECT 1")
        print("[BANCHO] DB ping OK.", flush=True)
    except Exception as e:
        print(f"[BANCHO][FATAL] cannot reach Postgres at startup: {e!r}", flush=True)
        traceback.print_exc()
        # don't sys.exit — let the health endpoint surface the error to you

    app = web.Application(client_max_size=10 * 1024 * 1024)
    app.router.add_post("/", bancho_handler)
    app.router.add_get("/", health_handler)

    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", BANCHO_PORT)
    await site.start()
    print(f"[BANCHO] Listening on 0.0.0.0:{BANCHO_PORT}", flush=True)

    await background_tasks()


if __name__ == "__main__":
    asyncio.run(main())
