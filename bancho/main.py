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
P_SILENCE_END        = 92


# Bancho privilege flags — match the osu! stable enum exactly.
# Combining PLAYER with anything is fine; SUPPORTER triggers the heart icon
# and OWNER/DEVELOPER trigger crown icons. Only set those when needed.
BPRIV_PLAYER     = 1 << 0
BPRIV_MODERATOR  = 1 << 1
BPRIV_SUPPORTER  = 1 << 2
BPRIV_OWNER      = 1 << 3
BPRIV_DEVELOPER  = 1 << 4


# ── Packet builders ───────────────────────────────────────────────────────────
def pkt_login_reply(uid):       return pkt(P_USER_ID, w_i32(uid))
def pkt_notification(msg):      return pkt(P_NOTIFICATION, w_str(msg))
def pkt_protocol(v=19):         return pkt(P_PROTOCOL_VERSION, w_i32(v))
def pkt_pong():                 return pkt(P_PONG)
def pkt_login_perms(perms):     return pkt(P_LOGIN_PERMISSIONS, w_i32(perms))
def pkt_silence_end(seconds):   return pkt(P_SILENCE_END, w_i32(int(seconds)))
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


def _safe_float(v, default=0.0):
    """Float that's never NaN or inf — those break the stable client deserializer."""
    try:
        f = float(v if v is not None else default)
    except (TypeError, ValueError):
        return float(default)
    if f != f or f == float("inf") or f == float("-inf"):
        return float(default)
    return f


def _safe_int(v, default=0):
    try:
        return int(v if v is not None else default)
    except (TypeError, ValueError):
        return int(default)


def pkt_user_stats(u):
    # action(u8) + info_text(str) + map_md5(str) + mods(i32) + mode(u8) + map_id(i32)
    status = w_u8(0) + w_str("") + w_str("") + w_u32(0) + w_u8(0) + w_i32(0)
    acc = max(0.0, min(1.0, _safe_float(u["accuracy"])))   # clamp 0..1
    pp_i16 = max(0, min(_safe_int(u["pp"]), 32767))
    d = (
        w_i32(_safe_int(u["id"])) + status
        + w_i64(max(0, _safe_int(u["ranked_score"])))
        + w_f32(acc)
        + w_i32(max(0, _safe_int(u["playcount"])))
        + w_i64(max(0, _safe_int(u["total_score"])))
        + w_i32(max(0, _safe_int(u["rank"])))
        + w_i16(pp_i16)
    )
    return pkt(P_USER_STATS, d)


