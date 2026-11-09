/* Kalshi Portal — mobile SPA */
const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

function toast(msg) {
  const el = $("#toast");
  el.textContent = msg;
  el.classList.add("show");
  setTimeout(() => el.classList.remove("show"), 2800);
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
    ...opts,
  });
  let data;
  try {
    data = await res.json();
  } catch {
    data = { error: await res.text() };
  }
  if (!res.ok) {
    const err = data.detail || data.error || res.statusText;
    throw new Error(typeof err === "string" ? err : JSON.stringify(err));
  }
  return data;
}

function fmtTs(iso) {
  if (!iso) return "—";
  try {
    const d = new Date(iso);
    return d.toLocaleString(undefined, { hour12: true });
  } catch {
    return iso;
  }
}

/* nav */
function showPanel(name) {
  $$("#nav button").forEach((b) => b.classList.toggle("active", b.dataset.panel === name));
  $$(".panel").forEach((p) => p.classList.toggle("active", p.id === "panel-" + name));
  window.scrollTo({ top: 0, behavior: "smooth" });
}
$$("#nav button").forEach((btn) => {
  btn.addEventListener("click", () => showPanel(btn.dataset.panel));
});

/* state shared by the bet form */
let lastScan = null;
let currentContract = null;
let currentMode = { paper: true, armed: false };
let scanFilter = "all";

function paintMode(s) {
  const gate = s.order_gate || {};
  const paper = Boolean(s.dry_run);
  const armed = Boolean(gate.live_armed);
  const mb = $("#badge-mode");
  const pill = $("#mode-pill");
  const detail = $("#mode-detail");
  const cta = $("#live-cta");
  const ctaTitle = $("#live-cta-title");
  const ctaText = $("#live-cta-text");
  const ctaBtn = $("#btn-allow-live-top");
  if (!paper && armed) {
    mb.textContent = "Live armed";
    mb.className = "badge danger";
    if (pill) {
      pill.textContent = "Real bets (uses Kalshi balance)";
      pill.className = "mode-pill danger";
    }
    if (detail) detail.textContent = "This session can place real orders. Disarm to stop.";
    cta?.classList.add("hidden");
  } else if (!paper) {
    mb.textContent = "Live ready";
    mb.className = "badge warn";
    if (pill) {
      pill.textContent = "Live trading allowed";
      pill.className = "mode-pill warn";
    }
    if (detail) detail.textContent = "Real bets are allowed. Type ARM LIVE below before any order spends money.";
    cta?.classList.remove("hidden");
    if (ctaTitle) ctaTitle.textContent = "Live trading is allowed";
    if (ctaText) ctaText.textContent = "Type ARM LIVE to turn on real orders for this session.";
    if (ctaBtn) ctaBtn.textContent = "Turn on real orders";
  } else {
    mb.textContent = "Paper bets";
    mb.className = "badge ok";
    if (pill) {
      pill.textContent = "Paper bets (no money)";
      pill.className = "mode-pill ok";
    }
    if (detail) detail.textContent = "Orders stay on this phone. Tap Allow live trading when you want real bets.";
    cta?.classList.remove("hidden");
    if (ctaTitle) ctaTitle.textContent = "Want real bets?";
    if (ctaText) ctaText.textContent = "Allow live trading here. You still type ARM LIVE before any order spends money.";
    if (ctaBtn) ctaBtn.textContent = "Allow live trading";
  }
  currentMode = { paper, armed };
  paintOrderMode();
  $("#btn-allow-live")?.classList.toggle("is-current", !paper);
  $("#btn-paper-only")?.classList.toggle("is-current", paper);
  const arm = $("#arm-card");
  if (arm) {
    arm.classList.toggle("arm-ready", !paper && !armed);
    arm.classList.toggle("arm-on", !paper && armed);
  }
  const safety = $("#safety-out");
  if (safety) {
    if (!paper && armed) safety.textContent = "Real bets are on for this session. They use your Kalshi balance.";
    else if (!paper) safety.textContent = "Live trading is allowed. Type ARM LIVE before a real bet is sent.";
    else safety.textContent = "Paper bets only. Allow live trading before ARM LIVE will work.";
  }
}

