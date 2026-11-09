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
$$("#nav button[data-panel]").forEach((btn) => {
  btn.addEventListener("click", () => {
    showPanel(btn.dataset.panel);
    $("#nav").classList.remove("more-open");
    $("#nav-more-btn").setAttribute("aria-expanded", "false");
    $("#nav-more-btn").classList.toggle("active", btn.classList.contains("nav-more-item"));
  });
});

/* state shared by the bet form */
let lastScan = null;
let currentContract = null;
let currentMode = { paper: true, armed: false };
let scanFilter = "all";

/* ---------- ARM LIVE strip (operator console, screen 4) ---------- */
let lastStatus = null;
let stripBankroll = null;

function todayPnl() {
  const today = new Date().toDateString();
  return allBets
    .filter((b) => b.outcome != null && b.ts && new Date(b.ts).toDateString() === today)
    .reduce((t, b) => t + Number(b.pnl || 0), 0);
}

function paintArmStrip() {
  const s = lastStatus;
  if (!s) return;
  const gate = s.order_gate || {};
  const armed = Boolean(gate.live_armed) && !s.dry_run;
  const mode = $("#strip-mode");
  mode.textContent = armed ? "LIVE · REAL MONEY" : s.dry_run ? "DRY RUN · paper" : "LIVE READY · not armed";
  mode.className = "arm-mode " + (armed ? "live" : s.dry_run ? "paper" : "ready");
  document.body.classList.toggle("live-armed", armed);
  $("#strip-bankroll").textContent = stripBankroll == null ? (s.has_keys ? "locked" : "no key") : "$" + Number(stripBankroll).toFixed(2);
  const p = todayPnl();
  $("#strip-pnl").textContent = (p < 0 ? "−$" : "$") + Math.abs(p).toFixed(2);
  $("#strip-pnl").className = p < 0 ? "neg" : p > 0 ? "pos" : "";
  $("#strip-arm-phrase").classList.toggle("hidden", armed);
  $("#strip-arm").classList.toggle("hidden", armed);
  $("#strip-arm-totp").classList.toggle("hidden", armed || !s.totp_configured);
  $("#strip-disarm").classList.toggle("hidden", !armed);
  $("#strip-arm").title = s.dry_run ? "Allow live trading in Settings first" : "Arms real orders for this session";
}

async function refreshStripMoney() {
  if (lastStatus && lastStatus.has_keys) {
    try {
      const b = await api("/api/account/balance");
      stripBankroll = b.balance_dollars ?? null;
    } catch {
      stripBankroll = null;
    }
  }
  try {
    allBets = (await api("/bets")).bets || allBets;
  } catch {}
  paintArmStrip();
}

$("#strip-arm-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  if (lastStatus && lastStatus.dry_run) {
    toast("Allow live trading in Settings first");
    openSettings("#arm-card");
    return;
  }
  try {
    const r = await api("/api/trading/arm", {
      method: "POST",
      body: JSON.stringify({
        confirm_phrase: $("#strip-arm-phrase").value,
        totp_code: $("#strip-arm-totp").value || null,
      }),
    });
    toast(r.ok ? "Real orders are on" : r.error || "Could not arm");
    $("#strip-arm-phrase").value = "";
    $("#strip-arm-totp").value = "";
    refreshStatus();
  } catch (e) {
    toast(e.message);
  }
});
$("#strip-disarm").addEventListener("click", async () => {
  await api("/api/trading/disarm", { method: "POST", body: "{}" });
  toast("Real orders paused");
  refreshStatus();
});
setInterval(() => document.visibilityState === "visible" && refreshStripMoney(), 30000);

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
    lastStatus = s;
    paintMode(s);
    paintArmStrip();
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

/* ---------- Scanner table (operator console, screen 1) ---------- */
const VIRTUAL_AFTER = 50; // window rows once the list is longer than this
const OVERSCAN = 6;
let scanRows = [];
let scanWeights = null;
let expandedTicker = null;

function isPhone() {
  return window.matchMedia("(max-width: 600px)").matches;
}
function baseRowH() {
  return isPhone() ? 128 : 48;
}
function detailH() {
  return isPhone() ? 600 : 440;
}
function rowH(m) {
  return baseRowH() + (m.ticker === expandedTicker ? detailH() : 0);
}
function pct(p) {
  return p == null || !Number.isFinite(Number(p)) ? "—" : (Number(p) * 100).toFixed(1) + "%";
}
function edgePP(m) {
  const md = m.model || {};
  if (md.p_yes == null || md.market_p_yes == null) return null;
  return (Number(md.p_yes) - Number(md.market_p_yes)) * 100;
}

