/* Global per-listing comments + community fraud flags, backed by Supabase.
   Anyone can read and post — no login. Loaded as a module so it can import the
   Supabase client. app.js calls window.onCardsRendered() after each render. */
import { createClient } from "https://esm.sh/@supabase/supabase-js@2";

const sb = (window.SUPABASE_URL && window.SUPABASE_KEY)
  ? createClient(window.SUPABASE_URL, window.SUPABASE_KEY)
  : null;

const cache = new Map();      // listing_id -> [comment, ...]
let loaded = false;

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function ago(iso) {
  const s = Math.max(1, (Date.now() - new Date(iso).getTime()) / 1000);
  const u = [["y", 31536000], ["mo", 2592000], ["d", 86400], ["h", 3600], ["m", 60]];
  for (const [label, secs] of u) if (s >= secs) return Math.floor(s / secs) + label + " ago";
  return "just now";
}

function push(c) {
  if (!cache.has(c.listing_id)) cache.set(c.listing_id, []);
  cache.get(c.listing_id).push(c);
}

async function loadAll(ids) {
  if (!sb || !ids?.length) { loaded = true; return; }
  const { data, error } = await sb.from("comments")
    .select("*").in("listing_id", ids).order("created_at", { ascending: true });
  if (error) { console.warn("comments load:", error.message); loaded = true; return; }
  cache.clear();
  (data || []).forEach(push);
  loaded = true;
}

function commentHTML(c) {
  const fraud = c.kind === "fraud";
  return `<div class="cmt ${fraud ? "cmt--fraud" : ""}" data-id="${c.id}">
    <div class="cmt__meta"><b>${esc(c.author || "Anonymous")}</b>
      ${fraud ? '<span class="cmt__flag">⚠ fraud report</span>' : ""}
      <span class="cmt__time">${ago(c.created_at)}</span></div>
    <div class="cmt__body">${esc(c.body)}</div>
    <button class="cmt__agree" type="button" data-id="${c.id}">👍 <span>${c.agrees || 0}</span></button>
  </div>`;
}

function renderList(listingId, panel) {
  const list = cache.get(listingId) || [];
  const box = panel.querySelector(".cmts__list");
  box.innerHTML = list.length
    ? list.map(commentHTML).join("")
    : `<p class="cmts__empty">No comments yet — add local knowledge, or flag it if something looks off.</p>`;
  box.querySelectorAll(".cmt__agree").forEach((b) =>
    b.addEventListener("click", () => onAgree(b)));
}

async function onAgree(btn) {
  if (!sb) return;
  btn.disabled = true;
  const id = btn.dataset.id;
  const { data, error } = await sb.rpc("agree_comment", { comment_id: id });
  if (!error) {
    btn.querySelector("span").textContent = data;
    for (const arr of cache.values()) {
      const c = arr.find((x) => x.id === id); if (c) { c.agrees = data; break; }
    }
  }
  btn.disabled = false;
}

function updateToggle(node) {
  const list = cache.get(node.dataset.listing) || [];
  const frauds = list.filter((c) => c.kind === "fraud").length;
  const label = node.querySelector(".cmts__count");
  label.textContent = list.length ? `Comments (${list.length})` : "Add a comment";
  const card = node.closest(".card");
  let warn = card.querySelector(".card__fraudwarn");
  if (frauds) {
    if (!warn) {
      warn = document.createElement("div");
      warn.className = "card__fraudwarn";
      card.prepend(warn);
    }
    warn.textContent = `⚠ ${frauds} fraud report${frauds > 1 ? "s" : ""} from the community — verify very carefully`;
  } else if (warn) { warn.remove(); }
}

function wireOne(node) {
  if (node.dataset.wired === "1") { updateToggle(node); return; }
  node.dataset.wired = "1";
  const toggle = node.querySelector(".cmts__toggle");
  const panel = node.querySelector(".cmts__panel");
  const form = node.querySelector(".cmts__form");
  updateToggle(node);

  toggle.addEventListener("click", () => {
    const open = panel.hasAttribute("hidden");
    if (open) { panel.removeAttribute("hidden"); renderList(node.dataset.listing, panel); }
    else panel.setAttribute("hidden", "");
  });

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!sb) { alert("Comments backend not configured."); return; }
    const body = form.querySelector(".cmts__body").value.trim();
    if (!body) return;
    const btn = form.querySelector(".cmts__send");
    btn.disabled = true; btn.textContent = "Posting…";
    try {
      const row = {
        listing_id: node.dataset.listing,
        author: form.querySelector(".cmts__name").value.trim() || "Anonymous",
        body,
        kind: form.querySelector(".cmts__fraud input").checked ? "fraud" : "comment",
      };
      const { data, error } = await sb.from("comments").insert(row).select().single();
      if (error) throw error;
      push(data);
      form.reset();
      renderList(node.dataset.listing, panel);
      updateToggle(node);
    } catch (err) {
      alert("Could not post: " + (err.message || err));
    } finally {
      btn.disabled = false; btn.textContent = "Post comment";
    }
  });
}

async function wireAll() {
  const nodes = [...document.querySelectorAll(".js-comments[data-listing]")];
  if (!nodes.length) return;
  if (!loaded) await loadAll(window.__listingIds || nodes.map((n) => n.dataset.listing));
  nodes.forEach(wireOne);
}

window.onCardsRendered = wireAll;
// In case cards already rendered before this module loaded:
if (document.querySelector(".js-comments")) wireAll();
