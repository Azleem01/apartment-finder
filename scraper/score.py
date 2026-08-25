"""
Suitability scoring.

Each listing gets a 0-100 % built from five components, weighted per config.
Budget and commute dominate (your priorities); bills-included and a Sept-Oct
move-in are your chosen boosts; freshness gently favours brand-new ads.

Every component also records a plain-English reason, stored on the listing as
`score_breakdown` and shown in the dashboard's "why this score" panel.
"""

from __future__ import annotations

import datetime as dt
import re

from .spareroom import Listing


def _interp(x: float, points: list[tuple[float, float]]) -> float:
    """Piecewise-linear interpolation over (x, y) anchor points (x ascending)."""
    if x <= points[0][0]:
        return points[0][1]
    if x >= points[-1][0]:
        return points[-1][1]
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        if x0 <= x <= x1:
            t = (x - x0) / (x1 - x0) if x1 != x0 else 0
            return y0 + t * (y1 - y0)
    return points[-1][1]


def budget_score(price: int, cfg: dict) -> tuple[float, str]:
    lo = cfg["budget"]["min"]
    hard = cfg["budget"]["max"]
    pref = cfg["budget"].get("preferred_max", hard)
    if price <= 0:
        return 0.3, "rent unknown"
    # Cheaper is always better. Top score at the floor, ~0.6 at the preferred
    # ceiling, then a steeper drop through the stretch band up to the hard max.
    if hard > pref:
        s = _interp(price, [(lo, 1.0), (pref, 0.6), (hard, 0.25)])
    else:
        s = _interp(price, [(lo, 1.0), (pref, 0.6), (pref + 100, 0.2)])
    note = f"£{price} pcm (best value ≤ £{pref})"
    if price > pref:
        note = f"£{price} pcm — above your preferred £{pref}"
    return max(0.0, min(1.0, s)), note


def commute_score(minutes: int | None, cfg: dict) -> tuple[float, str]:
    if minutes is None:
        return 0.3, "commute unknown"
    ideal = cfg["commute"]["ideal_minutes"]
    mx = cfg["commute"]["max_minutes"]
    s = _interp(minutes, [(10, 1.0), (ideal, 0.8), (mx, 0.5), (mx + 20, 0.0)])
    return max(0.0, min(1.0, s)), f"{minutes} min to Imperial"


def bills_score(listing: Listing) -> tuple[float, str]:
    b = listing.bills_included
    if b == "yes":
        return 1.0, "bills included"
    if b == "no":
        return 0.0, "bills not included"
    return 0.4, "bills not stated"


_DATE_FORMATS = ("%d %b %Y", "%d %B %Y", "%d %b %y", "%d/%m/%Y", "%d/%m/%y")


def parse_available_date(text: str) -> dt.date | None:
    t = (text or "").strip()
    if not t:
        return None
    # Word-boundary match so "unknown" / "not known" are not treated as "now".
    if re.search(r"\bnow\b", t, re.I):
        return dt.date.today()
    m = re.search(r"\d{1,2}[ /][A-Za-z]{3,9}[ /]\d{2,4}|\d{1,2}/\d{1,2}/\d{2,4}", t)
    frag = m.group(0) if m else t
    for fmt in _DATE_FORMATS:
        try:
            return dt.datetime.strptime(frag.strip(), fmt).date()
        except ValueError:
            continue
    return None


def parse_term_months(text: str):
    """Parse a SpareRoom term string to months. 'None' -> None, days -> 0."""
    t = (text or "").strip().lower()
    if not t or t in ("none", "n/a", "no minimum", "no maximum", "-"):
        return None
    m = re.search(r"(\d+)\s*(year|yr)", t)
    if m:
        return int(m.group(1)) * 12
    m = re.search(r"(\d+)\s*month", t)
    if m:
        return int(m.group(1))
    m = re.search(r"(\d+)\s*week", t)
    if m:
        return max(1, round(int(m.group(1)) / 4.345))
    if re.search(r"(\d+)\s*(night|day)", t):
        return 0                       # days => far too short
    return None