const MODEL_FEATURES = ["market_edge", "time_to_close", "volume_z", "espn_delta", "spread_z", "flow_z"];

function featureBreakdown(m) {
  const md = m.model || {};
  const f = md.features || {};
  const w = scanWeights || {};
  // Order = w1..wN on the server (app/strategy/model.py FEATURES).
  const names = MODEL_FEATURES;
  let z = Number(w.w0 || 0);
  const lines = names.map((n, i) => {
    const wi = Number(w["w" + (i + 1)] || 0);
    const x = Number(f[n] || 0);
    z += wi * x;
    const nul = f[n] == null ? " <small>(none → 0)</small>" : "";
    return `<tr><td>${n}${nul}</td><td>${x.toFixed(3)}</td><td>${wi.toFixed(3)}</td><td>${(wi * x).toFixed(3)}</td></tr>`;
  });
  const p = 1 / (1 + Math.exp(-z));
  return `<div class="feat" style="height:${detailH()}px">
    <table class="feat-table"><thead><tr><th>input</th><th>x</th><th>w</th><th>w·x</th></tr></thead>
    <tbody><tr><td>w0 (bias)</td><td>1</td><td>${Number(w.w0 || 0).toFixed(3)}</td><td>${Number(w.w0 || 0).toFixed(3)}</td></tr>
    ${lines.join("")}</tbody></table>
    <p class="mono feat-sum">z = ${z.toFixed(3)} → p = ${(p * 100).toFixed(1)}% · mispricing ${
      m.mispricing == null ? "— (not in top 10 by volume)" : (m.mispricing * 100).toFixed(2) + "pp"
    }${m.model_uncalibrated ? " · Kelly halved (uncalibrated)" : ""}</p>
    ${gemBreakdown(m)}
  </div>`;
}

function gemBadge(m) {
  const g = m.gemini;
  if (!g) return "";
  const tier = `<span class="tier t${g.payout_tier}">${g.payout_tier === "13x" ? "13x+" : g.payout_tier} · ${Number(g.payout_odds).toFixed(1)}x</span>`;
  const gem = m.gem
    ? `<span class="gem-badge${m.gem_status === "unverified" ? " unverified" : ""}" title="Payout anomaly: high payout, tight spread, Nash > 0.4, model above market, positive half-Kelly. A screen for odd prices, not a sure thing${
        m.model_uncalibrated ? " — model is not calibrated yet" : ""
      }.">◆ GEM${m.gem_status === "unverified" ? "?" : ""}</span>`
    : "";
  return gem + tier;
}

function gemBreakdown(m) {
  const g = m.gemini;
  if (!g) return "";
  const ck = g.checks || {};
  const label = {
    high_payout_tier: "13x+ tier",
    tight_spread: "spread ≤ 3¢",
    nash_go: "Nash > 0.4",
    beats_implied: "p ≥ implied + 2pp",
    positive_half_kelly: "½-Kelly > 0",
  };
  const fmt = (v, d = 4) => (v == null ? "—" : Number(v).toFixed(d));
  return `<div class="gem-box">
    <p class="gem-head">Gemini anomaly check (${String(m.best_side || "").toUpperCase()} side) ${
      m.gem ? `<span class="gem-badge">◆ GEM${m.gem_status === "unverified" ? "? · unverified: model not calibrated yet" : ""}</span>` : ""
    }</p>
    <table class="feat-table"><tbody>
      <tr><td>spread (¢)</td><td>${fmt(g.spread_cents, 2)}</td><td>spread_compression</td><td>${fmt(g.spread_compression)}</td></tr>
      <tr><td>vol_oi_ratio</td><td>${fmt(g.vol_oi_ratio, 3)}</td><td>implied_prob</td><td>${fmt(g.implied_prob)}</td></tr>
      <tr><td>flow_z</td><td>${fmt(g.flow_z, 3)}</td><td>unusual flow (≥3x OI)</td><td>${g.unusual_flow ? "yes" : "no"}</td></tr>
      <tr><td>payout_odds</td><td>${fmt(g.payout_odds, 2)}x</td><td>payout_tier</td><td>${escapeHtml(g.payout_tier)}</td></tr>
      <tr><td>model p</td><td>${fmt(g.p)}</td><td>edge vs implied</td><td>${(g.edge_vs_implied * 100).toFixed(1)}pp</td></tr>
      <tr><td>nash_payoff</td><td>${fmt(g.nash_payoff)}</td><td>½-Kelly</td><td>${(g.half_kelly_fraction * 100).toFixed(2)}% · $${fmt(g.half_kelly_allocation, 2)}</td></tr>
    </tbody></table>
    <div class="gem-checks">${Object.keys(label)
      .map((k) => `<span class="${ck[k] ? "ok" : "no"}">${ck[k] ? "✓" : "✗"} ${label[k]}</span>`)
      .join("")}</div>
    ${m.gem_suppressed ? `<p class="gem-note">✗ one gem per event: ${escapeHtml(m.gem_suppressed.reason || "")}</p>` : ""}
    ${analogLine(m)}
  </div>`;
}

