/* Simplified CCS Admin — Telegram-gated */
"use strict";
(() => {
  const $ = (s, r = document) => r.querySelector(s);
  const state = { user: null, csrf: null, token: null, view: "overview" };
  const tg = window.Telegram && window.Telegram.WebApp ? window.Telegram.WebApp : null;
  if (tg) { tg.ready(); tg.expand(); }

  function h(tag, attrs, ...kids) {
    const e = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v == null || v === false) continue;
      if (k === "class") e.className = v;
      else if (k.startsWith("on") && typeof v === "function") e.addEventListener(k.slice(2), v);
      else if (v === true) e.setAttribute(k, "");
      else e.setAttribute(k, v);
    }
    for (const c of kids.flat()) if (c != null && c !== false) e.append(c.nodeType ? c : document.createTextNode(String(c)));
    return e;
  }
  const clear = (el) => { while (el.firstChild) el.removeChild(el.firstChild); return el; };
  const money = (m) => "$" + ((m || 0) / 100).toFixed(2);
  function toast(msg, kind = "") {
    const t = h("div", { class: "toast " + kind }, msg);
    document.body.append(t);
    setTimeout(() => t.remove(), 3500);
  }

  async function api(path, { method = "GET", body } = {}) {
    const headers = { Accept: "application/json" };
    if (state.token) headers.Authorization = "Bearer " + state.token;
    if (body !== undefined) headers["Content-Type"] = "application/json";
    if (method !== "GET" && state.csrf) headers["X-CSRF-Token"] = state.csrf;
    const res = await fetch("/api" + path, {
      method, credentials: "same-origin", headers,
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
    let data = null;
    try { data = await res.json(); } catch (_) {}
    if (!res.ok) throw new Error((data && data.error) || `Error ${res.status}`);
    return data;
  }

  async function boot() {
    const err = $("#gateErr");
    try {
      if (!tg || !tg.initData) {
        err.textContent = "Open this admin page from the Telegram Mini App (admin only).";
        return;
      }
      const r = await api("/auth/telegram", { method: "POST", body: { init_data: tg.initData } });
      state.user = r.user;
      state.csrf = r.csrf;
      state.token = r.token;
      if (!r.user.is_admin) {
        err.textContent = "Your Telegram ID is not in TELEGRAM_ADMIN_IDS. Access denied.";
        return;
      }
      $("#who").textContent = (r.user.username ? "@" + r.user.username : r.user.name) + " · admin";
      $("#gate").classList.add("hide");
      $("#app").classList.remove("hide");
      route("overview");
    } catch (e) {
      err.textContent = e.message || "Auth failed";
    }
  }

  document.querySelectorAll(".tab").forEach((b) => {
    b.addEventListener("click", () => {
      document.querySelectorAll(".tab").forEach((x) => x.classList.toggle("active", x === b));
      route(b.dataset.v);
    });
  });

  async function route(v) {
    state.view = v;
    const root = clear($("#view"));
    if (v === "overview") await viewOverview(root);
    else if (v === "add") await viewAdd(root);
    else if (v === "cards") await viewCards(root);
    else if (v === "users") await viewUsers(root);
    else if (v === "orders") await viewOrders(root);
  }

  async function viewOverview(root) {
    root.append(h("div", { class: "empty" }, "Loading…"));
    try {
      const o = await api("/admin/overview");
      clear(root);
      const grid = h("div", { class: "grid" });
      const tile = (l, n) => h("div", { class: "panel stat" }, h("div", { class: "n" }, String(n)), h("div", { class: "l" }, l));
      grid.append(
        tile("Available", o.cards_available),
        tile("Sold", o.cards_sold),
        tile("Users", o.users),
        tile("Orders", o.orders_paid),
        tile("Revenue", money(o.revenue_minor)),
      );
      root.append(grid);
    } catch (e) {
      clear(root).append(h("div", { class: "err" }, e.message));
    }
  }

  async function viewAdd(root) {
    const ta = h("textarea", {
      placeholder: "Paste one card per line:\ncc|exp|cvv|address|city|state|zip|country|extra info\n\nExample:\n4111111111111111|12/28|123|123 Main St|New York|NY|10001|US|note",
    });
    const price = h("input", { type: "number", step: "0.01", min: "0", value: "5.00", placeholder: "Price USD" });
    const err = h("div", { class: "err" });
    const result = h("div");
    const btn = h("button", { class: "btn primary", type: "button" }, "Parse + Gemini enrich + Save");
    btn.addEventListener("click", async () => {
      err.textContent = "";
      btn.disabled = true;
      btn.textContent = "Working…";
      try {
        const dollars = parseFloat(price.value || "0");
        const price_minor = Math.round(dollars * 100);
        const r = await api("/admin/cards/bulk", {
          method: "POST",
          body: { text: ta.value, price_minor, status: "available" },
        });
        toast(`Inserted ${r.inserted}, skipped ${r.skipped}`, "ok");
        clear(result);
        if (r.sample && r.sample.length) {
          result.append(h("div", { class: "panel" },
            h("strong", null, "Sample enrichment"),
            h("table", null,
              h("thead", null, h("tr", null, ["BIN", "Brand", "Type", "Bank", "Country"].map((x) => h("th", null, x)))),
              h("tbody", null, r.sample.map((s) => h("tr", null,
                h("td", { class: "mono" }, s.bin),
                h("td", null, s.brand || "—"),
                h("td", null, s.card_type || "—"),
                h("td", null, s.bank || "—"),
                h("td", null, s.country || "—"),
              )))
            )
          ));
        }
        ta.value = "";
      } catch (e) {
        err.textContent = e.message;
      } finally {
        btn.disabled = false;
        btn.textContent = "Parse + Gemini enrich + Save";
      }
    });
    root.append(
      h("div", { class: "panel" },
        h("h2", null, "Bulk add cards"),
        h("p", { class: "hint" }, "Format: cc | exp | cvv | address | city | state | zip | country | extra. Gemini fills brand / type / bank from BIN."),
        h("label", null, "Cards"),
        ta,
        h("label", null, "Default price (USD)"),
        price,
        err,
        btn,
        result
      )
    );
  }

  async function viewCards(root) {
    const status = h("select", null,
      ["", "available", "sold", "hold"].map((v) => h("option", { value: v }, v || "All statuses"))
    );
    const q = h("input", { placeholder: "Search BIN / brand / bank" });
    const box = h("div");
    const reload = async () => {
      clear(box).append(h("div", { class: "hint" }, "Loading…"));
      try {
        const params = new URLSearchParams();
        if (status.value) params.set("status", status.value);
        if (q.value.trim()) params.set("q", q.value.trim());
        const r = await api("/admin/cards?" + params.toString());
        clear(box);
        box.append(h("div", { class: "hint" }, r.total + " card(s)"));
        const table = h("table", null,
          h("thead", null, h("tr", null, ["CC", "Exp", "CVV", "Brand", "Country", "Price", "Status", ""].map((x) => h("th", null, x)))),
          h("tbody", null, r.items.map((c) => {
            const priceIn = h("input", { type: "number", step: "0.01", value: ((c.price_minor || 0) / 100).toFixed(2), style: "width:80px;margin:0" });
            return h("tr", null,
              h("td", { class: "mono" }, c.cc),
              h("td", { class: "mono" }, c.exp),
              h("td", { class: "mono" }, c.cvv),
              h("td", null, [c.brand, c.card_type].filter(Boolean).join(" ")),
              h("td", null, c.country || "—"),
              h("td", null, priceIn),
              h("td", null, h("span", { class: "chip " + (c.status === "available" ? "ok" : c.status === "sold" ? "bad" : "warn") }, c.status)),
              h("td", null,
                h("button", { class: "btn sm", type: "button", onclick: async () => {
                  try {
                    await api("/admin/cards/" + c.id, { method: "PATCH", body: { price_minor: Math.round(parseFloat(priceIn.value) * 100) } });
                    toast("Saved");
                  } catch (e) { toast(e.message, "bad"); }
                } }, "Save"),
                " ",
                h("button", { class: "btn sm danger", type: "button", onclick: async () => {
                  if (!confirm("Delete this card?")) return;
                  try {
                    await api("/admin/cards/" + c.id, { method: "DELETE" });
                    toast("Deleted");
                    reload();
                  } catch (e) { toast(e.message, "bad"); }
                } }, "Del")
              )
            );
          }))
        );
        box.append(h("div", { class: "panel", style: "overflow-x:auto" }, table));
      } catch (e) {
        clear(box).append(h("div", { class: "err" }, e.message));
      }
    };
    status.addEventListener("change", reload);
    q.addEventListener("keydown", (e) => { if (e.key === "Enter") reload(); });
    root.append(
      h("div", { class: "panel row" }, status, q, h("button", { class: "btn sm", type: "button", onclick: reload }, "Search")),
      box
    );
    await reload();
  }

  async function viewUsers(root) {
    try {
      const r = await api("/admin/users");
      const table = h("table", null,
        h("thead", null, h("tr", null, ["Name", "TG", "Role", "Balance", "Credit"].map((x) => h("th", null, x)))),
        h("tbody", null, r.items.map((u) => {
          const amt = h("input", { type: "number", step: "0.01", placeholder: "USD", style: "width:90px;margin:0" });
          return h("tr", null,
            h("td", null, u.name || "—"),
            h("td", null, u.username ? "@" + u.username : (u.tg_id || "—")),
            h("td", null, u.role),
            h("td", null, money(u.balance_minor)),
            h("td", null, h("div", { class: "row" }, amt, h("button", { class: "btn sm", type: "button", onclick: async () => {
              try {
                await api(`/admin/users/${u.id}/credit?amount=${encodeURIComponent(amt.value)}`, { method: "POST" });
                toast("Credited");
                route("users");
              } catch (e) { toast(e.message, "bad"); }
            } }, "+"))
          );
        }))
      );
      root.append(h("div", { class: "panel", style: "overflow-x:auto" }, table));
    } catch (e) {
      root.append(h("div", { class: "err" }, e.message));
    }
  }

  async function viewOrders(root) {
    try {
      const r = await api("/admin/orders");
      const table = h("table", null,
        h("thead", null, h("tr", null, ["Order", "User", "Total", "Status", "When"].map((x) => h("th", null, x)))),
        h("tbody", null, r.items.map((o) => h("tr", null,
          h("td", { class: "mono" }, o.number),
          h("td", { class: "mono" }, (o.user_id || "").slice(0, 8)),
          h("td", null, money(o.total_minor)),
          h("td", null, o.status),
          h("td", null, (o.created_at || "").replace("T", " ").slice(0, 19)),
        )))
      );
      root.append(h("div", { class: "panel", style: "overflow-x:auto" }, table));
    } catch (e) {
      root.append(h("div", { class: "err" }, e.message));
    }
  }

  boot();
})();
