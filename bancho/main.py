"""
Scythe — Bancho TCP Server (Railway deployment)
Handles osu! stable client persistent connection.
Connects to Supabase for all data.
"""
import asyncio
import hashlib
import struct
import json
import time
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

# ── Config from env ───────────────────────────────────────────────────────────
SERVER_NAME   = os.environ.get("SERVER_NAME", "Scythe")
SERVER_DOMAIN = os.environ.get("SERVER_DOMAIN", "scythe.gg")
BANCHO_PORT   = int(os.environ.get("PORT", 13381))

# ── Supabase via asyncpg ──────────────────────────────────────────────────────
import asyncpg

DATABASE_URL = os.environ["SUPABASE_DB_URL"]
_pool = None

async def get_pool():
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=10)
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

# ── Online registry ───────────────────────────────────────────────────────────
online: dict = {}   # user_id → BanchoClient

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
    if data[off] == 0: return "", off+1
    off += 1; length = shift = 0
    while True:
        byte = data[off]; off += 1
        length |= (byte & 0x7F) << shift; shift += 7
        if not (byte & 0x80): break
    return data[off:off+length].decode("utf-8","ignore"), off+length

def r_i32(data, off): return struct.unpack_from("<i",data,off)[0], off+4

# ── Server → Client packet builders ──────────────────────────────────────────
def pkt_login_reply(uid):    return pkt(5, w_i32(uid))
def pkt_notification(msg):   return pkt(24, w_str(msg))
def pkt_protocol(v=19):      return pkt(75, w_i32(v))
def pkt_pong():               return pkt(8)
def pkt_logout(uid):         return pkt(12, w_i32(uid))
def pkt_friends(ids):
    d = w_i16(len(ids))
    for i in ids: d += w_i32(i)
    return pkt(102, d)
def pkt_channel_info(name, topic, count):
    return pkt(65, w_str(name)+w_str(topic)+w_i16(count))
def pkt_channel_join_ok(name): return pkt(64, w_str(name))
def pkt_menu_icon(icon, url):  return pkt(26, w_str(f"{icon}|{url}"))
def pkt_message(sender, text, channel, sid):
    return pkt(7, w_str(sender)+w_str(text)+w_str(channel)+w_i32(sid))

def pkt_user_stats(u):
    status = (w_i8(0)+w_str("")+w_str("")+w_u32(0)+w_i8(0)+w_i32(0))
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

# ── Client session ────────────────────────────────────────────────────────────
class BanchoClient:
    def __init__(self, reader, writer, user):
        self.reader   = reader
        self.writer   = writer
        self.user     = dict(user)
        self.user_id  = user["id"]
        self.username = user["username"]
        self.queue    = asyncio.Queue()
        self.channels = {"#osu"}
        self.spectating = None
        self.spectators = set()

    async def send(self, data):
        try: self.writer.write(data); await self.writer.drain()
        except Exception: pass

    async def enqueue(self, data): await self.queue.put(data)

    async def flush(self):
        buf = b""
        while not self.queue.empty():
            buf += await self.queue.get()
        if buf: await self.send(buf)

# ── Login ─────────────────────────────────────────────────────────────────────
async def parse_login(reader):
    try:
        raw = await asyncio.wait_for(reader.read(4096), timeout=10)
        lines = raw.decode("utf-8","ignore").split("\n")
        if len(lines) < 3: return None
        username = lines[0].strip()
        pw_md5   = lines[1].strip()
        parts    = lines[2].strip().split("|")
        tz       = int(parts[1]) if len(parts) > 1 else 0
        return username, pw_md5, tz
    except Exception: return None

