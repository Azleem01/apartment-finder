# 🏠 Rooms near Imperial — daily digest

A personal, self-refreshing web dashboard that finds **rooms in shared flats near
Imperial College London (South Kensington)** within your budget and commute, ranks
each by how well it fits, and updates itself every day — for free.

- **Source:** SpareRoom (the main site for London flatshares at this budget)
- **Budget:** £500–850 / month per room (a hard filter — set in `config.yml`)
- **Commute:** real Transport for London journey time to Imperial (≤ 45 min)
- **Ranking:** a transparent suitability % from price, commute, bills-included,
  tenancy fit (move in ~Sept 2026 and stay the full 12-month course), and freshness.
  Short-lets and sublets are filtered out.
- **Safety:** a “verified advertiser” badge, automatic scam red-flags (e.g. no
  photos, a suspiciously cheap central room), and **community comments + fraud
  reports** on every listing that everyone can see
- **Hosting:** static site on **GitHub Pages**, refreshed daily by a **GitHub
  Actions** cron job; comments live in a free **Supabase** database. Nothing to pay for.

> **Reality check.** £500–850 *within 45 minutes of South Kensington* is still a
> tight slice of London. Expect a few dozen matches on any given day, mostly in
> Fulham, Hammersmith, Earls Court, Holland Park, Putney and Battersea, with the
> cheapest rooms sitting further out. That's the market, not a bug — the app shows
> the best of what's actually there.

---

## How it works

```
Daily (GitHub Actions cron)
  scraper/pipeline.py
    1. spareroom.py  search rooms around SW7 (radius, £700-850, flatshares only)
    2. spareroom.py  fetch each ad's detail page: coords, bills, availability
    3. enrich.py     TfL journey time to Imperial (weekday 09:00), + geocoding
    4. score.py      suitability %  (budget + commute + bills + move-in + fresh)
    5. write docs/data/listings.json + meta.json  ->  git commit + push
GitHub Pages serves /docs  ->  the dashboard reads the JSON, filters & sorts live
```

Caches in `caches/` (committed) remember geocoding and commute results so repeat
listings aren't re-queried every day.

### Community comments & fraud flags

Every listing has a public comment thread (Supabase-backed) — **anyone can post
without logging in**, and everyone sees the same comments. Ticking **“Report as
possibly fraudulent”** files a fraud report; once a listing has any, a red
**“⚠ N fraud reports from the community — verify very carefully”** banner shows on
its card. Readers can 👍 a comment to agree.

Row-Level Security allows only *read* and *insert* — nobody can edit or delete
someone else's comment through the site. The Supabase URL + publishable key in
`docs/supabase-config.js` are safe to be public (that's what publishable keys are
for). Comments are **unmoderated** — signals to weigh, not proof.

> Fraud safety is best-effort: SpareRoom's verified badge + red-flag heuristics
> (`scraper/pipeline.py:flag_risks`) + community reports. No tool can guarantee a
> listing is genuine — always view in person and never pay before viewing.

---

## Deploy it (free, ~5 minutes)

1. **Create a GitHub repo** (public) and push this folder to it:
   ```bash
   git init && git add -A && git commit -m "Apartment finder"
   git branch -M main
   git remote add origin https://github.com/<you>/<repo>.git
   git push -u origin main
   ```
2. **Turn on Pages:** repo **Settings → Pages → Build and deployment → Deploy from a
   branch → Branch: `main`, Folder: `/docs` → Save.** Your site appears at
   `https://<you>.github.io/<repo>/`.
3. **Populate data now:** repo **Actions** tab → *Daily refresh* → **Run workflow**.
   (Enable Actions if prompted.) It scrapes, scores, and commits the data.
4. Open your Pages URL — on your laptop or phone. It refreshes on its own every day
   at 06:00 UTC.

> If Actions can't push, check **Settings → Actions → General → Workflow permissions**
> is set to **Read and write**.

---

## Run it locally

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

python -m scraper.pipeline            # full run -> writes docs/data/*.json
python -m scraper.pipeline --limit 15 # quick test (fewer candidates)

python3 -m http.server -d docs 8000   # then open http://localhost:8000
python -m pytest -q                   # or: python tests/test_parsing.py
```

---

## Tune it — everything lives in `config.yml`

| Setting | What it does |
|---|---|
| `budget.min` / `max` | Your hard rent band (per room, per month). |
| `budget.tolerance` | Optionally include a little over budget (penalised, not hidden). |
| `commute.max_minutes` | Drop anything slower than this to Imperial. |
| `commute.ideal_minutes` | At/under this = full commute score. |
| `search.radius_miles` | How far around South Kensington to look (commute filter is the real gate). |
| `search.max_pages` / `enrich_limit` | Pool size vs. how many get full detail + commute. |
| `prefs.move_in_window` | The Sept–Oct dates the move-in boost rewards. |
| `weights.*` | Re-balance the ranking (e.g. value commute over price). |

---

## Notes & caveats

- **SpareRoom's terms** don't permit automated access. This runs **once a day, at low
  volume, with polite delays** for personal use. If you'd rather stay fully within
  their terms, use SpareRoom's own saved-search **email alerts** instead, or a paid
  data provider (e.g. Apify) — the code is structured so another source can be added.
- **GitHub's runners use datacenter IPs**, which SpareRoom *may* block. If a daily run
  ever returns nothing, the fallback is to run the exact same `python -m scraper.pipeline`
  on your Mac on a schedule (macOS `launchd`) and let it push the data — identical result.
- **Always verify a listing yourself and never send money before viewing.** This tool
  points you at ads; it doesn't vet landlords.

**Adding Rightmove / Zoopla later:** they block free scraping and have no public search
API, so they'd need a paid provider. Drop a new module next to `scraper/spareroom.py`
that returns `Listing` objects and have `pipeline.py` merge its results — scoring,
commute and the dashboard all work unchanged.
