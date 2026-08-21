"""
SpareRoom source adapter.

Two jobs:
  1. search()  -> paginate SpareRoom search-result pages and return light-weight
                  "card" dicts parsed from the rich data-* attributes on each
                  <li class="listing-result"> element.
  2. fetch_detail() -> pull the full ad page for one listing and extract the
                  things only shown there: exact coordinates, bills-included,
                  availability, furnishings, deposit and a description snippet.

All parsing selectors live in THIS file, so if SpareRoom changes its markup
this is the only place that needs updating. Everything is done against the
raw server-rendered HTML (no JavaScript required).
"""

from __future__ import annotations

import re
import time
import html as _html
from dataclasses import dataclass, field, asdict
from typing import Iterable

import requests
from bs4 import BeautifulSoup
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

BASE = "https://www.spareroom.co.uk"
SEARCH_BASE = BASE + "/flatshare/"
DETAIL_URL = BASE + "/flatshare/flatshare_detail.pl?flatshare_id={id}"

# A realistic desktop browser UA — SpareRoom serves the full HTML to this.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
}

_COORDS_RE = re.compile(r'latitude:\s*"([-0-9.]+)"\s*,\s*longitude:\s*"([-0-9.]+)"')
_PRICE_RE = re.compile(r"([0-9][0-9,]*(?:\.[0-9]+)?)")


@dataclass
class Listing:
    """One room ad. Fields fill in progressively: search -> detail -> commute -> score."""
    id: str
    title: str = ""
    url: str = ""                    # canonical public ad URL
    price_pcm: int = 0               # normalised monthly rent for the room
    price_raw: str = ""              # e.g. "£195 pw"
    postcode: str = ""               # outcode only from search, e.g. "SW15"
    neighbourhood: str = ""
    property_type: str = ""
    rooms_in_property: str = ""
    advertiser_role: str = ""        # agent / live in landlord / current flatmate ...
    days_old: int = 0
    available_now: bool = False
    image: str = ""
    # filled by fetch_detail()
    lat: float | None = None
    lng: float | None = None
    bills_included: str = "unknown"  # "yes" / "no" / "unknown"
    available: str = ""              # human string, e.g. "Available now" / "01 Sep 2026"
    room_type: str = ""              # "double" / "single" / ""
    furnishings: str = ""
    description: str = ""
    # filled by enrich/commute + score
    commute_minutes: int | None = None
    commute_summary: str = ""
    suitability: int = 0
    score_breakdown: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return asdict(self)


def make_session() -> requests.Session:
    """A session primed with SpareRoom's browser_ident cookie."""
    s = requests.Session()
    s.headers.update(HEADERS)
    try:
        s.get(SEARCH_BASE, timeout=20)  # sets browser_ident cookie
    except requests.RequestException:
        pass
    return s


@retry(
    retry=retry_if_exception_type(requests.RequestException),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=2, max=15),
    reraise=True,
)
def _get(session: requests.Session, url: str) -> requests.Response:
    r = session.get(url, timeout=25)
    r.raise_for_status()
    return r


# --------------------------------------------------------------------------- #
#  Price helpers
# --------------------------------------------------------------------------- #
def to_pcm(rate: str, period: str) -> int:
    """Normalise a SpareRoom rate string + period to whole pounds per month."""
    if not rate:
        return 0
    m = _PRICE_RE.search(rate.replace(",", ""))
    if not m:
        return 0
    value = float(m.group(1))
    period = (period or "").lower()
    if period == "pw":               # per week -> per calendar month
        value = value * 52 / 12
    elif period == "pd":             # per day (rare)
        value = value * 365 / 12
    return int(round(value))


# --------------------------------------------------------------------------- #
#  Search (list) pages
# --------------------------------------------------------------------------- #
def _build_search_url(cfg: dict, offset: int) -> str:
    s = cfg["search"]
    b = cfg["budget"]
    max_rent = b["max"] + b.get("tolerance", 0)
    params = {
        "min_rent": b["min"],
        "max_rent": max_rent,
        "per": "pcm",
        "miles_from_max": s["radius_miles"],
        "sort_by": s.get("sort_by", "days_since_placed"),
        "offset": offset,
    }
    if s.get("rooms_only", True):
        params["showme_rooms"] = "Y"
    if s.get("exclude_1beds", True):
        params["showme_1beds"] = "N"
    if s.get("exclude_buddyup", True):
        params["showme_buddyup_properties"] = "N"
    query = "&".join(f"{k}={v}" for k, v in params.items())
    return f"{SEARCH_BASE}{s['center_path']}?{query}"


