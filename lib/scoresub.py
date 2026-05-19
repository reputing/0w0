"""
Scythe — Score submission helpers
- Multipart/form-data parser (no external lib)
- AES-256-CBC decryption of the encrypted `score` field that osu! stable sends
- Score string parser with the CORRECT field offsets

This was previously broken in two ways:
  1. The handler used urllib.parse_qsl() on the raw multipart body, which
     never produced any fields, so the score string was always empty.
  2. Field offsets were off-by-one (parts[2] is 'online_md5', not count300).
Score submission has therefore never worked end-to-end.
"""
from __future__ import annotations

import base64
import re
from typing import Dict, Optional


# ── Multipart ─────────────────────────────────────────────────────────────────

def parse_multipart(content_type: str, body: bytes) -> Dict[str, bytes]:
    """
    Minimal multipart/form-data parser.
    Returns {field_name: bytes_value}.
    Falls back to URL-encoded if Content-Type isn't multipart.
    """
    if not content_type or "multipart/form-data" not in content_type.lower():
        # Fallback: URL-encoded body
        try:
            from urllib.parse import parse_qsl
            text = body.decode("utf-8", "ignore")
            return {k: v.encode("utf-8") for k, v in parse_qsl(text, keep_blank_values=True)}
        except Exception:
            return {}

    m = re.search(r'boundary=(?:"([^"]+)"|([^\s;]+))', content_type)
    if not m:
        return {}
    boundary = (m.group(1) or m.group(2)).encode("latin-1")
    delimiter = b"--" + boundary

    out: Dict[str, bytes] = {}
    for part in body.split(delimiter):
        # Strip leading/trailing CRLFs and the closing "--"
        part = part.strip(b"\r\n")
        if not part or part == b"--":
            continue
        # Each part has headers, then \r\n\r\n, then value
        head_end = part.find(b"\r\n\r\n")
        if head_end == -1:
            continue
        head = part[:head_end].decode("latin-1", "ignore")
        value = part[head_end + 4:]
        # Trim trailing CRLF before the next boundary
        if value.endswith(b"\r\n"):
            value = value[:-2]
        nm = re.search(r'name="([^"]+)"', head)
        if nm:
            out[nm.group(1)] = value
    return out


# ── AES decryption ────────────────────────────────────────────────────────────

def decrypt_score(encoded_score: bytes, encoded_iv: bytes, osu_version: str) -> Optional[str]:
    """
    osu! stable encrypts the score string with AES-256-CBC.
    Key:  b"osu!-scoreburgr---------" + osu_version (UTF-8)
    IV:   provided as base64
    Plaintext is PKCS7-padded.

    Returns the decrypted score string, or None on failure.
    """
    try:
        # Lazy import so /api/users.py and /api/misc.py don't need cryptography.
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

        key = (b"osu!-scoreburgr---------" + osu_version.encode("utf-8")).ljust(32, b"\0")[:32]
        iv = base64.b64decode(encoded_iv)
        ct = base64.b64decode(encoded_score)

        cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
        decryptor = cipher.decryptor()
        pt = decryptor.update(ct) + decryptor.finalize()
        # PKCS7 unpad (last byte = pad length)
        pad = pt[-1] if pt else 0
        if 0 < pad <= 16:
            pt = pt[:-pad]
        return pt.decode("utf-8", "ignore")
    except Exception:
        return None


# ── Score string parser (CORRECT offsets) ─────────────────────────────────────

# osu! stable score string format (colon-separated, 18 fields):
#   0  beatmap_md5
#   1  username (note: trailing space if user is "supporter")
#   2  online_md5  (replay hash; we ignore)
#   3  count_300
#   4  count_100
#   5  count_50
#   6  count_geki   (mania-only; "perfect 300" in std)
#   7  count_katu   (mania-only)
#   8  count_miss
#   9  total_score
#   10 max_combo
#   11 perfect      ("True"/"False" — full combo)
#   12 rank         (server should re-derive from accuracy)
#   13 mods         (integer bitfield)
#   14 passed       ("True"/"False")
#   15 mode         (0=std, 1=taiko, 2=ctb, 3=mania)
#   16 play_time    (osu! local timestamp string)
#   17 osu_version  (e.g. "b20231130.2")

def parse_score_string(s: str) -> Optional[dict]:
    parts = s.split(":")
    if len(parts) < 15:
        return None
    try:
        return {
            "beatmap_md5":  parts[0].strip(),
            "username":     parts[1].rstrip(" "),
            "count300":     int(parts[3]),
            "count100":     int(parts[4]),
            "count50":      int(parts[5]),
            "countgeki":    int(parts[6]) if parts[6].isdigit() else 0,
            "countkatu":    int(parts[7]) if parts[7].isdigit() else 0,
            "countmiss":    int(parts[8]),
            "score":        int(parts[9]),
            "max_combo":    int(parts[10]),
            "is_fc":        parts[11].strip() in ("True", "1", "true"),
            "client_rank":  parts[12].strip(),
            "mods":         int(parts[13]),
            "passed":       parts[14].strip() in ("True", "1", "true"),
            "mode":         int(parts[15]) if len(parts) > 15 and parts[15].isdigit() else 0,
            "osu_version":  parts[17].strip() if len(parts) > 17 else "",
        }
    except (ValueError, IndexError):
        return None