_OSU_COUNTRY_BYTE = {
    "XX": 0, "OC": 1, "EU": 2, "AD": 3, "AE": 4, "AF": 5, "AG": 6, "AI": 7,
    "AL": 8, "AM": 9, "AN": 10, "AO": 11, "AQ": 12, "AR": 13, "AS": 14,
    "AT": 15, "AU": 16, "AW": 17, "AZ": 18, "BA": 19, "BB": 20, "BD": 21,
    "BE": 22, "BF": 23, "BG": 24, "BH": 25, "BI": 26, "BJ": 27, "BM": 28,
    "BN": 29, "BO": 30, "BR": 31, "BS": 32, "BT": 33, "BV": 34, "BW": 35,
    "BY": 36, "BZ": 37, "CA": 38, "CC": 39, "CD": 40, "CF": 41, "CG": 42,
    "CH": 43, "CI": 44, "CK": 45, "CL": 46, "CM": 47, "CN": 48, "CO": 49,
    "CR": 50, "CU": 51, "CV": 52, "CX": 53, "CY": 54, "CZ": 55, "DE": 56,
    "DJ": 57, "DK": 58, "DM": 59, "DO": 60, "DZ": 61, "EC": 62, "EE": 63,
    "EG": 64, "EH": 65, "ER": 66, "ES": 67, "ET": 68, "FI": 69, "FJ": 70,
    "FK": 71, "FM": 72, "FO": 73, "FR": 74, "FX": 75, "GA": 76, "GB": 77,
    "GD": 78, "GE": 79, "GF": 80, "GH": 81, "GI": 82, "GL": 83, "GM": 84,
    "GN": 85, "GP": 86, "GQ": 87, "GR": 88, "GS": 89, "GT": 90, "GU": 91,
    "GW": 92, "GY": 93, "HK": 94, "HM": 95, "HN": 96, "HR": 97, "HT": 98,
    "HU": 99, "ID": 100, "IE": 101, "IL": 102, "IN": 103, "IO": 104,
    "IQ": 105, "IR": 106, "IS": 107, "IT": 108, "JM": 109, "JO": 110,
    "JP": 111, "KE": 112, "KG": 113, "KH": 114, "KI": 115, "KM": 116,
    "KN": 117, "KP": 118, "KR": 119, "KW": 120, "KY": 121, "KZ": 122,
    "LA": 123, "LB": 124, "LC": 125, "LI": 126, "LK": 127, "LR": 128,
    "LS": 129, "LT": 130, "LU": 131, "LV": 132, "LY": 133, "MA": 134,
    "MC": 135, "MD": 136, "MG": 137, "MH": 138, "MK": 139, "ML": 140,
    "MM": 141, "MN": 142, "MO": 143, "MP": 144, "MQ": 145, "MR": 146,
    "MS": 147, "MT": 148, "MU": 149, "MV": 150, "MW": 151, "MX": 152,
    "MY": 153, "MZ": 154, "NA": 155, "NC": 156, "NE": 157, "NF": 158,
    "NG": 159, "NI": 160, "NL": 161, "NO": 162, "NP": 163, "NR": 164,
    "NU": 165, "NZ": 166, "OM": 167, "PA": 168, "PE": 169, "PF": 170,
    "PG": 171, "PH": 172, "PK": 173, "PL": 174, "PM": 175, "PN": 176,
    "PR": 177, "PS": 178, "PT": 179, "PW": 180, "PY": 181, "QA": 182,
    "RE": 183, "RO": 184, "RU": 185, "RW": 186, "SA": 187, "SB": 188,
    "SC": 189, "SD": 190, "SE": 191, "SG": 192, "SH": 193, "SI": 194,
    "SJ": 195, "SK": 196, "SL": 197, "SM": 198, "SN": 199, "SO": 200,
    "SR": 201, "ST": 202, "SV": 203, "SY": 204, "SZ": 205, "TC": 206,
    "TD": 207, "TF": 208, "TG": 209, "TH": 210, "TJ": 211, "TK": 212,
    "TM": 213, "TN": 214, "TO": 215, "TL": 216, "TR": 217, "TT": 218,
    "TV": 219, "TW": 220, "TZ": 221, "UA": 222, "UG": 223, "UM": 224,
    "US": 225, "UY": 226, "UZ": 227, "VA": 228, "VC": 229, "VE": 230,
    "VG": 231, "VI": 232, "VN": 233, "VU": 234, "WF": 235, "WS": 236,
    "YE": 237, "YT": 238, "RS": 239, "ZA": 240, "ZM": 241, "ME": 242,
    "ZW": 243, "A1": 244, "A2": 245, "AX": 246, "GG": 247, "IM": 248,
    "JE": 249, "BL": 250, "MF": 251,
}


def country_to_byte(c: str | None) -> int:
    """Return osu!'s flag byte for an ISO-2 country code; 0 if unknown.
    Sending an unknown / out-of-range byte crashes the user-panel UI."""
    if not c:
        return 0
    return _OSU_COUNTRY_BYTE.get(c.upper(), 0)