async function refreshStatus() {
  try {
    const s = await api("/api/status");
    paintMode(s);
    $("#badge-host").textContent = (s.host_key || "?") + " · " + (s.host || "").replace("https://", "");
    const ex = s.exchange;
    const eb = $("#badge-ex");
    if (ex && ex.exchange_active) {
      eb.textContent = "EXCHANGE ACTIVE";
      eb.className = "badge live";
    } else if (s.exchange_error) {
      eb.textContent = "EXCHANGE ERR";
      eb.className = "badge danger";
    } else {
      eb.textContent = "EXCHANGE ?";
      eb.className = "badge warn";
    }
    if (s.host_key) $("#host-key").value = s.host_key;
    updateCredentialStatus(s);
    $("#first-run-banner").classList.toggle("hidden", Boolean(s.has_keys));
  } catch (e) {
    $("#badge-ex").textContent = "OFFLINE";
    $("#badge-ex").className = "badge danger";
  }
}

function updateCredentialStatus(status) {
  const badge = $("#key-status-badge");
  if (!badge) return;
  if (!status.has_keys) {
    badge.textContent = "No keys";
    badge.className = "badge warn";
  } else if (status.vault_unlocked) {
    badge.textContent = "Unlocked";
    badge.className = "badge ok";
  } else {
    badge.textContent = "Saved on this phone";
    badge.className = "badge live";
  }
}

function openSettings(anchor) {
  $$("#nav button").forEach((b) => b.classList.toggle("active", b.dataset.panel === "settings"));
  $$(".panel").forEach((p) => p.classList.toggle("active", p.id === "panel-settings"));
  const el = typeof anchor === "string" ? document.querySelector(anchor) : $("#api-credentials-card");
  el?.scrollIntoView({ behavior: "smooth", block: "start" });
}

async function allowLiveTrading() {
  if (!confirm("This can spend real money. You must still type ARM LIVE before any real order.")) return;
  await api("/api/settings/mode", {
    method: "POST",
    body: JSON.stringify({ allow_live: true }),
  });
  toast("Live trading allowed — type ARM LIVE before a real bet");
  openSettings("#arm-card");
  refreshStatus();
}

async function paperBetsOnly() {
  await api("/api/settings/mode", {
    method: "POST",
    body: JSON.stringify({ dry_run: true }),
  });
  toast("Paper bets only — no money is sent");
  refreshStatus();
}

function payoutSort(markets) {
  return (markets || []).slice().sort((a, b) => {
    const d = (Number(b.best_payout_multiple) || 0) - (Number(a.best_payout_multiple) || 0);
    if (d) return d;
    const ev =
      (Number(b.kelly?.expected_value_per_contract) || 0) -
      (Number(a.kelly?.expected_value_per_contract) || 0);
    if (ev) return ev;
    return (Number(b.nash_payoff_gemini) || 0) - (Number(a.nash_payoff_gemini) || 0);
  });
}

