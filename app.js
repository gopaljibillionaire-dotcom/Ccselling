/* CCS Telegram Mini App — themed like storefront (light/dark) */
"use strict";
(() => {
  const $ = (s, r = document) => r.querySelector(s);
  const state = { user: null, csrf: null, token: null, view: "shop", cfg: {} };

  const tg = window.Telegram && window.Telegram.WebApp ? window.Telegram.WebApp : null;
  if (tg) {
    tg.ready();
    tg.expand();
  }

  function applyTheme(t, persist) {
    t = t === "dark" ? "dark" : "light";
    document.documentElement.setAttribute("data-theme", t);
    const meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.setAttribute("content", t === "dark" ? "#0b1424" : "#f5f7fb");
    if (tg) {
      try {
        if (t === "dark") {
          tg.setHeaderColor("#0b1424");
          tg.setBackgroundColor("#0b1424");
        } else {
          tg.setHeaderColor("#ffffff");
          tg.setBackgroundColor("#f5f7fb");
        }
      } catch (_) {}
    }
    if (persist) {
      try { localStorage.setItem("tg_theme", t); } catch (_) {}
    }
  }

  function initTheme() {
    let t = "light";
    try {
      const saved = localStorage.getItem("tg_theme");
      if (saved === "dark" || saved === "light") t = saved;
      else if (tg && tg.colorScheme === "dark") t = "dark";
    } catch (_) {}
    applyTheme(t, false);
  }
  initTheme();

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
  const money = (minor) => {
    try {
      return new Intl.NumberFormat(undefined, {
        style: "currency",
        currency: state.cfg.currency || "USD",
      }).format((minor || 0) / 100);
    } catch {
      return ((minor || 0) / 100).toFixed(2);
    }
  };
  function toast(msg, kind) {
    const t = h("div", { class: "tg-toast " + (kind || "") }, msg);
    document.body.append(t);
    setTimeout(() => t.remove(), 3500);
  }

  async function api(path, { method = "GET", body } = {}) {
    const headers = { Accept: "application/json" };
    if (state.token) headers.Authorization = "Bearer " + state.token;
    if (body !== undefined) headers["Content-Type"] = "application/json";
    if (method !== "GET" && state.csrf) headers["X-CSRF-Token"] = state.csrf;
    const res = await fetch("/api" + path, {
      method,
      credentials: "same-origin",
      headers,
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
    let data = null;
    try { data = await res.json(); } catch (_) {}
    if (!res.ok) throw new Error((data && data.error) || "Request failed (" + res.status + ")");
    return data;
  }

  async function authWithTelegram() {
    if (!tg || !tg.initData) {
      $("#uname").textContent = "Open in Telegram";
      return false;
    }
    try {
      const r = await api("/auth/telegram", { method: "POST", body: { init_data: tg.initData } });
      state.user = r.user;
      state.csrf = r.csrf;
      state.token = r.token;
      renderUser();
      return true;
    } catch (e) {
      toast(e.message || "Auth failed", "bad");
      return false;
    }
  }

  function renderUser() {
    const u = state.user;
    if (!u) return;
    const chip = clear($("#userchip"));
    if (u.photo_url) chip.append(h("img", { src: u.photo_url, alt: "" }));
    chip.append(h("span", { class: "name", id: "uname" }, u.username ? "@" + u.username : (u.name || "User")));
    chip.append(h("span", { class: "tg-bal", id: "bal" }, money(u.balance_minor || 0)));
    const tab = $("#adminTab");
    if (u.is_admin) tab.classList.add("show");
    else tab.classList.remove("show");
  }

  async function loadConfig() {
    try {
      state.cfg = await api("/config");
      $("#brand").textContent = state.cfg.brand_name || "CCS Shop";
    } catch (_) {}
  }

  function setNav(view) {
    document.querySelectorAll(".tg-tab").forEach((b) => b.classList.toggle("active", b.dataset.view === view));
    document.querySelectorAll(".tg-bottom button[data-nav]").forEach((b) => {
      const nav = b.getAttribute("data-nav");
      if (nav === "theme") return;
      b.setAttribute("aria-current", nav === view ? "page" : "false");
    });
  }

  async function viewShop() {
    const root = clear($("#app"));
    const q = h("input", { class: "tg-search", placeholder: "Search BIN, brand, bank…", type: "search" });
    const list = h("div", { class: "tg-list", id: "list" });
    root.append(q, list);

    async function load(query) {
      clear(list).append(h("div", { class: "tg-empty" }, "Loading…"));
      try {
        const r = await api("/cards?status=available&q=" + encodeURIComponent(query || ""));
        clear(list);
        if (!r.items.length) {
          list.append(h("div", { class: "tg-empty" }, "No cards available."));
          return;
        }
        for (const c of r.items) {
          list.append(h("article", { class: "tg-card" },
            h("div", { class: "row" },
              h("div", null,
                h("div", { class: "bin" }, (c.bin || "••••••") + "******"),
                h("div", { class: "meta" }, [c.brand, c.card_type, c.bank, c.country, c.city].filter(Boolean).join(" · "))
              ),
              h("div", { style: "text-align:right" },
                h("div", { class: "price" }, money(c.price_minor)),
                h("div", { class: "actions" },
                  h("button", {
                    class: "btn primary sm", type: "button",
                    onclick: () => buy(c.id),
                  }, "Buy")
                )
              )
            )
          ));
        }
      } catch (e) {
        clear(list).append(h("div", { class: "tg-empty" }, e.message));
      }
    }

    let timer = null;
    q.addEventListener("input", () => {
      clearTimeout(timer);
      timer = setTimeout(() => load(q.value.trim()), 280);
    });
    await load("");
  }

  async function buy(id) {
    if (!state.user) return toast("Sign in first", "bad");
    let ok = true;
    if (tg && tg.showConfirm) {
      ok = await new Promise((resolve) => tg.showConfirm("Buy this card with wallet balance?", resolve));
    } else {
      ok = confirm("Buy this card with wallet balance?");
    }
    if (!ok) return;
    try {
      const r = await api("/checkout", { method: "POST", body: { card_id: id } });
      toast("Purchased · " + r.order_number, "ok");
      const me = await api("/auth/me");
      state.user = me.user;
      state.csrf = me.csrf;
      renderUser();
      showDelivery(r.delivery, r.order_number);
    } catch (e) {
      toast(e.message, "bad");
    }
  }

  function showDelivery(d, number) {
    const root = clear($("#app"));
    root.append(
      h("div", { class: "tg-card panel" },
        h("h2", { style: "margin:0 0 8px;font-size:1.05rem" }, "Order " + number),
        h("div", { class: "meta muted small" }, "Full details"),
        h("div", { class: "tg-secret" }, d.cc + "|" + d.exp + "|" + d.cvv),
        d.address
          ? h("div", { class: "tg-secret" }, [d.address, d.city, d.state, d.zip, d.country].filter(Boolean).join(", "))
          : null,
        d.extra ? h("div", { class: "meta muted small" }, d.extra) : null,
        h("button", {
          class: "btn primary", type: "button", style: "width:100%;margin-top:10px",
          onclick: () => {
            const text = d.cc + "|" + d.exp + "|" + d.cvv +
              (d.address ? "\n" + [d.address, d.city, d.state, d.zip, d.country].filter(Boolean).join(", ") : "");
            if (navigator.clipboard) navigator.clipboard.writeText(text).then(() => toast("Copied", "ok"));
            else toast("Select and copy manually");
          },
        }, "Copy all"),
        h("button", {
          class: "btn", type: "button", style: "width:100%;margin-top:8px",
          onclick: () => route("orders"),
        }, "My orders")
      )
    );
  }

  async function viewOrders() {
    const root = clear($("#app"));
    root.append(h("div", { class: "tg-empty" }, "Loading…"));
    try {
      const r = await api("/orders");
      clear(root);
      if (!r.orders.length) {
        root.append(h("div", { class: "tg-empty" }, "No orders yet."));
        return;
      }
      const list = h("div", { class: "tg-list" });
      for (const o of r.orders) {
        const box = h("div", { class: "tg-card" },
          h("div", { class: "row" },
            h("strong", null, o.number),
            h("span", { class: "price" }, money(o.total_minor))
          ),
          h("div", { class: "meta" },
            (o.created_at || "").replace("T", " ").slice(0, 19) + " · ",
            h("span", { class: "chip " + (o.status === "paid" ? "ok" : "warn") }, o.status)
          )
        );
        if (o.delivery) {
          const d = o.delivery;
          box.append(h("div", { class: "tg-secret" }, d.cc + "|" + d.exp + "|" + d.cvv));
        }
        list.append(box);
      }
      root.append(list);
    } catch (e) {
      clear(root).append(h("div", { class: "tg-empty" }, e.message));
    }
  }

  async function viewWallet() {
    const root = clear($("#app"));
    try {
      const w = await api("/wallet");
      if (state.user) state.user.balance_minor = w.balance_minor;
      renderUser();
      root.append(
        h("div", { class: "tg-card" },
          h("div", { class: "meta muted small" }, "Wallet balance"),
          h("div", { class: "price", style: "font-size:1.85rem;margin-top:4px" }, money(w.balance_minor)),
          h("p", { class: "meta muted small", style: "margin-top:12px" },
            "Ask an admin to credit your wallet after payment outside the app.")
        )
      );
    } catch (e) {
      root.append(h("div", { class: "tg-empty" }, e.message));
    }
  }

  function viewAdminGate() {
    location.href = "/admin";
  }

  async function route(view) {
    state.view = view || state.view;
    setNav(state.view);
    if (state.view === "shop") await viewShop();
    else if (state.view === "orders") await viewOrders();
    else if (state.view === "wallet") await viewWallet();
    else if (state.view === "admin") viewAdminGate();
  }

  document.querySelectorAll(".tg-tab").forEach((b) => {
    b.addEventListener("click", () => route(b.dataset.view));
  });
  document.querySelectorAll(".tg-bottom button[data-nav]").forEach((b) => {
    b.addEventListener("click", () => {
      const nav = b.getAttribute("data-nav");
      if (nav === "theme") {
        const cur = document.documentElement.getAttribute("data-theme") === "dark" ? "dark" : "light";
        applyTheme(cur === "dark" ? "light" : "dark", true);
        return;
      }
      route(nav);
    });
  });

  (async () => {
    await loadConfig();
    const ok = await authWithTelegram();
    if (ok) await route("shop");
    else {
      clear($("#app")).append(
        h("div", { class: "tg-empty" },
          h("p", null, "Open this page from the Telegram bot Mini App button."),
          h("p", { class: "meta muted small" }, "Set TELEGRAM_BOT_TOKEN and launch via BotFather web_app.")
        )
      );
    }
  })();
})();