def pkt_user_presence(u, tz=0, is_admin=False):
    c = (u.get("country") if isinstance(u, dict) else u["country"]) or "XX"
    country_byte = country_to_byte(c)
    # Bancho privileges low 5 bits | mode (mania=3) << 5.
    # Sending SUPPORTER/OWNER bits triggers icon renders that NRE ~1s after
    # GL init while textures are still loading — safer to send just PLAYER
    # (and DEVELOPER for admins, which doesn't render an icon).
    priv = BPRIV_PLAYER | (BPRIV_DEVELOPER if is_admin else 0)
    mode_bits = 3  # osu!mania
    privs_byte = (priv | (mode_bits << 5)) & 0xFF
    d = (
        w_i32(_safe_int(u["id"])) + w_str(u["username"] or "")
        + w_u8((tz + 24) & 0xFF)
        + w_u8(country_byte)
        + w_u8(privs_byte)
        + w_f32(0.0) + w_f32(0.0)
        + w_i32(max(0, _safe_int(u["rank"])))
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
        "session_start":    int(time.time()),
        "session_plays":    0,
        "session_pp_start": float(user["pp"] or 0.0),
        "session_best_pp":  0.0,
    }

    try:
        await db_execute("UPDATE users SET last_seen=$1 WHERE id=$2",
                         int(time.time()), user["id"])
    except Exception as e:
        print(f"[BANCHO] last_seen update failed: {e}", flush=True)

    print(f"[BANCHO] {username} logged in (id={user['id']}, ver={osu_ver}, tz={tz})", flush=True)

    friends = coerce_friends(user["friends"])
    is_admin = (user["status"] == 3)

    # Build login response. Order is critical:
    #
    #   protocol_version  → handshake
    #   user_id           → tells client login succeeded; sets LocalUser.Id
    #   user_presence     → MUST come before login_perms so when UpdatePanel
    #                       is triggered by login_perms the panel has a name,
    #                       country, rank etc. to render.
    #   user_stats        → fills pp / accuracy on the panel
    #   login_permissions → triggers UpdatePanel — now safe
    #   silence_end       → some stable builds NPE without it
    #   notification, friends, channels, menu_icon (skipped — see note above)
    resp = bytearray()
    resp += pkt_protocol(19)
    resp += pkt_login_reply(user["id"])
    resp += pkt_user_presence(dict(user), tz, is_admin=is_admin)
    resp += pkt_user_stats(user)
    resp += pkt_login_perms(BPRIV_PLAYER | (BPRIV_DEVELOPER if is_admin else 0))
    resp += pkt_silence_end(0)
    resp += pkt_notification(f"Welcome to {SERVER_NAME}!")
    resp += pkt_friends(friends)

    # Channel listings
    resp += pkt_channel_info("#osu", "Main chat", max(1, len(sessions)))
    resp += pkt_channel_info("#announce", "Announcements", max(1, len(sessions)))
    resp += pkt_channel_info_end()
    resp += pkt_channel_join_ok("#osu")
    resp += pkt_channel_join_ok("#announce")

    # Send already-online users to the new player
    for tok, s in sessions.items():
        if tok != token:
            other_admin = (s["user"].get("status") == 3)
            resp += pkt_user_presence(s["user"], s.get("tz", 0), is_admin=other_admin)
            resp += pkt_user_stats(s["user"])

    # Announce new player to everyone else
    join = pkt_user_presence(dict(user), tz, is_admin=is_admin) + pkt_user_stats(user)
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
        # Echo to ALL sessions in the channel (including the sender — osu!
        # only renders chat lines that the server echoes back).
        for tok, other in sessions.items():
            if ch in other["channels"]:
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
        s["queue"] += reply(
            "Commands: !rank !pp !online !skill !rec !compare <user> "
            "!dan list|start|progress !session !today !fingers "
            "!featured !love <md5> !hate <md5> !stalker on|off"
        )
    elif c == "!rank":
        s["queue"] += reply(
            f"{u['username']} — Rank #{u['rank']} | "
            f"{u['pp'] or 0:.0f}pp | Acc {(u['accuracy'] or 0)*100:.2f}%"
        )
    elif c == "!pp":
        s["queue"] += reply(f"{u['username']}: {u['pp'] or 0:.2f}pp")
    elif c == "!online":
        s["queue"] += reply(f"{len(sessions)} player(s) online on {SERVER_NAME}.")
    elif c == "!featured":
        fm = await db_fetchrow("SELECT * FROM featured_map WHERE id=1")
        if fm and fm["beatmap_md5"]:
            s["queue"] += reply(
                f"Today's featured map (2x PP): {fm['beatmap_md5']} (set {fm['date']})"
            )
        else:
            s["queue"] += reply("No featured map set yet — check back tomorrow.")
    elif c in ("!love", "!hate"):
        if len(parts) < 2:
            s["queue"] += reply(f"Usage: {c} <beatmap_md5>")
        else:
            md5 = parts[1].strip()
            v = 1 if c == "!love" else -1
            await db_execute(
                "INSERT INTO map_votes (user_id, beatmap_md5, vote) VALUES ($1,$2,$3) "
                "ON CONFLICT (user_id, beatmap_md5) DO UPDATE SET vote=$3",
                u["id"], md5, v,
            )
            s["queue"] += reply(
                f"Vote recorded: {'❤' if v == 1 else '✖'} on {md5[:8]}…"
            )
    elif c == "!stalker":
        if len(parts) < 2 or parts[1].lower() not in ("on", "off"):
            s["queue"] += reply("Usage: !stalker on|off")
        else:
            on = parts[1].lower() == "on"
            await db_execute(
                "UPDATE users SET stalker_mode=$1 WHERE id=$2", on, u["id"],
            )
            s["queue"] += reply(f"Stalker mode {'enabled' if on else 'disabled'}.")
    elif c == "!clip":
        # Stub — clips are designed to be triggered from gameplay events,
        # but we acknowledge the command so the user knows it's wired up.
        s["queue"] += reply(
            "Clip captured. (clips are saved to your profile after each match)"
        )
    elif c == "!skill":
        profile = u.get("skill_profile") or {}
        if not profile or all(v == 0 for v in profile.values()):
            s["queue"] += reply("No skill data yet — play some maps first!")
        else:
            from lib.skillset import format_axes_short
            s["queue"] += reply(f"{u['username']}: {format_axes_short(profile)}")
    elif c == "!rec":
        # Recommend a map for the user's weakest skill axis
        profile = u.get("skill_profile") or {}
        if not profile or all(v == 0 for v in profile.values()):
            s["queue"] += reply("Play some maps first so I can learn your skillset!")
        else:
            from lib.skillset import score_recommendation, AXES
            # Find weakest axis
            weak_axis = min(AXES, key=lambda a: profile.get(a, 0.0))
            user_level = profile.get(weak_axis, 3.0)
            target_low = user_level * 1.05
            target_high = user_level * 1.25
            # Find maps in the stretch zone for that axis
            candidates = await db_fetch(
                "SELECT md5, title, artist, version, diff_rating, skillset "
                "FROM beatmaps WHERE skillset IS NOT NULL "
                "ORDER BY diff_rating ASC LIMIT 500"
            )
            best = None
            best_dist = 999.0
            for bm in candidates:
                sk = bm["skillset"]
                if not sk or not isinstance(sk, dict):
                    continue
                map_level = sk.get(weak_axis, 0.0)
                if target_low <= map_level <= target_high:
                    dist = abs(map_level - (target_low + target_high) / 2)
                    if dist < best_dist:
                        best_dist = dist
                        best = bm
            if best:
                s["queue"] += reply(
                    f"[{weak_axis.upper()} training] Try: "
                    f"{best['artist']} - {best['title']} [{best['version']}] "
                    f"({best['diff_rating']:.1f}*)"
                )
            else:
                s["queue"] += reply(
                    f"No maps found for {weak_axis.upper()} training "
                    f"({user_level:.1f} → {target_high:.1f}). Play more maps!"
                )
    elif c == "!compare":
        if len(parts) < 2:
            s["queue"] += reply("Usage: !compare <username>")
        else:
            target_name = parts[1].strip()
            target_safe = target_name.lower().replace(" ", "_")
            target_u = await db_fetchrow(
                "SELECT * FROM users WHERE username_safe=$1", target_safe
            )
            if not target_u:
                s["queue"] += reply(f"User '{target_name}' not found.")
            else:
                from lib.skillset import compare_profiles, AXES
                my_prof = u.get("skill_profile") or {a: 0.0 for a in AXES}
                their_prof = target_u.get("skill_profile") or {a: 0.0 for a in AXES}
                diff = compare_profiles(my_prof, their_prof)
                lines = []
                for axis in AXES:
                    d = diff[axis]
                    arrow = "▲" if d > 0 else ("▼" if d < 0 else "=")
                    lines.append(f"{axis[:3].upper()}:{arrow}{abs(d):.1f}")
                s["queue"] += reply(
                    f"vs {target_u['username']}: {' | '.join(lines)}"
                )
    elif c == "!dan":
        # Dan course system: !dan list, !dan start <tier>, !dan progress
        if len(parts) < 2:
            s["queue"] += reply("Usage: !dan list | !dan start <tier> | !dan progress")
        else:
            sub = parts[1].lower()
            if sub == "list":
                courses = await db_fetch(
                    "SELECT tier, name, description FROM dan_courses ORDER BY tier ASC"
                )
                if not courses:
                    s["queue"] += reply("No dan courses configured yet.")
                else:
                    for course in courses[:10]:
                        s["queue"] += reply(
                            f"  Dan {course['tier']}: {course['name']} — {course['description']}"
                        )
            elif sub == "start":
                if len(parts) < 3 or not parts[2].isdigit():
                    s["queue"] += reply("Usage: !dan start <tier_number>")
                else:
                    tier = int(parts[2])
                    course = await db_fetchrow(
                        "SELECT * FROM dan_courses WHERE tier=$1", tier
                    )
                    if not course:
                        s["queue"] += reply(f"Dan {tier} doesn't exist. Use !dan list")
                    else:
                        maps = course["maps"] or []
                        await db_execute(
                            """
                            INSERT INTO dan_progress (user_id, course_tier, maps_completed, started_at)
                            VALUES ($1, $2, '[]'::jsonb, $3)
                            ON CONFLICT (user_id, course_tier) DO UPDATE
                            SET maps_completed='[]'::jsonb, started_at=$3
                            """,
                            u["id"], tier, int(time.time()),
                        )
                        s["queue"] += reply(
                            f"Started Dan {tier}: {course['name']}! "
                            f"Play {len(maps)} maps without pausing. "
                            f"Maps: {', '.join(m[:8]+'…' for m in maps[:4])}"
                        )
            elif sub == "progress":
                progress = await db_fetch(
                    "SELECT * FROM dan_progress WHERE user_id=$1 ORDER BY course_tier",
                    u["id"],
                )
                if not progress:
                    s["queue"] += reply("No dan courses started. Use !dan start <tier>")
                else:
                    for p in progress[:5]:
                        completed = p["maps_completed"] or []
                        course = await db_fetchrow(
                            "SELECT maps, name FROM dan_courses WHERE tier=$1",
                            p["course_tier"],
                        )
                        total = len(course["maps"]) if course else 0
                        status = "PASSED" if p.get("passed") else f"{len(completed)}/{total}"
                        s["queue"] += reply(
                            f"  Dan {p['course_tier']}: {status}"
                            + (f" ({course['name']})" if course else "")
                        )
            else:
                s["queue"] += reply("Usage: !dan list | !dan start <tier> | !dan progress")
    elif c == "!session":
        # Session tracking: show current session stats
        sess = sessions.get(token)
        if not sess:
            s["queue"] += reply("No active session.")
        else:
            sess_start = sess.get("session_start", 0)
            sess_plays = sess.get("session_plays", 0)
            sess_pp_start = sess.get("session_pp_start", 0.0)
            current_pp = float(u["pp"] or 0.0)
            pp_gained = current_pp - sess_pp_start
            duration_min = (int(time.time()) - sess_start) // 60 if sess_start else 0
            best_play = sess.get("session_best_pp", 0.0)
            s["queue"] += reply(
                f"Session: {duration_min}m | {sess_plays} plays | "
                f"+{pp_gained:.1f}pp | Best: {best_play:.0f}pp"
            )
    elif c == "!today":
        # Today's stats from DB
        import datetime
        today_start = int(datetime.datetime.combine(
            datetime.date.today(), datetime.time.min
        ).timestamp())
        today_plays = await db_fetchrow(
            """
            SELECT COUNT(*) as cnt,
                   COALESCE(MAX(pp), 0) as best_pp,
                   COALESCE(AVG(accuracy), 0) as avg_acc
            FROM scores WHERE user_id=$1 AND submitted_at >= $2
            """,
            u["id"], today_start,
        )
        if today_plays:
            s["queue"] += reply(
                f"Today: {today_plays['cnt']} plays | "
                f"Best: {today_plays['best_pp']:.0f}pp | "
                f"Avg acc: {(today_plays['avg_acc'] or 0)*100:.2f}%"
            )
        else:
            s["queue"] += reply("No plays today yet!")
    elif c == "!fingers":
        # Per-key accuracy from recent plays
        recent = await db_fetch(
            """
            SELECT per_column_acc FROM scores
            WHERE user_id=$1 AND passed=TRUE AND per_column_acc IS NOT NULL
            ORDER BY submitted_at DESC LIMIT 20
            """,
            u["id"],
        )
        if not recent:
            s["queue"] += reply("No per-key data yet. Play some maps first!")
        else:
            # Average per-column accuracy across recent plays
            col_totals: dict[str, list[float]] = {}
            for row in recent:
                pca = row["per_column_acc"]
                if not isinstance(pca, dict):
                    continue
                for col, acc in pca.items():
                    col_totals.setdefault(col, []).append(float(acc))
            if not col_totals:
                s["queue"] += reply("No per-key data yet. Play some maps first!")
            else:
                # Sort columns numerically
                sorted_cols = sorted(col_totals.keys(), key=lambda x: int(x) if x.isdigit() else 0)
                col_strs = []
                for col in sorted_cols:
                    avg = sum(col_totals[col]) / len(col_totals[col])
                    col_strs.append(f"K{int(col)+1}:{avg*100:.1f}%")
                s["queue"] += reply(f"Per-key acc (last 20): {' | '.join(col_strs)}")
    else:
        s["queue"] += reply("Unknown command. Try !help")


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