function renderMarkets(data) {
  const list = $("#market-list");
  list.innerHTML = "";
  const meta = [
    `Updated: ${fmtTs(data.updated_at)}`,
    data.from_cache ? "cache HIT" : "live fetch",
    `host: ${data.host}`,
    `enriched ${data.count}/${data.scanned}`,
    `sort ${data.sort || "payout"}`,
    `TTL ${data.cache_ttl_seconds}s`,
  ];
  if (data.espn) meta.push(`ESPN ${data.espn.matched || 0} matched (${data.espn.live || 0} live)`);
  const rows = payoutSort(data.markets).filter(matchesFilter);
  if (scanFilter !== "all") meta.push(`showing ${rows.length} (${filterLabel(scanFilter)})`);
  const metaText = meta.join(" · ");
  $("#scan-meta").textContent = metaText + " — " + (data.disclaimer || "");
  if (!rows.length) {
    list.innerHTML = `<div class="card muted">No markets match “${escapeHtml(
      filterLabel(scanFilter)
    )}” right now. Tap All, or scan again closer to game time.</div>`;
    return;
  }

  rows.slice(0, 60).forEach((m) => {
    const div = document.createElement("div");
    div.className = "market-item";
    const action = m.nash?.action || "PASS";
    const sig = m.signal === "GO" ? "GO" : "NO-GO";
    const gk = m.gemini_kelly || {};
    const why = (m.explainer && (m.explainer.edge_plain || m.explainer.why_odds)) || "";
    const hint = m.espn_hint || "";
    const mult = m.best_payout_multiple ?? "—";
    div.innerHTML = `
      <div class="stats">
        <span class="chip payout">${mult}x</span>
        ${probSourceChip(m)}
        <span class="chip ${sig === "GO" ? "go" : "nogo"}">${sig}</span>
        <span class="chip">Gemini Kelly $${gk.allocation ?? "—"}</span>
        <span class="chip">edge ${m.kelly?.edge ?? "—"}</span>
        <span class="chip">EV ${m.kelly?.expected_value_per_contract ?? "—"}</span>
      </div>
      <div class="ticker" style="margin-top:6px">${escapeHtml(m.ticker || "")}</div>
      <div class="title">${escapeHtml(m.title || "")}</div>
      ${espnLine(m)}
      <div class="stats">
        <span class="chip yes">YES ${m.yes_ask_cents}¢ · ${m.yes_payout_multiple}x</span>
        <span class="chip no">NO ${m.no_ask_cents}¢ · ${m.no_payout_multiple}x</span>
        <span class="chip action">${action}</span>
        <span class="chip">Nash ${m.nash_payoff_gemini ?? "—"}</span>
      </div>
      ${hint ? `<p class="why espn-hint">${escapeHtml(hint)}</p>` : ""}
      <p class="why">${escapeHtml(why)}</p>`;
    div.addEventListener("click", () => openContract(m.ticker, m));
    list.appendChild(div);
  });
}

function probSourceChip(m) {
  const src = (m && (m.prob_source || m.model_p_source)) || "";
  const league = (m && m.espn && m.espn.league ? String(m.espn.league).toUpperCase() + " " : "");
  if (src === "espn_live") return `<span class="chip espn live-dot">ESPN ${escapeHtml(league)}live</span>`;
  if (src === "espn_pre") return `<span class="chip espn">ESPN ${escapeHtml(league)}pregame</span>`;
  if (m && m.sports_game) return `<span class="chip muted-chip">Game · no ESPN match</span>`;
  return "";
}

function espnLine(m) {
  const e = m && m.espn;
  if (!e || !(e.summary || e.detail)) return "";
  const parts = [e.summary, e.detail].filter(Boolean).map(escapeHtml).join(" · ");
  return `<div class="espn-line">🏟️ ${parts}</div>`;
}

function isEspn(m) {
  const src = m.prob_source || m.model_p_source;
  return src === "espn_live" || src === "espn_pre";
}

function matchesFilter(m) {
  switch (scanFilter) {
    case "espn":
      return isEspn(m);
    case "live":
      return (m.prob_source || m.model_p_source) === "espn_live";
    case "sports":
      return Boolean(m.sports_game) || isEspn(m);
    case "go":
      return m.signal === "GO";
    default:
      return true;
  }
}

function filterLabel(f) {
  return (
    { all: "All", espn: "ESPN matched", live: "ESPN live now", sports: "Sports games", go: "GO only" }[f] || f
  );
}

$$("#scan-filter .filter").forEach((btn) => {
  btn.addEventListener("click", () => {
    scanFilter = btn.dataset.filter;
    $$("#scan-filter .filter").forEach((b) => b.classList.toggle("active", b === btn));
    if (lastScan) renderMarkets(lastScan);
  });
});

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

async function runScan(force = false) {
  const limit = parseInt($("#scan-limit").value || "80", 10);
  toast(force ? "Force refreshing…" : "Scanning…");
  try {
    const data = await api(
      `/api/markets/scan?limit=${limit}&refresh=${force ? "true" : "false"}&sort=payout`
    );
    lastScan = data;
    renderMarkets(data);
    toast(`Scan done · ${data.count} markets`);
  } catch (e) {
    toast("Scan failed: " + e.message);
    $("#scan-meta").textContent = "Error: " + e.message;
  }
}

