"""
OpenRent source adapter.

OpenRent renders its map/search results from a set of parallel JavaScript arrays
embedded in the search page (PROPERTYIDS, prices, coordinates, isshared, bills,
hoursLive, ...). We fetch the search page, parse those arrays, zip them by index
and keep the shared-house rooms — coordinates, bills, exact freshness and price
all come straight from the page, so no per-listing detail fetch is needed.
"""

from __future__ import annotations

import re
import requests
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from .spareroom import Listing, HEADERS

NAME = "OpenRent"
SEARCH = ("https://www.openrent.co.uk/properties-to-rent/london"
          "?term={term}&prices_min={min}&prices_max={max}&bedrooms_max=1&isLive=true")
LISTING_URL = "https://www.openrent.co.uk/{id}"


@retry(retry=retry_if_exception_type(requests.RequestException),
       stop=stop_after_attempt(3), wait=wait_exponential(multiplier=2, min=2, max=15),
       reraise=True)
def _get(session, url):
    r = session.get(url, timeout=30)
    r.raise_for_status()
    return r


def _array(html: str, name: str) -> list[str]:
    """Extract a JS `name = [ ... ]` literal as a list of trimmed string tokens."""
    m = re.search(r'\b' + re.escape(name) + r'\s*=\s*\[([^\]]*)\]', html)
    if not m:
        return []
    out = []
    for tok in m.group(1).split(","):
        tok = tok.strip().strip('"').strip("'").strip()
        out.append(tok)
    return [t for t in out if t != ""] if name == "PROPERTYIDS" else out


def _num(tok: str) -> float | None:
    try:
        return float(tok)
    except (TypeError, ValueError):
        return None


def _truthy(tok: str) -> bool:
    return str(tok).strip().lower() in ("1", "true", "yes")


def search(session: requests.Session, cfg: dict, log=print) -> list[Listing]:
    b = cfg["budget"]
    term = cfg.get("sources", {}).get("openrent", {}).get("location", "South Kensington, London")
    url = SEARCH.format(term=requests.utils.quote(term),
                        min=b["min"], max=b["max"] + b.get("tolerance", 0))
    try:
        session.headers.update(HEADERS)
        resp = _get(session, url)
    except requests.RequestException as e:
        log(f"  ! OpenRent search failed: {e}")
        return []
    html = resp.text

    ids = _array(html, "PROPERTYIDS")
    if not ids:
        log("  OpenRent: no property arrays found (markup change or block)")
        return []
    prices = _array(html, "prices")
    shared = _array(html, "isshared")
    bills = _array(html, "bills")
    hours = _array(html, "hoursLive")
    lats = _array(html, "PROPERTYLISTLATITUDES")
    lngs = _array(html, "PROPERTYLISTLONGITUDES")
    ptypes = _array(html, "propertyTypes")
    minten = _array(html, "minimumTenancy")
    availf = _array(html, "availableFrom")

    def at(arr, i):
        return arr[i] if i < len(arr) else ""

    out: list[Listing] = []
    for i, pid in enumerate(ids):
        if not _truthy(at(shared, i)):     # keep only rooms in shared homes
            continue
        price = _num(at(prices, i))
        lat = _num(at(lats, i))
        lng = _num(at(lngs, i))
        if price is None or lat is None:
            continue
        hrs = _num(at(hours, i)) or 0
        ptype = at(ptypes, i) or ""
        mt = _num(at(minten, i))
        if ptype.isdigit():               # propertyTypes carries the bedroom count
            title = f"Room in a {ptype}-bed shared house"
            ptype_label = f"{ptype}-bed share"
        else:
            title = f"Room in a shared {ptype.lower()}" if ptype else "Room in a shared home"
            ptype_label = ptype
        out.append(Listing(
            id=f"OR{pid}",
            source=NAME,
            title=title,
            url=LISTING_URL.format(id=pid),
            price_pcm=int(round(price)),
            price_raw=f"£{int(round(price))} pcm",
            neighbourhood="",                     # filled by reverse-geocode later
            property_type=ptype_label,
            advertiser_role="landlord",
            days_old=int(hrs // 24),
            days_old_known=True,
            available=at(availf, i),
            min_term=f"{int(mt)} months" if mt else "",
            bills_included=("yes" if _truthy(at(bills, i)) else "no"),
            lat=lat, lng=lng,
        ))
    log(f"  OpenRent: {len(ids)} scanned, {len(out)} shared rooms")
    return out
