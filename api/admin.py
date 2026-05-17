"""
Scythe — Admin Panel API
POST /api/admin
All admin actions in one function, routed by ?action= param.
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from http.server import BaseHTTPRequestHandler
import urllib.parse, json, asyncio, hashlib, secrets, time

from lib.db import (
    fetchall, fetchone, fetchval, execute,
    set_featured_map, get_featured_map
)

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "changeme123")
_sessions: dict = {}


class handler(BaseHTTPRequestHandler):

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8", errors="ignore")
        try:
            data = json.loads(body)
        except Exception:
            data = dict(urllib.parse.parse_qsl(body))

        path   = urllib.parse.urlparse(self.path).path
        action = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("action", [""])[0]
        token  = self.headers.get("X-Admin-Token", "")

        result, code = asyncio.run(self._handle(action, data, token, path))
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(result).encode())

    def do_GET(self):
        path   = urllib.parse.urlparse(self.path).path
        action = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("action", [""])[0]
        token  = self.headers.get("X-Admin-Token", "") or \
                 urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("token", [""])[0]

        result, code = asyncio.run(self._handle(action, {}, token, path))
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(result).encode())

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "X-Admin-Token, Content-Type")
        self.end_headers()

    async def _handle(self, action: str, data: dict, token: str, path: str):

        # ── Login (no token needed) ───────────────────────────────────────────
        if action == "login":
            pw = data.get("password", "")
            if hashlib.md5(pw.encode()).hexdigest() == hashlib.md5(ADMIN_PASSWORD.encode()).hexdigest():
                t = secrets.token_hex(32)
                _sessions[t] = True
                return {"token": t}, 200
            return {"error": "Invalid credentials"}, 401

        # ── All other actions require token ───────────────────────────────────
        if token not in _sessions:
            return {"error": "Unauthorized"}, 401

        # ── Server stats ──────────────────────────────────────────────────────
        if action == "stats":
            total_users  = await fetchval("SELECT COUNT(*) FROM users")
            total_scores = await fetchval("SELECT COUNT(*) FROM scores")
            flagged      = await fetchval("SELECT COUNT(*) FROM scores WHERE ac_flagged=TRUE")
            unreviewed   = await fetchval("SELECT COUNT(*) FROM scores WHERE ac_flagged=TRUE AND ac_reviewed=FALSE")
            shadowbanned = await fetchval("SELECT COUNT(*) FROM users WHERE status=2")
            restricted   = await fetchval("SELECT COUNT(*) FROM users WHERE status=1")
            return {
                "server": "Scythe",
                "total_users": total_users,
                "total_scores": total_scores,
                "flagged_scores": flagged,
                "unreviewed_flags": unreviewed,
                "shadowbanned_users": shadowbanned,
                "restricted_users": restricted,
            }, 200

        # ── List users ────────────────────────────────────────────────────────
        if action == "users":
            rows = await fetchall("""
                SELECT id, username, email, pp, rank, playcount, status,
                       hot_streak, registered_at, last_seen, country
                FROM users ORDER BY pp DESC LIMIT 100
            """)
            status_map = {0: "normal", 1: "restricted", 2: "shadowbanned", 3: "admin"}
            return [
                {
                    "id": r["id"],
                    "username": r["username"],
                    "email": r["email"],
                    "pp": r["pp"],
                    "rank": r["rank"],
                    "playcount": r["playcount"],
                    "status": status_map.get(r["status"], "unknown"),
                    "status_code": r["status"],
                    "hot_streak": r["hot_streak"],
                    "last_seen": r["last_seen"],
                    "country": r["country"],
                }
                for r in rows
            ], 200

        # ── Shadowban ─────────────────────────────────────────────────────────
        if action == "shadowban":
            uid = data.get("user_id")
            if not uid:
                return {"error": "user_id required"}, 400
            await execute("UPDATE users SET status=2 WHERE id=$1 AND status!=3", int(uid))
            await execute("""
                INSERT INTO ac_log (user_id, flag_type, flag_detail, action_taken)
                VALUES ($1,'manual','Admin shadowban','shadowban')
            """, int(uid))
            return {"ok": True, "action": "shadowbanned", "user_id": uid}, 200

        # ── Full restrict ─────────────────────────────────────────────────────
        if action == "restrict":
            uid = data.get("user_id")
            if not uid:
                return {"error": "user_id required"}, 400
            await execute("UPDATE users SET status=1 WHERE id=$1 AND status!=3", int(uid))
            await execute("""
                INSERT INTO ac_log (user_id, flag_type, flag_detail, action_taken)
                VALUES ($1,'manual','Admin restrict','restrict')
            """, int(uid))
            return {"ok": True, "action": "restricted", "user_id": uid}, 200

        # ── Unrestrict ────────────────────────────────────────────────────────
        if action == "unrestrict":
            uid = data.get("user_id")
            if not uid:
                return {"error": "user_id required"}, 400
            await execute("UPDATE users SET status=0 WHERE id=$1", int(uid))
            return {"ok": True, "action": "unrestricted", "user_id": uid}, 200

        # ── Make admin ────────────────────────────────────────────────────────
        if action == "make_admin":
            uid = data.get("user_id")
            if not uid:
                return {"error": "user_id required"}, 400
            await execute("UPDATE users SET status=3 WHERE id=$1", int(uid))
            return {"ok": True, "action": "admin_granted", "user_id": uid}, 200

        # ── AC flags list ─────────────────────────────────────────────────────
        if action == "ac_flags":
            rows = await fetchall("""
                SELECT s.id as score_id, s.user_id, u.username, s.beatmap_md5,
                       s.pp, s.accuracy, s.max_combo, s.mods, s.submitted_at,
                       s.ac_flag_reason, s.ac_reviewed, u.status as user_status
                FROM scores s JOIN users u ON s.user_id=u.id
                WHERE s.ac_flagged=TRUE
                ORDER BY s.submitted_at DESC LIMIT 200
            """)
            return [
                {
                    "score_id": r["score_id"],
                    "user_id": r["user_id"],
                    "username": r["username"],
                    "beatmap_md5": r["beatmap_md5"],
                    "pp": r["pp"],
                    "accuracy": r["accuracy"],
                    "mods": r["mods"],
                    "submitted_at": r["submitted_at"],
                    "flag_reason": r["ac_flag_reason"],
                    "reviewed": r["ac_reviewed"],
                    "user_status": r["user_status"],
                }
                for r in rows
            ], 200

        # ── Review flagged score ──────────────────────────────────────────────
        if action == "review_score":
            sid    = data.get("score_id")
            result = data.get("result", "clear")  # "clear" or "confirm"
            if not sid:
                return {"error": "score_id required"}, 400
            await execute("UPDATE scores SET ac_reviewed=TRUE WHERE id=$1", int(sid))
            if result == "confirm":
                row = await fetchone("SELECT user_id FROM scores WHERE id=$1", int(sid))
                if row:
                    await execute("UPDATE users SET status=1 WHERE id=$1 AND status=2", row["user_id"])
                    await execute("""
                        INSERT INTO ac_log (user_id, score_id, flag_type, flag_detail, action_taken)
                        VALUES ($1,$2,'manual_review','Admin confirmed cheat','restrict')
                    """, row["user_id"], int(sid))
            return {"ok": True, "result": result, "score_id": sid}, 200

        # ── Wipe score from leaderboard ───────────────────────────────────────
        if action == "wipe_score":
            sid = data.get("score_id")
            if not sid:
                return {"error": "score_id required"}, 400
            await execute("UPDATE scores SET passed=FALSE WHERE id=$1", int(sid))
            return {"ok": True, "wiped": sid}, 200

        # ── AC log ────────────────────────────────────────────────────────────
        if action == "ac_log":
            rows = await fetchall("""
                SELECT l.*, u.username FROM ac_log l
                JOIN users u ON l.user_id=u.id
                ORDER BY l.flagged_at DESC LIMIT 100
            """)
            return [dict(r) for r in rows], 200

        # ── Set featured map ──────────────────────────────────────────────────
        if action == "set_featured":
            md5 = data.get("beatmap_md5", "").strip()
            if not md5:
                return {"error": "beatmap_md5 required"}, 400
            await set_featured_map(md5)
            return {"ok": True, "featured": md5}, 200

        if action == "get_featured":
            fm = await get_featured_map()
            return dict(fm) if fm else {}, 200

        # ── Update beatmap metadata (stars, AR, OD etc) ───────────────────────
        if action == "update_beatmap":
            md5    = data.get("md5", "").strip()
            stars  = data.get("stars")
            ar     = data.get("ar")
            od     = data.get("od")
            bmc    = data.get("max_combo")
            title  = data.get("title")
            artist = data.get("artist")
            if not md5:
                return {"error": "md5 required"}, 400
            updates, vals = [], []
            if stars  is not None: updates.append(f"diff_rating=${len(vals)+1}"); vals.append(float(stars))
            if ar     is not None: updates.append(f"ar=${len(vals)+1}");          vals.append(float(ar))
            if od     is not None: updates.append(f"od=${len(vals)+1}");          vals.append(float(od))
            if bmc    is not None: updates.append(f"max_combo=${len(vals)+1}");   vals.append(int(bmc))
            if title  is not None: updates.append(f"title=${len(vals)+1}");       vals.append(str(title))
            if artist is not None: updates.append(f"artist=${len(vals)+1}");      vals.append(str(artist))
            if updates:
                vals.append(md5)
                await execute(f"UPDATE beatmaps SET {','.join(updates)} WHERE md5=${len(vals)}", *vals)
            return {"ok": True, "md5": md5}, 200

        return {"error": f"Unknown action: {action}"}, 400