function openContract(ticker, preview) {
  $("#contract-ticker").value = ticker;
  showPanel("contract");
  if (preview) {
    paintContract({ enriched: preview, ticker, orderbook: null, host: "preview" });
    prefillOrder(preview);
  }
  loadContract();
}

async function loadContract() {
  const ticker = $("#contract-ticker").value.trim();
  if (!ticker) return;
  $("#contract-body").textContent = "Loading…";
  try {
    const data = await api("/api/markets/" + encodeURIComponent(ticker));
    paintContract(data);
    // Prefill nash board
    const e = data.enriched;
    if (e) {
      prefillOrder(e);
      $("#nash-yes").value = e.yes_ask;
      $("#nash-no").value = e.no_ask;
    }
  } catch (err) {
    $("#contract-body").textContent = err.message;
  }
}

function paintContract(data) {
  const e = data.enriched;
  if (!e) {
    $("#contract-body").textContent = JSON.stringify(data.raw || data, null, 2);
    return;
  }
  const notes = (e.nash?.socratic_notes || []).map((n) => `<li>${escapeHtml(n)}</li>`).join("");
  const ex = e.explainer || {};
  const gk = e.gemini_kelly || {};
  const sig = e.signal === "GO" ? "GO" : "NO-GO";
  $("#contract-body").innerHTML = `
    <div class="ticker mono">${escapeHtml(e.ticker)}</div>
    <div style="margin:6px 0 10px">${escapeHtml(e.title)}</div>
    <div class="stats">
      <span class="chip payout">${e.best_payout_multiple ?? "—"}x</span>
      ${probSourceChip(e)}
      <span class="chip ${sig === "GO" ? "go" : "nogo"}">${sig}</span>
      <span class="chip yes">YES ask ${e.yes_ask_cents}¢ (${e.yes_payout_multiple}x)</span>
      <span class="chip no">NO ask ${e.no_ask_cents}¢ (${e.no_payout_multiple}x)</span>
      <span class="chip action">${e.nash.action}</span>
      <span class="chip">Gemini Kelly $${gk.allocation ?? "—"}</span>
      <span class="chip">edge ${e.kelly?.edge ?? "—"}</span>
    </div>
    <div class="explainer card" style="margin-top:12px;padding:12px">
      <h3>Odds explainer</h3>
      <p><strong>Price.</strong> ${escapeHtml(ex.price_plain || "")}</p>
      <p><strong>You win if.</strong> ${escapeHtml(ex.win_if || "")}</p>
      <p><strong>You lose if.</strong> ${escapeHtml(ex.lose_if || "")}</p>
      ${e.espn_hint ? `<p class="espn-hint"><strong>ESPN.</strong> ${escapeHtml(e.espn_hint)}</p>` : ""}
      <p><strong>Why these odds.</strong> ${escapeHtml(ex.why_odds || "")}</p>
      <p><strong>Edge.</strong> ${escapeHtml(ex.edge_plain || "")}</p>
      <p><strong>Payout.</strong> ${escapeHtml(ex.payout_plain || "")}</p>
      <p><strong>Size.</strong> ${escapeHtml(ex.size_plain || "")}</p>
      <p class="muted"><strong>Risk.</strong> ${escapeHtml(ex.risk_plain || "High multiple ≠ likely. Not guaranteed.")}</p>
    </div>
    <h3>Kelly</h3>
    <p class="muted mono">side=${e.kelly.side} · contracts=${e.kelly.contracts} · alloc=$${e.kelly.allocation_usd}
      · edge=${e.kelly.edge} · Nash payoff=${e.nash_payoff_gemini ?? "—"} · ${escapeHtml(e.kelly.reason)}</p>
    <h3>Socratic notes</h3>
    <ul class="muted" style="margin:0;padding-left:18px">${notes}</ul>
    <p class="muted" style="margin-top:8px">Host: ${escapeHtml(data.host || "")} · close: ${escapeHtml(e.close_time || "—")}</p>
  `;
}

