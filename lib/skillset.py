"""
Scythe — Etterna-MSD-inspired skill profiler for osu! standard

Five skill axes:
  - aim       : cursor positioning under spacing pressure
  - speed     : tap rate (BPM × density)
  - acc       : hitting circles cleanly under OD pressure
  - stamina   : sustained difficulty over time
  - flashlight: reading / memory (low AR + small CS)

Two computations:
  1. compute_beatmap_skillset(b) -> dict of axis -> float
     Vector of "how much each skill this map demands".
  2. compute_user_profile(top_scores_with_skillsets) -> dict of axis -> float
     Vector of "what skill level the user has demonstrated".

Both vectors live in the same ~star-rating-like scale (~1-10) so they're
directly comparable. Recommendations work by finding maps in the user's
"stretch zone" (slightly above their current axis level).

This is a heuristic, not true note-pattern MSD. It's calibrated against the
metadata fields the bancho already stores; accuracy improves naturally as
more scores are submitted (because score quality folds into the user vector).
"""
from __future__ import annotations

import math
from typing import Iterable

AXES = ("aim", "speed", "acc", "stamina", "flashlight")


# ── Per-beatmap skill vector ─────────────────────────────────────────────────

def compute_beatmap_skillset(b: dict) -> dict:
    """Compute the skillset demanded by a beatmap from its metadata."""
    sr     = float(b.get("diff_rating") or 3.0)
    ar     = float(b.get("ar") or 9.0)
    od     = float(b.get("od") or 8.0)
    cs     = float(b.get("cs") or 4.0)
    bpm    = float(b.get("bpm") or 180.0)
    length = max(1, int(b.get("total_length") or 60))
    combo  = max(1, int(b.get("max_combo") or 0))

    # Notes-per-second proxy. max_combo grows roughly with note count.
    nps = combo / length

    aim = sr * (1.0 + (cs - 4.0) * 0.08) * (1.0 + max(0.0, nps - 4.0) * 0.05)
    speed = sr * (bpm / 180.0) * (1.0 + max(0.0, nps - 4.0) * 0.10)
    acc_ = sr * (od / 8.0) * 1.10
    stamina = sr * (0.5 if length < 60 else 1.0 + math.log(length / 60.0) * 0.25)
    flashlight = sr * max(0.3, (11.0 - ar) / 4.0) * (1.0 + max(0.0, cs - 4.0) * 0.05)

    return {
        "aim":        round(max(0.5, aim), 3),
        "speed":      round(max(0.5, speed), 3),
        "acc":        round(max(0.5, acc_), 3),
        "stamina":    round(max(0.5, stamina), 3),
        "flashlight": round(max(0.5, flashlight), 3),
    }


# ── Per-user skill vector ────────────────────────────────────────────────────

def compute_user_profile(scores_with_skillsets: Iterable[dict],
                         per_axis_top: int = 30) -> dict:
    """
    Build a per-axis skill profile.
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
            acc    = max(0.0, min(1.0, float(r.get("accuracy") or 0.0)))
            is_fc  = bool(r.get("is_fc"))
            mods   = int(r.get("mods") or 0)

            mod_bonus = 1.0
            if mods & (1 << 1):  mod_bonus *= 1.10  # EZ
            if mods & (1 << 4):  mod_bonus *= 1.06  # HR
            if mods & (1 << 6):  mod_bonus *= 1.12  # DT
            if mods & (1 << 9):  mod_bonus *= 1.08  # NC
            if mods & (1 << 10): mod_bonus *= 1.10  # FL

            quality = (acc ** 4) * (1.10 if is_fc else 1.0) * mod_bonus
            demonstrated = sk[axis] * quality
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
        # Pick the user's WEAKEST axis: that's where to grow.
        axis = min(user_profile, key=lambda a: user_profile[a])

    user_level = user_profile.get(axis, 3.0) or 3.0
    map_level  = beatmap_skillset.get(axis, 3.0)

    target_low  = user_level * 1.05
    target_high = user_level * 1.25

    if map_level < target_low:
        # Too easy
        distance = (target_low - map_level) * 2.0 + 1.0
    elif map_level > target_high:
        # Too hard
        distance = (map_level - target_high) * 1.5 + 0.5
    else:
        # Sweet spot — center is the ideal
        center = (target_low + target_high) / 2
        distance = abs(map_level - center) * 0.3

    # Mastery penalty: if user already passed this map cleanly, reduce priority
    if user_best_pp_on_map is not None and user_best_pp_on_map > 0:
        # heuristic: if pp > map_level * 30 they've kinda mastered the axis here
        if user_best_pp_on_map > map_level * 30:
            distance += 4.0

    return distance, axis


def format_axes_short(profile: dict) -> str:
    """Compact one-line skill display for chat."""
    return " | ".join(
        f"{a[:3].upper()}:{profile.get(a, 0.0):.1f}" for a in AXES
    )


def compare_profiles(a: dict, b: dict) -> dict:
    """Per-axis diff. Positive = a stronger than b on that axis."""
    return {ax: round((a.get(ax, 0.0) or 0.0) - (b.get(ax, 0.0) or 0.0), 2)
            for ax in AXES}
