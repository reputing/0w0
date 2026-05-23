"""
Scythe — User Registration
POST /users

Accepts:
  - user[username], user[user_email], user[plain_password]
  - user[country] (optional 2-letter ISO code; auto-detected from IP if missing)
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from http.server import BaseHTTPRequestHandler
import urllib.parse, json, hashlib, asyncio
from urllib.request import urlopen, Request
from urllib.error import URLError

from lib.db import create_user


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


def detect_country_from_ip(ip: str) -> str:
    """Free IP geolocation. Returns 2-letter ISO code or 'XX' on failure."""
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

        result, code = asyncio.run(self._handle(data, ip))
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(result).encode())

    async def _handle(self, data, ip):
        username = data.get("user[username]", "").strip()
        email    = data.get("user[user_email]", "").strip()
        password = data.get("user[plain_password]", "").strip()
        country  = (data.get("user[country]", "") or "").strip().upper()

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

        # Validate country override or auto-detect from IP
        if country and country in VALID_COUNTRIES:
            final_country = country
        else:
            loop = asyncio.get_event_loop()
            final_country = await loop.run_in_executor(None, detect_country_from_ip, ip)

        pw_md5 = hashlib.md5(password.encode()).hexdigest()
        uid, err = await create_user(username, pw_md5, email, country=final_country)
        if err:
            if "username" in err.lower():
                return {"form_error": {"user": {"username": ["Already taken"]}}}, 400
            return {"form_error": {"user": {"email": ["Already in use"]}}}, 400

        return {"user_id": uid, "country": final_country}, 200