async function analyze() {
  const body = {
    yes_ask: parseFloat($("#nash-yes").value),
    no_ask: parseFloat($("#nash-no").value),
    bankroll: parseFloat($("#nash-bankroll").value || "1000"),
    run_simulation: true,
  };
  const mp = $("#nash-model").value;
  if (mp !== "") body.model_prob_yes = parseFloat(mp);
  try {
    const data = await api("/api/strategy/analyze", { method: "POST", body: JSON.stringify(body) });
    const n = data.nash;
    const k = data.kelly;
    const s = data.simulation;
    const matrix = n.payoff_matrix || {};
    $("#nash-out").innerHTML = `
      <div class="stats" style="margin-bottom:10px">
        <span class="chip action">${n.action}</span>
        <span class="chip">coherence ${n.coherence}</span>
        <span class="chip">model P(YES) ${n.model_prob_yes}</span>
        <span class="chip yes">edge YES ${n.edge_yes}</span>
        <span class="chip no">edge NO ${n.edge_no}</span>
      </div>
      <div class="matrix">
        ${["BUY_YES", "BUY_NO", "PASS"]
          .map(
            (a) => `<div class="cell"><strong>${a}</strong>EV ${matrix[a]?.EV ?? "—"}</div>`
          )
          .join("")}
      </div>
      <h3>Kelly size</h3>
      <p class="mono muted">${k.side} · ${k.contracts} contracts · $${k.allocation_usd}
        (${k.kelly_pct_of_bankroll}% bankroll) · full f*=${k.full_kelly} · ${escapeHtml(k.reason)}</p>
      <h3>Monte Carlo sim</h3>
      <p class="muted">${escapeHtml(s?.label || "")}</p>
      <p class="mono muted">collapsed p=${s?.collapsed_prob} · payoff=${s?.nash_payoff}
        · signal=${s?.signal} · p05/p50/p95=${s?.p05}/${s?.p50}/${s?.p95}</p>
      <p class="muted">${escapeHtml(s?.disclaimer || data.disclaimer || "")}</p>
      <h3>Notes</h3>
      <ul class="muted">${(n.socratic_notes || []).map((x) => `<li>${escapeHtml(x)}</li>`).join("")}</ul>
    `;
  } catch (e) {
    toast(e.message);
  }
}

/* ---------- bet form ---------- */

function setSide(side) {
  $("#order-side").value = side;
  $$(".side-btn").forEach((b) => b.classList.toggle("active", b.dataset.side === side));
  updatePreview();
}
$$(".side-btn").forEach((b) => b.addEventListener("click", () => setSide(b.dataset.side)));

function askCents(e, side) {
  if (!e) return null;
  const v = side === "yes" ? e.yes_ask_cents : e.no_ask_cents;
  const n = Math.ceil(Number(v));
  return Number.isFinite(n) && n >= 1 && n <= 99 ? n : null;
}

function prefillOrder(e) {
  currentContract = e;
  const side = e.best_side === "no" ? "no" : "yes";
  setSide(side);
  const ask = askCents(e, side);
  if (ask) $("#order-price").value = ask;
  updatePreview();
}

function readOrderForm() {
  const side = $("#order-side").value;
  const count = Number($("#order-count").value);
  const priceRaw = $("#order-price").value.trim();
  const price = priceRaw === "" ? null : Number(priceRaw);
  return { side, count, price, type: $("#order-type").value };
}

function validateOrderForm(f) {
  if (!Number.isInteger(f.count) || f.count < 1) return "Contracts must be a whole number, 1 or more.";
  if (f.count > 10000) return "Max 10,000 contracts per order.";
  if (f.price === null) return "Set a max price in cents (1–99).";
  if (!Number.isInteger(f.price) || f.price < 1 || f.price > 99) return "Max price must be a whole number of cents from 1 to 99.";
  return null;
}

function updatePreview() {
  const f = readOrderForm();
  const el = $("#order-preview");
  if (!el) return;
  const err = validateOrderForm(f);
  if (err) {
    el.textContent = err;
    el.className = "cost-preview warn";
    return;
  }
  const cost = (f.count * f.price) / 100;
  const payout = f.count * 1;
  const mult = (100 / f.price).toFixed(2);
  el.className = "cost-preview";
  el.textContent = `Buy ${f.count} × ${f.side.toUpperCase()} at up to ${f.price}¢ → max cost $${cost.toFixed(
    2
  )}. Pays $${payout.toFixed(2)} if ${f.side.toUpperCase()} wins (${mult}x). You can lose the full $${cost.toFixed(2)}.`;
}
["#order-count", "#order-price", "#order-type"].forEach((sel) => $(sel)?.addEventListener("input", updatePreview));

