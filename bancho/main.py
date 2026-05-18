"""
Scythe — Bancho HTTP Server (Railway deployment)
osu! stable uses HTTP POST to cho.domain for all bancho communication.
"""
import asyncio
import struct
import time
import os
import sys
import uuid
from aiohttp import web

sys.path.insert(0, os.path.dirname(__file__))

# ── Config ────────────────────────────────────────────────────────────────────
SERVER_NAME   = os.environ.get("SERVER_NAME", "Scythe")
SERVER_DOMAIN = os.environ.get("SERVER_DOMAIN", "0w0.fit")
BANCHO_PORT   = int(os.environ.get("PORT", 13381))

# ── Database ──────────────────────────────────────────────────────────────────
import asyncpg

DATABASE_URL = os.environ["SUPABASE_DB_URL"]
_pool = None

async def get_pool():
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            DATABASE_URL, min_size=1, max_size=10,
            statement_cache_size=0
        )
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
sessions = {}  # token → {"user": row, "queue": bytearray, "channels": set}

# ── Packet helpers ────────────────────────────────────────────────────────────
def uleb128(v):
    r = b""
    while True:
        b = v & 0x7F; v >>= 7
        if v: b |= 0x80
        r += bytes([b])
        if not v: break
    return r

def w_str(s):
    if not s: return b"\x00"
    e = s.encode(); return b"\x0b" + uleb128(len(e)) + e

def w_i32(v):  return struct.pack("<i", v)
def w_u32(v):  return struct.pack("<I", v)
def w_i64(v):  return struct.pack("<q", v)
def w_f32(v):  return struct.pack("<f", v)
def w_i16(v):  return struct.pack("<h", v)
def w_i8(v):   return struct.pack("<b", v)

def pkt(pid, data=b""):
    return w_i16(pid) + b"\x00" + w_u32(len(data)) + data

def r_str(data, off):
    if off >= len(data) or data[off] == 0: return "", off+1
    off += 1; length = shift = 0
    while True:
        byte = data[off]; off += 1
        length |= (byte & 0x7F) << shift; shift += 7
        if not (byte & 0x80): break
    return data[off:off+length].decode("utf-8","ignore"), off+length

def r_i32(data, off): return struct.unpack_from("<i",data,off)[0], off+4

# ── Packet builders ───────────────────────────────────────────────────────────
def pkt_login_reply(uid):      return pkt(5, w_i32(uid))
def pkt_notification(msg):     return pkt(24, w_str(msg))
def pkt_protocol(v=19):        return pkt(75, w_i32(v))
def pkt_pong():                 return pkt(8)
def pkt_logout(uid):           return pkt(12, w_i32(uid))
def pkt_channel_info(name, topic, count):
    return pkt(65, w_str(name)+w_str(topic)+w_i16(count))
def pkt_channel_join_ok(name): return pkt(64, w_str(name))
def pkt_menu_icon(icon, url):  return pkt(26, w_str(f"{icon}|{url}"))
def pkt_message(sender, text, channel, sid):
    return pkt(7, w_str(sender)+w_str(text)+w_str(channel)+w_i32(sid))
def pkt_friends(ids):
    d = w_i16(len(ids))
    for i in ids: d += w_i32(i)
    return pkt(102, d)

def pkt_user_stats(u):
    status = w_i8(0)+w_str("")+w_str("")+w_u32(0)+w_i8(0)+w_i32(0)
    d = (w_i32(u["id"]) + status +
         w_i64(int(u["ranked_score"] or 0)) +
         w_f32(float(u["accuracy"] or 0)) +
         w_i32(int(u["playcount"] or 0)) +
         w_i64(int(u["total_score"] or 0)) +
         w_i32(int(u["rank"] or 0)) +
         w_i16(int(u["pp"] or 0)))
    return pkt(11, d)

def pkt_user_presence(u, tz=0):
    d = (w_i32(u["id"]) + w_str(u["username"]) +
         w_i8(tz+24) + w_i8(0) + w_i8(0b11111) +
         w_f32(0.0) + w_f32(0.0) + w_i32(int(u["rank"] or 0)))
    return pkt(83, d)

# ── Enqueue to a session ──────────────────────────────────────────────────────
def enqueue(token, data):
    if token in sessions:
        sessions[token]["queue"] += data

def broadcast(data, exclude_token=None):
    for tok, s in sessions.items():
        if tok != exclude_token:
            s["queue"] += data