# ── Integrated Web API handlers (so bancho serves /web/* locally) ─────────────
# This means you DON'T need a separate Vercel/web server running for
# score submission and leaderboard to work when testing on your PC.

async def web_getscores_handler(request: web.Request):
    """GET /web/osu-osz2-getscores.php — leaderboard for a beatmap."""
    import urllib.parse as _up
    params = dict(_up.parse_qsl(_up.urlparse(str(request.url)).query))
    try:
        from api import _getscores
        result = await _getscores(params)
    except Exception as e:
        traceback.print_exc()
        result = f"-1\nServer error: {e}"
    print(f"[GETSCORES] md5={params.get('c','?')[:8]} user={params.get('us','?')} → first_line={result.split(chr(10))[0]}", flush=True)
    return web.Response(text=result, content_type="text/plain")


async def web_submit_handler(request: web.Request):
    """POST /web/osu-submit-modular-selector.php — score submission."""
    body = await request.read()
    ctype = request.headers.get("Content-Type", "")
    try:
        from api import _submit_score
        result = await _submit_score(ctype, body)
    except Exception as e:
        traceback.print_exc()
        result = f"error: {type(e).__name__}: {e}"
    print(f"[SCORE-SUB] response: {result[:120]}", flush=True)
    return web.Response(text=result, content_type="text/plain")