function paintOrderMode() {
  const strip = $("#order-mode-strip");
  const btn = $("#btn-paper");
  if (!strip || !btn) return;
  if (!currentMode.paper && currentMode.armed) {
    strip.textContent = "REAL BET — uses your Kalshi balance";
    strip.className = "mode-strip danger";
    btn.textContent = "Place REAL bet";
    btn.classList.add("danger");
  } else {
    strip.textContent = currentMode.paper
      ? "Paper bet — no money is sent"
      : "Paper bet — live is allowed but not armed (type ARM LIVE in Settings)";
    strip.className = "mode-strip ok";
    btn.textContent = "Place paper bet";
    btn.classList.remove("danger");
  }
}

$("#btn-use-ask").onclick = () => {
  const ask = askCents(currentContract, $("#order-side").value);
  if (!ask) return toast("Load a market first");
  $("#order-price").value = ask;
  updatePreview();
};
$("#btn-use-kelly").onclick = () => {
  const k = currentContract && currentContract.kelly;
  const n = k ? Math.floor(Number(k.contracts)) : 0;
  if (!n || n < 1) return toast("Half-Kelly says skip this one (0 contracts)");
  if (k.side) setSide(k.side === "no" ? "no" : "yes");
  $("#order-count").value = n;
  updatePreview();
  toast(`Half-Kelly: ${n} contracts (estimate, not a guarantee)`);
};

async function submitOrder() {
  const ticker = $("#contract-ticker").value.trim();
  if (!ticker) {
    toast("Pick a market first");
    return;
  }
  const f = readOrderForm();
  const err = validateOrderForm(f);
  if (err) {
    $("#order-result").textContent = err;
    $("#order-result").className = "order-result bad";
    toast(err);
    return;
  }
  const status = await api("/api/trading/status");
  const body = { ticker, side: f.side, count: f.count, price_cents: f.price, order_type: f.type };
  if (status.effective_mode === "LIVE" || status.live_armed) {
    const cost = ((f.count * f.price) / 100).toFixed(2);
    if (
      !confirm(
        `REAL BET\n\nBuy ${f.count} × ${f.side.toUpperCase()} on ${ticker}\nat up to ${f.price}¢ each (max $${cost}).\n\nThis uses your Kalshi balance. Continue?`
      )
    ) {
      toast("Cancelled — nothing sent");
      return;
    }
    body.confirm_live = "PLACE LIVE ORDER";
  }
  const btn = $("#btn-paper");
  btn.disabled = true;
  try {
    const res = await api("/api/trading/order", { method: "POST", body: JSON.stringify(body) });
    $("#order-json").textContent = JSON.stringify(res, null, 2);
    const out = $("#order-result");
    if (res.ok) {
      out.textContent = "✅ " + (res.message || "Submitted");
      out.className = "order-result good";
    } else {
      out.textContent = "⚠️ " + (res.error || "Order not placed");
      out.className = "order-result bad";
    }
    toast(res.ok ? res.message || "Submitted" : res.error || "Order not placed");
    refreshActivity();
  } catch (e) {
    $("#order-result").textContent = "⚠️ " + e.message;
    $("#order-result").className = "order-result bad";
    toast(e.message);
  } finally {
    btn.disabled = false;
  }
}

async function refreshActivity() {
  try {
    const data = await api("/api/activity");
    $("#activity-log").innerHTML = (data.activity || [])
      .map(
        (a) =>
          `<div class="entry"><span class="ts">${fmtTs(a.ts)}</span> · <strong>${escapeHtml(
            a.kind
          )}</strong> — ${escapeHtml(a.message)}</div>`
      )
      .join("");
    $("#order-log").innerHTML = (data.orders || [])
      .map(
        (o) =>
          `<div class="entry"><span class="ts">${fmtTs(o.ts)}</span> · ${
            o.dry_run ? "PAPER" : "LIVE"
          } ${escapeHtml(o.status)} ${escapeHtml(o.side)} ${o.count}x ${escapeHtml(
            o.ticker
          )}</div>`
      )
      .join("");
  } catch (e) {
    toast(e.message);
  }
}