# Titles/descriptions that signal a short stay, unsuitable for a 12-month course.
_SHORTLET_RE = re.compile(
    r"\b(sub-?let|short[- ]?let|short[- ]?term|holiday\s+let|temporary|"
    r"mon(day)?\s*[-–to ]+\s*fri(day)?|weekdays?\s+only|nights?\s+only|"
    r"days?\s+only|per\s+night|\d+\s*nights?\s*(a|per|/)\s*week|nights?\s+a\s+week)\b", re.I)


def short_let_reason(listing: Listing, min_stay_months: int):
    """Return a reason string if this is a short-let (unsuitable), else None."""
    hay = f"{listing.title} {listing.description}"
    if _SHORTLET_RE.search(hay):
        return "short-term / sublet listing"
    mx = parse_term_months(listing.max_term)
    if mx is not None and mx < min_stay_months:
        return f"max term ~{mx} month(s) — too short for your course"
    return None


def tenancy_score(listing: Listing, cfg: dict) -> tuple[float, str]:
    """Combines move-in timing (~Sept 2026) with the ability to stay the full course."""
    win = cfg["prefs"]["move_in_window"]
    end = dt.date.fromisoformat(str(win["to"]))
    need = int(cfg["prefs"].get("tenancy_months", 12))

    date = parse_available_date(listing.available) or (
        dt.date.today() if listing.available_now else None)
    if date is None:
        timing, tnote = 0.5, "availability unknown"
    elif date <= end:
        timing, tnote = 1.0, f"available {date.isoformat()}"
    else:
        late = (date.year - end.year) * 12 + (date.month - end.month)
        timing, tnote = max(0.0, 1.0 - 0.25 * late), f"available {date.isoformat()} (late)"

    mx = parse_term_months(listing.max_term)
    if mx is None:
        stay, snote = 0.9, "no max term"
    elif mx >= need:
        stay, snote = 1.0, f"stay up to {mx} months"
    else:
        stay, snote = max(0.0, mx / need), f"max {mx} months"

    return 0.6 * timing + 0.4 * stay, f"{tnote}; {snote}"


def freshness_score(days_old: int) -> tuple[float, str]:
    s = _interp(days_old, [(0, 1.0), (1, 1.0), (3, 0.85), (7, 0.65), (14, 0.45), (30, 0.3)])
    label = "listed today" if days_old == 0 else f"listed {days_old} day(s) ago"
    return s, label


def score(listing: Listing, cfg: dict) -> None:
    """Compute suitability % + breakdown, writing them onto the listing in place."""
    w = cfg["weights"]
    parts = {
        "budget": (budget_score(listing.price_pcm, cfg), w["budget"]),
        "commute": (commute_score(listing.commute_minutes, cfg), w["commute"]),
        "bills": (bills_score(listing), w["bills"]),
        "tenancy": (tenancy_score(listing, cfg), w["tenancy"]),
        "freshness": (freshness_score(listing.days_old), w["freshness"]),
    }
    total = 0.0
    breakdown = {}
    for name, ((raw, note), weight) in parts.items():
        contribution = raw * weight
        total += contribution
        breakdown[name] = {
            "score": round(raw, 3),
            "weight": weight,
            "points": round(contribution * 100, 1),  # points out of 100
            "note": note,
        }
    # Deprioritise some sources (e.g. SpareRoom) with a ranking multiplier.
    priority = cfg.get("source_priority", {}).get(listing.source, 1.0)
    listing.suitability = int(round(total * priority * 100))
    listing.score_breakdown = breakdown
    if priority != 1.0:
        breakdown["source"] = {"score": priority, "weight": 0, "points": 0,
                               "note": f"{listing.source} ×{priority} priority"}