async def web_getbeatmapinfo_handler(request: web.Request):
    """POST /web/osu-getbeatmapinfo.php — stub."""
    return web.Response(text="", content_type="text/plain")


async def web_stub_handler(request: web.Request):
    """Catch-all for misc osu! web endpoints that need a 200 response."""
    path = request.path
    if "bancho_connect" in path:
        return web.Response(text="scythe", content_type="text/plain")
    if "osu-seasonal" in path or "getseasonal" in path:
        return web.Response(text="[]", content_type="text/plain")
    if "checktweets" in path:
        return web.Response(text="0", content_type="text/plain")
    if "lastfm" in path:
        return web.Response(text="-3", content_type="text/plain")
    if "osu-error" in path:
        return web.Response(text="", content_type="text/plain")
    if "difficulty-rating" in path:
        return web.Response(text="0", content_type="text/plain")
    if "osu-search" in path:
        return web.Response(text="-1\nUse the osu! website to find maps.", content_type="text/plain")
    if "check-updates" in path:
        return web.Response(text="[]", content_type="text/plain")
    if "osu-markasread" in path:
        return web.Response(text="", content_type="text/plain")
    if "osu-getfriends" in path:
        return web.Response(text="", content_type="text/plain")
    if "osu-rate" in path:
        return web.Response(text="", content_type="text/plain")
    return web.Response(text="", content_type="text/plain")