/* wire buttons */
$("#btn-scan").onclick = () => runScan(false);
$("#btn-scan-force").onclick = () => runScan(true);
$("#btn-load-contract").onclick = loadContract;
$("#btn-analyze").onclick = analyze;
$("#btn-paper").onclick = submitOrder;
$("#btn-refresh-log").onclick = refreshActivity;
$("#btn-open-settings").onclick = () => openSettings("#api-credentials-card");
$("#btn-allow-live").onclick = () => allowLiveTrading().catch((e) => toast(e.message));
$("#btn-paper-only").onclick = () => paperBetsOnly().catch((e) => toast(e.message));
$("#btn-allow-live-top").onclick = async () => {
  try {
    const s = await api("/api/status");
    if (!s.dry_run) {
      openSettings("#arm-card");
      return;
    }
    await allowLiveTrading();
  } catch (e) {
    toast(e.message);
  }
};

function money(v) {
  const n = Number(v);
  return Number.isFinite(n) ? "$" + n.toFixed(2) : "—";
}

async function accountCall(path, render) {
  const box = $("#account-summary");
  box.textContent = "Loading…";
  try {
    const data = await api(path);
    $("#account-out").textContent = JSON.stringify(data, null, 2);
    if (data.ok === false) {
      box.innerHTML = `<p class="warn-text">${escapeHtml(data.error || "Not available")}. Add your key in Settings.</p>`;
      return;
    }
    box.innerHTML = render(data);
  } catch (e) {
    box.innerHTML = `<p class="warn-text">${escapeHtml(e.message)}</p>`;
    $("#account-out").textContent = e.message;
  }
}

$("#btn-balance").onclick = () =>
  accountCall(
    "/api/account/balance",
    (d) => `<div class="big-number">${money(d.balance_dollars)}</div><p class="muted">Available cash${
      d.portfolio_value_dollars != null ? ` · positions worth ${money(d.portfolio_value_dollars)}` : ""
    }</p>`
  );
$("#btn-positions").onclick = () =>
  accountCall("/api/account/positions", (d) => {
    const rows = d.summary || [];
    if (!rows.length) return `<p class="muted">No open positions.</p>`;
    return rows
      .map(
        (r) => `<div class="pos-row"><span class="chip ${r.side === "YES" ? "yes" : "no"}">${r.side} ×${
          r.contracts
        }</span> <span class="mono">${escapeHtml(r.ticker || "")}</span> <span class="muted">exposure ${money(
          r.exposure_dollars
        )}</span></div>`
      )
      .join("");
  });
$("#btn-orders").onclick = () =>
  accountCall("/api/account/orders", (d) => {
    const list = (d.data && d.data.orders) || [];
    if (!list.length) return `<p class="muted">No recent orders.</p>`;
    return list
      .slice(0, 20)
      .map(
        (o) =>
          `<div class="pos-row"><span class="chip">${escapeHtml(o.status || "")}</span> <span class="mono">${escapeHtml(
            o.ticker || ""
          )}</span> <span class="muted">${escapeHtml(String(o.outcome_side || o.side || "").toUpperCase())} @ $${escapeHtml(
            (o.outcome_side || o.side) === "no" ? o.no_price_dollars || "" : o.yes_price_dollars || ""
          )} · filled ${escapeHtml(o.fill_count_fp || "0")} · ${escapeHtml(o.remaining_count_fp || "0")} left</span></div>`
      )
      .join("");
  });

$("#btn-host").onclick = async () => {
  try {
    const r = await api("/api/settings/host", {
      method: "POST",
      body: JSON.stringify({ host_key: $("#host-key").value }),
    });
    toast("Host → " + r.host);
    refreshStatus();
  } catch (e) {
    toast(e.message);
  }
};
$("#btn-ping").onclick = async () => {
  try {
    const r = await api("/api/ping");
    toast("Exchange active=" + r.exchange_active);
    refreshStatus();
  } catch (e) {
    toast(e.message);
  }
};

