"""
Smoke tests for the parsing + scoring that the whole pipeline depends on.

No network: fixtures mirror the real SpareRoom markup captured during build.
Run:  python -m pytest -q      (or)   python tests/test_parsing.py
If SpareRoom changes its HTML, the card/detail tests here fail loudly — that's
the signal to update the selectors in scraper/spareroom.py.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bs4 import BeautifulSoup
from scraper import spareroom, score

# --- fixtures (trimmed from real pages) ----------------------------------
CARD_HTML = (
    '<li class="listing-result" data-listing-id="12345" '
    'data-listing-title="Double Room in Fulham" '
    'data-listing-url="/flatshare/london/fulham/12345" '
    'data-listing-ad-rate-normalised="&pound;195" '
    'data-listing-ad-rate-normalised-period="pw" '
    'data-listing-postcode="SW6" data-listing-neighbourhood="Fulham" '
    'data-listing-days-old="0" data-listing-available-now="1" '
    'data-listing-property-type-more="flat" '
    'data-listing-advertiser-role="agent" '
    'data-listing-rooms-in-property="3" '
    'data-listing-ad-profile-photo="https://photos2.spareroom.co.uk/x.jpg"></li>'
)

DETAIL_HTML = """
<html><body>
<script>var m = { location: {latitude: "51.4812", longitude: "-0.1998",}, };</script>
<dl class="feature-list">
  <dt class="feature-list__key">Available</dt><dd class="feature-list__value">01 Sep 2026</dd>
  <dt class="feature-list__key">Furnishings</dt><dd class="feature-list__value">Furnished</dd>
  <dt class="feature-list__key">Bills included?</dt><dd class="feature-list__value">Yes</dd>
</dl>
</body></html>
"""

CFG = {
    "budget": {"min": 700, "max": 850, "tolerance": 0},
    "commute": {"ideal_minutes": 30, "max_minutes": 40},
    "weights": {"budget": .35, "commute": .40, "bills": .10, "move_in": .10, "freshness": .05},
    "prefs": {"move_in_window": {"from": "2026-09-01", "to": "2026-10-31"}},
}


def test_price_normalisation():
    assert spareroom.to_pcm("£850", "pcm") == 850
    assert spareroom.to_pcm("£195", "pw") == round(195 * 52 / 12)   # 845
    assert spareroom.to_pcm("£1,000", "pcm") == 1000
    assert spareroom.to_pcm("", "pcm") == 0


def test_card_parsing():
    li = BeautifulSoup(CARD_HTML, "html.parser").find("li")
    l = spareroom._parse_card(li)
    assert l is not None and l.id == "12345"
    assert l.price_pcm == round(195 * 52 / 12)      # pw -> pcm
    assert l.postcode == "SW6" and l.neighbourhood == "Fulham"
    assert l.available_now is True and l.days_old == 0
    assert l.url.endswith("flatshare_id=12345")


def test_detail_parsing():
    l = spareroom.Listing(id="12345", title="Double Room in Fulham")

    class _Resp:  # stand in for requests.Response
        text = DETAIL_HTML
    spareroom._get = lambda *a, **k: _Resp()          # monkeypatch network
    spareroom.fetch_detail(session=None, listing=l)
    assert abs(l.lat - 51.4812) < 1e-6 and abs(l.lng + 0.1998) < 1e-6
    assert l.bills_included == "yes"
    assert l.available == "01 Sep 2026"
    assert l.room_type == "double"                    # from title


def test_scoring_bounds_and_order():
    good = spareroom.Listing(id="a", price_pcm=720, days_old=0, bills_included="yes",
                             available="Available now", commute_minutes=22)
    poor = spareroom.Listing(id="b", price_pcm=850, days_old=20, bills_included="no",
                             available="01 Dec 2026", commute_minutes=40)
    score.score(good, CFG)
    score.score(poor, CFG)
    assert 0 <= poor.suitability <= good.suitability <= 100
    assert good.suitability > 70 and set(good.score_breakdown) == {
        "budget", "commute", "bills", "move_in", "freshness"}


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn(); print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} tests passed")