# ── Connection handler ────────────────────────────────────────────────────────
async def handle_conn(reader, writer):
    addr = writer.get_extra_info("peername")
    parsed = await parse_login(reader)
    if not parsed: writer.close(); return

    username, pw_md5, tz = parsed
    safe = username.lower().replace(" ", "_")
    user = await db_fetchrow("SELECT * FROM users WHERE username_safe=$1", safe)

    if not user:
        writer.write(pkt_login_reply(-1) + pkt_notification("User not found."))
        await writer.drain(); writer.close(); return

    if user["password_md5"] != pw_md5:
        writer.write(pkt_login_reply(-1) + pkt_notification("Wrong password."))
        await writer.drain(); writer.close(); return

    if user["status"] == 1:
        writer.write(pkt_login_reply(-3) + pkt_notification(
            "Your account has been restricted."))
        await writer.drain(); writer.close(); return

    # status=2 (shadowban) logs in normally — silent
    client = BanchoClient(reader, writer, user)
    online[user["id"]] = client
    print(f"[BANCHO] {username} logged in (id={user['id']}, status={user['status']})")

    # Update last seen
    await db_execute("UPDATE users SET last_seen=$1 WHERE id=$2", int(time.time()), user["id"])

    friends = user["friends"] or []

    # Build login burst
    resp = b""
    resp += pkt_login_reply(user["id"])
    resp += pkt_protocol(19)
    resp += pkt_notification(f"Welcome to {SERVER_NAME}! PP on all maps | Ghost scores | Hot streak PP")
    resp += pkt_menu_icon("", f"https://{SERVER_DOMAIN}")
    resp += pkt_friends(list(friends))
    resp += pkt_channel_info("#osu", "Main", len(online))
    resp += pkt_channel_info("#announce", "Announcements", 1)
    resp += pkt_channel_join_ok("#osu")
    resp += pkt_channel_join_ok("#announce")
    resp += pkt_user_stats(user)
    resp += pkt_user_presence(user, tz)

    for uid, oc in online.items():
        if uid != user["id"]:
            resp += pkt_user_presence(oc.user) + pkt_user_stats(oc.user)

    # Announce arrival
    join_pkt = (pkt_user_presence(user) + pkt_user_stats(user) +
                pkt_message(SERVER_NAME, f"{username} connected!", "#osu", 0))
    for uid, oc in online.items():
        if uid != user["id"]:
            await oc.enqueue(join_pkt)

    await client.send(resp)

    # ── Packet loop ───────────────────────────────────────────────────────────
    try:
        while True:
            await client.flush()
            try:
                header = await asyncio.wait_for(reader.read(7), timeout=65)
            except asyncio.TimeoutError:
                break
            if len(header) < 7: break
            pid  = struct.unpack_from("<H", header, 0)[0]
            plen = struct.unpack_from("<I", header, 3)[0]
            body = await reader.read(min(plen, 65536)) if plen else b""
            await handle_pkt(client, pid, body)
    except Exception as e:
        print(f"[BANCHO] {username} error: {e}")
    finally:
        await disconnect(client)

# ── Packet dispatch ───────────────────────────────────────────────────────────
async def handle_pkt(client, pid, body):
    # Ping
    if pid == 4:
        await client.send(pkt_pong())

    # Logout
    elif pid == 2:
        raise ConnectionError("logout")

    # Request stats update
    elif pid == 3:
        u = await db_fetchrow("SELECT * FROM users WHERE id=$1", client.user_id)
        if u: await client.send(pkt_user_stats(u))

    # Public chat
    elif pid == 1:
        await handle_chat(client, body, public=True)

    # Private message
    elif pid == 25:
        await handle_chat(client, body, public=False)

    # Channel join
    elif pid == 63:
        ch, _ = r_str(body, 0)
        client.channels.add(ch)
        await client.send(pkt_channel_join_ok(ch))

    # Friend add
    elif pid == 73:
        tid = struct.unpack_from("<i", body, 0)[0]
        await friend_add(client.user_id, tid)

    # Friend remove
    elif pid == 74:
        tid = struct.unpack_from("<i", body, 0)[0]
        await friend_remove(client.user_id, tid)

    # Start spectating
    elif pid == 16:
        tid = struct.unpack_from("<i", body, 0)[0]
        await start_spec(client, tid)

    # Stop spectating
    elif pid == 17:
        await stop_spec(client)

    # Stats request for list of users
    elif pid == 85:
        count = struct.unpack_from("<h", body, 0)[0]
        for i in range(count):
            uid = struct.unpack_from("<i", body, 2+i*4)[0]
            u   = await db_fetchrow("SELECT * FROM users WHERE id=$1", uid)
            if u: await client.send(pkt_user_stats(u)+pkt_user_presence(u))

