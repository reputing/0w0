"""
Scythe — PP Calculator (shared lib)
"""
import math

MOD_NF = 1 << 0
MOD_EZ = 1 << 1
MOD_HD = 1 << 3
MOD_HR = 1 << 4
MOD_DT = 1 << 6
MOD_HT = 1 << 8
MOD_FL = 1 << 10
MOD_SO = 1 << 12

HOT_STREAK_MULTIPLIER = 1.15
FEATURED_MAP_MULTIPLIER = 2.0


def calculate_pp(stars, accuracy, max_combo, beatmap_max_combo,
                 count300, count100, count50, countmiss, mods, ar, od,
                 is_featured_map=False, hot_streak=0):
    if mods & MOD_DT: stars *= 1.06
    if mods & MOD_HT: stars *= 0.85
    if mods & MOD_HR:
        stars *= 1.05
        od = min(od * 1.4, 10.0)
        ar = min(ar * 1.4, 10.0)
    if mods & MOD_EZ:
        stars *= 0.9
        od *= 0.5
        ar *= 0.5

    combo_ratio = (max_combo / beatmap_max_combo) if beatmap_max_combo > 0 else 1.0

    # Aim
    aim = _base(stars)
    ar_bonus = 1.0
    if ar > 10.33: ar_bonus += 0.4 * (ar - 10.33)
    elif ar < 8.0: ar_bonus += 0.01 * (8.0 - ar)
    aim *= ar_bonus
    if mods & MOD_HD: aim *= 1.18
    if mods & MOD_FL: aim *= 1.45
    aim *= min(combo_ratio ** 0.8, 1.0)
    aim *= 0.97 ** countmiss
    aim *= (0.5 + accuracy / 2)

    # Accuracy
    total = count300 + count100 + count50 + countmiss
    if total == 0: return 0.0
    acc_pp = (0.98 + (od ** 2) / 2500) * (accuracy ** 24) * 26.25
    if mods & MOD_HD: acc_pp *= 1.08
    if mods & MOD_EZ: acc_pp *= 0.5

    # Speed
    speed = _base(stars) * 0.35
    if mods & MOD_DT: speed *= 1.1
    if mods & MOD_HD: speed *= 1.05
    speed *= min(combo_ratio ** 0.7, 1.0)
    speed *= 0.95 ** countmiss
    speed *= max(accuracy, 0.6)

    total_pp = (aim ** 1.1 + speed ** 1.1 + acc_pp ** 1.1) ** (1 / 1.1)
    if mods & MOD_NF: total_pp *= 0.9
    if mods & MOD_SO: total_pp *= 0.95
    if is_featured_map: total_pp *= FEATURED_MAP_MULTIPLIER
    if hot_streak >= 3:
        bonus = 1.0 + (HOT_STREAK_MULTIPLIER - 1.0) * min(hot_streak / 10, 1.0)
        total_pp *= bonus

    return round(total_pp, 2)


def _base(stars):
    return ((5 * max(1.0, stars / 0.0675) - 4) ** 3) / 100000


def calculate_accuracy(c300, c100, c50, miss):
    total = c300 + c100 + c50 + miss
    if total == 0: return 0.0
    return round((c300 * 6 + c100 * 2 + c50) / (total * 6), 6)


def get_rank_string(accuracy, countmiss, c300, c100, c50, mods):
    hd_fl = (mods & MOD_HD) or (mods & MOD_FL)
    if countmiss == 0:
        if accuracy == 1.0: return "SSH" if hd_fl else "SS"
        if accuracy > 0.98 and c50 == 0:
            return "SH" if hd_fl else "S"
    if accuracy >= 0.94: return "A"
    if accuracy >= 0.90: return "B"
    if accuracy >= 0.80: return "C"
    return "D"


def recalculate_user_pp(scores: list):
    if not scores: return 0.0, 0.0
    weighted = sum(s["pp"] * (0.95 ** i) for i, s in enumerate(scores))
    weighted += 416.6667 * (1 - 0.9994 ** len(scores))
    divisor = sum(0.95 ** i for i in range(len(scores)))
    acc = sum(s["accuracy"] * (0.95 ** i) for i, s in enumerate(scores)) / divisor
    return round(weighted, 2), round(acc, 6)
