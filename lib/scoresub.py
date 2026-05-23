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
    Tries cryptography first, falls back to Crypto (pycryptodome),
    then to a minimal pure-Python AES as last resort.
    """
    try:
        key = (b"osu!-scoreburgr---------" + osu_version.encode("utf-8")).ljust(32, b"\0")[:32]
        iv = base64.b64decode(encoded_iv)
        ct = base64.b64decode(encoded_score)
    except Exception:
        return None

    pt = None

    # Method 1: cryptography (fastest, C extension)
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
        decryptor = cipher.decryptor()
        pt = decryptor.update(ct) + decryptor.finalize()
    except Exception:
        pass

    # Method 2: pycryptodome (Crypto.Cipher)
    if pt is None:
        try:
            from Crypto.Cipher import AES as _AES
            cipher = _AES.new(key, _AES.MODE_CBC, iv)
            pt = cipher.decrypt(ct)
        except Exception:
            pass

    # Method 3: Pure-Python AES-CBC (no dependencies, slower but always works)
    if pt is None:
        try:
            pt = _aes_cbc_decrypt_pure(key, iv, ct)
        except Exception:
            return None

    if pt is None:
        return None

    # PKCS7 unpad (last byte = pad length)
    pad = pt[-1] if pt else 0
    if 0 < pad <= 16:
        pt = pt[:-pad]
    return pt.decode("utf-8", "ignore")


def _aes_cbc_decrypt_pure(key: bytes, iv: bytes, ct: bytes) -> bytes:
    """Minimal pure-Python AES-256-CBC decryption. No external deps."""
    # AES S-box
    _SBOX = [
        0x63,0x7c,0x77,0x7b,0xf2,0x6b,0x6f,0xc5,0x30,0x01,0x67,0x2b,0xfe,0xd7,0xab,0x76,
        0xca,0x82,0xc9,0x7d,0xfa,0x59,0x47,0xf0,0xad,0xd4,0xa2,0xaf,0x9c,0xa4,0x72,0xc0,
        0xb7,0xfd,0x93,0x26,0x36,0x3f,0xf7,0xcc,0x34,0xa5,0xe5,0xf1,0x71,0xd8,0x31,0x15,
        0x04,0xc7,0x23,0xc3,0x18,0x96,0x05,0x9a,0x07,0x12,0x80,0xe2,0xeb,0x27,0xb2,0x75,
        0x09,0x83,0x2c,0x1a,0x1b,0x6e,0x5a,0xa0,0x52,0x3b,0xd6,0xb3,0x29,0xe3,0x2f,0x84,
        0x53,0xd1,0x00,0xed,0x20,0xfc,0xb1,0x5b,0x6a,0xcb,0xbe,0x39,0x4a,0x4c,0x58,0xcf,
        0xd0,0xef,0xaa,0xfb,0x43,0x4d,0x33,0x85,0x45,0xf9,0x02,0x7f,0x50,0x3c,0x9f,0xa8,
        0x51,0xa3,0x40,0x8f,0x92,0x9d,0x38,0xf5,0xbc,0xb6,0xda,0x21,0x10,0xff,0xf3,0xd2,
        0xcd,0x0c,0x13,0xec,0x5f,0x97,0x44,0x17,0xc4,0xa7,0x7e,0x3d,0x64,0x5d,0x19,0x73,
        0x60,0x81,0x4f,0xdc,0x22,0x2a,0x90,0x88,0x46,0xee,0xb8,0x14,0xde,0x5e,0x0b,0xdb,
        0xe0,0x32,0x3a,0x0a,0x49,0x06,0x24,0x5c,0xc2,0xd3,0xac,0x62,0x91,0x95,0xe4,0x79,
        0xe7,0xc8,0x37,0x6d,0x8d,0xd5,0x4e,0xa9,0x6c,0x56,0xf4,0xea,0x65,0x7a,0xae,0x08,
        0xba,0x78,0x25,0x2e,0x1c,0xa6,0xb4,0xc6,0xe8,0xdd,0x74,0x1f,0x4b,0xbd,0x8b,0x8a,
        0x70,0x3e,0xb5,0x66,0x48,0x03,0xf6,0x0e,0x61,0x35,0x57,0xb9,0x86,0xc1,0x1d,0x9e,
        0xe1,0xf8,0x98,0x11,0x69,0xd9,0x8e,0x94,0x9b,0x1e,0x87,0xe9,0xce,0x55,0x28,0xdf,
        0x8c,0xa1,0x89,0x0d,0xbf,0xe6,0x42,0x68,0x41,0x99,0x2d,0x0f,0xb0,0x54,0xbb,0x16,
    ]
    _INV_SBOX = [0]*256
    for i, v in enumerate(_SBOX):
        _INV_SBOX[v] = i

    _RCON = [0x01,0x02,0x04,0x08,0x10,0x20,0x40,0x80,0x1b,0x36]

    def _xtime(a): return ((a<<1)^0x11b) & 0xff if a & 0x80 else (a<<1) & 0xff
    def _mul(a,b):
        r = 0
        for _ in range(8):
            if b & 1: r ^= a
            a = _xtime(a); b >>= 1
        return r

    # Key expansion for AES-256
    nk = len(key) // 4  # 8 for AES-256
    nr = nk + 6         # 14 rounds
    w = [int.from_bytes(key[4*i:4*i+4], 'big') for i in range(nk)]
    for i in range(nk, 4*(nr+1)):
        t = w[i-1]
        if i % nk == 0:
            # RotWord + SubWord + Rcon
            t = ((t << 8) | (t >> 24)) & 0xffffffff
            t = (_SBOX[(t>>24)&0xff]<<24)|(_SBOX[(t>>16)&0xff]<<16)|(_SBOX[(t>>8)&0xff]<<8)|_SBOX[t&0xff]
            t ^= _RCON[i//nk - 1] << 24
        elif nk > 6 and i % nk == 4:
            t = (_SBOX[(t>>24)&0xff]<<24)|(_SBOX[(t>>16)&0xff]<<16)|(_SBOX[(t>>8)&0xff]<<8)|_SBOX[t&0xff]
        w.append(w[i-nk] ^ t)

    def _inv_cipher_block(block: bytes) -> bytes:
        s = [list(block[i:i+4]) for i in range(0, 16, 4)]
        # Transpose to column-major
        state = [[s[r][c] for r in range(4)] for c in range(4)]
        # AddRoundKey (last round key)
        for c in range(4):
            ww = w[nr*4+c]
            for r in range(4):
                state[c][r] ^= (ww >> (24 - 8*r)) & 0xff
        for rnd in range(nr-1, 0, -1):
            # InvShiftRows
            for r in range(1, 4):
                row = [state[c][r] for c in range(4)]
                for c in range(4):
                    state[c][r] = row[(c - r) % 4]
            # InvSubBytes
            for c in range(4):
                for r in range(4):
                    state[c][r] = _INV_SBOX[state[c][r]]
            # AddRoundKey
            for c in range(4):
                ww = w[rnd*4+c]
                for r in range(4):
                    state[c][r] ^= (ww >> (24 - 8*r)) & 0xff
            # InvMixColumns
            for c in range(4):
                s0,s1,s2,s3 = state[c]
                state[c][0] = _mul(0x0e,s0)^_mul(0x0b,s1)^_mul(0x0d,s2)^_mul(0x09,s3)
                state[c][1] = _mul(0x09,s0)^_mul(0x0e,s1)^_mul(0x0b,s2)^_mul(0x0d,s3)
                state[c][2] = _mul(0x0d,s0)^_mul(0x09,s1)^_mul(0x0e,s2)^_mul(0x0b,s3)
                state[c][3] = _mul(0x0b,s0)^_mul(0x0d,s1)^_mul(0x09,s2)^_mul(0x0e,s3)
        # Final: InvShiftRows + InvSubBytes + AddRoundKey
        for r in range(1, 4):
            row = [state[c][r] for c in range(4)]
            for c in range(4):
                state[c][r] = row[(c - r) % 4]
        for c in range(4):
            for r in range(4):
                state[c][r] = _INV_SBOX[state[c][r]]
        for c in range(4):
            ww = w[c]
            for r in range(4):
                state[c][r] ^= (ww >> (24 - 8*r)) & 0xff
        # Transpose back
        out = bytearray(16)
        for c in range(4):
            for r in range(4):
                out[r*4+c] = state[c][r]
        return bytes(out)

    # CBC decrypt
    plaintext = bytearray()
    prev = iv
    for i in range(0, len(ct), 16):
        block = ct[i:i+16]
        decrypted = _inv_cipher_block(block)
        plaintext.extend(bytes(a ^ b for a, b in zip(decrypted, prev)))
        prev = block
    return bytes(plaintext)


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
