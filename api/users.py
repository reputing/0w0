"""
Scythe — User Registration
POST /users

Accepts:
  - user[username], user[user_email], user[plain_password]

Country is determined automatically — there is no manual override:
  1. Cloudflare's `cf-ipcountry` header (when CF is in front of Vercel)
  2. Vercel's `x-vercel-ip-country` header
  3. ip-api.com fallback (only if both edge headers are missing)
  4. "XX" (unknown) if all else fails
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from http.server import BaseHTTPRequestHandler
import urllib.parse, json, hashlib, asyncio
from urllib.request import urlopen, Request
from urllib.error import URLError

from lib.db import create_user, VALID_COUNTRIES


def detect_country_from_headers(headers) -> str | None:
    """Read country from Cloudflare/Vercel edge headers. Returns 2-letter code or None."""
    for h in ("cf-ipcountry", "x-vercel-ip-country"):
        v = (headers.get(h) or "").strip().upper()
        # CF returns "XX" for Tor / unknown; treat that as no signal.
        if v and v != "XX" and v in VALID_COUNTRIES:
            return v
    return None


def detect_country_from_ip(ip: str) -> str:
    """Fallback IP geolocation. Returns 2-letter ISO code or 'XX' on failure."""
    if not ip or ip.startswith("127.") or ip.startswith("::1") or ip == "":
        return "XX"
    try:
        url = f"http://ip-api.com/json/{ip}?fields=countryCode"
        req = Request(url, headers={"User-Agent": "Scythe/1.0"})
        with urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            cc = (data.get("countryCode") or "").upper()
            if cc and cc in VALID_COUNTRIES:
                return cc
    except (URLError, OSError, json.JSONDecodeError):
        pass
    return "XX"


class handler(BaseHTTPRequestHandler):

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8", errors="ignore")
        data = dict(urllib.parse.parse_qsl(body))

        # Get user's IP from common proxy headers (Vercel uses x-forwarded-for)
        ip = (
            self.headers.get("x-forwarded-for", "").split(",")[0].strip()
            or self.headers.get("x-real-ip", "")
            or self.client_address[0]
        )

        result, code = asyncio.run(self._handle(data, ip, self.headers))
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(result).encode())

    async def _handle(self, data, ip, headers):
        username = data.get("user[username]", "").strip()
        email    = data.get("user[user_email]", "").strip()
        password = data.get("user[plain_password]", "").strip()

        errors = {}
        if not username or len(username) < 2:
            errors["username"] = ["Username too short (min 2)"]
        elif len(username) > 15:
            errors["username"] = ["Username too long (max 15)"]
        if not email or "@" not in email:
            errors["email"] = ["Invalid email"]
        if not password or len(password) < 6:
            errors["password"] = ["Password too short (min 6)"]
        if errors:
            return {"form_error": {"user": errors}}, 400

        # Country is 100% auto-detected; user input is ignored.
        final_country = detect_country_from_headers(headers)
        if not final_country:
            loop = asyncio.get_event_loop()
            final_country = await loop.run_in_executor(None, detect_country_from_ip, ip)

        pw_md5 = hashlib.md5(password.encode()).hexdigest()
        uid, err = await create_user(username, pw_md5, email, country=final_country)
        if err:
            if "username" in err.lower():
                return {"form_error": {"user": {"username": ["Already taken"]}}}, 400
            return {"form_error": {"user": {"email": ["Already in use"]}}}, 400

        return {"user_id": uid, "country": final_country}, 200