# ── Login handler ─────────────────────────────────────────────────────────────
async def handle_login(body_bytes):
    try:
        lines = body_bytes.decode("utf-8", "ignore").split("\n")
        if len(lines) < 3:
            return -1, b"", "Bad request"
        username = lines[0].strip()
        pw_md5   = lines[1].strip()
        parts    = lines[2].strip().split("|")
        tz       = int(parts[1]) if len(parts) > 1 else 0
    except Exception:
        return -1, b"", "Parse error"

    safe = username.lower().replace(" ", "_")
    user = await db_fetchrow("SELECT * FROM users WHERE username_safe=$1", safe)

    if not user:
        resp = pkt_login_reply(-1) + pkt_notification("User not found.")
        return -1, resp, "no_user"

    if user["password_md5"] != pw_md5:
        resp = pkt_login_reply(-1) + pkt_notification("Wrong password.")
        return -1, resp, "bad_pw"

    if user["status"] == 1:
        resp = pkt_login_reply(-3) + pkt_notification("Your account is restricted.")
        return -1, resp, "restricted"

    token = str(uuid.uuid4())
    sessions[token] = {
        "user": dict(user),
        "queue": bytearray(),
        "channels": {"#osu"},
        "token": token,
        "tz": tz,
    }

    await db_execute("UPDATE users SET last_seen=$1 WHERE id=$2", int(time.time()), user["id"])
    print(f"[BANCHO] {username} logged in (id={user['id']})")

    friends = list(user["friends"] or [])

    resp = bytearray()
    resp += pkt_login_reply(user["id"])
    resp += pkt_protocol(19)
    resp += pkt_notification(f"Welcome to {SERVER_NAME}!")
    resp += pkt_menu_icon("", f"https://{SERVER_DOMAIN}")
    resp += pkt_friends(friends)
    resp += pkt_channel_info("#osu", "Main", len(sessions))
    resp += pkt_channel_info("#announce", "Announcements", 1)
    resp += pkt_channel_join_ok("#osu")
    resp += pkt_channel_join_ok("#announce")
    resp += pkt_user_stats(user)
    resp += pkt_user_presence(user, tz)

    # Send existing users to new player
    for tok, s in sessions.items():
        if tok != token:
            resp += pkt_user_presence(s["user"]) + pkt_user_stats(s["user"])

    # Announce to everyone else
    join = pkt_user_presence(user) + pkt_user_stats(user)
    broadcast(join, exclude_token=token)

    return token, bytes(resp), "ok"

# ── Packet loop ───────────────────────────────────────────────────────────────
async def handle_packets(token, body):
    if token not in sessions:
        return b""

    s = sessions[token]
    user = s["user"]
    off = 0

    while off < len(body) - 6:
        pid  = struct.unpack_from("<H", body, off)[0]; off += 2
        off += 1  # padding
        plen = struct.unpack_from("<I", body, off)[0]; off += 4
        pkt_body = body[off:off+plen]; off += plen

        # Ping
        if pid == 4:
            s["queue"] += pkt_pong()

        # Logout
        elif pid == 2:
            await disconnect(token)
            return bytes(s["queue"])

        # Status update (just ack)
        elif pid == 0:
            pass

        # Request stats
        elif pid == 3:
            u = await db_fetchrow("SELECT * FROM users WHERE id=$1", user["id"])
            if u:
                s["user"] = dict(u)
                s["queue"] += pkt_user_stats(u)

        # Public chat
        elif pid == 1:
            await handle_chat(token, pkt_body, public=True)

        # Private chat
        elif pid == 25:
            await handle_chat(token, pkt_body, public=False)

        # Channel join
        elif pid == 63:
            ch, _ = r_str(pkt_body, 0)
            s["channels"].add(ch)
            s["queue"] += pkt_channel_join_ok(ch)

        # Friend add
        elif pid == 73:
            tid = struct.unpack_from("<i", pkt_body, 0)[0]
            await friend_add(user["id"], tid)

        # Friend remove
        elif pid == 74:
            tid = struct.unpack_from("<i", pkt_body, 0)[0]
            await friend_remove(user["id"], tid)

        # User stats request
        elif pid == 85:
            count = struct.unpack_from("<h", pkt_body, 0)[0]
            for i in range(count):
                uid = struct.unpack_from("<i", pkt_body, 2+i*4)[0]
                u = await db_fetchrow("SELECT * FROM users WHERE id=$1", uid)
                if u: s["queue"] += pkt_user_stats(u) + pkt_user_presence(u)

    out = bytes(s["queue"])
    s["queue"] = bytearray()
    return out

