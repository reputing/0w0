"""
Scythe — Mania-specific MSD-style skill profiler

Seven skill axes (matching Etterna's terminology for VSRG):
  - stream       : fast single-note runs in one or few columns
  - jumpstream   : streams interleaved with doubles (jumps)
  - handstream   : streams interleaved with triples+
  - jacks        : same-column repeated notes (speed jacks)
  - chordjack    : chord patterns with jack elements
  - tech         : irregular rhythms, SV changes, mixed snaps
  - stamina      : sustained density over long maps

Detection strategy:
  We don't have access to the raw .osu file from the server side, so we
  use a heuristic combining:
    1. Map title/version/tags keywords (most reliable for categorization)
    2. Metadata (BPM, OD, length, max_combo, diff_rating) for scaling

  This gives surprisingly good results because the mania mapping community
  is extremely consistent about labeling their maps:
    - "[Jack]", "Chordjack", "CJ" in title/version
    - "Stream", "Speed" in title/version
    - "LN", "Long Note", "Noodle" for LN-heavy (mapped to tech)
    - "Stamina", "Marathon" for long maps
    - "Tech", "SV" for technical maps
    - "JS", "Jumpstream", "HS", "Handstream" explicitly

Two computations:
  1. compute_beatmap_skillset(b) -> dict of axis -> float
  2. compute_user_profile(top_scores_with_skillsets) -> dict of axis -> float

Both vectors live in the same ~star-rating-like scale (~1-10).
"""
from __future__ import annotations

import math
import re
from typing import Iterable

AXES = ("stream", "jumpstream", "handstream", "jacks", "chordjack", "tech", "stamina")

# ── Pattern detection from text ──────────────────────────────────────────────

# Each pattern maps a regex to the axes it boosts (weight 0-1)
_PATTERN_KEYWORDS = [
    # Jacks
    (re.compile(r"\b(jack|jacks|jackspeed)\b", re.I), {"jacks": 1.0, "chordjack": 0.3}),
    (re.compile(r"\b(chordjack|cj|chord\s*jack)\b", re.I), {"chordjack": 1.0, "jacks": 0.4}),
    # Streams
    (re.compile(r"\b(stream|streams|speed|rice)\b", re.I), {"stream": 1.0, "stamina": 0.3}),
    (re.compile(r"\b(jumpstream|js|jump\s*stream)\b", re.I), {"jumpstream": 1.0, "stream": 0.4}),
    (re.compile(r"\b(handstream|hs|hand\s*stream)\b", re.I), {"handstream": 1.0, "jumpstream": 0.3}),
    # Tech / SV
    (re.compile(r"\b(tech|technical|sv|speed\s*variation)\b", re.I), {"tech": 1.0}),
    (re.compile(r"\b(ln|long\s*note|noodle|release|inverse)\b", re.I), {"tech": 0.8, "stamina": 0.3}),
    # Stamina
    (re.compile(r"\b(stamina|marathon|endurance|density)\b", re.I), {"stamina": 1.0}),
    # Dump (maps everything a bit)
    (re.compile(r"\b(dump|dumpstream)\b", re.I), {"tech": 0.5, "stream": 0.3, "jacks": 0.2}),
]


def _detect_patterns_from_text(text: str) -> dict[str, float]:
    """Scan title/version/tags for pattern keywords. Returns axis weights 0-1."""
    weights: dict[str, float] = {a: 0.0 for a in AXES}
    if not text:
        return weights
    for pattern, boosts in _PATTERN_KEYWORDS:
        if pattern.search(text):
            for axis, w in boosts.items():
                weights[axis] = max(weights[axis], w)
    return weights


# ── Per-beatmap skill vector ─────────────────────────────────────────────────

def compute_beatmap_skillset(b: dict) -> dict:
    """
    Compute the skillset demanded by a mania beatmap.
    Uses metadata + title/version/tags keyword detection.
    """
    sr     = float(b.get("diff_rating") or 3.0)
    od     = float(b.get("od") or 8.0)
    bpm    = float(b.get("bpm") or 150.0)
    length = max(1, int(b.get("total_length") or 60))
    combo  = max(1, int(b.get("max_combo") or 100))

    # Notes-per-second: primary density indicator for mania
    nps = combo / length

    # Build searchable text from all available metadata
    title   = str(b.get("title") or "")
    version = str(b.get("version") or "")
    artist  = str(b.get("artist") or "")
    tags    = str(b.get("tags") or "")
    search_text = f"{title} {version} {artist} {tags}"

    # Detect patterns from text
    text_weights = _detect_patterns_from_text(search_text)

    # Check if ANY pattern was detected from text
    has_text_signal = any(v > 0 for v in text_weights.values())

    # Base skill values derived from metadata
    # If no text signal, distribute based on NPS/BPM heuristics
    if not has_text_signal:
        # Fallback: infer from raw stats
        # High NPS + high BPM = likely stream/speed
        # High NPS + low BPM = likely chordjack/jack (dense at lower BPM)
        # Long map = stamina
        # Moderate everything = jumpstream (most common pattern)
        bpm_factor = bpm / 150.0
        nps_factor = nps / 8.0  # 8 NPS is roughly a mid-level mania map

        text_weights["stream"]     = min(1.0, nps_factor * bpm_factor * 0.6)
        text_weights["jumpstream"] = min(1.0, nps_factor * 0.8)  # most maps are JS-ish
        text_weights["handstream"] = min(1.0, nps_factor * 0.4) if nps > 10 else 0.2
        text_weights["jacks"]      = min(1.0, nps_factor * 0.3 * (1.5 - bpm_factor)) if bpm < 180 else 0.1
        text_weights["chordjack"]  = min(1.0, nps_factor * 0.4 * (1.3 - bpm_factor * 0.5))
        text_weights["tech"]       = 0.2  # baseline
        text_weights["stamina"]    = min(1.0, math.log(max(1, length) / 60.0) * 0.5) if length > 60 else 0.1

    # Scale each axis by star rating and apply OD/density modifiers
    result = {}
    for axis in AXES:
        base_weight = text_weights.get(axis, 0.0)

        # Scale by SR — a 7* jack map is harder than a 3* jack map
        scaled = sr * base_weight

        # Axis-specific modifiers
        if axis == "stream":
            scaled *= (1.0 + max(0, bpm - 150) / 200.0)  # faster BPM = harder streams
        elif axis == "jacks":
            scaled *= (1.0 + max(0, nps - 6) * 0.1)  # denser = harder jacks
        elif axis == "chordjack":
            scaled *= (1.0 + max(0, nps - 5) * 0.08)
        elif axis == "stamina":
            scaled *= (1.0 + math.log(max(1, length) / 60.0) * 0.3)
        elif axis == "tech":
            scaled *= (od / 8.0)  # higher OD = harder tech
        elif axis in ("jumpstream", "handstream"):
            scaled *= (1.0 + max(0, nps - 7) * 0.06)

        result[axis] = round(max(0.0, scaled), 3)

    # Ensure at least one axis has meaningful value (fallback to jumpstream)
    if all(v < 0.5 for v in result.values()):
        result["jumpstream"] = round(sr * 0.7, 3)

    return result