function analogLine(m) {
  const a = m.analogs;
  if (!a) return `<p class="gem-note muted">Analogs: no settled history loaded.</p>`;
  const cls = a.analog_status === "ok" ? "" : " muted";
  return `<p class="gem-note${cls}">${escapeHtml(a.analog_text || "")} <small>(tier ${escapeHtml(
    (m.gemini && m.gemini.payout_tier) || ""
  )}, min ${a.analog_min_n}; does not verify a gem)</small></p>`;
}

function scanRowHtml(m) {
  const e = edgePP(m);
  const edgeCls = e == null ? "" : e > 0.05 ? "pos" : e < -0.05 ? "neg" : "flat";
  const g = m.geo || { allowed: true };
  const stake = m.kelly && m.kelly.allocation_usd != null ? "$" + Number(m.kelly.allocation_usd).toFixed(2) : "—";
  const geoCell = g.allowed
    ? `<span class="geo ok" title="${g.sports ? "Sports allowed in " + escapeHtml(g.state || "") : "Not a sports market"}">${
        g.sports ? "OK " + escapeHtml(g.state || "") : "OK"
      }</span>`
    : `<span class="geo blocked" title="${escapeHtml(g.reason || "Blocked")}">BLOCKED${g.state ? " " + escapeHtml(g.state) : ""}</span>`;
  return `
    <div class="scan-main">
      <span class="c-ticker" data-label="Ticker"><span class="mono">${escapeHtml(m.ticker || "")}</span><small>${gemBadge(m)} ${escapeHtml(
        m.title || ""
      )}</small></span>
      <span data-label="Mkt P">${pct(m.model && m.model.market_p_yes)}</span>
      <span data-label="Model P">${pct(m.model && m.model.p_yes)}</span>
      <span data-label="Nash P">${pct(m.nash_prob)}</span>
      <span data-label="Edge" class="edge ${edgeCls}">${e == null ? "—" : (e > 0 ? "+" : "") + e.toFixed(1) + "pp"}</span>
      <span data-label="Kelly $">${stake}</span>
      <span data-label="Geo">${geoCell}</span>
      <span class="c-buy" ${g.allowed ? "" : `title="${escapeHtml(g.reason || "Blocked")}"`}><button type="button" class="btn small buy-btn"
        data-ticker="${escapeHtml(m.ticker || "")}" ${
          g.allowed ? "" : `disabled title="${escapeHtml(g.reason || "Blocked")}" aria-label="Buy blocked: ${escapeHtml(g.reason || "Blocked")}"`
        }>Buy</button></span>
    </div>
    ${m.ticker === expandedTicker ? featureBreakdown(m) : ""}`;
}

function paintScanWindow() {
  const body = $("#market-list");
  if (!body) return;
  const rows = scanRows;
  const offsets = new Array(rows.length + 1);
  offsets[0] = 0;
  for (let i = 0; i < rows.length; i++) offsets[i + 1] = offsets[i] + rowH(rows[i]);
  body.style.height = offsets[rows.length] + "px";
  let start = 0;
  let end = rows.length;
  if (rows.length > VIRTUAL_AFTER) {
    const top = body.getBoundingClientRect().top + window.scrollY;
    const viewTop = window.scrollY - top;
    const viewBot = viewTop + window.innerHeight;
    let lo = 0;
    let hi = rows.length;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (offsets[mid + 1] < viewTop) lo = mid + 1;
      else hi = mid;
    }
    start = Math.max(0, lo - OVERSCAN);
    end = start;
    while (end < rows.length && offsets[end] < viewBot) end++;
    end = Math.min(rows.length, end + OVERSCAN);
  }
  const html = [];
  for (let i = start; i < end; i++) {
    const m = rows[i];
    const g = m.geo || { allowed: true };
    html.push(
      `<div class="scan-row${g.allowed ? "" : " is-blocked"}${m.ticker === expandedTicker ? " open" : ""}" role="row" data-i="${i}"
        style="top:${offsets[i]}px;height:${rowH(m)}px">${scanRowHtml(m)}</div>`
    );
  }
  body.innerHTML = html.join("");
}

