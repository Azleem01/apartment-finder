/* Rooms near Imperial — dashboard logic (vanilla JS, no dependencies) */
"use strict";

const SR_BASE = "https://www.spareroom.co.uk";
const state = { all: [], meta: null };

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function badgeClass(v) { return v >= 75 ? "badge--high" : v >= 60 ? "badge--mid" : "badge--low"; }

function goodImage(url) {
  if (!url) return null;
  if (!/^https?:\/\//.test(url)) return null;          // relative placeholder
  if (/profilepic|\/icons\//i.test(url)) return null;   // generic avatar icon
  return url;
}

function thumb(l) {
  const img = goodImage(l.image);
  if (img) return `<img class="thumb" loading="lazy" src="${esc(img)}" alt="" onerror="this.replaceWith(Object.assign(document.createElement('div'),{className:'thumb',textContent:'🏠',style:'display:grid;place-content:center;font-size:26px'}))">`;
  return `<div class="thumb" style="display:grid;place-content:center;font-size:26px">🏠</div>`;
}

function facts(l) {
  const bits = [];
  bits.push(`<span class="fact fact--price">💷 <b>£${l.price_pcm}</b> pcm</span>`);
  if (l.commute_minutes != null)
    bits.push(`<span class="fact">🚇 <b>${l.commute_minutes} min</b> to Imperial</span>`);
  if (l.room_type) bits.push(`<span class="fact">🛏️ ${esc(l.room_type)}</span>`);
  else if (l.property_type) bits.push(`<span class="fact">🏢 ${esc(l.property_type)}</span>`);
  if (l.advertiser_role) bits.push(`<span class="fact">👤 ${esc(l.advertiser_role)}</span>`);
  if (l.bills_included === "yes") bits.push(`<span class="chip chip--bills">Bills included</span>`);
  if (Number(l.days_old) === 0) bits.push(`<span class="chip chip--new">New today</span>`);
  return bits.join("");
}

function breakdown(l) {
  const order = ["budget", "commute", "bills", "move_in", "freshness"];
  const rows = order.map((k) => {
    const b = l.score_breakdown?.[k];
    if (!b) return "";
    const pct = Math.round(b.score * 100);
    return `<div class="brow"><span>${k.replace("_", " ")}</span>
      <span class="bar"><i style="width:${pct}%"></i></span>
      <span class="pts">${b.points}</span></div>
      <div class="bnote">${esc(b.note)}</div>`;
  }).join("");
  return `<div class="breakdown">${rows}
      <div class="bnote" style="margin-top:8px">Points are each factor's contribution to the ${l.suitability}% total.</div></div>`;
}

function card(l) {
  const url = l.url && /^https?:/.test(l.url) ? l.url : SR_BASE + (l.url || "");
  const loc = [l.neighbourhood, l.postcode].filter(Boolean).join(" · ");
  const commute = l.commute_summary ? esc(l.commute_summary) : "";
  return `<article class="card">
    <div class="card__top">
      ${thumb(l)}
      <div class="card__head">
        <h3 class="card__title">${esc(l.title || "Room to rent")}</h3>
        <p class="card__loc">${esc(loc)}${commute ? " · " + commute : ""}</p>
      </div>
      <div class="badge ${badgeClass(l.suitability)}">${l.suitability}<small>MATCH</small></div>
    </div>
    <div class="card__facts">${facts(l)}</div>
    ${breakdown(l)}
    <div class="card__foot">
      <button class="why" type="button">Why this score</button>
      <a class="btn" href="${esc(url)}" target="_blank" rel="noopener noreferrer">View on SpareRoom ↗</a>
    </div>
  </article>`;
}

function apply() {
  const sort = $("sort").value;
  const maxC = +$("maxCommute").value;
  const maxP = +$("maxPrice").value;
  const billsOnly = $("billsOnly").checked;
  const newOnly = $("newOnly").checked;
  const q = $("q").value.trim().toLowerCase();

  let rows = state.all.filter((l) => {
    if (l.price_pcm > maxP) return false;
    if (l.commute_minutes != null && l.commute_minutes > maxC) return false;
    if (billsOnly && l.bills_included !== "yes") return false;
    if (newOnly && Number(l.days_old) !== 0) return false;
    if (q) {
      const hay = `${l.neighbourhood} ${l.postcode} ${l.title}`.toLowerCase();
      if (!hay.includes(q)) return false;
    }
    return true;
  });

  const cmp = {
    suitability: (a, b) => b.suitability - a.suitability,
    price: (a, b) => a.price_pcm - b.price_pcm,
    commute: (a, b) => (a.commute_minutes ?? 999) - (b.commute_minutes ?? 999),
    newest: (a, b) => a.days_old - b.days_old,
  }[sort];
  rows.sort(cmp);

  const grid = $("grid");
  grid.innerHTML = rows.map(card).join("");
  $("empty").hidden = rows.length > 0;
  $("statCount").textContent = rows.length;

  grid.querySelectorAll(".why").forEach((btn) => {
    btn.addEventListener("click", () => {
      const bd = btn.closest(".card").querySelector(".breakdown");
      const open = bd.classList.toggle("open");
      btn.textContent = open ? "Hide breakdown" : "Why this score";
    });
  });
}

function initControls() {
  const sync = () => {
    $("maxCommuteVal").textContent = $("maxCommute").value;
    $("maxPriceVal").textContent = "£" + $("maxPrice").value;
  };
  ["sort", "maxCommute", "maxPrice", "billsOnly", "newOnly", "q"].forEach((id) => {
    const ev = id === "q" ? "input" : "change";
    $(id).addEventListener(ev, () => { sync(); apply(); });
  });
  $("maxCommute").addEventListener("input", sync);
  $("maxPrice").addEventListener("input", sync);
  sync();
}

async function main() {
  try {
    const [listings, meta] = await Promise.all([
      fetch("data/listings.json", { cache: "no-store" }).then((r) => r.json()),
      fetch("data/meta.json", { cache: "no-store" }).then((r) => r.json()).catch(() => null),
    ]);
    state.all = Array.isArray(listings) ? listings : [];
    state.meta = meta;

    if (meta) {
      $("updated").textContent = "Updated " + (meta.generated_at_human || "recently");
      $("statScanned").textContent = meta.scanned ?? "–";
      $("statBudget").textContent = `£${meta.budget?.min}–${meta.budget?.max}`;
      $("statCommute").textContent = `${meta.commute?.max_minutes ?? 40} min`;
      $("maxCommute").value = meta.commute?.max_minutes ?? 40;
      $("maxPrice").value = meta.budget?.max ?? 850;
    } else {
      $("updated").textContent = "Loaded";
    }
    $("stats").hidden = false;
    $("controls").hidden = false;

    if (!state.all.length) {
      $("notice").hidden = false;
      $("notice").textContent = "No listings in the latest run. The daily refresh will try again — or widen the budget/commute in config.";
    }
    initControls();
    apply();
  } catch (e) {
    $("updated").textContent = "Couldn't load data";
    $("notice").hidden = false;
    $("notice").textContent = "Could not load listings.json yet. If you just deployed, run the refresh workflow once to generate data.";
    console.error(e);
  }
}

main();