# ── Per-user skill vector ────────────────────────────────────────────────────

def compute_user_profile(scores_with_skillsets: Iterable[dict],
                         per_axis_top: int = 30) -> dict:
    """
    Build a per-axis skill profile from the user's top plays.
    Each input row should have:
        accuracy (float 0-1), is_fc (bool), mods (int),
        skillset (dict: axis -> float)

    For each axis, we take the user's top `per_axis_top` plays *ranked by
    score-quality on that axis* (axis_difficulty * acc^4 * fc_bonus) and
    weight them by 0.95^i. The result lands in the same scale as the
    beatmap skill values.
    """
    rows = [r for r in scores_with_skillsets if r.get("skillset")]
    profile = {axis: 0.0 for axis in AXES}
    if not rows:
        return profile

    for axis in AXES:
        ranked = []
        for r in rows:
            sk     = r["skillset"]
            if not isinstance(sk, dict):
                continue
            acc    = max(0.0, min(1.0, float(r.get("accuracy") or 0.0)))
            is_fc  = bool(r.get("is_fc"))
            mods   = int(r.get("mods") or 0)

            mod_bonus = 1.0
            if mods & (1 << 6):  mod_bonus *= 1.12  # DT
            if mods & (1 << 9):  mod_bonus *= 1.08  # NC

            quality = (acc ** 4) * (1.10 if is_fc else 1.0) * mod_bonus
            axis_val = sk.get(axis, 0.0)
            if not axis_val:
                continue
            demonstrated = axis_val * quality
            ranked.append(demonstrated)

        ranked.sort(reverse=True)
        ranked = ranked[:per_axis_top]
        weights = [0.95 ** i for i in range(len(ranked))]
        wsum = sum(weights) or 1.0
        profile[axis] = round(sum(d * w for d, w in zip(ranked, weights)) / wsum, 3)

    return profile


# ── Map recommendation ──────────────────────────────────────────────────────

def score_recommendation(user_profile: dict,
                         beatmap_skillset: dict,
                         user_best_pp_on_map: float | None,
                         axis: str | None = None) -> tuple[float, str]:
    """
    Lower returned score = better recommendation.
    Returns (distance, primary_axis).

    "Stretch zone" = 5%-25% above user's level on the chosen axis.
    Maps already mastered (good pp on them) are penalized.
    """
    if axis is None:
        # Pick the user's WEAKEST axis that has any data
        valid_axes = {a: v for a, v in user_profile.items() if v > 0}
        if valid_axes:
            axis = min(valid_axes, key=lambda a: valid_axes[a])
        else:
            axis = "stream"  # default

    user_level = user_profile.get(axis, 3.0) or 3.0
    map_level  = beatmap_skillset.get(axis, 0.0)

    if map_level < 0.1:
        return 999.0, axis  # map has no data for this axis

    target_low  = user_level * 1.05
    target_high = user_level * 1.25

    if map_level < target_low:
        distance = (target_low - map_level) * 2.0 + 1.0
    elif map_level > target_high:
        distance = (map_level - target_high) * 1.5 + 0.5
    else:
        center = (target_low + target_high) / 2
        distance = abs(map_level - center) * 0.3

    if user_best_pp_on_map is not None and user_best_pp_on_map > 0:
        if user_best_pp_on_map > map_level * 30:
            distance += 4.0

    return distance, axis


def format_axes_short(profile: dict) -> str:
    """Compact one-line skill display for chat."""
    abbrevs = {
        "stream": "STR",
        "jumpstream": "JS",
        "handstream": "HS",
        "jacks": "JCK",
        "chordjack": "CJ",
        "tech": "TCH",
        "stamina": "STA",
    }
    parts = []
    for a in AXES:
        v = profile.get(a, 0.0)
        if v > 0:
            parts.append(f"{abbrevs.get(a, a[:3].upper())}:{v:.1f}")
    return " | ".join(parts) if parts else "No data"


def compare_profiles(a: dict, b: dict) -> dict:
    """Per-axis diff. Positive = a stronger than b on that axis."""
    return {ax: round((a.get(ax, 0.0) or 0.0) - (b.get(ax, 0.0) or 0.0), 2)
            for ax in AXES}
