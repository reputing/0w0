"""
Scythe — User Registration
POST /api/users
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from http.server import BaseHTTPRequestHandler
import urllib.parse, json, hashlib, asyncio

from lib.db import create_user


class handler(BaseHTTPRequestHandler):

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8", errors="ignore")
        data = dict(urllib.parse.parse_qsl(body))
        result, code = asyncio.run(self._handle(data))
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(result).encode())

    async def _handle(self, data):
        username = data.get("user[username]", "").strip()
        email    = data.get("user[user_email]", "").strip()
        password = data.get("user[plain_password]", "").strip()

        errors = {}
        if not username or len(username) < 2:
            errors["username"] = ["Username too short (min 2)"]
        if len(username) > 15:
            errors["username"] = ["Username too long (max 15)"]
        if not email or "@" not in email:
            errors["email"] = ["Invalid email"]
        if not password or len(password) < 6:
            errors["password"] = ["Password too short (min 6)"]
        if errors:
            return {"form_error": {"user": errors}}, 400

        pw_md5 = hashlib.md5(password.encode()).hexdigest()
        uid, err = await create_user(username, pw_md5, email)
        if err:
            if "username" in err.lower():
                return {"form_error": {"user": {"username": ["Already taken"]}}}, 400
            return {"form_error": {"user": {"email": ["Already in use"]}}}, 400

        return {"user_id": uid}, 200
