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

from . import spareroom, gumtree, openrent, enrich, score
from .cache import JsonCache

# Registry of source adapters (each exposes search(session, cfg, log) -> [Listing]).
SOURCE_MODULES = {"SpareRoom": spareroom, "Gumtree": gumtree, "OpenRent": openrent}

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


def ensure_coords(c, api, outcode_cache, place_cache):
    """Fill a listing's lat/lng from the best source: exact coords, then a
    postcode district, then a free-text area name. Returns (lat, lng) or (None, None)."""
    if c.lat is not None and c.lng is not None:
        return c.lat, c.lng
    if c.postcode:
        oc = enrich.geocode_outcode(api, c.postcode, outcode_cache.data)
        if oc:
            c.lat, c.lng = oc
            return oc
    if c.neighbourhood:
        pl = enrich.geocode_place(api, f"{c.neighbourhood}, London", place_cache.data)
        if pl:
            c.lat, c.lng = pl
            return pl
    return None, None


# Central/expensive districts where a very cheap room is a classic scam red-flag.
_PREMIUM_OUTCODES = {"SW3", "SW7", "SW1", "SW5", "SW10", "W8", "W11", "W1", "W2", "SW1X", "SW1W"}


def flag_risks(l, cfg) -> None:
    """Lightweight, honest scam heuristics. Not a guarantee — a nudge to verify."""
    flags = []
    lo = cfg["budget"]["min"]
    # Photo-count is only meaningful for SpareRoom (we don't parse it elsewhere).
    if l.source == "SpareRoom" and l.num_photos == 0:
        flags.append("No photos in the ad")
    if l.price_pcm and l.price_pcm <= lo + 80 and l.postcode in _PREMIUM_OUTCODES:
        flags.append("Unusually cheap for this area — verify carefully")
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
    place_cache = JsonCache(CACHE_DIR / "places.json")
    reverse_cache = JsonCache(CACHE_DIR / "reverse.json")

    # Imperial coordinates for the distance pre-filter.
    imperial = enrich.geocode_postcode(api, cfg["commute"]["destination_postcode"]) \
        or (51.4988, -0.1749)
    depart_date, depart_time = enrich.resolve_departure(cfg)
    log(f"Commute estimate: depart {depart_date} {depart_time} -> "
        f"{cfg['commute']['destination_postcode']} (Imperial)")

    # 1) SEARCH across all enabled sources --------------------------------
    enabled = cfg.get("sources", {}).get("enabled", ["SpareRoom"])
    cards: list[spareroom.Listing] = []
    per_source: dict[str, int] = {}
    for name in enabled:
        mod = SOURCE_MODULES.get(name)
        if not mod:
            log(f"  (skipping unknown source {name})")
            continue
        log(f"Searching {name}...")
        sess = sr if name == "SpareRoom" else requests.Session()
        try:
            got = mod.search(sess, cfg, log=log)
        except Exception as e:                      # one bad source must not sink the run
            log(f"  ! {name} raised {e!r}")
            got = []
        per_source[name] = len(got)
        cards += got
    scanned = len(cards)

    # 2) BUDGET + RECENCY FILTER + de-dupe --------------------------------
    max_days = cfg["search"].get("max_days_old", 3)
    in_budget = [c for c in cards if lo <= c.price_pcm <= hi]
    # Purge stale ads where the posting age is known; keep unknown-age ones
    # (e.g. Gumtree), which we fetch newest-first instead.
    fresh = [c for c in in_budget if (not c.days_old_known) or c.days_old <= max_days]
    log(f"{scanned} scanned -> {len(in_budget)} within £{lo}-£{hi} "
        f"-> {len(fresh)} fresh (<= {max_days} days or unknown age)")

    # 3) PRUNE by rough distance, then prioritise close -------------------
    prefilter_km = cfg["search"]["radius_miles"] * 1.60934 + 3
    def rough_km(c):
        lat, lng = ensure_coords(c, api, outcode_cache, place_cache)
        return enrich.haversine_km(imperial[0], imperial[1], lat, lng) if lat is not None else 999.0
    for c in fresh:
        c._km = rough_km(c)  # type: ignore[attr-defined]
    candidates = [c for c in fresh if c._km <= prefilter_km]
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
    dropped_shortlet = 0
    for i, c in enumerate(candidates, 1):
        # -- SpareRoom only: fetch the detail page for coords/bills/term --
        if c.source == "SpareRoom":
            cached = detail_cache.get_fresh(c.id, DETAIL_MAX_AGE)
            if cached:
                for k in ("lat", "lng", "bills_included", "available", "min_term",
                          "max_term", "room_type", "furnishings", "description", "url"):
                    if cached.get(k) not in (None, ""):
                        setattr(c, k, cached[k])
            else:
                spareroom.fetch_detail(sr, c, log=log)
                detail_cache.put(c.id, {
                    "lat": c.lat, "lng": c.lng, "bills_included": c.bills_included,
                    "available": c.available, "min_term": c.min_term, "max_term": c.max_term,
                    "room_type": c.room_type, "furnishings": c.furnishings,
                    "description": c.description, "url": c.url,
                })
                time.sleep(delay)

        # -- drop short-lets / sublets: no use for a 12-month course --
        if cfg["prefs"].get("exclude_short_lets", True):
            reason = score.short_let_reason(c, cfg["prefs"].get("min_stay_months", 10))
            if reason:
                dropped_shortlet += 1
                continue

        # -- coordinates (already set for OpenRent; resolved otherwise) --
        lat, lng = ensure_coords(c, api, outcode_cache, place_cache)

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

        # -- a display area/postcode when we only had coordinates (OpenRent) --
        if not c.neighbourhood and lat is not None:
            rg = enrich.reverse_geocode(api, lat, lng, reverse_cache.data)
            c.neighbourhood = rg.get("area", "") or c.neighbourhood
            if not c.postcode:
                c.postcode = rg.get("postcode", "")

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
        f"({dropped_commute} dropped for commute, {dropped_shortlet} short-lets removed)")

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
        "posted_within_days": max_days,
        "sources": enabled,
        "scanned_by_source": per_source,
        "results_by_source": {s: sum(1 for c in results if c.source == s) for s in enabled},
        "budget": {"min": lo, "max": hi, "preferred_max": cfg["budget"].get("preferred_max", hi)},
        "commute": {
            "destination": cfg["commute"]["destination_postcode"],
            "max_minutes": max_minutes,
            "depart": f"{depart_date} {depart_time}",
        },
    }
    meta_path.write_text(json.dumps(meta, indent=1))

    outcode_cache.save(); detail_cache.save(); commute_cache.save()
    place_cache.save(); reverse_cache.save()
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