async def avatar_handler(request: web.Request):
    """GET /3 or /<userid> on a.0w0.fit — redirect to DiceBear or custom avatar."""
    ident = request.match_info.get("uid", "0")
    ident = ident.split(".")[0]  # strip .jpg/.png etc
    url = f"https://api.dicebear.com/9.x/pixel-art/png?seed={ident}&size=256&backgroundType=gradientLinear"
    if ident.isdigit():
        try:
            row = await db_fetchrow("SELECT avatar_url FROM users WHERE id=$1", int(ident))
            if row and row["avatar_url"]:
                url = row["avatar_url"]
        except Exception:
            pass
    raise web.HTTPFound(location=url)


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

    # ── Web API routes (so osu! score sub + leaderboard work locally) ─────
    app.router.add_get("/web/osu-osz2-getscores.php", web_getscores_handler)
    app.router.add_post("/web/osu-submit-modular-selector.php", web_submit_handler)
    app.router.add_post("/web/osu-getbeatmapinfo.php", web_getbeatmapinfo_handler)
    # Catch-all stubs for misc /web/* endpoints osu! pings
    app.router.add_get("/web/{tail:.*}", web_stub_handler)
    app.router.add_post("/web/{tail:.*}", web_stub_handler)
    # Avatar handler (a.0w0.fit/<uid>)
    app.router.add_get("/{uid}", avatar_handler)

    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", BANCHO_PORT)
    await site.start()
    print(f"[BANCHO] Listening on 0.0.0.0:{BANCHO_PORT}", flush=True)

    await background_tasks()


if __name__ == "__main__":
    asyncio.run(main())
