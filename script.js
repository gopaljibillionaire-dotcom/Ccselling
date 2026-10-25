"use strict";
(() => {
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const clear = (e) => { while (e.firstChild) e.removeChild(e.firstChild); return e; };
const st = { token: null, csrf: null, user: null, base: "", cats: [], countries: [] };

const h = (tag, a, ...kids) => { const e = document.createElement(tag); for (const [k, v] of Object.entries(a || {})) { if (v == null || v === false) continue; if (k === "class") e.className = v; else if (k.startsWith("on") && typeof v === "function") e.addEventListener(k.slice(2), v); else if (v === true) e.setAttribute(k, ""); else e.setAttribute(k, v); } for (const c of kids.flat()) if (c != null && c !== false) e.append(c.nodeType ? c : document.createTextNode(String(c))); return e; };
const money = (m, c = "USD") => { try { return new Intl.NumberFormat(undefined, { style: "currency", currency: c }).format(m / 100); } catch { return (m / 100).toFixed(2); } };
const when = (s) => s ? new Date(s).toLocaleString() : "";
function toast(m, k = "") { const t = h("div", { class: "toast " + k, role: "status" }, m); document.body.append(t); setTimeout(() => t.remove(), 4500); }
const chip = (s) => h("span", { class: "chip " + (["paid", "published", "active", "ok", "matched", "approved", "delivered", "in_stock"].includes(s) ? "ok" : ["pending", "created", "draft", "open", "unfulfilled", "processing", "low_stock"].includes(s) ? "warn" : ["failed", "expired", "cancelled", "refunded", "suspended", "archived", "mismatch", "rejected", "out_of_stock"].includes(s) ? "bad" : "") }, s);

async function api(path, { method = "GET", body, raw } = {}) {
  const hd = { Accept: "application/json" }; if (st.token) hd.Authorization = "Bearer " + st.token; if (body !== undefined) hd["Content-Type"] = "application/json";
  let r; try { r = await fetch(st.base + "/api" + path, { method, headers: hd, body: body !== undefined ? JSON.stringify(body) : undefined }); } catch { throw new Error("Cannot reach the server."); }
  if (raw && r.ok) return r;
  let d = null; try { d = await r.json(); } catch {}
  if (!r.ok) { if (r.status === 401 && st.user) { signedOut("Session expired. Please sign in again."); } throw new Error((d && d.error) || "Request failed (" + r.status + ")"); }
  return d;
}
const go = (fn) => async (...a) => { try { return await fn(...a); } catch (e) { toast(e.message, "bad"); } };

function signedOut(msg) { st.token = null; st.user = null; localStorage.removeItem("tok"); toast(msg || "Signed out."); route(); }

async function route() {
  const hash = location.hash.slice(1) || "/";
  const app = clear($("#app"));
  const parts = hash.split("/").filter(Boolean);
  const view = parts[0];

  if (!st.user && view !== "login" && view !== "register") {
    // For now, just render the public view, but we could force login for account/cart.
    // In this rewrite, we will use a simple guest mode.
  }

  if (view === "login") return showLogin(app);
  if (view === "register") return showRegister(app);
  if (view === "shop") return showShop(app);
  if (view === "cart") return showCart(app);
  if (view === "account") return showAccount(app);
  if (view === "order") return showOrder(app, parts[1]);

  // Default Home
  showHome(app);
}

async function boot() {
  try {
    const [c, k] = await Promise.all([api("/config"), api("/categories")]);
    $("#brandName").textContent = c.brand_name || "Auralis";
    $("#footBrand").textContent = c.brand_name || "Auralis";
    st.cats = k.categories;
    // Populate category nav
    const nav = clear($("#catRow"));
    nav.append(h("a", { class: "pill", href: "#/shop", "aria-current": "true" }, "All Products"));
    st.cats.forEach(cat => nav.append(h("a", { class: "pill", href: "#/shop?cat=" + cat.slug }, cat.name)));

    // Populate country select
    const cs = await api("/countries");
    st.countries = cs.countries;
    const sel = $("#countrySel");
    cs.countries.forEach(c => sel.append(h("option", { value: c.code }, c.name)));

    // Restore session if token exists
    const tok = localStorage.getItem("tok");
    if (tok) {
      st.token = tok;
      try {
        const me = await api("/auth/me");
        st.user = me.user; st.csrf = me.csrf;
        updateAuthUI();
      } catch (e) { signedOut(); }
    }
  } catch (e) { console.error(e); toast("Failed to load config.", "bad"); }
  route();
}

function updateAuthUI() {
  if (st.user) {
    $("#authBtn").classList.add("hide");
    $("#acctBtn").classList.remove("hide");
    $("#acctBtn").textContent = "Account";
  } else {
    $("#authBtn").classList.remove("hide");
    $("#acctBtn").classList.add("hide");
  }
}

// ---------------- Views ----------------

async function showHome(app) {
  app.append(h("div", { class: "hero" }, h("h1", null, "Discover the world, delivered."), h("p", null, "Curated products by country, with member rewards.")));
  const res = await api("/home");
  const grid = h("div", { class: "products grid" });
  res.new_arrivals.forEach(p => grid.append(productCard(p)));
  app.append(h("h2", { class: "section-head" }, "New Arrivals"), grid);
}

async function showShop(app) {
  app.append(h("h2", { style: "margin-top:20px" }, "Shop"));
  const res = await api("/products?limit=24");
  const grid = h("div", { class: "products grid" });
  res.items.forEach(p => grid.append(productCard(p)));
  app.append(grid);
}

function productCard(p) {
  const img = (p.images && p.images.length) ? p.images[0].url : "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='100' height='100'%3E%3Crect width='100' height='100' fill='%23101a38'/%3E%3C/svg%3E";
  return h("div", { class: "card" },
    h("a", { href: "#/product/" + p.id, class: "img" }, h("img", { src: img, alt: p.name, loading: "lazy" })),
    h("div", { class: "body" },
      h("div", { class: "name" }, p.name),
      h("div", { class: "desc" }, p.short_description),
      h("div", { class: "price" }, money(p.final_price_minor, p.currency), p.discount_percent ? h("s", null, money(p.price_minor, p.currency)) : null),
      h("div", { class: "actions" }, h("button", { class: "btn primary", onclick: go(async () => {
        if (!st.user) return location.hash = "#/login";
        await api("/cart/items", { method: "PUT", body: { product_id: p.id, sku: p.sku || "DEFAULT", qty: 1 } });
        toast("Added to cart.", "ok");
        updateCartCount();
      }) }, "Add to Cart"))
    )
  );
}

async function showCart(app) {
  if (!st.user) return location.hash = "#/login";
  const cart = await api("/cart");
  app.append(h("h2", null, "Your Cart"));
  if (!cart.items.length) return app.append(h("p", { class: "muted" }, "Your cart is empty."));
  const list = h("div", { class: "panel" });
  cart.items.forEach(it => list.append(h("div", { class: "row between", style: "border-bottom:1px solid var(--line);padding:10px 0" }, h("div", null, h("strong", null, it.name), h("div", { class: "small muted" }, it.variant_label)), h("div", null, money(it.line_total_minor, it.currency)))));
  app.append(list, h("div", { class: "row between", style: "margin-top:16px" }, h("h3", null, "Total: " + money(cart.total_minor, cart.currency)), h("button", { class: "btn primary", onclick: go(async () => {
    const idemp = crypto.randomUUID();
    const res = await api("/checkout", { method: "POST", headers: { "Idempotency-Key": idemp }, body: { payment_method: "plisio" } });
    if (res.order.payment_url) location.href = res.order.payment_url;
    else { toast("Order placed.", "ok"); location.hash = "#/order/" + res.order.number; }
  }) }, "Checkout")));
}

async function showAccount(app) {
  if (!st.user) return location.hash = "#/login";
  app.append(h("h1", null, "My Account"), h("p", null, "Welcome back, " + st.user.name), h("button", { class: "btn danger", onclick: () => { st.token = null; st.user = null; localStorage.removeItem("tok"); location.hash = "#/"; } }, "Sign Out"));
}

async function showOrder(app, number) {
  if (!st.user) return location.hash = "#/login";
  const res = await api("/orders/" + number);
  const o = res.order;
  app.append(h("h1", null, "Order " + o.number), h("p", null, "Status: ", chip(o.status)));
  app.append(h("h2", { style: "margin-top:20px" }, "Items"));
  const list = h("div", { class: "panel" });
  o.items.forEach(it => list.append(h("div", { class: "row between", style: "border-bottom:1px solid var(--line);padding:10px 0" }, h("div", null, h("strong", null, it.name), h("div", { class: "small muted" }, it.variant_label)), h("div", null, money(it.line_total_minor, o.currency)))));
  app.append(list);

  if (o.status === "paid" && o.deliveries) {
    app.append(h("h2", { style: "margin-top:20px" }, "Your Delivery"));
    o.deliveries.forEach(d => {
      const p = h("div", { class: "panel delivery" }, h("h3", null, d.name));
      d.fields.forEach(f => {
        p.append(h("div", { class: "dfield" }, h("span", { class: "dlabel" }, f.label), h("div", { class: f.kind === "secret" ? "secret" : "" }, f.value)));
      });
      app.append(p);
    });
  } else if (o.status === "pending" || o.status === "created") {
    app.append(h("div", { class: "panel" }, h("p", null, "Waiting for payment confirmation...")));
  }
}

function showLogin(app) {
  const em = h("input", { type: "email", placeholder: "Email" }), pw = h("input", { type: "password", placeholder: "Password" }), btn = h("button", { class: "btn primary", style: "width:100%" }, "Sign In");
  const err = h("div", { class: "err" });
  btn.addEventListener("click", go(async () => {
    const r = await api("/auth/login", { method: "POST", body: { email: em.value, password: pw.value, captcha_id: "", captcha_answer: "" } });
    st.token = r.token; st.user = r.user; localStorage.setItem("tok", r.token); updateAuthUI();
    toast("Welcome back.", "ok"); location.hash = "#/";
  }));
  app.append(h("div", { class: "panel", style: "max-width:400px;margin:40px auto" }, h("h2", null, "Sign In"), h("div", { class: "field" }, em), h("div", { class: "field" }, pw), err, btn));
}

function showRegister(app) {
  const em = h("input", { type: "email", placeholder: "Email" }), nm = h("input", { placeholder: "Name" }), pw = h("input", { type: "password", placeholder: "Password" }), btn = h("button", { class: "btn primary", style: "width:100%" }, "Create Account");
  const err = h("div", { class: "err" });
  btn.addEventListener("click", go(async () => {
    const r = await api("/auth/register", { method: "POST", body: { email: em.value, name: nm.value, password: pw.value, accept_terms: true, captcha_id: "", captcha_answer: "" } });
    st.token = r.token; st.user = r.user; localStorage.setItem("tok", r.token); updateAuthUI();
    toast("Account created.", "ok"); location.hash = "#/";
  }));
  app.append(h("div", { class: "panel", style: "max-width:400px;margin:40px auto" }, h("h2", null, "Register"), h("div", { class: "field" }, em), h("div", { class: "field" }, nm), h("div", { class: "field" }, pw), err, btn));
}

async function updateCartCount() {
  if (!st.user) return;
  try {
    const c = await api("/cart");
    const n = c.items.reduce((a, b) => a + b.qty, 0);
    [$("#cartCount"), $("#cartCount2")].forEach(e => { if (e) { e.textContent = n; e.classList.toggle("hide", !n); } });
  } catch {}
}

window.addEventListener("hashchange", route);
document.addEventListener("DOMContentLoaded", boot);
})();
