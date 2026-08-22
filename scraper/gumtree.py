"""
Gumtree source adapter (property-to-share = rooms in shared homes).

Gumtree renders result cards server-side (hashed CSS classes, but stable
`data-q` hooks). We parse each card's text for title, price, area and
availability. Gumtree's list pages do NOT expose a posting date, so freshness
is marked unknown (days_old_known=False) and we rely on newest-first ordering.
Area names are geocoded to coordinates later in the pipeline.
"""

from __future__ import annotations

import re
import time
import requests
from bs4 import BeautifulSoup
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from .spareroom import Listing, HEADERS, to_pcm

NAME = "Gumtree"
BASE = "https://www.gumtree.com"
SEARCH = BASE + "/flats-houses/property-to-share/uk/london"

_PRICE_RE = re.compile(r"£([\d,]+)\s*(pppw|pcm|pm|pw)", re.I)
_AVAIL_RE = re.compile(r"Date available:\s*(\d{1,2}\s+\w+\s+\d{4})", re.I)
# Area sits right after the property type, e.g. "... House Enfield, London £800pm".
_LOC_RE = re.compile(r"(?:Flat|House|Studio|Property|Parking|Room)\s+([A-Za-z .'\-]+?),\s*London\s*£", re.I)


@retry(retry=retry_if_exception_type(requests.RequestException),
       stop=stop_after_attempt(3), wait=wait_exponential(multiplier=2, min=2, max=15),
       reraise=True)
def _get(session, url):
    r = session.get(url, timeout=30)
    r.raise_for_status()
    return r


def _price_pcm(text: str) -> tuple[int, str]:
    m = _PRICE_RE.search(text)
    if not m:
        return 0, ""
    amount = m.group(1)
    per = m.group(2).lower()
    raw = f"£{amount} {per}"
    # pppw / pw are per-week; pm / pcm per-month.
    period = "pw" if per in ("pw", "pppw") else "pcm"
    return to_pcm(amount, period), raw


def _parse_card(card) -> Listing | None:
    a = card.select_one('[data-q="search-result-anchor"]') or card.find("a", href=True)
    if not a or not a.get("href"):
        return None
    href = a["href"]
    m = re.search(r"/(\d+)(?:$|[?#])", href)
    lid = m.group(1) if m else href.rsplit("/", 1)[-1]
    text = re.sub(r"\s+", " ", card.get_text(" ", strip=True)).strip()

    price_pcm, price_raw = _price_pcm(text)
    if not price_pcm:
        return None

    loc_m = _LOC_RE.search(text)
    area = loc_m.group(1).strip() if loc_m else ""
    avail_m = _AVAIL_RE.search(text)
    role = ("agent" if re.search(r"\bAgency\b", text) else
            "private landlord" if re.search(r"\bPrivate\b", text) else "")
    # Title = text before the advertiser word, minus a leading "Featured N".
    title = re.split(r"\b(Private|Agency)\b", text)[0]
    title = re.sub(r"^\s*(Featured|Urgent|Spotlight)?\s*\d*\s*", "", title).strip()[:90] or "Room to rent"

    bills = "yes" if re.search(r"bills?\s*(incl|included)|incl[a-z. ]*bills|"
                               r"all[\s-]*inclusive|inc\.?\s*bills", text, re.I) else "unknown"

    return Listing(
        id=f"GT{lid}",
        source=NAME,
        title=title,
        url=BASE + href if href.startswith("/") else href,
        price_pcm=price_pcm,
        price_raw=price_raw,
        bills_included=bills,
        neighbourhood=area.replace(", London", "").strip(),
        property_type=("house" if "House" in text else "flat" if "Flat" in text else ""),
        advertiser_role=role,
        days_old=0,
        days_old_known=False,           # Gumtree list pages hide the posting date
        available=avail_m.group(1) if avail_m else "",
        image="",
    )


def search(session: requests.Session, cfg: dict, log=print) -> list[Listing]:
    session.headers.update(HEADERS)
    pages = cfg.get("sources", {}).get("gumtree", {}).get("max_pages", 4)
    delay = cfg["search"].get("request_delay_seconds", 1.5)
    seen: dict[str, Listing] = {}
    for page in range(1, pages + 1):
        url = SEARCH + (f"?page={page}&sort=date" if page > 1 else "?sort=date")
        try:
            resp = _get(session, url)
        except requests.RequestException as e:
            log(f"  ! Gumtree page {page} failed: {e}")
            break
        soup = BeautifulSoup(resp.text, "html.parser")
        cards = soup.select('[data-q="search-result"]')
        if not cards:
            anchors = soup.select('[data-q="search-result-anchor"]')
            cards = [a.find_parent("article") or a.parent for a in anchors]
        added = 0
        for card in cards:
            if not card:
                continue
            listing = _parse_card(card)
            if listing and listing.id not in seen:
                seen[listing.id] = listing
                added += 1
        log(f"  Gumtree page {page}: {len(cards)} cards, {added} new")
        if not added:
            break
        time.sleep(delay)
    return list(seen.values())
