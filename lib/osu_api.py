"""
Scythe — osu! API v1 beatmap metadata fetcher

When a new map is played for the first time, we fetch its real metadata
from the osu! API so leaderboards show real names instead of "Unknown".

Requires OSU_API_KEY env var (get one from https://osu.ppy.sh/p/api).
If the key is not set, the fetcher silently does nothing (graceful degradation).
"""
from __future__ import annotations

import os
import json
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError
from typing import Optional

OSU_API_KEY = os.environ.get("OSU_API_KEY", "")
OSU_API_BASE = "https://osu.ppy.sh/api"


def fetch_beatmap_by_md5(md5: str) -> Optional[dict]:
    """
    Fetch beatmap metadata from osu! API v1 by MD5 hash.
    Returns a normalized dict ready to UPDATE into the beatmaps table,
    or None if the map wasn't found or the API is unavailable.
    """
    if not OSU_API_KEY or not md5:
        return None

    url = f"{OSU_API_BASE}/get_beatmaps?k={OSU_API_KEY}&h={md5}&limit=1"

    try:
        req = Request(url, headers={"User-Agent": "Scythe/1.0"})
        with urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (URLError, HTTPError, OSError, json.JSONDecodeError):
        return None

    if not data or not isinstance(data, list) or len(data) == 0:
        return None

    bm = data[0]

    # Normalize into our schema's column names
    result = {
        "beatmap_id":    _int(bm.get("beatmap_id")),
        "beatmapset_id": _int(bm.get("beatmapset_id")),
        "title":         bm.get("title") or "",
        "artist":        bm.get("artist") or "",
        "version":       bm.get("version") or "",
        "creator":       bm.get("creator") or "",
        "ar":            _float(bm.get("diff_approach")),
        "od":            _float(bm.get("diff_overall")),
        "cs":            _float(bm.get("diff_size")),  # CS = key count in mania
        "hp":            _float(bm.get("diff_drain")),
        "bpm":           _float(bm.get("bpm")),
        "total_length":  _int(bm.get("total_length")),
        "max_combo":     _int(bm.get("max_combo")),
        "diff_rating":   _float(bm.get("difficultyrating") or bm.get("difficulty_rating")),
        "tags":          bm.get("tags") or "",
        "mode":          _int(bm.get("mode")),  # 0=std, 1=taiko, 2=ctb, 3=mania
    }

    return result


async def fetch_and_update_beatmap(md5: str, db_execute, db_fetchrow) -> Optional[dict]:
    """
    Fetch from osu! API and update the beatmaps table in one shot.
    This is async-compatible — it runs the HTTP fetch in a thread pool
    to avoid blocking the event loop.

    Returns the fetched data dict, or None if fetch failed.
    """
    import asyncio

    # Run synchronous HTTP call in a thread
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, fetch_beatmap_by_md5, md5)

    if not result:
        return None

    # Build UPDATE query dynamically from available fields
    # Only update fields that have real values (not empty/zero)
    sets = []
    args = []
    idx = 1

    for col in ("beatmap_id", "beatmapset_id", "title", "artist", "version",
                "creator", "ar", "od", "cs", "hp", "bpm", "total_length",
                "max_combo", "diff_rating"):
        val = result.get(col)
        if val and val != 0 and val != "":
            sets.append(f"{col}=${idx}")
            args.append(val)
            idx += 1

    # Store tags in a new column or append to version for skillset detection
    tags = result.get("tags", "")
    if tags:
        # We don't have a dedicated tags column, but we can store it
        # by appending to the version field for keyword detection.
        # Better: add a tags column. Let's do that.
        sets.append(f"tags=${idx}")
        args.append(tags)
        idx += 1

    if not sets:
        return result

    args.append(md5)
    query = f"UPDATE beatmaps SET {', '.join(sets)} WHERE md5=${idx}"

    try:
        await db_execute(query, *args)
    except Exception as e:
        # Column might not exist (tags) — try without it
        if "tags" in str(e):
            sets_no_tags = [s for s in sets if "tags" not in s]
            args_no_tags = args[:-2]  # remove tags val and md5
            args_no_tags.append(md5)
            idx2 = len(args_no_tags)
            query2 = f"UPDATE beatmaps SET {', '.join(sets_no_tags)} WHERE md5=${idx2}"
            try:
                await db_execute(query2, *args_no_tags)
            except Exception:
                pass

    return result


def _int(v) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _float(v) -> float:
    try:
        return float(v or 0.0)
    except (TypeError, ValueError):
        return 0.0
