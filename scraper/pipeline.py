"""
Orchestrator: SpareRoom search -> prune -> enrich (detail + commute) -> score
            -> write docs/data/{listings,meta}.json

Run:
    python -m scraper.pipeline                 # full daily run (uses config.yml)
    python -m scraper.pipeline --limit 15      # quick test: few candidates
    python -m scraper.pipeline --config other.yml

Caches (caches/*.json) are reused across runs and committed by the Action, so
repeat listings are not re-geocoded or re-routed every day.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import time
from pathlib import Path

import requests
import yaml

from . import spareroom, enrich, score
from .cache import JsonCache

ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = ROOT / "caches"

DETAIL_MAX_AGE = 14   # ad facts (coords/bills) rarely change
COMMUTE_MAX_AGE = 7   # journey time is stable but refresh weekly


def load_config(path: str) -> dict:
    return yaml.safe_load(Path(path).read_text())


def api_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": "apartment-finder/1.0 (personal room search)"})
    return s


# Central/expensive districts where a very cheap room is a classic scam red-flag.
_PREMIUM_OUTCODES = {"SW3", "SW7", "SW1", "SW5", "SW10", "W8", "W11", "W1", "W2", "SW1X", "SW1W"}


def flag_risks(l, cfg) -> None:
    """Lightweight, honest scam heuristics. Not a guarantee — a nudge to verify."""
    flags = []
    lo = cfg["budget"]["min"]
    if l.num_photos == 0:
        flags.append("No photos in the ad")
    if l.price_pcm and l.price_pcm <= lo + 80 and l.postcode in _PREMIUM_OUTCODES:
        flags.append("Unusually cheap for this area — verify carefully")
    if not l.verified and l.advertiser_role in ("", "live in landlord") and l.num_photos == 0:
        flags.append("Unverified advertiser with no photos")
    l.risk_flags = flags


def run(config_path: str, limit: int | None = None, log=print) -> dict:
    cfg = load_config(config_path)
    if limit:                       # test mode: shrink the run
        cfg["search"]["max_pages"] = min(cfg["search"]["max_pages"], 2)
        cfg["search"]["enrich_limit"] = limit

    lo = cfg["budget"]["min"]
    hi = cfg["budget"]["max"] + cfg["budget"].get("tolerance", 0)
    delay = cfg["search"].get("request_delay_seconds", 1.5)

    sr = spareroom.make_session()
    api = api_session()

    outcode_cache = JsonCache(CACHE_DIR / "outcodes.json")
    detail_cache = JsonCache(CACHE_DIR / "details.json")
    commute_cache = JsonCache(CACHE_DIR / "commute.json")

    # Imperial coordinates for the distance pre-filter.
    imperial = enrich.geocode_postcode(api, cfg["commute"]["destination_postcode"]) \
        or (51.4988, -0.1749)
    depart_date, depart_time = enrich.resolve_departure(cfg)
    log(f"Commute estimate: depart {depart_date} {depart_time} -> "
        f"{cfg['commute']['destination_postcode']} (Imperial)")

    # 1) SEARCH ------------------------------------------------------------
    log("Searching SpareRoom...")
    cards = spareroom.search(sr, cfg, log=log)
    scanned = len(cards)

    # 2) BUDGET FILTER + de-dupe ------------------------------------------
    in_budget = [c for c in cards if lo <= c.price_pcm <= hi]
    log(f"{scanned} scanned -> {len(in_budget)} within £{lo}-£{hi}")

    # 3) PRUNE by rough distance, then prioritise fresh + close -----------
    prefilter_km = cfg["search"]["radius_miles"] * 1.60934 + 3
    def rough_km(c):
        coords = enrich.geocode_outcode(api, c.postcode, outcode_cache.data)
        return enrich.haversine_km(*imperial, *coords) if coords else 999.0
    for c in in_budget:
        c._km = rough_km(c)  # type: ignore[attr-defined]
    candidates = [c for c in in_budget if c._km <= prefilter_km]
    # Enrich closest-first: these are the likeliest to clear the commute gate and
    # rank well, so we spend the enrichment budget where it pays off. (Freshness
    # still influences the final suitability score, just not this ordering.)
    candidates.sort(key=lambda c: (round(c._km, 1), c.days_old))
    enrich_limit = cfg["search"].get("enrich_limit", 60)
    candidates = candidates[:enrich_limit]
    log(f"{len(candidates)} candidates to enrich (<= {prefilter_km:.0f} km, "
        f"capped at {enrich_limit})")

    # 4) ENRICH: detail page + commute ------------------------------------
    max_minutes = cfg["commute"]["max_minutes"]
    results: list[spareroom.Listing] = []
    dropped_commute = 0
    for i, c in enumerate(candidates, 1):
        # -- detail (coords, bills, availability) --
        cached = detail_cache.get_fresh(c.id, DETAIL_MAX_AGE)
        if cached:
            for k in ("lat", "lng", "bills_included", "available",
                      "room_type", "furnishings", "description", "url"):
                if cached.get(k) not in (None, ""):
                    setattr(c, k, cached[k])
        else:
            spareroom.fetch_detail(sr, c, log=log)
            detail_cache.put(c.id, {
                "lat": c.lat, "lng": c.lng, "bills_included": c.bills_included,
                "available": c.available, "room_type": c.room_type,
                "furnishings": c.furnishings, "description": c.description, "url": c.url,
            })
            time.sleep(delay)

        # -- commute (prefer exact coords, fall back to outcode centroid) --
        lat, lng = (c.lat, c.lng)
        if lat is None or lng is None:
            oc = enrich.geocode_outcode(api, c.postcode, outcode_cache.data)
            lat, lng = oc if oc else (None, None)

        cc = commute_cache.get_fresh(c.id, COMMUTE_MAX_AGE)
        if cc:
            c.commute_minutes = cc.get("minutes")
            c.commute_summary = cc.get("summary", "")
        elif lat is not None:
            res = enrich.commute(api, lat, lng, cfg, depart_date, depart_time)
            if res:
                c.commute_minutes, c.commute_summary = res
                commute_cache.put(c.id, {"minutes": res[0], "summary": res[1]})

        # -- enforce the real commute constraint --
        if c.commute_minutes is not None and c.commute_minutes > max_minutes:
            dropped_commute += 1
            continue
        results.append(c)
        if i % 10 == 0:
            log(f"  enriched {i}/{len(candidates)}")

    # 5) SCORE + SORT ------------------------------------------------------
    for c in results:
        score.score(c, cfg)
        flag_risks(c, cfg)
    results.sort(key=lambda c: c.suitability, reverse=True)
    max_results = cfg["output"].get("max_results", 80)
    results = results[:max_results]
    log(f"{len(results)} listings within {max_minutes} min "
        f"({dropped_commute} dropped for commute)")

    # 6) WRITE -------------------------------------------------------------
    listings_path = ROOT / cfg["output"]["listings_path"]
    meta_path = ROOT / cfg["output"]["meta_path"]
    listings_path.parent.mkdir(parents=True, exist_ok=True)
    listings_path.write_text(json.dumps([c.as_dict() for c in results], indent=1))

    now = dt.datetime.now(dt.timezone.utc)
    meta = {
        "generated_at": now.isoformat(),
        "generated_at_human": now.strftime("%a %d %b %Y, %H:%M UTC"),
        "count": len(results),
        "scanned": scanned,
        "within_budget": len(in_budget),
        "source": "SpareRoom",
        "budget": {"min": lo, "max": hi},
        "commute": {
            "destination": cfg["commute"]["destination_postcode"],
            "max_minutes": max_minutes,
            "depart": f"{depart_date} {depart_time}",
        },
    }
    meta_path.write_text(json.dumps(meta, indent=1))

    outcode_cache.save(); detail_cache.save(); commute_cache.save()
    log(f"Wrote {listings_path.relative_to(ROOT)} and "
        f"{meta_path.relative_to(ROOT)}")
    return meta


def main() -> None:
    ap = argparse.ArgumentParser(description="Find & rank SpareRoom rooms near Imperial.")
    ap.add_argument("--config", default=str(ROOT / "config.yml"))
    ap.add_argument("--limit", type=int, default=None,
                    help="test mode: only enrich this many candidates")
    args = ap.parse_args()
    meta = run(args.config, limit=args.limit)
    print(json.dumps(meta, indent=1))


if __name__ == "__main__":
    main()
