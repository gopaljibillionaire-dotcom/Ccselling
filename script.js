/* Digital Products Storefront. Safe DOM only. Prices from server. */
"use strict";
(() => {
const $ = (s, r = document) => r.querySelector(s);
const state = { user: null, csrf: null, cfg: { currency: "USD", tiers: [] }, countries: [], categories: [], country: "", cart: null, wishlist: new Set(), loadId: 0, captchaToken: "" };
const store = {
  get(k, d) { try { const v = localStorage.getItem(k); return v == null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch {} },
};

function h(tag, attrs, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === "class") e.className = v;
    else if (k.startsWith("on") && typeof v === "function") e.addEventListener(k.slice(2), v);
    else if (k === "text") e.textContent = v;
    else if (v === true) e.setAttribute(k, "");
    else e.setAttribute(k, v);
  }
  for (const c of kids.flat()) if (c != null && c !== false) e.append(c.nodeType ? c : document.createTextNode(String(c)));
  return e;
}
const clear = (el) => { while (el.firstChild) el.removeChild(el.firstChild); return el; };
const money = (minor, cur = state.cfg.currency) => { try { return new Intl.NumberFormat(undefined, { style: "currency", currency: cur }).format(minor / 100); } catch { return (minor / 100).toFixed(2) + " " + cur; } };
const fmtDate = (s) => (s ? new Date(s).toLocaleString() : "");
const safeImg = (src, alt) => { const i = h("img", { src: src || "", alt: alt || "", loading: "lazy", decoding: "async" }); i.addEventListener("error", () => { i.src = "data:image/svg+xml;utf8," + encodeURIComponent("<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><rect width='100' height='100' fill='#12204a'/><text x='50' y='56' font-size='12' text-anchor='middle' fill='#9fb0d4'>Digital</text></svg>"); }, { once: true }); return i; };
function toast(msg, kind = "") { const t = h("div", { class: "toast " + kind, role: kind === "bad" ? "alert" : "status" }, msg); $("#toasts").append(t); setTimeout(() => t.remove(), 4500); }
const bar = (on) => { const b = $("#loadbar"); b.style.width = on ? "70%" : "100%"; if (!on) setTimeout(() => (b.style.width = "0"), 250); };

class ApiError extends Error { constructor(msg, status) { super(msg); this.status = status; } }
async function api(path, { method = "GET", body, headers = {} } = {}) {
  const opts = { method, credentials: "same-origin", headers: { Accept: "application/json", ...headers } };
  if (body !== undefined) { opts.body = JSON.stringify(body); opts.headers["Content-Type"] = "application/json"; }
  if (method !== "GET" && state.csrf) opts.headers["X-CSRF-Token"] = state.csrf;
  let res;
  try { res = await fetch("/api" + path, opts); } catch { throw new ApiError("Network problem - check your connection and retry.", 0); }
  let data = null;
  try { data = await res.json(); } catch {}
  if (!res.ok) {
    if (res.status === 401 && state.user) { state.user = null; state.csrf = null; renderAuthState(); }
    const ra = res.headers.get("Retry-After");
    throw new ApiError((data && data.error) ? String(data.error) + (res.status === 429 && ra ? ` (retry in ${ra}s)` : "") : `Request failed (${res.status})`, res.status);
  }
  return data;
}
const guard = (fn) => async (...a) => { try { return await fn(...a); } catch (e) { toast(e.message || "Something went wrong", "bad"); } };

function openDialog(title, content, onClose) {
  const d = $("#dlg"); clear(d);
  d.append(h("div", { class: "row between" }, h("h2", { id: "dlgTitle" }, title), h("button", { class: "btn sm", type: "button", "aria-label": "Close", onclick: () => d.close() }, "✕")), content);
  d.onclose = () => { d.onclose = null; onClose && onClose(); };
  if (!d.open) d.showModal();
  return d;
}
const closeDialog = () => { const d = $("#dlg"); if (d.open) d.close(); };

function setTheme(t, persist = true) {
  document.documentElement.setAttribute("data-theme", t);
  try { localStorage.setItem("theme", t); } catch {}
  if (persist && state.user) api("/account/profile", { method: "PATCH", body: { theme: t } }).catch(() => {});
}
const toggleTheme = () => setTheme(document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark");

function stars(avg, count) { if (!count) return null; return h("span", { class: "stars", title: `${avg} of 5` }, "★".repeat(Math.round(avg)) + "☆".repeat(5 - Math.round(avg)), h("span", { class: "muted small" }, ` (${count})`)); }
function priceEl(p) { return h("div", { class: "price" }, money(p.final_price_minor), p.discount_percent ? h("s", null, money(p.price_minor)) : null); }
const availChip = () => h("span", { class: "chip ok" }, "Instant Delivery");

function productCard(p) {
  const href = "#/p/" + encodeURIComponent(p.slug);
  const fav = h("button", { class: "fav", type: "button", "aria-pressed": String(state.wishlist.has(p.id)), "aria-label": "Toggle wishlist", onclick: guard(async (ev) => { ev.preventDefault(); await toggleWish(p.id, fav); }) }, "♥");
  const cardFields = (p.fields || []).filter((f) => f.card).slice(0, 2);
  return h("article", { class: "card" },
    h("a", { class: "img", href, "aria-label": p.name }, safeImg(p.images[0] && p.images[0].url, p.images[0] && p.images[0].alt || p.name),
      h("div", { class: "tags" }, p.discount_percent ? h("span", { class: "chip warn" }, `-${p.discount_percent}%`) : null, h("span", { class: "chip ok" }, "Instant")), fav),
    h("div", { class: "body" },
      h("div", { class: "row wrap small muted" }, h("span", null, p.country.name), h("span", null, "·"), h("span", null, p.category.name)),
      h("a", { class: "name", href }, p.name),
      h("div", { class: "desc" }, p.short_description),
      cardFields.map((f) => h("div", { class: "small muted" }, `${f.label}: ${Array.isArray(f.value) ? f.value.join(", ") : typeof f.value === "object" ? "" : String(f.value)}`)),
      stars(p.rating_avg, p.rating_count), priceEl(p),
      h("div", { class: "row wrap" }, availChip()),
      h("div", { class: "actions" },
        h("button", { class: "btn primary sm", type: "button", onclick: guard(() => addToCart(p.id, 1)) }, "Add to cart"),
        h("a", { class: "btn sm", href }, "Details"),
        h("button", { class: "btn sm", type: "button", title: "Buy now", onclick: guard(() => buyNow(p.id, 1)) }, "Buy now"))));
}
const skeletons = (n = 8) => h("div", { class: "grid products" }, Array.from({ length: n }, () => h("div", { class: "skeleton" })));
const empty = (msg, cta) => h("div", { class: "empty" }, h("p", null, msg), cta || null);
const errorBox = (e, retry) => h("div", { class: "empty", role: "alert" }, h("p", null, e.message || "Could not load this section."), retry ? h("button", { class: "btn", type: "button", onclick: retry }, "Retry") : null);

function renderAuthState() {
  const li = !!state.user;
  $("#authBtn").classList.toggle("hide", li); $("#acctBtn").classList.toggle("hide", !li);
  $("#notifBtn").classList.toggle("hide", !li);
  $("#cartCount").classList.toggle("hide", true);
  updateCartBadge();
}
async function loadSession() {
  try {
    const r = await api("/auth/me");
    state.user = r.user; state.csrf = r.csrf; state.wishlist = new Set(r.user.wishlist || []);
    if (r.user.theme) setTheme(r.user.theme, false);
  } catch { state.user = null; state.csrf = null; }
  renderAuthState();
}
function requireLogin(msg = "Please sign in to continue.") { if (state.user) return true; toast(msg); authDialog("login"); return false; }

async function fetchCaptcha() {
  try { const r = await api("/captcha"); state.captchaToken = r.token; return r.question; } catch { return ""; }
}

function authDialog(mode = "login") {
  const err = h("div", { class: "error-text", role: "alert" });
  const email = h("input", { id: "a_email", type: "email", autocomplete: "email", required: true, maxlength: 254 });
  const pw = h("input", { id: "a_pw", type: "password", autocomplete: mode === "login" ? "current-password" : "new-password", required: true, maxlength: 128 });
  const name = h("input", { id: "a_name", type: "text", autocomplete: "name", maxlength: 80 });
  const invite = h("input", { id: "a_inv", type: "text", maxlength: 100, autocomplete: "off" });
  const ref = h("input", { id: "a_ref", type: "text", maxlength: 20, value: store.get("ref", "") });
  const terms = h("input", { id: "a_terms", type: "checkbox" });
  const captchaQ = h("span", { class: "question" }, "Loading…");
  const captchaA = h("input", { id: "a_cap", inputmode: "numeric", maxlength: "6", required: true });
  const captchaBox = h("div", { class: "captcha-box", id: "dlgCaptcha" }, captchaQ, h("button", { class: "refresh", type: "button", title: "Refresh", onclick: async () => { captchaQ.textContent = "…"; captchaQ.textContent = await fetchCaptcha(); captchaA.value = ""; } }, "↻"));
  const f = (label, input, hint) => h("div", { class: "field" }, h("label", { for: input.id }, label), input, hint ? h("div", { class: "hint" }, hint) : null);
  const form = h("form", { novalidate: true }, f("Email", email), mode === "register" ? f("Full name", name) : null, f("Password", pw, mode === "register" ? "At least 10 characters with three of: lowercase, uppercase, digit, symbol." : null),
    mode === "register" && state.cfg.invite_only ? f("Invitation code", invite, "Registration is invitation-only. No code? Apply from the support page.") : null,
    mode === "register" ? f("Referral code (optional)", ref) : null,
    h("div", { class: "field" }, h("label", null, "Security check"), captchaBox, f("Answer", captchaA)),
    mode === "register" ? h("label", { class: "check" }, terms, "I accept the terms of service and privacy policy") : null, err,
    h("button", { class: "btn primary", type: "submit", style: "width:100%" }, mode === "login" ? "Sign in" : "Create account"),
    h("p", { class: "small muted" }, mode === "login" ? "New here? " : "Have an account? ", h("a", { href: "#", onclick: (e) => { e.preventDefault(); authDialog(mode === "login" ? "register" : "login"); } }, mode === "login" ? "Create an account" : "Sign in")));
  fetchCaptcha().then(q => captchaQ.textContent = q);
  form.addEventListener("submit", async (e) => {
    e.preventDefault(); err.textContent = "";
    if (!email.value || !pw.value) { err.textContent = "Email and password are required."; return; }
    if (!captchaA.value.trim()) { err.textContent = "Please answer the security question."; return; }
    try {
      const body = mode === "login"
        ? { email: email.value, password: pw.value, captcha_answer: state.captchaToken + "|" + captchaA.value.trim() }
        : { email: email.value, password: pw.value, name: name.value || "Customer", accept_terms: terms.checked, invite_code: invite.value || null, referral_code: ref.value || null };
      if (mode === "register" && !terms.checked) { err.textContent = "Please accept the terms to continue."; return; }
      const r = await api(mode === "login" ? "/auth/login" : "/auth/register", { method: "POST", body });
      if (r.mfa_required) { err.textContent = "Administrator accounts sign in on the /admin console."; return; }
      state.user = r.user; state.csrf = r.csrf; state.wishlist = new Set(r.user.wishlist || []);
      closeDialog(); renderAuthState(); await mergeGuestCart(); toast(mode === "login" ? "Welcome back!" : "Account created. Welcome!", "ok"); route();
    } catch (ex) { err.textContent = ex.message; if (/captcha/i.test(ex.message)) { captchaQ.textContent = await fetchCaptcha(); captchaA.value = ""; } }
  });
  openDialog(mode === "login" ? "Sign in" : "Create account", form);
  setTimeout(() => email.focus(), 30);
}
const logout = guard(async () => { await api("/auth/logout", { method: "POST" }); state.user = null; state.csrf = null; state.cart = null; state.wishlist = new Set(); renderAuthState(); toast("Signed out.", "ok"); location.hash = "#/"; });

const guestCart = () => store.get("guestCart", []);
async function mergeGuestCart() {
  const g = guestCart(); if (!g.length) return;
  for (const it of g) { try { await api("/cart/items", { method: "PUT", body: { product_id: it.product_id, qty: it.qty } }); } catch {} }
  store.set("guestCart", []); await refreshCart();
}
async function refreshCart() { if (!state.user) { state.cart = null; updateCartBadge(); return; } try { state.cart = await api("/cart"); } catch { state.cart = null; } updateCartBadge(); }
function updateCartBadge() {
  const n = state.user ? (state.cart ? state.cart.items.reduce((s, i) => s + i.qty, 0) : 0) : guestCart().reduce((s, i) => s + i.qty, 0);
  for (const id of ["cartCount", "cartCount2"]) { const b = $("#" + id); b.textContent = String(n); b.classList.toggle("hide", n === 0); }
}
async function addToCart(pid, qty) {
  if (!state.user) { const g = guestCart(); const ex = g.find((i) => i.product_id === pid); if (ex) ex.qty = Math.min(10, ex.qty + qty); else g.push({ product_id: pid, qty }); store.set("guestCart", g); updateCartBadge(); toast("Added to cart. Sign in at checkout.", "ok"); return; }
  const cur = state.cart && state.cart.items.find((i) => i.product_id === pid);
  state.cart = await api("/cart/items", { method: "PUT", body: { product_id: pid, qty: Math.min(10, (cur ? cur.qty : 0) + qty) } });
  updateCartBadge(); toast("Added to cart.", "ok");
}
async function setQty(pid, qty) { state.cart = await api("/cart/items", { method: "PUT", body: { product_id: pid, qty } }); updateCartBadge(); }
async function buyNow(pid, qty) {
  if (!requireLogin("Sign in to buy this product.")) return;
  await api("/cart/items", { method: "PUT", body: { product_id: pid, qty } });
  await refreshCart();
  location.hash = "#/cart";
}

async function toggleWish(pid, btn) {
  if (!requireLogin("Sign in to save items to your wishlist.")) return;
  const on = state.wishlist.has(pid);
  await api(`/wishlist/${pid}`, { method: on ? "DELETE" : "PUT" });
  on ? state.wishlist.delete(pid) : state.wishlist.add(pid);
  if (btn) btn.setAttribute("aria-pressed", String(!on));
}
const rememberViewed = (id) => { const v = [id, ...store.get("viewed", []).filter((x) => x !== id)].slice(0, 12); store.set("viewed", v); };

const app = () => $("#app");
function parseHash() {
  const raw = location.hash.replace(/^#\/?/, ""); const [path, qs] = raw.split("?");
  return { parts: path.split("/").filter(Boolean).map(decodeURIComponent), q: new URLSearchParams(qs || "") };
}
async function route() {
  const { parts, q } = parseHash(); const id = ++state.loadId;
  closeDialog(); bar(true); window.scrollTo({ top: 0 });
  document.querySelectorAll(".bottomnav a").forEach((a) => a.removeAttribute("aria-current"));
  const nav = { "": "home", shop: "shop", cart: "cart", account: "account" }[parts[0] || ""]; if (nav) $(`.bottomnav [data-nav="${nav}"]`)?.setAttribute("aria-current", "page");
  const root = clear(app()); root.append(skeletons(4));
  try {
    const view = !parts.length ? await homeView() : parts[0] === "shop" ? await shopView(q) : parts[0] === "p" ? await productView(parts[1]) : parts[0] === "cart" ? await cartView() :
      parts[0] === "account" ? await accountView(parts[1] || "overview") : parts[0] === "orders" ? await orderView(parts[1]) : parts[0] === "deliveries" ? await deliveriesView() : parts[0] === "support" ? await supportView() : empty("Page not found.", h("a", { class: "btn", href: "#/" }, "Go home"));
    if (id !== state.loadId) return; clear(root).append(view);
  } catch (e) { if (id === state.loadId) clear(root).append(errorBox(e, route)); }
  bar(false); root.focus({ preventScroll: true });
}

async function homeView() {
  const cfg = state.cfg; const wrap = h("div");
  wrap.append(h("section", { class: "hero" }, h("h1", null, cfg.hero_title || "Instant digital delivery."), h("p", null, cfg.hero_subtitle || ""),
    h("div", { class: "row wrap" }, h("a", { class: "btn primary", href: "#/shop" }, "Browse digital products"), state.user ? h("a", { class: "btn", href: "#/account" }, "My account") : h("button", { class: "btn", type: "button", onclick: () => authDialog("register") }, "Join free"))));
  const top = state.categories.filter((c) => !c.parent_id);
  if (top.length) wrap.append(h("section", { class: "section" }, h("div", { class: "section-head" }, h("h2", null, "Browse categories")),
    h("div", { class: "grid cols-2 cols-4" }, top.slice(0, 8).map((c) => h("a", { class: "cat-tile", href: `#/shop?category=${c.id}` }, h("strong", null, c.name), h("span", { class: "small muted" }, c.description || "Explore"))))));
  const data = await api("/home" + (state.country ? `?country=${state.country}` : ""));
  const sec = (title, items, link) => items && items.length ? h("section", { class: "section" }, h("div", { class: "section-head" }, h("h2", null, title), link ? h("a", { href: link }, "See all") : null), h("div", { class: "grid products" }, items.map(productCard))) : null;
  wrap.append(sec("New arrivals", data.new_arrivals, "#/shop?sort=newest"), sec("Best sellers", data.best_sellers, "#/shop?sort=popularity"), sec("Recommended", data.recommended), sec("Special offers", data.offers));
  const viewed = store.get("viewed", []);
  if (viewed.length) { try { const r = await api("/products/by-ids?ids=" + viewed.join(",")); wrap.append(sec("Recently viewed", r.items)); } catch {} }
  const nl = newsletterForm();
  wrap.append(h("section", { class: "section panel" }, h("div", { class: "row between wrap" }, h("div", null, h("h2", null, "Need help?"), h("p", { class: "muted" }, "Our support team answers order, payment and account questions.")), h("a", { class: "btn", href: "#/support" }, "Contact support"))), h("section", { class: "section panel" }, h("h2", null, "Newsletter"), nl));
  return wrap;
}
function newsletterForm() {
  const email = h("input", { type: "email", id: "nl_email", required: true, maxlength: 254, placeholder: "you@example.com", "aria-label": "Email address" });
  const consent = h("input", { type: "checkbox", id: "nl_c" }); const msg = h("div", { class: "error-text", role: "status" });
  const f = h("form", null, h("div", { class: "row wrap" }, h("div", { class: "grow" }, email), h("button", { class: "btn primary", type: "submit" }, "Subscribe")), h("label", { class: "check", style: "margin-top:8px" }, consent, "I agree to receive marketing emails and understand I can unsubscribe at any time."), msg);
  f.addEventListener("submit", async (e) => { e.preventDefault(); msg.textContent = ""; if (!consent.checked) { msg.textContent = "Please tick the consent box."; return; } try { await api("/newsletter", { method: "POST", body: { email: email.value, consent: true } }); f.reset(); toast("Subscribed. Thank you!", "ok"); } catch (x) { msg.textContent = x.message; } });
  return f;
}

async function shopView(q) {
  const f = { q: q.get("q") || "", country: q.get("country") || state.country || "", category: q.get("category") || "", min_price: q.get("min_price") || "", max_price: q.get("max_price") || "", sort: q.get("sort") || (q.get("q") ? "relevance" : "newest") };
  const dyn = {}; for (const [k, v] of q.entries()) if (k.startsWith("f_")) dyn[k] = v;
  const root = h("div"); const grid = h("div"); const more = h("div", { class: "row", style: "justify-content:center;margin:16px 0" }); let page = 1; let total = 0;
  const push = (patch) => { const n = new URLSearchParams(); const merged = { ...f, ...dyn, ...patch }; for (const [k, v] of Object.entries(merged)) if (v) n.set(k, v); location.hash = "#/shop?" + n.toString(); };
  const sel = (id, opts, val, onchange) => { const s = h("select", { id, onchange: (e) => onchange(e.target.value) }, opts.map(([v, l]) => h("option", { value: v, selected: v === val }, l))); return s; };
  const catSel = sel("f_cat", [["", "All categories"], ...state.categories.map((c) => [c.id, (c.parent_id ? "— " : "") + c.name])], f.category, (v) => push({ category: v }));
  const cSel = sel("f_country", [["", "All countries"], ...state.countries.map((c) => [c.code, c.name])], f.country, (v) => push({ country: v }));
  const min = h("input", { id: "f_min", type: "number", min: "0", step: "0.01", placeholder: "Min", value: f.min_price }); const max = h("input", { id: "f_max", type: "number", min: "0", step: "0.01", placeholder: "Max", value: f.max_price });
  const filters = h("aside", { class: "panel filters stack", "aria-label": "Filters" }, h("h3", null, "Filters"),
    h("div", { class: "field" }, h("label", { for: "f_country" }, "Country"), cSel), h("div", { class: "field" }, h("label", { for: "f_cat" }, "Category"), catSel),
    h("div", { class: "field" }, h("label", null, "Price range"), h("div", { class: "row" }, min, max)),
    h("div", { class: "row" }, h("button", { class: "btn primary sm grow", type: "button", onclick: () => push({ min_price: min.value, max_price: max.value }) }, "Apply"), h("button", { class: "btn sm grow", type: "button", onclick: () => { location.hash = "#/shop"; } }, "Clear all")));
  try { const sc = await api("/schemas/public" + (f.category ? `?category_id=${f.category}` : "")); for (const fl of sc.filters) { const cur = dyn["f_" + fl.key] || ""; const inp = fl.options.length ? sel("fd_" + fl.key, [["", "Any"], ...fl.options.map((o) => [o, o])], cur, (v) => push({ ["f_" + fl.key]: v })) : h("input", { id: "fd_" + fl.key, type: "text", maxlength: 100, value: cur, onchange: (e) => push({ ["f_" + fl.key]: e.target.value }) }); filters.insertBefore(h("div", { class: "field" }, h("label", { for: "fd_" + fl.key }, fl.label), inp), filters.lastChild); } } catch {}
  const head = h("div", { class: "row between wrap" }, h("h1", null, f.q ? `Results for “${f.q}”` : "Shop Digital Products"), h("div", { class: "row" },
    h("label", { class: "sr-only", for: "f_sort" }, "Sort"), sel("f_sort", [["relevance", "Relevance"], ["newest", "Newest"], ["popularity", "Popularity"], ["price_asc", "Price: low to high"], ["price_desc", "Price: high to low"]], f.sort, (v) => push({ sort: v }))));
  async function load() {
    const p = new URLSearchParams({ page: String(page), limit: "12", sort: f.sort }); for (const k of ["q", "country", "category", "min_price", "max_price"]) if (f[k]) p.set(k, f[k]); for (const [k, v] of Object.entries(dyn)) p.set(k, v);
    const r = await api("/products?" + p.toString()); total = r.total;
    if (page === 1) { clear(grid); grid.className = "grid products"; if (!r.items.length) { grid.className = ""; grid.append(empty("No products match these filters.", h("a", { class: "btn", href: "#/shop" }, "Clear all filters"))); } }
    r.items.forEach((i) => grid.append(productCard(i))); clear(more);
    if (r.has_more) more.append(h("button", { class: "btn", type: "button", onclick: guard(async () => { page++; await load(); }) }, `Load more (${grid.children.length} of ${total})`));
  }
  root.append(h("div", { class: "section" }, head), h("div", { class: "shop" }, filters, h("div", null, grid, more))); grid.append(skeletons(6)); await load();
  return root;
}

async function productView(ref) {
  const { product: p, related } = await api("/products/" + encodeURIComponent(ref)); rememberViewed(p.id); document.title = p.name + " - " + (state.cfg.brand_name || "Store");
  let idx = 0; const main = h("div", { class: "main" }); const thumbs = h("div", { class: "thumbs" });
  const show = (i) => { idx = i; clear(main).append(safeImg(p.images[i] && p.images[i].url, p.images[i] && p.images[i].alt || p.name)); [...thumbs.children].forEach((b, j) => b.setAttribute("aria-current", String(j === i))); };
  p.images.forEach((im, i) => thumbs.append(h("button", { type: "button", "aria-label": `Image ${i + 1}`, onclick: () => show(i) }, safeImg(im.url, im.alt)))); show(0);
  let qty = 1;
  const priceBox = h("div"); const out = h("output", null, "1");
  const drawPrice = () => { clear(priceBox).append(h("div", { class: "price", style: "font-size:1.6rem" }, money(p.final_price_minor), p.discount_percent ? h("s", null, money(p.price_minor)) : null), p.tax_rate_bps ? h("div", { class: "small muted" }, `Plus ${(p.tax_rate_bps / 100).toFixed(2)}% tax at checkout`) : h("div", { class: "small muted" }, "Taxes calculated at checkout")); };
  const addBtn = h("button", { class: "btn primary grow", type: "button", onclick: guard(() => addToCart(p.id, qty)) }, "Add to cart");
  const buyBtn = h("button", { class: "btn grow", type: "button", onclick: guard(() => buyNow(p.id, qty)) }, "Buy now");
  const wish = h("button", { class: "btn", type: "button", "aria-pressed": String(state.wishlist.has(p.id)), onclick: guard(() => toggleWish(p.id, wish)) }, "♥ Wishlist");
  const specs = h("dl", { class: "specs" }, [["Country", p.country.name], ["Category", p.category.name + (p.subcategory ? " › " + p.subcategory : "")], ...p.fields.map((f) => [f.label, Array.isArray(f.value) ? f.value.join(", ") : typeof f.value === "object" ? Object.entries(f.value).map(([k, v]) => `${k}: ${v}`).join("; ") : typeof f.value === "boolean" ? (f.value ? "Yes" : "No") : String(f.value)])].flatMap(([k, v]) => [h("dt", null, k), h("dd", null, v)]));
  const root = h("div", { class: "stack" }, h("p", { class: "small" }, h("a", { href: "#/shop" }, "Shop"), " › ", h("a", { href: `#/shop?category=${p.category.id}` }, p.category.name)),
    h("div", { class: "pd" }, h("div", { class: "gallery" }, main, thumbs), h("div", { class: "stack" }, h("h1", null, p.name), h("div", { class: "row wrap" }, availChip(), stars(p.rating_avg, p.rating_count)), h("p", { class: "muted" }, p.short_description), priceBox,
      h("div", { class: "row wrap" }, h("div", { class: "qty" }, h("button", { type: "button", "aria-label": "Decrease quantity", onclick: () => { qty = Math.max(1, qty - 1); out.textContent = qty; } }, "−"), out, h("button", { type: "button", "aria-label": "Increase quantity", onclick: () => { qty = Math.min(10, qty + 1); out.textContent = qty; } }, "+")), addBtn, buyBtn, wish),
      h("div", { class: "panel" }, h("h3", null, "Instant Delivery"), h("p", { class: "small muted" }, p.delivery_info || "You'll receive access details immediately after payment verification.")),
      h("div", { class: "panel" }, h("h3", null, "Refunds"), h("p", { class: "small muted" }, p.refund_policy || "Refunds are issued as store credit to your wallet.")))),
    h("section", { class: "panel" }, h("h2", null, "Description"), h("p", { style: "white-space:pre-wrap" }, p.description || p.short_description)), h("section", { class: "panel" }, h("h2", null, "Details"), specs));
  drawPrice();
  const reviews = h("section", { class: "panel" }, h("h2", null, "Customer reviews")); root.append(reviews);
  try { const r = await api(`/products/${p.id}/reviews`); reviews.append(r.reviews.length ? h("div", { class: "stack" }, r.reviews.map((v) => h("div", null, h("div", { class: "stars" }, "★".repeat(v.rating)), h("strong", null, " " + v.title), h("p", { class: "small muted" }, `${v.author} · ${fmtDate(v.created_at)} · verified purchase`), h("p", null, v.body)))) : h("p", { class: "muted" }, "No reviews yet. Verified buyers can leave the first one.")); } catch { reviews.append(h("p", { class: "muted" }, "Reviews are unavailable right now.")); }
  if (state.user) reviews.append(reviewForm(p.id));
  if (related.length) root.append(h("section", { class: "section" }, h("h2", null, "Related products"), h("div", { class: "grid products" }, related.map(productCard))));
  return root;
}
function reviewForm(pid) {
  const rating = h("select", { id: "rv_r", "aria-label": "Rating" }, [5, 4, 3, 2, 1].map((n) => h("option", { value: n }, `${n} star${n > 1 ? "s" : ""}`))); const title = h("input", { id: "rv_t", maxlength: 100, placeholder: "Title", required: true }); const body = h("textarea", { id: "rv_b", maxlength: 2000, placeholder: "Share your experience (verified purchasers only)", required: true });
  const f = h("form", { class: "stack" }, h("h3", null, "Write a review"), rating, title, body, h("button", { class: "btn", type: "submit" }, "Submit review"));
  f.addEventListener("submit", guard(async (e) => { e.preventDefault(); await api(`/products/${pid}/reviews`, { method: "POST", body: { rating: Number(rating.value), title: title.value, body: body.value } }); toast("Thanks for your review!", "ok"); route(); })); return f;
}

async function cartView() {
  if (!state.user) {
    const g = guestCart(); if (!g.length) return empty("Your cart is empty.", h("a", { class: "btn primary", href: "#/shop" }, "Start shopping"));
    return h("div", { class: "stack" }, h("h1", null, "Your cart"), h("div", { class: "panel" }, h("p", null, `You have ${g.reduce((s, i) => s + i.qty, 0)} item(s) saved on this device.`), h("p", { class: "muted small" }, "Sign in to see live prices, apply membership discounts and check out."), h("button", { class: "btn primary", type: "button", onclick: () => authDialog("login") }, "Sign in to continue")));
  }
  await refreshCart(); const c = state.cart; if (!c || !c.items.length) return empty("Your cart is empty.", h("a", { class: "btn primary", href: "#/shop" }, "Start shopping"));
  const method = { v: "plisio" }; const err = h("div", { class: "error-text", role: "alert" });
  const lines = c.items.map((i) => h("div", { class: "panel row" }, h("div", { style: "width:72px;height:72px;flex:none;border-radius:10px;overflow:hidden" }, safeImg(i.image, i.name)),
    h("div", { class: "grow" }, h("a", { href: "#/p/" + i.slug }, h("strong", null, i.name)), h("div", null, money(i.unit_price_minor), " × ", i.qty)),
    h("div", { class: "stack" }, h("div", { class: "qty" }, h("button", { type: "button", "aria-label": "Decrease", onclick: guard(async () => { await setQty(i.product_id, i.qty - 1); route(); }) }, "−"), h("output", null, String(i.qty)), h("button", { type: "button", "aria-label": "Increase", onclick: guard(async () => { await setQty(i.product_id, Math.min(10, i.qty + 1)); route(); }) }, "+")),
      h("button", { class: "btn sm danger", type: "button", onclick: guard(async () => { await setQty(i.product_id, 0); route(); }) }, "Remove"))));
  const totals = h("div", { class: "panel stack" }, h("h3", null, "Order summary"), ...[["Subtotal", c.subtotal_minor], c.member_discount_minor ? ["Member discount", -c.member_discount_minor] : null, ["Tax", c.tax_minor]].filter(Boolean).map(([l, v]) => h("div", { class: "row between" }, h("span", { class: "muted" }, l), h("span", null, money(v)))), h("div", { class: "row between price" }, h("span", null, "Total"), h("span", null, money(c.total_minor))),
    h("fieldset", { style: "border:0;padding:0;margin:0" }, h("legend", { class: "small muted" }, "Payment method"), h("label", { class: "check" }, h("input", { type: "radio", name: "pm", checked: true, onchange: () => (method.v = "plisio") }), "Cryptocurrency (secure hosted checkout)"), h("label", { class: "check" }, h("input", { type: "radio", name: "pm", onchange: () => (method.v = "wallet") }), "Wallet balance")), err,
    h("button", { class: "btn primary", type: "button", id: "payBtn", onclick: async (e) => { e.currentTarget.disabled = true; err.textContent = ""; try { const r = await api("/checkout", { method: "POST", body: { payment_method: method.v }, headers: { "Idempotency-Key": (crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random()) } }); state.cart = null; updateCartBadge(); const o = r.order; if (o.payment_url && /^https:\/\/([a-z0-9-]+\.)*plisio\.(net|com)\//i.test(o.payment_url)) { toast("Redirecting to the secure payment page…"); location.assign(o.payment_url); } else { location.hash = "#/orders/" + o.number; } } catch (x) { err.textContent = x.message; e.currentTarget.disabled = false; } } }, "Place order & pay"),
    h("p", { class: "small muted" }, "Your order is confirmed only after the payment provider verifies your payment with our server."));
  return h("div", { class: "stack" }, h("h1", null, "Your cart"), ...(c.problems && c.problems.length ? [h("div", { class: "panel error-text", role: "alert" }, c.problems.join(" · "))] : []), h("div", { class: "shop", style: "grid-template-columns:1fr" }, h("div", { class: "stack" }, lines), totals));
}

const statusChip = (s) => h("span", { class: "chip " + (s === "paid" ? "ok" : ["pending", "created"].includes(s) ? "warn" : "bad") }, s);
async function orderView(number) {
  if (!requireLogin()) return empty("Sign in to view your orders.");
  const data = await api("/orders/" + encodeURIComponent(number));
  const o = data.order; const deliveries = data.deliveries || [];
  const box = h("div", { class: "stack" }, h("p", { class: "small" }, h("a", { href: "#/account/orders" }, "‹ Orders")), h("h1", null, o.number), h("div", { class: "row wrap" }, statusChip(o.status), h("span", { class: "muted small" }, fmtDate(o.created_at))),
    h("div", { class: "panel" }, o.items.map((i) => h("div", { class: "row between", style: "padding:6px 0" }, h("span", null, `${i.name} × ${i.qty}`), h("span", null, money(i.line_total_minor, o.currency)))), h("hr"), h("div", { class: "row between" }, h("span", { class: "muted" }, "Subtotal"), money(o.subtotal_minor, o.currency)), o.member_discount_minor ? h("div", { class: "row between" }, h("span", { class: "muted" }, "Member discount"), "−" + money(o.member_discount_minor, o.currency)) : null, h("div", { class: "row between" }, h("span", { class: "muted" }, "Tax"), money(o.tax_minor, o.currency)), h("div", { class: "row between price" }, h("span", null, "Total"), money(o.total_minor, o.currency)),
      o.points_earned ? h("p", { class: "small muted" }, `Reward points earned: ${o.points_earned}`) : null));
  if (deliveries.length) box.append(h("div", { class: "panel stack" }, h("h2", null, "Digital Deliveries"), deliveries.map((d) => h("div", { class: "panel-raised" }, h("div", { class: "row between" }, h("strong", null, d.product_name), h("span", { class: "chip " + (d.status === "delivered" ? "ok" : "warn") }, d.status)), d.status === "delivered" && d.delivery_data && Object.keys(d.delivery_data).length ? h("div", { style: "margin-top:8px" }, Object.entries(d.delivery_data).map(([k, v]) => h("div", { class: "small" }, h("strong", null, k + ": "), h("span", { style: "word-break:break-all;white-space:pre-wrap" }, v)))) : h("p", { class: "small muted" }, "Delivery pending - our team will provide your access details soon."))))));
  if (["created", "pending"].includes(o.status)) {
    box.append(h("div", { class: "panel stack" }, h("p", null, "Waiting for payment confirmation from the provider. This page updates automatically."),
      o.payment_url ? h("a", { class: "btn primary", href: o.payment_url, rel: "noopener" }, "Open payment page") : null, h("button", { class: "btn danger", type: "button", onclick: guard(async () => { await api(`/orders/${o.number}/cancel`, { method: "POST" }); toast("Order cancelled.", "ok"); route(); }) }, "Cancel order")));
    const t = setTimeout(() => { if (parseHash().parts[1] === number) route(); }, 8000); window.addEventListener("hashchange", () => clearTimeout(t), { once: true });
  }
  return box;
}

async function deliveriesView() {
  if (!requireLogin()) return empty("Sign in to view your deliveries.");
  const r = await api("/deliveries");
  if (!r.deliveries.length) return empty("No digital deliveries yet.", h("a", { class: "btn primary", href: "#/shop" }, "Browse products"));
  return h("div", { class: "stack" }, h("h1", null, "My Deliveries"), r.deliveries.map((d) => h("div", { class: "panel" }, h("div", { class: "row between" }, h("strong", null, d.product_name), h("span", { class: "chip " + (d.status === "delivered" ? "ok" : "warn") }, d.status)), h("p", { class: "small muted" }, `Order ${d.order_number} · ${fmtDate(d.created_at)}`),
    d.status === "delivered" && d.delivery_data && Object.keys(d.delivery_data).length ? h("div", { style: "margin-top:8px" }, Object.entries(d.delivery_data).map(([k, v]) => h("div", { class: "small" }, h("strong", null, k + ": "), h("span", { style: "word-break:break-all;white-space:pre-wrap" }, v)))) : h("p", { class: "small muted" }, "Delivery pending - our team will provide your access details soon."))));
}

const TABS = [["overview", "Overview"], ["orders", "Orders"], ["deliveries", "Deliveries"], ["wallet", "Wallet"], ["wishlist", "Wishlist"], ["rewards", "Rewards"], ["referrals", "Referrals"], ["support", "Support"], ["settings", "Settings"]];
async function accountView(tab) {
  if (!state.user) { setTimeout(() => authDialog("login"), 0); return empty("Sign in to see your account.", h("button", { class: "btn primary", type: "button", onclick: () => authDialog("login") }, "Sign in")); }
  const body = h("div", { class: "stack" });
  const tabs = h("nav", { class: "tabs", "aria-label": "Account sections" }, TABS.map(([k, l]) => h("a", { class: "pill", href: "#/account/" + k, "aria-current": String(k === tab) }, l)));
  const root = h("div", { class: "stack" }, h("div", { class: "row between" }, h("h1", null, "Hi, " + (state.user.name || "there").split(" ")[0]), h("button", { class: "btn sm", type: "button", onclick: logout }, "Sign out")), tabs, body);
  const views = { overview: accOverview, orders: accOrders, deliveries: accDeliveries, wallet: accWallet, wishlist: accWishlist, rewards: accRewards, referrals: accReferrals, support: supportView, settings: accSettings };
  body.append(await (views[tab] || accOverview)()); return root;
}
async function accOverview() {
  const [w, m] = await Promise.all([api("/wallet"), api("/membership")]);
  return h("div", { class: "stack" },
    h("div", { class: "balance stack" }, h("div", { class: "small muted" }, "Wallet balance"), h("div", { class: "amount" }, money(w.balance_minor, w.currency)), h("div", { class: "row wrap" }, h("button", { class: "btn primary", type: "button", disabled: !state.cfg.payments_enabled, onclick: () => topUpDialog() }, "+ Top up"), h("a", { class: "btn", href: "#/account/wallet" }, "History"))),
    h("div", { class: "panel stack" }, h("div", { class: "row between" }, h("strong", null, `${m.tier.name} member`), h("span", { class: "chip" }, `${m.points_balance} pts`)), h("div", { class: "progress", role: "progressbar", "aria-valuenow": String(m.progress_percent), "aria-valuemin": "0", "aria-valuemax": "100" }, h("span", { style: `width:${m.progress_percent}%` })),
      h("p", { class: "small muted" }, m.next_tier ? `${money(Math.max(0, m.next_tier.threshold_minor - m.lifetime_spend_minor), m.currency)} more to reach ${m.next_tier.name}` : "You have reached the highest tier."), h("ul", { class: "small" }, m.tier.benefits.map((b) => h("li", null, b)))),
    h("div", { class: "quick" }, [["orders", "Orders", "📦"], ["deliveries", "Deliveries", "📬"], ["wishlist", "Wishlist", "♥"], ["support", "Help", "?"]].map(([k, l, i]) => h("a", { href: "#/account/" + k }, h("span", { "aria-hidden": "true", style: "font-size:1.3rem" }, i), l))));
}
function topUpDialog() {
  const amt = h("input", { id: "tu", type: "number", min: "5", max: "5000", step: "0.01", value: "25", required: true }); const err = h("div", { class: "error-text", role: "alert" });
  const f = h("form", { class: "stack" }, h("div", { class: "field" }, h("label", { for: "tu" }, `Amount (${state.cfg.currency}, 5 - 5000)`), amt), err, h("button", { class: "btn primary", type: "submit" }, "Continue to secure payment"), h("p", { class: "small muted" }, "Your wallet is credited only after the payment provider confirms the payment to our server."));
  f.addEventListener("submit", async (e) => { e.preventDefault(); try { const r = await api("/wallet/deposit", { method: "POST", body: { amount: String(amt.value) } }); location.assign(r.payment_url); } catch (x) { err.textContent = x.message; } }); openDialog("Top up wallet", f);
}
async function accOrders() {
  const r = await api("/orders"); if (!r.orders.length) return empty("No orders yet.", h("a", { class: "btn primary", href: "#/shop" }, "Start shopping"));
  return h("div", { class: "stack" }, r.orders.map((o) => h("a", { class: "panel row between", href: "#/orders/" + o.number, style: "color:inherit" }, h("div", null, h("strong", null, o.number), h("div", { class: "small muted" }, fmtDate(o.created_at) + " · " + o.items.length + " item(s)")), h("div", { class: "stack", style: "text-align:right" }, money(o.total_minor, o.currency), statusChip(o.status)))));
}
async function accDeliveries() {
  const r = await api("/deliveries"); if (!r.deliveries.length) return empty("No digital deliveries yet.", h("a", { class: "btn primary", href: "#/shop" }, "Browse products"));
  return h("div", { class: "stack" }, r.deliveries.map((d) => h("div", { class: "panel" }, h("div", { class: "row between" }, h("strong", null, d.product_name), h("span", { class: "chip " + (d.status === "delivered" ? "ok" : "warn") }, d.status)), h("p", { class: "small muted" }, `Order ${d.order_number} · ${fmtDate(d.created_at)}`),
    d.status === "delivered" && d.delivery_data && Object.keys(d.delivery_data).length ? h("div", { style: "margin-top:8px" }, Object.entries(d.delivery_data).map(([k, v]) => h("div", { class: "small" }, h("strong", null, k + ": "), h("span", { style: "word-break:break-all;white-space:pre-wrap" }, v)))) : h("p", { class: "small muted" }, "Delivery pending - our team will provide your access details soon."))));
}
async function accWallet() {
  const w = await api("/wallet");
  return h("div", { class: "stack" }, h("div", { class: "balance" }, h("div", { class: "small muted" }, "Balance"), h("div", { class: "amount" }, money(w.balance_minor, w.currency))), h("h2", null, "Transactions"),
    w.ledger.length ? h("div", { class: "panel table-wrap" }, h("table", null, h("thead", null, h("tr", null, ["Date", "Type", "Amount", "Status"].map((x) => h("th", null, x)))), h("tbody", null, w.ledger.map((e) => h("tr", null, h("td", null, fmtDate(e.created_at)), h("td", null, e.type), h("td", null, money(e.amount_minor, w.currency)), h("td", null, e.status)))))) : empty("No wallet activity yet."));
}
async function accWishlist() {
  const ids = [...state.wishlist]; if (!ids.length) return empty("Your wishlist is empty.", h("a", { class: "btn", href: "#/shop" }, "Browse products"));
  const r = await api("/products/by-ids?ids=" + ids.slice(0, 24).join(",")); return h("div", { class: "grid products" }, r.items.map(productCard));
}
async function accRewards() {
  const r = await api("/rewards"); return h("div", { class: "stack" }, h("div", { class: "balance" }, h("div", { class: "small muted" }, "Reward points"), h("div", { class: "amount" }, String(r.points_balance))),
    r.ledger.length ? h("div", { class: "panel table-wrap" }, h("table", null, h("thead", null, h("tr", null, ["Date", "Type", "Points", "Order"].map((x) => h("th", null, x)))), h("tbody", null, r.ledger.map((e) => h("tr", null, h("td", null, fmtDate(e.created_at)), h("td", null, e.type), h("td", null, String(e.points)), h("td", null, e.order_number || "—")))))) : empty("Points you earn on paid orders appear here."));
}
async function accReferrals() {
  const r = await api("/referrals"); const link = h("input", { readonly: true, value: r.link, "aria-label": "Referral link" });
  return h("div", { class: "stack" }, h("div", { class: "panel stack" }, h("h2", null, "Invite friends"), h("p", { class: "muted" }, `Earn ${r.points_per_reward} points when a friend you refer completes their first paid order.`), link, h("button", { class: "btn", type: "button", onclick: async () => { try { await navigator.clipboard.writeText(r.link); toast("Link copied.", "ok"); } catch { link.select(); } } }, "Copy link")), h("div", { class: "grid cols-2" }, h("div", { class: "panel" }, h("div", { class: "small muted" }, "Referred"), h("div", { class: "price" }, String(r.referred))), h("div", { class: "panel" }, h("div", { class: "small muted" }, "Rewarded"), h("div", { class: "price" }, String(r.rewarded)))));
}
async function accSettings() {
  const s = await api("/account/sessions"); const nt = state.user.notifications || {};
  const name = h("input", { id: "st_name", value: state.user.name || "", maxlength: 80 }); const orders = h("input", { type: "checkbox", id: "st_o", checked: nt.orders !== false }); const promo = h("input", { type: "checkbox", id: "st_p", checked: !!nt.promotions });
  const profile = h("form", { class: "panel stack" }, h("h2", null, "Profile & notifications"), h("div", { class: "field" }, h("label", { for: "st_name" }, "Name"), name), h("label", { class: "check" }, orders, "Order & payment updates"), h("label", { class: "check" }, promo, "Promotions"), h("button", { class: "btn primary", type: "submit" }, "Save"));
  profile.addEventListener("submit", guard(async (e) => { e.preventDefault(); const u = await api("/account/profile", { method: "PATCH", body: { name: name.value, notifications: { orders: orders.checked, promotions: promo.checked } } }); state.user = { ...state.user, ...u }; toast("Saved.", "ok"); }));
  const cur = h("input", { type: "password", id: "pw_c", autocomplete: "current-password" }); const nw = h("input", { type: "password", id: "pw_n", autocomplete: "new-password" });
  const pwf = h("form", { class: "panel stack" }, h("h2", null, "Change password"), h("div", { class: "field" }, h("label", { for: "pw_c" }, "Current password"), cur), h("div", { class: "field" }, h("label", { for: "pw_n" }, "New password"), nw), h("button", { class: "btn", type: "submit" }, "Update password"));
  pwf.addEventListener("submit", guard(async (e) => { e.preventDefault(); await api("/account/password", { method: "POST", body: { current_password: cur.value, new_password: nw.value } }); pwf.reset(); toast("Password updated. Other sessions were signed out.", "ok"); }));
  const sess = h("div", { class: "panel stack" }, h("div", { class: "row between" }, h("h2", null, "Active sessions"), h("button", { class: "btn sm", type: "button", onclick: guard(async () => { const r = await api("/account/sessions/revoke-others", { method: "POST" }); toast(`Signed out ${r.revoked} other session(s).`, "ok"); route(); }) }, "Sign out others")),
    s.sessions.map((x) => h("div", { class: "row between small" }, h("div", null, h("div", null, (x.ua || "Unknown device").slice(0, 60), x.current ? h("span", { class: "chip ok" }, "this device") : null), h("div", { class: "muted" }, `${x.ip} · last active ${fmtDate(x.last_seen)}`)), x.current ? null : h("button", { class: "btn sm danger", type: "button", onclick: guard(async () => { await api("/account/sessions/" + x.id, { method: "DELETE" }); route(); }) }, "Revoke"))));
  return h("div", { class: "stack" }, profile, pwf, sess);
}

async function supportView() {
  const root = h("div", { class: "stack" }, h("h1", null, "Support"));
  if (state.cfg.support_url) root.append(h("p", null, h("a", { href: state.cfg.support_url, rel: "noopener noreferrer", target: "_blank" }, "Visit our help centre")));
  if (!state.user) {
    root.append(h("div", { class: "panel stack" }, h("p", null, "Sign in to open a support ticket about an order, payment or your account."), h("button", { class: "btn primary", type: "button", onclick: () => authDialog("login") }, "Sign in")));
    if (state.cfg.invite_only) root.append(applyForm()); return root;
  }
  const subj = h("input", { id: "tk_s", maxlength: 120, required: true }); const ord = h("input", { id: "tk_o", maxlength: 20, placeholder: "ORD-…" }); const msg = h("textarea", { id: "tk_m", maxlength: 3000, required: true });
  const f = h("form", { class: "panel stack" }, h("h2", null, "New ticket"), h("div", { class: "field" }, h("label", { for: "tk_s" }, "Subject"), subj), h("div", { class: "field" }, h("label", { for: "tk_o" }, "Order number (optional)"), ord), h("div", { class: "field" }, h("label", { for: "tk_m" }, "How can we help?"), msg), h("button", { class: "btn primary", type: "submit" }, "Send"));
  f.addEventListener("submit", guard(async (e) => { e.preventDefault(); await api("/support/tickets", { method: "POST", body: { subject: subj.value, message: msg.value, order_number: ord.value || null } }); toast("Ticket created.", "ok"); route(); }));
  const t = await api("/support/tickets");
  root.append(f, h("div", { class: "stack" }, t.tickets.map((k) => { const reply = h("input", { maxlength: 3000, placeholder: "Reply…", "aria-label": "Reply" }); const rf = h("form", { class: "row" }, h("div", { class: "grow" }, reply), h("button", { class: "btn sm", type: "submit" }, "Send")); rf.addEventListener("submit", guard(async (e) => { e.preventDefault(); if (!reply.value) return; await api(`/support/tickets/${k.id}/messages`, { method: "POST", body: { message: reply.value } }); route(); }));
    return h("div", { class: "panel stack" }, h("div", { class: "row between" }, h("strong", null, k.subject), h("span", { class: "chip" }, k.status)), k.messages.map((m) => h("div", { class: "small" }, h("strong", null, m.from === "support" ? "Support" : "You"), h("span", { class: "muted" }, " · " + fmtDate(m.at)), h("div", { style: "white-space:pre-wrap" }, m.body))), k.status !== "closed" ? rf : null); })));
  return root;
}
function applyForm() {
  const email = h("input", { type: "email", id: "ap_e", required: true, maxlength: 254 }); const name = h("input", { id: "ap_n", maxlength: 80, required: true }); const msg = h("textarea", { id: "ap_m", maxlength: 500 });
  const f = h("form", { class: "panel stack" }, h("h2", null, "Apply for an invitation"), h("div", { class: "field" }, h("label", { for: "ap_e" }, "Email"), email), h("div", { class: "field" }, h("label", { for: "ap_n" }, "Name"), name), h("div", { class: "field" }, h("label", { for: "ap_m" }, "Why would you like to join?"), msg), h("button", { class: "btn primary", type: "submit" }, "Submit application"));
  f.addEventListener("submit", guard(async (e) => { e.preventDefault(); await api("/invitations/apply", { method: "POST", body: { email: email.value, name: name.value, message: msg.value } }); f.reset(); toast("Application received.", "ok"); })); return f;
}

async function openNotifications() {
  if (!requireLogin()) return;
  try { const r = await api("/notifications"); const list = h("div", { class: "stack" }, r.items.length ? r.items.map((n) => h("div", { class: "panel" }, h("strong", null, n.title), h("div", { class: "small muted" }, n.body), h("div", { class: "small muted" }, fmtDate(n.created_at)))) : h("p", { class: "muted" }, "You're all caught up."));
    openDialog("Notifications", list, () => { if (r.unread) api("/notifications/read", { method: "POST" }).then(() => $("#notifCount").classList.add("hide")).catch(() => {}); }); } catch (e) { toast(e.message, "bad"); }
}
async function pollNotifs() { if (!state.user) return; try { const r = await api("/notifications"); const b = $("#notifCount"); b.textContent = String(r.unread); b.classList.toggle("hide", !r.unread); } catch {} }

function setupSearch() {
  const q = $("#q"), box = $("#suggest"); let t = null, items = [], active = -1;
  const hideBox = () => { box.classList.add("hide"); q.setAttribute("aria-expanded", "false"); active = -1; };
  const go = (text) => { hideBox(); location.hash = "#/shop?q=" + encodeURIComponent(text.trim().slice(0, 80)); };
  q.addEventListener("input", () => { clearTimeout(t); const v = q.value.trim(); if (v.length < 2) return hideBox(); t = setTimeout(async () => { try { const r = await api("/products/suggest?q=" + encodeURIComponent(v)); items = r.suggestions; clear(box); active = -1; if (!items.length) return hideBox(); items.forEach((s, i) => box.append(h("button", { type: "button", role: "option", id: "sg" + i, onclick: () => { hideBox(); location.hash = "#/p/" + encodeURIComponent(s.slug); } }, s.name))); box.classList.remove("hide"); q.setAttribute("aria-expanded", "true"); } catch { hideBox(); } }, 250); });
  q.addEventListener("keydown", (e) => { const b = [...box.children]; if (e.key === "ArrowDown" || e.key === "ArrowUp") { e.preventDefault(); if (!b.length) return; active = (active + (e.key === "ArrowDown" ? 1 : -1) + b.length) % b.length; b.forEach((x, i) => x.setAttribute("aria-selected", String(i === active))); } else if (e.key === "Enter" && active >= 0) { e.preventDefault(); b[active].click(); } else if (e.key === "Escape") hideBox(); });
  $("#searchForm").addEventListener("submit", (e) => { e.preventDefault(); if (q.value.trim()) go(q.value); }); document.addEventListener("click", (e) => { if (!e.target.closest(".search")) hideBox(); });
}

function renderCatNav() {
  const row = clear($("#catRow")); row.append(h("a", { class: "pill", href: "#/shop" }, "All"));
  state.categories.filter((c) => !c.parent_id).forEach((c) => row.append(h("a", { class: "pill", href: `#/shop?category=${c.id}` }, c.name)));
}
function renderCountrySelect() {
  const s = $("#countrySel"); clear(s).append(h("option", { value: "" }, "All countries")); state.countries.forEach((c) => s.append(h("option", { value: c.code, selected: c.code === state.country }, c.name)));
  s.addEventListener("change", () => { state.country = s.value; store.set("country", s.value); route(); });
}

async function boot() {
  $("#year").textContent = new Date().getFullYear();
  const params = new URLSearchParams(location.search);
  if (params.get("ref")) store.set("ref", params.get("ref").slice(0, 20));
  state.country = store.get("country", "");
  $("#themeBtn").addEventListener("click", toggleTheme); $("#themeBtn2").addEventListener("click", toggleTheme); $("#authBtn").addEventListener("click", () => authDialog("login")); $("#notifBtn").addEventListener("click", openNotifications);
  $("#menuBtn").addEventListener("click", (e) => { const n = $("#catnav"); const open = n.classList.toggle("hide") === false; e.currentTarget.setAttribute("aria-expanded", String(open)); });
  if (matchMedia("(max-width: 859px)").matches) $("#catnav").classList.add("hide");
  setupSearch();
  try {
    const [cfg, co, ca] = await Promise.all([api("/config"), api("/countries"), api("/categories")]);
    state.cfg = cfg; state.countries = co.countries; state.categories = ca.categories;
    $("#brandName").textContent = cfg.brand_name || "Digital Store"; $("#footBrand").textContent = cfg.brand_name || "Digital Store"; document.title = (cfg.brand_name || "Digital Store") + " - Premium digital products";
    if (cfg.support_url) { const a = $("#supportLink"); a.href = cfg.support_url; a.target = "_blank"; a.rel = "noopener noreferrer"; }
    if (cfg.terms_url) { const a = $("#termsLink"); a.href = cfg.terms_url; a.classList.remove("hide"); a.target = "_blank"; a.rel = "noopener noreferrer"; }
    renderCatNav(); renderCountrySelect();
  } catch (e) { clear(app()).append(errorBox(e, () => location.reload())); return; }
  await loadSession(); await refreshCart();
  if (params.get("reset")) { resetDialog(params.get("reset")); history.replaceState(null, "", location.pathname + location.hash); }
  const ord = params.get("order"); if (ord && /^ORD-[A-F0-9]{10}$/.test(ord)) { history.replaceState(null, "", location.pathname); location.hash = "#/orders/" + ord; }
  window.addEventListener("hashchange", route); route(); pollNotifs(); setInterval(pollNotifs, 60000);
}
function resetDialog(token) {
  const pw = h("input", { type: "password", id: "rs_p", autocomplete: "new-password", required: true }); const err = h("div", { class: "error-text", role: "alert" });
  const f = h("form", { class: "stack" }, h("div", { class: "field" }, h("label", { for: "rs_p" }, "New password"), pw), err, h("button", { class: "btn primary", type: "submit" }, "Set password"));
  f.addEventListener("submit", async (e) => { e.preventDefault(); try { await api("/auth/password-reset/confirm", { method: "POST", body: { token, new_password: pw.value } }); closeDialog(); toast("Password updated. Please sign in.", "ok"); authDialog("login"); } catch (x) { err.textContent = x.message; } }); openDialog("Reset password", f);
}
boot();
})();