# ── Chat ──────────────────────────────────────────────────────────────────────
async def handle_chat(token, body, public=True):
    s = sessions.get(token)
    if not s: return
    try:
        off = 0
        _, off = r_str(body, off)
        msg, off = r_str(body, off)
        ch,  off = r_str(body, off)
        if msg.startswith("!"):
            await bot_cmd(token, msg, ch); return
        mpkt = pkt_message(s["user"]["username"], msg, ch, s["user"]["id"])
        for tok, other in sessions.items():
            if tok != token and ch in other["channels"]:
                other["queue"] += mpkt
    except Exception as e:
        print(f"[CHAT] {e}")

async def bot_cmd(token, cmd, ch):
    s = sessions.get(token)
    if not s: return
    parts = cmd.strip().split()
    c = parts[0].lower()
    u = await db_fetchrow("SELECT * FROM users WHERE id=$1", s["user"]["id"])

    def reply(msg): return pkt_message("ScytheBot", msg, ch, 0)

    if c == "!help":
        s["queue"] += reply("Commands: !rank !pp !online")
    elif c == "!rank":
        s["queue"] += reply(f"{u['username']} — Rank #{u['rank']} | {u['pp'] or 0:.0f}pp | Acc {(u['accuracy'] or 0)*100:.2f}%")
    elif c == "!pp":
        s["queue"] += reply(f"{u['username']}: {u['pp'] or 0:.2f}pp")
    elif c == "!online":
        s["queue"] += reply(f"{len(sessions)} player(s) online on {SERVER_NAME}.")

# ── Friend helpers ────────────────────────────────────────────────────────────
async def friend_add(uid, tid):
    u = await db_fetchrow("SELECT friends FROM users WHERE id=$1", uid)
    if u:
        friends = list(u["friends"] or [])
        if tid not in friends:
            friends.append(tid)
            await db_execute("UPDATE users SET friends=$1 WHERE id=$2", friends, uid)

async def friend_remove(uid, tid):
    u = await db_fetchrow("SELECT friends FROM users WHERE id=$1", uid)
    if u:
        friends = [f for f in (u["friends"] or []) if f != tid]
        await db_execute("UPDATE users SET friends=$1 WHERE id=$2", friends, uid)

# ── Disconnect ────────────────────────────────────────────────────────────────
async def disconnect(token):
    s = sessions.pop(token, None)
    if not s: return
    uid = s["user"]["id"]
    print(f"[BANCHO] {s['user']['username']} disconnected. Online: {len(sessions)}")
    broadcast(pkt_logout(uid))

# ── HTTP handler ──────────────────────────────────────────────────────────────
async def bancho_handler(request):
    token = request.headers.get("osu-token")
    body  = await request.read()

    if not token:
        # Login request
        tok, resp, status = await handle_login(body)
        if tok == -1:
            return web.Response(
                body=resp,
                headers={"cho-token": "no", "cho-protocol": "19",
                         "Content-Type": "application/octet-stream"}
            )
        return web.Response(
            body=resp,
            headers={"cho-token": tok, "cho-protocol": "19",
                     "Content-Type": "application/octet-stream"}
        )
    else:
        # Subsequent packets
        resp = await handle_packets(token, body)
        return web.Response(
            body=resp,
            headers={"Content-Type": "application/octet-stream"}
        )

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
                        INSERT INTO featured_map (id,beatmap_md5,date) VALUES(1,$1,$2)
                        ON CONFLICT(id) DO UPDATE SET beatmap_md5=$1,date=$2
                    """, md5, today)
                    print(f"[BANCHO] Featured map → {md5}")
        except Exception as e:
            print(f"[BG] Error: {e}")

# ── Entry point ───────────────────────────────────────────────────────────────
async def main():
    print(f"""
╔══════════════════════════════════════════╗
║    SCYTHE BANCHO — Railway Deployment    ║
║    Port {BANCHO_PORT} | HTTP mode | Supabase       ║
╚══════════════════════════════════════════╝
    """)
    app = web.Application(client_max_size=10*1024*1024)
    app.router.add_post("/", bancho_handler)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", BANCHO_PORT)
    await site.start()
    print(f"[BANCHO] Listening on 0.0.0.0:{BANCHO_PORT}")
    await background_tasks()

if __name__ == "__main__":
    asyncio.run(main())