let scanPaintQueued = false;
function queueScanPaint() {
  if (scanPaintQueued) return;
  scanPaintQueued = true;
  requestAnimationFrame(() => {
    scanPaintQueued = false;
    paintScanWindow();
  });
}
window.addEventListener("scroll", () => scanRows.length > VIRTUAL_AFTER && queueScanPaint(), { passive: true });
window.addEventListener("resize", queueScanPaint);

$("#market-list").addEventListener("click", (ev) => {
  const buy = ev.target.closest(".buy-btn");
  const rowEl = ev.target.closest(".scan-row");
  if (!rowEl) return;
  const m = scanRows[Number(rowEl.dataset.i)];
  if (!m) return;
  if (buy) {
    ev.stopPropagation();
    if (buy.disabled) return;
    openContract(m.ticker, m);
    return;
  }
  expandedTicker = expandedTicker === m.ticker ? null : m.ticker;
  paintScanWindow();
});

function renderMarkets(data) {
  scanWeights = data.model_weights || scanWeights;
  const meta = [
    `Updated ${fmtTs(data.updated_at)}`,
    data.from_cache ? "cached" : "live",
    `${data.count}/${data.scanned} markets`,
    `sort ${data.sort || "mispricing"}`,
    `model v${data.model_version ?? 0}`,
  ];
  if (data.model_uncalibrated) meta.push("model uncalibrated · Kelly halved");
  const gems = (data.markets || []).filter((m) => m.gem).length;
  meta.push(`${gems} gem candidate${gems === 1 ? "" : "s"}`);
  if (data.espn) meta.push(`ESPN ${data.espn.matched || 0} matched (${data.espn.live || 0} live)`);
  if (data.geo) meta.push(`geo ${data.geo.state || "unknown"} · ${data.geo.blocked || 0} blocked`);
  // Server order is the ranking (mispricing first); filters only hide rows.
  scanRows = (data.markets || []).filter(matchesFilter);
  if (scanFilter !== "all") meta.push(`showing ${scanRows.length} (${filterLabel(scanFilter)})`);
  $("#scan-meta").textContent = meta.join(" · ") + " — " + (data.disclaimer || "");
  if (!scanRows.length) {
    $("#market-list").style.height = "auto";
    $("#market-list").innerHTML = `<div class="card muted">No markets match “${escapeHtml(
      filterLabel(scanFilter)
    )}” right now. Tap All, or scan again closer to game time.</div>`;
    return;
  }
  paintScanWindow();
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
    case "gem":
      return Boolean(m.gem);
    default:
      return true;
  }
}