# ── Chat & bot commands ───────────────────────────────────────────────────────
async def handle_chat(client, body, public=True):
    try:
        off = 0
        _, off = r_str(body, off)
        msg, off = r_str(body, off)
        ch,  off = r_str(body, off)

        if msg.startswith("!"):
            await bot_cmd(client, msg, ch); return

        mpkt = pkt_message(client.username, msg, ch, client.user_id)
        for uid, oc in online.items():
            if uid != client.user_id and ch in oc.channels:
                await oc.enqueue(mpkt)
    except Exception as e:
        print(f"[CHAT] {e}")

async def bot_cmd(client, cmd, ch):
    parts = cmd.strip().split()
    c     = parts[0].lower()
    u     = await db_fetchrow("SELECT * FROM users WHERE id=$1", client.user_id)

    def reply(msg):
        return pkt_message("ScytheBot", msg, ch, 0)

    if c == "!help":
        await client.send(reply(
            "Commands: !rank !pp !online !featured !stalker on/off !love <md5> !hate <md5> !clip"
        ))
    elif c == "!rank":
        await client.send(reply(
            f"{client.username} — Rank #{u['rank']} | {u['pp']:.0f}pp | Acc {(u['accuracy'] or 0)*100:.2f}%"
        ))
    elif c == "!pp":
        await client.send(reply(f"{client.username}: {u['pp']:.2f}pp | Streak: {u['hot_streak']}x"))
    elif c == "!online":
        await client.send(reply(f"{len(online)} player(s) online on {SERVER_NAME}."))
    elif c == "!featured":
        fm = await db_fetchrow("SELECT * FROM featured_map WHERE id=1")
        if fm: await client.send(reply(f"Today's featured map (2x PP!): {fm['beatmap_md5']}"))
        else:  await client.send(reply("No featured map set today."))
    elif c == "!stalker" and len(parts) > 1:
        on = parts[1].lower() == "on"
        await db_execute("UPDATE users SET stalker_mode=$1 WHERE id=$2", on, client.user_id)
        await client.send(reply(f"Stalker mode {'ON' if on else 'OFF'}."))
    elif c == "!clip":
        await client.send(reply(f"Score bookmarked! View at https://{SERVER_DOMAIN}/u/{client.user_id}/clips"))
    elif c == "!love" and len(parts) > 1:
        md5 = parts[1]
        await db_execute("""
            INSERT INTO map_votes (user_id,beatmap_md5,vote) VALUES($1,$2,1)
            ON CONFLICT(user_id,beatmap_md5) DO UPDATE SET vote=1
        """, client.user_id, md5)
        await client.send(reply(f"Voted LOVE on {md5[:10]}..."))
    elif c == "!hate" and len(parts) > 1:
        md5 = parts[1]
        await db_execute("""
            INSERT INTO map_votes (user_id,beatmap_md5,vote) VALUES($1,$2,-1)
            ON CONFLICT(user_id,beatmap_md5) DO UPDATE SET vote=-1
        """, client.user_id, md5)
        await client.send(reply(f"Voted HATE on {md5[:10]}..."))

    # Admin commands
    elif c == "!shadowban" and u["status"] == 3 and len(parts) > 1:
        safe = parts[1].lower()
        await db_execute("UPDATE users SET status=2 WHERE username_safe=$1 AND status!=3", safe)
        await client.send(reply(f"[ADMIN] {parts[1]} shadowbanned."))
    elif c == "!restrict" and u["status"] == 3 and len(parts) > 1:
        safe = parts[1].lower()
        await db_execute("UPDATE users SET status=1 WHERE username_safe=$1 AND status!=3", safe)
        await client.send(reply(f"[ADMIN] {parts[1]} restricted."))
    elif c == "!unrestrict" and u["status"] == 3 and len(parts) > 1:
        safe = parts[1].lower()
        await db_execute("UPDATE users SET status=0 WHERE username_safe=$1", safe)
        await client.send(reply(f"[ADMIN] {parts[1]} unrestricted."))

