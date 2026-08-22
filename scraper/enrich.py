"""
Enrichment: geography + commute.

- geocode_outcode(): approximate coords for a postcode district (e.g. "SW15")
  via the free postcodes.io API. Used only to *pre-filter* obviously-too-far
  listings cheaply before we spend a detail fetch + a TfL call on them.
- commute(): real public-transport journey time to Imperial via TfL's free
  Journey Planner, for a fixed weekday-morning departure so numbers are stable.
"""

from __future__ import annotations

import math
import time
import datetime as dt
from urllib.parse import quote

import requests
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

POSTCODES_OUTCODE = "https://api.postcodes.io/outcodes/{outcode}"
POSTCODES_LOOKUP = "https://api.postcodes.io/postcodes/{pc}"
TFL_JOURNEY = "https://api.tfl.gov.uk/Journey/JourneyResults/{frm}/to/{to}"

# Modes we count as a real commute (exclude e.g. cycle-hire, coach).
_TRANSIT_MODES = {"tube", "dlr", "overground", "elizabeth-line",
                  "national-rail", "tram", "bus", "river-bus", "cable-car"}


@retry(
    retry=retry_if_exception_type(requests.RequestException),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1.5, min=1, max=10),
    reraise=True,
)
def _get_json(session: requests.Session, url: str) -> dict:
    r = session.get(url, timeout=25)
    r.raise_for_status()
    return r.json()


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def geocode_postcode(session: requests.Session, postcode: str) -> tuple[float, float] | None:
    """Full postcode -> (lat, lng). Used once for the Imperial destination."""
    try:
        data = _get_json(session, POSTCODES_LOOKUP.format(pc=quote(postcode)))
        res = data.get("result") or {}
        if res.get("latitude") is not None:
            return float(res["latitude"]), float(res["longitude"])
    except (requests.RequestException, ValueError, KeyError):
        pass
    return None


def geocode_outcode(session: requests.Session, outcode: str, cache: dict) -> tuple[float, float] | None:
    """Postcode district (e.g. 'SW15') -> approximate centroid (lat, lng), cached."""
    outcode = (outcode or "").strip().upper()
    if not outcode:
        return None
    if outcode in cache:
        v = cache[outcode]
        return (v[0], v[1]) if v else None
    coords = None
    try:
        data = _get_json(session, POSTCODES_OUTCODE.format(outcode=quote(outcode)))
        res = data.get("result") or {}
        if res.get("latitude") is not None:
            coords = (float(res["latitude"]), float(res["longitude"]))
    except (requests.RequestException, ValueError, KeyError):
        coords = None
    cache[outcode] = list(coords) if coords else None
    return coords


NOMINATIM = "https://nominatim.openstreetmap.org/search"
POSTCODES_REVERSE = "https://api.postcodes.io/postcodes?lon={lng}&lat={lat}&limit=1"


def geocode_place(session: requests.Session, query: str, cache: dict) -> tuple[float, float] | None:
    """Free-text UK place (e.g. 'Ealing, London') -> (lat, lng) via Nominatim, cached."""
    query = (query or "").strip()
    if not query:
        return None
    key = query.lower()
    if key in cache:
        v = cache[key]
        return (v[0], v[1]) if v else None
    coords = None
    try:
        url = f"{NOMINATIM}?q={quote(query)}&format=jsonv2&limit=1&countrycodes=gb"
        r = session.get(url, timeout=25, headers={
            "User-Agent": "apartment-finder/1.0 (personal room search; contact via github)"})
        r.raise_for_status()
        data = r.json()
        if data:
            coords = (float(data[0]["lat"]), float(data[0]["lon"]))
    except (requests.RequestException, ValueError, KeyError, IndexError):
        coords = None
    time.sleep(1.0)                      # Nominatim usage policy: max ~1 req/sec
    cache[key] = list(coords) if coords else None
    return coords


def reverse_geocode(session: requests.Session, lat: float, lng: float, cache: dict) -> dict:
    """Coords -> {'postcode','area'} via postcodes.io, cached by rounded coords."""
    key = f"{round(lat,4)},{round(lng,4)}"
    if key in cache:
        return cache[key]
    out = {"postcode": "", "area": ""}
    try:
        data = _get_json(session, POSTCODES_REVERSE.format(lng=lng, lat=lat))
        res = (data.get("result") or [])
        if res:
            r = res[0]
            out = {"postcode": (r.get("outcode") or r.get("postcode") or "").split()[0]
                   if (r.get("outcode") or r.get("postcode")) else "",
                   "area": r.get("admin_ward") or r.get("parish") or r.get("admin_district") or ""}
    except (requests.RequestException, ValueError, KeyError):
        pass
    cache[key] = out
    return out


def resolve_departure(cfg: dict) -> tuple[str, str]:
    """Return (YYYYMMDD, HHMM) for the commute estimate."""
    c = cfg["commute"]
    time_str = str(c.get("depart_time", "0900"))
    wd = str(c.get("weekday", "next-monday")).lower()
    today = dt.date.today()
    if wd == "next-monday":
        days_ahead = (0 - today.weekday()) % 7  # Monday == 0
        days_ahead = days_ahead or 7            # always a *future* Monday
        target = today + dt.timedelta(days=days_ahead)
    elif wd == "today":
        target = today
    else:
        target = today + dt.timedelta(days=1)
    return target.strftime("%Y%m%d"), time_str


def _summarise_journey(journey: dict) -> str:
    """Human summary like '34 min · District line' from a journey's transit legs."""
    lines: list[str] = []
    for leg in journey.get("legs", []):
        mode = (leg.get("mode", {}) or {}).get("name", "")
        if mode not in _TRANSIT_MODES:
            continue
        opts = leg.get("routeOptions") or []
        name = opts[0].get("name", "") if opts else ""
        if mode == "bus":
            label = f"bus {name}".strip() if name else "bus"
        elif mode in ("national-rail", "overground", "elizabeth-line", "dlr"):
            label = mode.replace("-", " ").title()      # e.g. "National Rail"
        else:
            label = name or mode.replace("-", " ").title()  # tube -> line name
        if label and label not in lines:
            lines.append(label)
    dur = journey.get("duration")
    route = " → ".join(lines) if lines else "walking"
    return f"{dur} min · {route}"


def commute(session: requests.Session, lat: float, lng: float, cfg: dict,
            date: str, time_str: str) -> tuple[int, str] | None:
    """Fastest public-transport journey (minutes, summary) from (lat,lng) to Imperial."""
    c = cfg["commute"]
    dest = quote(c["destination_postcode"])
    frm = f"{lat},{lng}"
    url = TFL_JOURNEY.format(frm=frm, to=dest)
    url += f"?date={date}&time={time_str}&timeIs=Departing"
    if c.get("app_key"):
        url += f"&app_key={c['app_key']}"
    try:
        data = _get_json(session, url)
    except requests.RequestException:
        return None
    journeys = data.get("journeys")
    if not journeys:
        return None
    fastest = min(journeys, key=lambda j: j.get("duration", 10**9))
    return int(fastest.get("duration", 0)), _summarise_journey(fastest)