function filterLabel(f) {
  return (
    { all: "All", espn: "ESPN matched", live: "ESPN live now", sports: "Sports games", go: "GO only", gem: "Gems" }[f] || f
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

let scanInFlight = false;
async function runScan(force = false, quiet = false) {
  if (scanInFlight) return;
  scanInFlight = true;
  const limit = parseInt($("#scan-limit").value || "80", 10);
  if (!quiet) toast(force ? "Force refreshing…" : "Scanning…");
  try {
    const data = await api(`/scanner?limit=${limit}&refresh=${force ? "true" : "false"}`);
    lastScan = data;
    renderMarkets(data);
    if (!quiet) toast(`Scan done · ${data.count} markets`);
  } catch (e) {
    if (!quiet) toast("Scan failed: " + e.message);
    $("#scan-meta").textContent = "Error: " + e.message;
  } finally {
    scanInFlight = false;
  }
}

function panelVisible(name) {
  const el = $("#panel-" + name);
  return document.visibilityState === "visible" && el && el.classList.contains("active");
}
// Poll every 5 s while the scanner is on screen (server caches for CACHE_TTL_SECONDS).
setInterval(() => panelVisible("scanner") && runScan(false, true), 5000);

/* ---------- Geo gate on the Bet panel (operator console, screen 5) ---------- */
let contractGeo = null;
function applyContractGeo() {
  const btn = $("#btn-paper");
  const note = $("#order-geo-note");
  const blocked = contractGeo && contractGeo.allowed === false;
  btn.disabled = Boolean(blocked);
  btn.title = blocked ? contractGeo.reason || "Blocked in your state" : "";
  note.textContent = blocked ? "⛔ " + (contractGeo.reason || "Blocked in your state") : "";
  note.classList.toggle("hidden", !blocked);
}

function openContract(ticker, preview) {
  contractGeo = (preview && preview.geo) || null;
  applyContractGeo();
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
    contractGeo = data.geo || null;
    applyContractGeo();
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

/* ---------- Calibration (operator console, screen 2) ---------- */
function calibSvg(buckets) {
  const W = 340, H = 260, L = 38, R = 10, T = 12, B = 34;
  const pw = W - L - R, ph = H - T - B;
  const x = (v) => L + v * pw;
  const y = (v) => T + (1 - v) * ph;
  const parts = [];
  for (let i = 0; i <= 10; i += 2) {
    const v = i / 10;
    parts.push(`<line x1="${L}" x2="${W - R}" y1="${y(v)}" y2="${y(v)}" class="grid"/>`);
    parts.push(`<text x="${L - 6}" y="${y(v) + 3}" class="ax" text-anchor="end">${i * 10}%</text>`);
  }
  buckets.forEach((b, i) => {
    const cx = x((i + 0.5) / 10);
    if (i % 2 === 0) parts.push(`<text x="${cx}" y="${H - B + 14}" class="ax" text-anchor="middle">${b.bucket}</text>`);
  });
  parts.push(`<text x="${L + pw / 2}" y="${H - 4}" class="ax" text-anchor="middle">model probability bucket</text>`);
  parts.push(`<line x1="${x(0)}" y1="${y(0)}" x2="${x(1)}" y2="${y(1)}" class="diag"/>`);
  const series = (key, cls) => {
    const pts = buckets
      .map((b, i) => (b.n && b[key] != null ? [x((i + 0.5) / 10), y(Number(b[key])), b] : null))
      .filter(Boolean);
    if (pts.length > 1) parts.push(`<polyline class="${cls}" points="${pts.map((p) => p[0] + "," + p[1]).join(" ")}"/>`);
    pts.forEach((p) =>
      parts.push(`<circle class="${cls}-dot" cx="${p[0]}" cy="${p[1]}" r="${Math.min(7, 3 + Math.log2(1 + p[2].n))}"><title>${
        p[2].bucket
      }: ${key} ${(Number(p[2][key]) * 100).toFixed(1)}% (n=${p[2].n})</title></circle>`)
    );
  };
  series("predicted", "pred");
  series("actual", "act");
  return parts.join("");
}

async function refreshCalibration() {
  try {
    const c = await api("/calibration");
    $("#calib-brier").textContent = c.brier == null ? "—" : Number(c.brier).toFixed(4);
    const banner = $("#calib-banner");
    if (c.brier != null && c.brier > c.threshold) {
      banner.textContent = `Model uncalibrated: Brier ${Number(c.brier).toFixed(3)} > ${c.threshold}. Kelly is halved on every scan.`;
      banner.className = "calib-banner bad";
    } else if (c.brier == null) {
      banner.textContent = "No settled bets yet, so the model is treated as uncalibrated and Kelly is halved.";
      banner.className = "calib-banner warn";
    } else {
      banner.className = "calib-banner hidden";
    }
    $("#calib-meta").textContent = `${c.n} settled bet${c.n === 1 ? "" : "s"} · threshold ${c.threshold} · updates every 30 s`;
    $("#calib-svg").innerHTML = calibSvg(c.buckets || []);
    $("#calib-table").innerHTML = (c.buckets || [])
      .map(
        (b) => `<div class="cb"><span>${b.bucket}%</span><span>pred ${b.predicted == null ? "—" : (b.predicted * 100).toFixed(1) + "%"}</span><span>act ${
          b.actual == null ? "—" : (b.actual * 100).toFixed(1) + "%"
        }</span><span>n ${b.n}</span></div>`
      )
      .join("");
  } catch (e) {
    $("#calib-meta").textContent = "Error: " + e.message;
  }
}
setInterval(() => panelVisible("calibration") && refreshCalibration(), 30000);
$$('#nav button[data-panel="calibration"]').forEach((b) => b.addEventListener("click", refreshCalibration));

/* ---------- Bet log (operator console, screen 3) ---------- */
let allBets = [];

function sparkSvg(values) {
  if (!values.length) return `<text x="150" y="34" text-anchor="middle" class="ax">no settled bets yet</text>`;
  const W = 300, H = 60, P = 4;
  const series = [0, ...values];
  const lo = Math.min(0, ...series), hi = Math.max(0, ...series);
  const span = hi - lo || 1;
  const x = (i) => P + (i / (series.length - 1)) * (W - 2 * P);
  const y = (v) => H - P - ((v - lo) / span) * (H - 2 * P);
  const last = series[series.length - 1];
  return `<line x1="0" x2="${W}" y1="${y(0)}" y2="${y(0)}" class="zero"/>
    <polyline class="${last >= 0 ? "up" : "down"}" points="${series.map((v, i) => x(i) + "," + y(v)).join(" ")}"/>`;
}

function betMatches(b) {
  const o = $("#bets-outcome").value;
  const t = $("#bets-ticker").value.trim().toUpperCase();
  if (o === "open" && b.outcome != null) return false;
  if (o === "won" && b.outcome !== 1) return false;
  if (o === "lost" && b.outcome !== 0) return false;
  if (t && !String(b.ticker || "").toUpperCase().includes(t)) return false;
  return true;
}

function renderBets() {
  const rows = allBets.filter(betMatches);
  const settled = rows.filter((b) => b.outcome != null).sort((a, b) => String(a.ts).localeCompare(String(b.ts)));
  let run = 0;
  const curve = settled.map((b) => (run += Number(b.pnl || 0)));
  $("#bets-pnl").textContent = (run < 0 ? "−$" : "$") + Math.abs(run).toFixed(2);
  $("#bets-pnl").className = "big-number " + (run < 0 ? "neg" : "pos");
  $("#bets-spark").innerHTML = sparkSvg(curve);
  $("#bets-meta").textContent = `${rows.length} bet(s) shown · ${rows.length - settled.length} open · ${settled.length} settled`;
  const list = $("#bets-list");
  if (!rows.length) {
    list.innerHTML = `<div class="card muted">No bets yet. Paper bets you place from the Bet screen show up here.</div>`;
    return;
  }
  list.innerHTML = rows
    .slice()
    .sort((a, b) => String(b.ts).localeCompare(String(a.ts)))
    .map((b) => {
      const status =
        b.outcome == null ? `<span class="chip">OPEN</span>` : b.outcome ? `<span class="chip go">WON</span>` : `<span class="chip nogo">LOST</span>`;
      const pnl = b.pnl == null ? "" : `<span class="mono ${b.pnl < 0 ? "neg" : "pos"}">${b.pnl < 0 ? "−" : "+"}$${Math.abs(b.pnl).toFixed(2)}</span>`;
      const mp = b.model_prob == null ? "—" : (b.model_prob * 100).toFixed(1) + "%";
      const settle =
        b.outcome == null
          ? `<div class="row settle-row"><button class="btn small" type="button" data-settle="${b.id}" data-result="yes">Settle: YES won</button>
             <button class="btn small ghost" type="button" data-settle="${b.id}" data-result="no">Settle: NO won</button></div>`
          : "";
      return `<div class="bet-item">
        <div class="bet-top">${status}<span class="mono ticker">${escapeHtml(b.ticker)}</span>${pnl}</div>
        <div class="bet-stats mono">
          <span>${String(b.side).toUpperCase()} @ ${Math.round(b.price * 100)}¢</span>
          <span>stake $${Number(b.stake).toFixed(2)}</span>
          <span title="Model probability this side wins, recorded at fill">model ${mp}</span>
          <span title="Market price implied probability">mkt ${b.market_prob == null ? "—" : (b.market_prob * 100).toFixed(1) + "%"}</span>
        </div>
        <div class="muted small-ts">#${b.id} · ${fmtTs(b.ts)}</div>
        ${settle}
      </div>`;
    })
    .join("");
}

async function refreshBets() {
  try {
    const r = await api("/bets");
    allBets = r.bets || [];
    renderBets();
    if (typeof paintArmStrip === "function") paintArmStrip();
  } catch (e) {
    $("#bets-meta").textContent = "Error: " + e.message;
  }
}

$("#bets-list").addEventListener("click", async (ev) => {
  const btn = ev.target.closest("[data-settle]");
  if (!btn) return;
  const id = btn.dataset.settle;
  const result = btn.dataset.result;
  if (!confirm(`Settle bet #${id} as ${result.toUpperCase()} won?`)) return;
  btn.disabled = true;
  try {
    const r = await api(`/api/bets/${id}/settle`, { method: "POST", body: JSON.stringify({ result }) });
    toast(`Bet #${id} settled · P&L ${r.bet && r.bet.pnl != null ? r.bet.pnl.toFixed(2) : "—"}`);
    await refreshBets();
  } catch (e) {
    toast("Settle failed: " + e.message);
    btn.disabled = false;
  }
});
$("#btn-bets-auto").addEventListener("click", async () => {
  try {
    const r = await api("/api/bets/settle", { method: "POST" });
    toast(`Kalshi results: ${(r.settled || []).length} bet(s) settled`);
    await refreshBets();
  } catch (e) {
    toast("Check failed: " + e.message);
  }
});
$("#bets-outcome").addEventListener("change", renderBets);
$("#bets-ticker").addEventListener("input", renderBets);
$$('#nav button[data-panel="bets"]').forEach((b) => b.addEventListener("click", refreshBets));

/* ---------- Backtest (operator console, screen 6) ---------- */
let lastBacktest = null;

function money(v) {
  if (v == null) return "—";
  return (v < 0 ? "−$" : "$") + Math.abs(Number(v)).toFixed(2);
}

function btColumn(label, side, other) {
  const r = side.report || {};
  const pnl = r.pnl || {};
  const better = (a, b, lowerIsBetter) =>
    a == null || b == null || a === b ? "" : (lowerIsBetter ? a < b : a > b) ? "better" : "worse";
  const wrows = Object.keys(side.weights || {})
    .sort()
    .map((k) => {
      const v = Number(side.weights[k]);
      const o = other && other.weights ? Number(other.weights[k]) : null;
      const d = o == null || label === "Current" ? "" : `<small class="${v - o >= 0 ? "pos" : "neg"}">${v - o >= 0 ? "+" : ""}${(v - o).toFixed(3)}</small>`;
      return `<tr><td>${k}</td><td>${v.toFixed(4)}</td><td>${d}</td></tr>`;
    })
    .join("");
  const o = (other && other.report) || {};
  return `<div class="card bt-col">
    <h3>${label} weights</h3>
    <div class="bt-metrics">
      <div><small>Brier</small><b class="${label === "Proposed" ? better(r.brier, o.brier, true) : ""}">${r.brier ?? "—"}</b></div>
      <div><small>Hit rate</small><b class="${label === "Proposed" ? better(r.hit_rate, o.hit_rate, false) : ""}">${
        r.hit_rate == null ? "—" : (r.hit_rate * 100).toFixed(1) + "%"
      }</b></div>
      <div><small>P&amp;L @ ½ Kelly</small><b class="${(pnl.total || 0) < 0 ? "neg" : "pos"}">${money(pnl.total)}</b></div>
      <div><small>Bets / wins</small><b>${pnl.bets ?? 0} / ${pnl.wins ?? 0}</b></div>
    </div>
    <svg class="spark" viewBox="0 0 300 60" preserveAspectRatio="none">${sparkSvg(pnl.curve || [])}</svg>
    <table class="feat-table"><thead><tr><th>weight</th><th>value</th><th>Δ</th></tr></thead><tbody>${wrows}</tbody></table>
    <details class="muted"><summary>Hit rate by bucket</summary>
      <div class="calib-table">${(r.buckets || [])
        .filter((b) => b.n)
        .map(
          (b) => `<div class="cb"><span>${b.bucket}%</span><span>n ${b.n}</span><span>hit ${(b.hit_rate * 100).toFixed(0)}%</span><span>act ${(
            b.actual * 100
          ).toFixed(0)}%</span></div>`
        )
        .join("")}</div>
    </details>
  </div>`;
}

async function runBacktest() {
  $("#bt-meta").textContent = "Running…";
  try {
    const r = await api("/backtest");
    lastBacktest = r;
    const n = (r.current && r.current.report && r.current.report.n) || 0;
    const fixture = r.source !== "ledger";
    $("#bt-meta").textContent = `${n} rows from ${fixture ? "the test fixture (no settled history yet)" : "your settled bets"} · gate ${
      r.gate_pass ? "PASS" : "FAIL"
    }${fixture ? " · Promote is off until you have real settled bets" : ""}`;
    $("#bt-grid").innerHTML = btColumn("Current", r.current, r.proposed) + btColumn("Proposed", r.proposed, r.current);
    const promote = $("#btn-promote");
    promote.disabled = !(r.gate_pass && !fixture);
    promote.title = fixture ? "Needs settled bets, not the fixture" : r.gate_pass ? "Write proposed weights to data/model_weights.json" : "Gate failed: Brier would get worse";
  } catch (e) {
    $("#bt-meta").textContent = "Error: " + e.message;
  }
}
$("#btn-backtest").addEventListener("click", runBacktest);

/* ---------- settled-market training (folded into Backtest + Calibration) ---------- */

function trainLine(t) {
  const lt = t.last_training;
  if (!lt) return "No settled-market refit yet.";
  const b = (v) => (v == null ? "—" : Number(v).toFixed(4));
  return `Last refit ${fmtTs(lt.ts)} · ${lt.n_markets} markets (${lt.n_train} train / ${lt.n_holdout} holdout rows) · holdout Brier ${b(
    lt.holdout_brier_current
  )} → ${b(lt.holdout_brier_proposed)} (book ${b(lt.holdout_brier_market)}) · gate ${lt.gate_pass ? "PASS" : "FAIL"} · ${
    lt.promoted ? "promoted v" + lt.version : "not promoted"
  }`;
}

function paintTraining(t) {
  const s = t.settled || {};
  const lt = t.last_training || {};
  const c = t.calibration || {};
  $("#train-metrics").innerHTML = `
    <div><small>Settled markets</small><b>${s.markets ?? 0}</b></div>
    <div><small>Snapshots</small><b>${s.snapshots ?? 0}</b></div>
    <div><small>Holdout Brier</small><b class="${lt.promoted ? "better" : ""}">${
      lt.holdout_brier_proposed == null ? "—" : Number(lt.holdout_brier_proposed).toFixed(4)
    }</b></div>
    <div><small>Gems</small><b class="${t.gems_verified ? "pos" : "neg"}">${t.gems_verified ? "verified" : "◆ GEM? unverified"}</b></div>`;
  $("#train-meta").textContent = `${trainLine(t)} · ${lt.reason || ""} · model v${t.model_version} · ${t.model_runs} model_runs · calibration n ${
    c.n ?? 0
  }/${c.min_n ?? "—"}`;
  const cl = $("#calib-train");
  if (cl) cl.textContent = `Training: ${s.markets ?? 0} settled markets · ${trainLine(t)}. Calibration (settled bets) still decides ◆ GEM vs ◆ GEM?.`;
}

async function refreshTraining() {
  try {
    paintTraining(await api("/api/training"));
  } catch (e) {
    $("#train-meta").textContent = "Error: " + e.message;
  }
}

async function trainAction(path, body, label) {
  $("#train-meta").textContent = label;
  try {
    const r = await api(path, { method: "POST", body: body ? JSON.stringify(body) : undefined });
    paintTraining(r.status);
    const rec = r.record || {};
    toast(rec.promoted ? `Promoted v${rec.version}: holdout Brier ${rec.holdout_brier_current} → ${rec.holdout_brier_proposed}` : `Not promoted: ${rec.reason || "no record"}`);
  } catch (e) {
    $("#train-meta").textContent = "Error: " + e.message;
  }
}
$("#btn-train-refit").addEventListener("click", () => trainAction("/api/training/refit", null, "Refitting…"));
$("#btn-train-ingest").addEventListener("click", () => {
  if (!confirm("Pull up to 100 settled markets from the last 7 days (public API, ~2–3 min) and refit?")) return;
  trainAction("/api/training/ingest", { days: 7, per_day: 15, max_markets: 100, refit: true }, "Pulling settled markets… this takes a few minutes.");
});
$$('#nav button[data-panel="backtest"], #nav button[data-panel="calibration"]').forEach((b) => b.addEventListener("click", refreshTraining));
$("#btn-promote").addEventListener("click", async () => {
  if (!lastBacktest || !confirm("Write the proposed weights? The scanner uses them from the next scan.")) return;
  try {
    const r = await api("/backtest/promote", { method: "POST" });
    toast(`Promoted weights v${r.version} (Brier ${r.brier_before} → ${r.brier_after})`);
    runBacktest();
  } catch (e) {
    toast("Not promoted: " + e.message);
  }
});

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
    applyContractGeo();
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
refreshStatus().then(refreshStripMoney);
runScan(false);
refreshActivity();
setInterval(refreshStatus, 30000);

// Keep the sticky scanner header just below the ARM strip, whatever its wrapped height.
function syncStripHeight() {
  const h = $("#arm-strip").getBoundingClientRect().height;
  document.documentElement.style.setProperty("--strip-h", Math.ceil(h) + "px");
}
window.addEventListener("resize", syncStripHeight);
syncStripHeight();

// Phones: bottom bar shows Markets / Calib / Bets / Backtest / More; More opens the rest.
$("#nav-more-btn").addEventListener("click", () => {
  const open = $("#nav").classList.toggle("more-open");
  $("#nav-more-btn").setAttribute("aria-expanded", String(open));
});
window.matchMedia("(max-width: 600px)").addEventListener("change", () => {
  expandedTicker = null;
  queueScanPaint();
  syncStripHeight();
});