# ── Spectating ────────────────────────────────────────────────────────────────
async def start_spec(client, target_id):
    if target_id in online:
        tgt = online[target_id]
        tgt.spectators.add(client.user_id)
        client.spectating = target_id
        tu = await db_fetchrow("SELECT stalker_mode FROM users WHERE id=$1", target_id)
        if tu and tu["stalker_mode"]:
            await tgt.enqueue(pkt_message(
                "ScytheBot", f"{client.username} is spectating you!", "#osu", 0
            ))

async def stop_spec(client):
    if client.spectating and client.spectating in online:
        online[client.spectating].spectators.discard(client.user_id)
    client.spectating = None

# ── Friend management ─────────────────────────────────────────────────────────
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
async def disconnect(client):
    online.pop(client.user_id, None)
    lpkt = pkt_logout(client.user_id)
    for oc in online.values():
        await oc.enqueue(lpkt)
    try: client.writer.close()
    except Exception: pass
    print(f"[BANCHO] {client.username} disconnected. Online: {len(online)}")

# ── Background: rank recalc + featured map rotation ──────────────────────────
async def background_tasks():
    import datetime, random
    while True:
        try:
            await asyncio.sleep(300)

            # Rank recalc every 5 min
            await db_execute("""
                UPDATE users SET rank=(
                    SELECT COUNT(*)+1 FROM users u2
                    WHERE u2.pp>users.pp AND u2.status!=1
                ) WHERE status!=1
            """)
            await db_execute("UPDATE users SET rank=0 WHERE status=1")

            # Featured map rotation (daily)
            today = datetime.date.today().isoformat()
            fm    = await db_fetchrow("SELECT date FROM featured_map WHERE id=1")
            if not fm or fm["date"] != today:
                candidate = await db_fetchrow("""
                    SELECT DISTINCT beatmap_md5 FROM scores WHERE passed=TRUE ORDER BY RANDOM() LIMIT 1
                """)
                if candidate:
                    md5 = candidate["beatmap_md5"]
                    await db_execute("""
                        INSERT INTO featured_map (id,beatmap_md5,date) VALUES(1,$1,$2)
                        ON CONFLICT(id) DO UPDATE SET beatmap_md5=$1,date=$2
                    """, md5, today)
                    msg = pkt_message(
                        "ScytheBot",
                        f"Today's featured map (2x PP!): {md5} — use !featured",
                        "#announce", 0
                    )
                    for oc in online.values():
                        await oc.enqueue(msg)
                    print(f"[BANCHO] Featured map rotated → {md5}")

        except Exception as e:
            print(f"[BG] Error: {e}")

# ── Entry point ───────────────────────────────────────────────────────────────
async def main():
    print(f"""
╔══════════════════════════════════════════╗
║    SCYTHE BANCHO — Railway Deployment    ║
║    Port {BANCHO_PORT} | Supabase backend          ║
╚══════════════════════════════════════════╝
    """)
    server = await asyncio.start_server(handle_conn, "0.0.0.0", BANCHO_PORT)
    print(f"[BANCHO] Listening on 0.0.0.0:{BANCHO_PORT}")
    async with server:
        await asyncio.gather(
            server.serve_forever(),
            background_tasks()
        )

if __name__ == "__main__":
    asyncio.run(main())