def _parse_card(li) -> Listing | None:
    """Turn one <li class='listing-result'> into a Listing using its data-* attrs."""
    lid = li.get("data-listing-id")
    if not lid:
        return None
    rate = _html.unescape(li.get("data-listing-ad-rate-normalised", "") or "")
    period = li.get("data-listing-ad-rate-normalised-period", "") or ""
    price_pcm = to_pcm(rate, period)
    try:
        days_old = int(li.get("data-listing-days-old") or 0)
    except ValueError:
        days_old = 0
    return Listing(
        id=str(lid),
        title=li.get("data-listing-title", "") or "",
        url=DETAIL_URL.format(id=lid),
        price_pcm=price_pcm,
        price_raw=f"{rate} {period}".strip(),
        postcode=li.get("data-listing-postcode", "") or "",
        neighbourhood=li.get("data-listing-neighbourhood", "") or "",
        property_type=(li.get("data-listing-property-type-more")
                       or li.get("data-listing-property-type") or ""),
        rooms_in_property=li.get("data-listing-rooms-in-property", "") or "",
        advertiser_role=li.get("data-listing-advertiser-role", "") or "",
        days_old=days_old,
        available_now=li.get("data-listing-available-now") == "1",
        image=li.get("data-listing-ad-profile-photo", "") or "",
    )


def search(session: requests.Session, cfg: dict, log=print) -> list[Listing]:
    """Paginate the search results and return de-duplicated cards."""
    seen: dict[str, Listing] = {}
    max_pages = cfg["search"]["max_pages"]
    delay = cfg["search"].get("request_delay_seconds", 1.5)
    for page in range(max_pages):
        url = _build_search_url(cfg, offset=page * 10)
        try:
            resp = _get(session, url)
        except requests.RequestException as e:
            log(f"  ! search page {page} failed: {e}")
            break
        soup = BeautifulSoup(resp.text, "html.parser")
        cards = soup.find_all("li", class_="listing-result")
        if not cards:
            log(f"  page {page}: 0 cards (end of results or markup change)")
            break
        added = 0
        for li in cards:
            listing = _parse_card(li)
            if listing and listing.id not in seen:
                seen[listing.id] = listing
                added += 1
        log(f"  page {page}: {len(cards)} cards, {added} new (total {len(seen)})")
        time.sleep(delay)
    return list(seen.values())


# --------------------------------------------------------------------------- #
#  Detail pages
# --------------------------------------------------------------------------- #
def _clean(txt: str) -> str:
    return re.sub(r"\s+", " ", txt or "").strip()


def fetch_detail(session: requests.Session, listing: Listing, log=print) -> None:
    """Fetch the full ad and fill coords, bills, availability, room type, etc."""
    url = DETAIL_URL.format(id=listing.id)
    try:
        resp = _get(session, url)
    except requests.RequestException as e:
        log(f"  ! detail {listing.id} failed: {e}")
        return
    text = resp.text

    # Coordinates embedded in the page's map data.
    m = _COORDS_RE.search(text)
    if m:
        try:
            listing.lat = float(m.group(1))
            listing.lng = float(m.group(2))
        except ValueError:
            pass

    soup = BeautifulSoup(text, "html.parser")

    # Feature list: <dt class="feature-list__key">K</dt><dd class="feature-list__value">V</dd>
    features: dict[str, str] = {}
    for dl in soup.find_all("dl", class_="feature-list"):
        keys = dl.find_all("dt", class_="feature-list__key")
        vals = dl.find_all("dd", class_="feature-list__value")
        for dt, dd in zip(keys, vals):
            k = _clean(dt.get_text()).lower().rstrip("?")
            features.setdefault(k, _clean(dd.get_text()))

    bills = features.get("bills included", "")
    if bills:
        listing.bills_included = "yes" if bills.lower().startswith("y") else "no"
    listing.available = features.get("available", "") or listing.available
    listing.furnishings = features.get("furnishings", "")

    # Room type from title/description.
    hay = f"{listing.title} {features.get('room', '')}".lower()
    if "double" in hay:
        listing.room_type = "double"
    elif "single" in hay:
        listing.room_type = "single"

    # A short description snippet for the card.
    desc = soup.find(class_=re.compile(r"detaildesc|description"))
    if desc:
        listing.description = _clean(desc.get_text())[:280]

    # Canonical public URL if present (nicer link than the .pl endpoint).
    canon = soup.find("link", rel="canonical")
    if canon and canon.get("href"):
        listing.url = canon["href"]