$("#btn-unlock").onclick = async () => {
  try {
    await api("/api/vault/unlock", {
      method: "POST",
      body: JSON.stringify({
        passphrase: $("#vault-pass").value,
        totp_code: $("#vault-totp").value || null,
      }),
    });
    toast("App unlocked");
    refreshStatus();
  } catch (e) {
    toast(e.message);
  }
};
$("#btn-lock").onclick = async () => {
  await api("/api/vault/lock", { method: "POST", body: "{}" });
  toast("Locked");
  refreshStatus();
};

$("#btn-save-keys").onclick = async () => {
  try {
    await api("/api/settings/keys", {
      method: "POST",
      body: JSON.stringify({
        key_id: $("#key-id").value,
        pem: $("#key-pem").value,
        passphrase: $("#vault-pass").value || null,
      }),
    });
    toast("Saved on this phone");
    $("#key-pem").value = "";
    refreshStatus();
  } catch (e) {
    toast(e.message);
  }
};
$("#btn-clear-keys").onclick = async () => {
  if (!confirm("Clear local Kalshi keys? This does not revoke the key on Kalshi.com.")) return;
  try {
    await api("/api/settings/keys/clear", {
      method: "POST",
      body: JSON.stringify({ passphrase: $("#vault-pass").value || null }),
    });
    $("#key-id").value = "";
    $("#key-pem").value = "";
    toast("Local keys cleared; revoke on Kalshi.com separately");
    refreshStatus();
  } catch (e) {
    toast(e.message);
  }
};

$("#btn-totp-setup").onclick = async () => {
  try {
    const r = await api("/api/mfa/setup", {
      method: "POST",
      body: JSON.stringify({ passphrase: $("#vault-pass").value || "init" }),
    });
    $("#totp-confirm-secret").value = r.secret;
    $("#totp-setup-out").textContent =
      "Secret: " + r.secret + "\n\nAdd to Aegis:\n" + r.otpauth_uri + "\n\nThen enter a code below to confirm.";
  } catch (e) {
    toast(e.message);
  }
};
$("#btn-totp-confirm").onclick = async () => {
  try {
    await api("/api/mfa/confirm", {
      method: "POST",
      body: JSON.stringify({
        secret: $("#totp-confirm-secret").value,
        code: $("#totp-confirm-code").value,
      }),
    });
    toast("TOTP saved");
  } catch (e) {
    toast(e.message);
  }
};

$("#btn-arm").onclick = async () => {
  try {
    const r = await api("/api/trading/arm", {
      method: "POST",
      body: JSON.stringify({
        confirm_phrase: $("#arm-phrase").value,
        totp_code: $("#arm-totp").value || null,
      }),
    });
    if (!r.ok && $("#safety-out")) $("#safety-out").textContent = r.error || "Could not arm";
    toast(r.ok ? "Real orders are on" : r.error);
    refreshStatus();
  } catch (e) {
    toast(e.message);
  }
};
$("#btn-disarm").onclick = async () => {
  await api("/api/trading/disarm", { method: "POST", body: "{}" });
  toast("Real orders paused");
  refreshStatus();
};

/* installable app */
let deferredInstall = null;
window.addEventListener("beforeinstallprompt", (e) => {
  e.preventDefault();
  deferredInstall = e;
  $("#btn-install")?.classList.remove("hidden");
});
$("#btn-install").onclick = async () => {
  if (!deferredInstall) return toast("Use Chrome menu ⋮ → Add to Home screen");
  deferredInstall.prompt();
  await deferredInstall.userChoice.catch(() => null);
  deferredInstall = null;
  $("#btn-install").classList.add("hidden");
};
if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => navigator.serviceWorker.register("/sw.js").catch(() => null));
}
if (!localStorage.getItem("kalshi_howto_seen")) {
  $("#howto-quick").open = true;
  localStorage.setItem("kalshi_howto_seen", "1");
}

updatePreview();
refreshStatus();
runScan(false);
refreshActivity();
setInterval(refreshStatus, 30000);
