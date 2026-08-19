/* Trading Bot web UI client */

async function api(url, opts = {}) {
  const headers = { ...(opts.headers || {}) };
  if (opts.body != null && !(opts.body instanceof FormData) && !headers["Content-Type"]) {
    headers["Content-Type"] = "application/json";
  }
  const res = await fetch(url, {
    credentials: "same-origin",
    ...opts,
    headers,
  });
  if (res.status === 401) {
    window.location.href = "/login";
    throw new Error("Unauthorized");
  }
  const ct = res.headers.get("content-type") || "";
  const data = ct.includes("application/json") ? await res.json() : await res.text();
  if (!res.ok) {
    let msg = res.statusText;
    if (data && typeof data === "object" && data.detail != null) {
      msg = typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail);
    } else if (typeof data === "string" && data) {
      msg = data;
    }
    throw new Error(msg);
  }
  return data;
}

function downloadText(filename, text, mime) {
  const blob = new Blob([text], { type: mime || "text/plain" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = filename;
  a.click();
  URL.revokeObjectURL(a.href);
}

function downloadB64(filename, b64, mime) {
  const bin = atob(b64);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  const blob = new Blob([bytes], { type: mime });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = filename;
  a.click();
  URL.revokeObjectURL(a.href);
}

/* ── Explorer ─────────────────────────────────────────────────────────── */
function initExplorer() {
  const listEl = document.getElementById("symbol-list");
  const status = document.getElementById("status");
  const header = document.getElementById("header");
  const chart = document.getElementById("chart");
  const placeholder = document.getElementById("chart-placeholder");
  const tbody = document.querySelector("#signals tbody");
  const savePng = document.getElementById("save-png");
  const saveCsv = document.getElementById("save-csv");
  let rows = [];
  let selected = null;
  let lastPayload = null;

  function renderList() {
    const q = (document.getElementById("filter").value || "").trim().toUpperCase();
    listEl.innerHTML = "";
    const filtered = rows.filter((r) => !q || r.symbol.includes(q) || r.exchange.includes(q));
    for (const r of filtered) {
      const li = document.createElement("li");
      li.innerHTML = `<span>${r.symbol}</span><span class="muted">${r.exchange}</span>`;
      if (selected && selected.symbol === r.symbol) li.classList.add("selected");
      li.onclick = () => loadSymbol(r);
      listEl.appendChild(li);
    }
  }

  async function refreshSymbols() {
    status.textContent = "Loading symbols...";
    const n = Number(document.getElementById("count").value || 50);
    const market = (document.getElementById("market") || {}).value || "";
    const data = await api(`/api/symbols?n=${encodeURIComponent(n)}&market=${encodeURIComponent(market)}`);
    rows = data.symbols || [];
    renderList();
    status.textContent = `${rows.length} symbols loaded.`;
  }

  async function loadSymbol(row) {
    selected = row;
    renderList();
    status.textContent = `Loading ${row.symbol}...`;
    header.textContent = `${row.symbol} | loading...`;
    const body = {
      symbol: row.symbol,
      exchange: row.exchange,
      timeframe: document.getElementById("timeframe").value,
      run_patterns: document.getElementById("run-patterns").checked,
      kronos_gate: document.getElementById("kronos-gate").checked,
      volume_gate: document.getElementById("volume-gate").checked,
      market: (document.getElementById("market") || {}).value || "",
    };
    try {
      const data = await api("/api/symbol", {
        method: "POST",
        body: JSON.stringify(body),
      });
      lastPayload = data;
      const o = data.ohlc;
      header.textContent =
        `${data.symbol} | ${data.timeframe} | ${data.exchange}  —  ` +
        `O ${o.open.toFixed(2)}  H ${o.high.toFixed(2)}  L ${o.low.toFixed(2)}  ` +
        `C ${o.close.toFixed(2)}  ${o.change >= 0 ? "+" : ""}${o.change.toFixed(2)} ` +
        `(${o.change_pct >= 0 ? "+" : ""}${o.change_pct.toFixed(2)}%)  bars=${data.bars}`;
      chart.src = `data:image/png;base64,${data.chart_png_b64}`;
      chart.hidden = false;
      placeholder.hidden = true;
      tbody.innerHTML = "";
      for (const s of data.signals) {
        const tr = document.createElement("tr");
        tr.innerHTML =
          `<td>${s.pattern}</td><td>${s.action}</td><td>${s.timeframe}</td>` +
          `<td>${s.confidence.toFixed(2)}</td><td>${s.price.toFixed(2)}</td><td>${s.notes || ""}</td>`;
        tbody.appendChild(tr);
      }
      savePng.disabled = false;
      saveCsv.disabled = false;
      status.textContent = `${data.symbol}: ${data.bars} bars, ${data.signals.length} pattern(s).`;
    } catch (err) {
      status.textContent = String(err.message || err);
      header.textContent = "Error";
    }
  }

  document.getElementById("refresh-symbols").onclick = () => refreshSymbols().catch((e) => {
    status.textContent = String(e.message || e);
  });
  document.getElementById("filter").oninput = renderList;
  document.getElementById("timeframe").onchange = () => {
    if (selected) loadSymbol(selected);
  };
  const marketEl = document.getElementById("market");
  if (marketEl) {
    marketEl.onchange = () => {
      const id = marketEl.value;
      const spec = (window.TB_MARKETS || []).find((m) => m.id === id);
      if (spec) {
        document.getElementById("count").value = spec.default_n_symbols;
        document.getElementById("kronos-gate").checked = !!spec.kronos_gate;
      }
      refreshSymbols().catch((e) => {
        status.textContent = String(e.message || e);
      });
    };
  }
  savePng.onclick = () => {
    if (!lastPayload) return;
    downloadB64(
      `${lastPayload.symbol}_${lastPayload.timeframe}.png`,
      lastPayload.chart_png_b64,
      "image/png",
    );
  };
  saveCsv.onclick = () => {
    if (!lastPayload) return;
    downloadText(
      `${lastPayload.symbol}_${lastPayload.timeframe}.csv`,
      lastPayload.csv,
      "text/csv",
    );
  };
  refreshSymbols().catch((e) => {
    status.textContent = String(e.message || e);
  });
}

/* ── Backtest ─────────────────────────────────────────────────────────── */
function initBacktest() {
  const form = document.getElementById("bt-form");
  const status = document.getElementById("bt-status");
  const progress = document.getElementById("bt-progress");
  const pctEl = document.getElementById("bt-pct");
  const elapsedEl = document.getElementById("bt-elapsed");
  const etaEl = document.getElementById("bt-eta");
  const summary = document.getElementById("bt-summary");
  const abBox = document.getElementById("bt-ab-box");
  const tbody = document.querySelector("#bt-trades tbody");
  const runBtn = document.getElementById("bt-run");
  const abBtn = document.getElementById("bt-ab");
  let pollTimer = null;

  function formPayload() {
    const fd = new FormData(form);
    const obj = {};
    for (const [k, v] of fd.entries()) obj[k] = v;
    // unchecked checkboxes are absent — mark known checks false
    for (const input of form.querySelectorAll('input[type="checkbox"]')) {
      if (!(input.name in obj)) obj[input.name] = false;
      else obj[input.name] = true;
    }
    return obj;
  }

  function renderState(s) {
    progress.value = s.pct || 0;
    pctEl.textContent = s.busy || s.pct ? `${(s.pct || 0).toFixed(0)}%` : "—";
    elapsedEl.textContent = `Elapsed: ${s.elapsed_s ? s.elapsed_s.toFixed(0) + "s" : "—"}`;
    etaEl.textContent =
      s.eta_s != null ? `ETA: ${s.eta_s < 3600 ? s.eta_s.toFixed(0) + "s" : (s.eta_s / 60).toFixed(1) + "m"}` : "ETA: —";
    status.textContent = s.status || "";
    runBtn.disabled = !!s.busy;
    abBtn.disabled = !!s.busy;
    if (s.error) {
      summary.textContent = `ERROR: ${s.error}`;
    } else if (s.result) {
      summary.textContent = s.result.summary || "";
      tbody.innerHTML = "";
      for (const t of s.result.trades || []) {
        const tr = document.createElement("tr");
        const cls = t.pnl_pct > 0 ? "gain" : t.pnl_pct < 0 ? "loss" : "";
        tr.innerHTML =
          `<td>${t.date}</td><td>${t.action}</td><td>${t.symbol}</td><td>${t.tf}</td>` +
          `<td>${t.entry}</td><td>${t.exit}</td>` +
          `<td class="${cls}">${t.pnl_pct > 0 ? "+" : ""}${t.pnl_pct.toFixed(2)}%</td>` +
          `<td>${t.reason}</td><td>${t.pattern}</td>`;
        tbody.appendChild(tr);
      }
    }
    if (s.ab) {
      abBox.hidden = false;
      const keys = Object.keys(s.ab.off || {});
      let text = "Volume gate A/B\n";
      for (const k of keys) {
        text += `${k}: OFF=${s.ab.off[k]}  ON=${s.ab.on[k]}\n`;
      }
      abBox.textContent = text;
    }
  }

  async function poll() {
    const s = await api("/api/backtest/status");
    renderState(s);
    if (s.busy) pollTimer = setTimeout(() => poll().catch(console.error), 1000);
    else pollTimer = null;
  }

  async function start(ab) {
    if (pollTimer) clearTimeout(pollTimer);
    tbody.innerHTML = "";
    abBox.hidden = true;
    summary.textContent = "Running...";
    await api(ab ? "/api/backtest/ab" : "/api/backtest/run", {
      method: "POST",
      body: JSON.stringify(formPayload()),
    });
    await poll();
  }

  runBtn.onclick = () => start(false).catch((e) => {
    status.textContent = String(e.message || e);
  });
  abBtn.onclick = () => start(true).catch((e) => {
    status.textContent = String(e.message || e);
  });
  const marketSel = document.getElementById("p-market");
  if (marketSel) {
    marketSel.addEventListener("change", () => {
      const spec = (window.TB_MARKETS || []).find((m) => m.id === marketSel.value);
      if (!spec) return;
      const setVal = (id, v) => {
        const el = document.getElementById(id);
        if (!el) return;
        if (el.type === "checkbox") el.checked = !!v;
        else el.value = v;
      };
      setVal("p-n_symbols", spec.default_n_symbols);
      setVal("p-txn_cost_pct", spec.txn_cost_pct);
      setVal("p-account_value", spec.account_value);
      setVal("p-kronos_gate", spec.kronos_gate);
      setVal("p-kronos_rank", spec.kronos_rank);
    });
  }
  poll().catch(console.error);
}

function applyBookLamps(envelope) {
  const host = document.getElementById("book-lamps");
  if (!host) return;
  const books = (envelope && envelope.books) || {};
  host.querySelectorAll("[data-book]").forEach((el) => {
    const snap = books[el.dataset.book] || {};
    el.classList.toggle("on", !!snap.running);
  });
}

function initBookLamps() {
  const host = document.getElementById("book-lamps");
  if (!host) return;
  async function tick() {
    try {
      const s = await api("/api/paper/status");
      applyBookLamps(s);
    } catch {
      /* ignore */
    }
    setTimeout(tick, 10000);
  }
  tick();
}

/* ── Paper ────────────────────────────────────────────────────────────── */
function initPaper() {
  const BOOKS = ["us", "ph"];
  const posBody = document.querySelector("#paper-pos tbody");
  const closedBody = document.querySelector("#paper-closed tbody");
  const closedStats = document.getElementById("paper-closed-stats");
  const closedSymbol = document.getElementById("paper-closed-symbol");
  const closedReason = document.getElementById("paper-closed-reason");
  const closedPattern = document.getElementById("paper-closed-pattern");
  const closedSearch = document.getElementById("paper-closed-search");
  const logsBody = document.querySelector("#paper-logs tbody");
  const logSymbol = document.getElementById("paper-log-symbol");
  const logPattern = document.getElementById("paper-log-pattern");
  const logStatus = document.getElementById("paper-log-status");
  const logSearch = document.getElementById("paper-log-search");
  const sortNote = document.getElementById("paper-sort-note");

  const esc = (value) => String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");

  let marketFilter = "all";
  let closedRows = [];
  let closedSort = { col: "closed", desc: true };
  let lastEnvelope = { books: {} };

  BOOKS.forEach((id) => {
    const stream = document.getElementById(`${id}-stream`);
    const wrap = document.getElementById(`${id}-stream-date-wrap`);
    if (stream && wrap) {
      stream.onchange = () => { wrap.hidden = !stream.checked; };
    }
  });

  document.querySelectorAll(".tabs .tab").forEach((btn) => {
    btn.addEventListener("click", () => {
      const name = btn.dataset.tab;
      document.querySelectorAll(".tabs .tab").forEach((b) => {
        const on = b === btn;
        b.classList.toggle("active", on);
        b.setAttribute("aria-selected", on ? "true" : "false");
      });
      document.querySelectorAll(".tab-panel").forEach((panel) => {
        const on = panel.id === `tab-${name}`;
        panel.classList.toggle("active", on);
        panel.hidden = !on;
      });
    });
  });

  document.querySelectorAll(".filter-pills .pill-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      marketFilter = btn.dataset.filter || "all";
      document.querySelectorAll(".filter-pills .pill-btn").forEach((b) => {
        b.classList.toggle("active", b === btn);
      });
      if (sortNote) sortNote.hidden = marketFilter !== "all";
      renderBlotter();
      renderPerf();
    });
  });

  function fmtTime(iso) {
    if (!iso) return "—";
    try { return new Date(iso).toLocaleString(); } catch { return iso; }
  }
  function fmtQty(q) {
    const n = Number(q);
    if (!Number.isFinite(n)) return "—";
    return Number.isInteger(n) ? String(n) : n.toFixed(2);
  }
  function fmtDays(d) {
    const n = Number(d);
    if (!Number.isFinite(n)) return "—";
    if (n < 1) return `${(n * 24).toFixed(1)}h`;
    return `${n.toFixed(1)}d`;
  }
  function fmtMoney(n, { signed = false, digits = 0, symbol = "$" } = {}) {
    const v = Number(n);
    if (!Number.isFinite(v)) return "—";
    const body = Math.abs(v).toLocaleString(undefined, {
      minimumFractionDigits: digits,
      maximumFractionDigits: digits,
    });
    if (signed) return `${v >= 0 ? "+" : "-"}${symbol}${body}`;
    return v < 0 ? `-${symbol}${body}` : `${symbol}${body}`;
  }
  function fmtSigned(n, digits = 2, suffix = "") {
    const v = Number(n);
    if (!Number.isFinite(v)) return "—";
    return `${v >= 0 ? "+" : ""}${v.toFixed(digits)}${suffix}`;
  }
  function fmtStamp(iso) {
    if (!iso) return "—";
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return iso;
    const mm = String(d.getMonth() + 1).padStart(2, "0");
    const dd = String(d.getDate()).padStart(2, "0");
    const hh = String(d.getHours()).padStart(2, "0");
    const mi = String(d.getMinutes()).padStart(2, "0");
    return `${mm}-${dd} ${hh}:${mi}`;
  }
  function fmtPattern(name) {
    const raw = String(name || "").trim();
    if (!raw) return "—";
    const m = raw.match(/^pattern_(\d+)_(.+)$/);
    if (m) return `${m[1]} ${m[2].replace(/_/g, " ")}`;
    return raw.replace(/_/g, " ");
  }
  const REASON_LABELS = {
    stop_loss: "Stop", take_profit: "Target", trailing_stop: "Trail",
    time_exit: "Time", breakeven_stop: "BE",
  };
  function fmtReason(t) {
    const raw = String(t.reason || "").trim();
    if (!raw) return "—";
    const label = REASON_LABELS[raw] || raw.replace(/_/g, " ");
    if (raw === "time_exit" && t.time_exit_bars_elapsed != null) {
      const conf = t.time_exit_bars_configured;
      return conf != null ? `${label} ${t.time_exit_bars_elapsed}/${conf}b` : `${label} ${t.time_exit_bars_elapsed}b`;
    }
    return label;
  }
  function reasonClass(reason) {
    if (reason === "stop_loss") return "reason-loss";
    if (reason === "take_profit" || reason === "trailing_stop") return "reason-gain";
    if (reason === "breakeven_stop") return "reason-flat";
    return "reason-muted";
  }
  function fmtHold(days, bars) {
    const parts = [];
    if (Number.isFinite(Number(days))) {
      const d = Number(days);
      parts.push(d < 1 ? `${(d * 24).toFixed(1)}h` : `${d.toFixed(1)}d`);
    }
    if (bars != null && Number.isFinite(Number(bars))) parts.push(`${bars}b`);
    return parts.join(" · ") || "—";
  }

  function visibleBooks() {
    const books = lastEnvelope.books || {};
    if (marketFilter === "all") return BOOKS.map((id) => books[id]).filter(Boolean);
    return books[marketFilter] ? [books[marketFilter]] : [];
  }

  function bookStartBody(id) {
    return {
      market: id,
      n_symbols: Number(document.getElementById(`${id}-n`).value || 50),
      extra_symbols: (document.getElementById(`${id}-extra`) || {}).value || "",
      use_stream: !!(document.getElementById(`${id}-stream`) || {}).checked,
      kronos_gate: !!(document.getElementById(`${id}-kronos`) || {}).checked,
      kronos_rank: !!(document.getElementById(`${id}-kronos-rank`) || {}).checked,
      volume_gate: !!(document.getElementById(`${id}-volume`) || {}).checked,
      stream_start: (document.getElementById(`${id}-stream-start`) || {}).value || null,
    };
  }

  function renderClock(id, snap, clock) {
    const c = clock || {};
    const timeEl = document.getElementById(`clock-${id}-time`);
    const sessEl = document.getElementById(`clock-${id}-session`);
    const lamp = document.getElementById(`lamp-${id}`);
    if (timeEl) timeEl.textContent = c.local_time || snap.local_time || "—";
    if (sessEl) {
      sessEl.textContent = c.session || snap.session || "—";
      sessEl.classList.toggle("open", !!(c.session_open || snap.session_open));
    }
    if (lamp) {
      lamp.dataset.running = snap.running ? "true" : "false";
      lamp.textContent = snap.running ? "running" : "stopped";
    }
  }

  function renderCard(id, snap) {
    const card = document.querySelector(`.book-card[data-book="${id}"]`);
    const startBtn = document.querySelector(`.book-start[data-book="${id}"]`);
    const stopBtn = document.querySelector(`.book-stop[data-book="${id}"]`);
    const stream = document.getElementById(`${id}-stream`);
    const sym = snap.currency_symbol || "$";
    const equityEl = document.getElementById(`${id}-equity`);
    const exposureEl = document.getElementById(`${id}-exposure`);
    const scanEl = document.getElementById(`${id}-scan`);
    const statusEl = document.getElementById(`${id}-status`);
    const totalPnl = Number(snap.metrics?.total_pnl_dollars || 0);
    const totalPnlPct = Number(snap.metrics?.total_pnl_pct || 0);
    if (equityEl) {
      equityEl.textContent =
        `Cash: ${fmtMoney(snap.cash, { digits: 2, symbol: sym })}   ` +
        `Equity: ${fmtMoney(snap.equity, { digits: 2, symbol: sym })}   ` +
        `Total P&L: ${fmtMoney(totalPnl, { signed: true, symbol: sym })} (${fmtSigned(totalPnlPct, 2, "%")})   ` +
        `Open: ${snap.open_count}   Closed: ${snap.closed_count}`;
    }
    const exp = snap.exposure || {};
    if (exposureEl) {
      const shortBit = snap.long_only
        ? `Short: 0% · long-only`
        : `Short: ${(exp.short_pct || 0).toFixed(1)}%`;
      exposureEl.textContent =
        `Long: ${(exp.long_pct || 0).toFixed(1)}%   ${shortBit}   ` +
        `Net: ${(exp.net_pct || 0) >= 0 ? "+" : ""}${(exp.net_pct || 0).toFixed(1)}%   ` +
        `Gross: ${(exp.gross_pct || 0).toFixed(1)}%   ` +
        `Realized: ${fmtMoney(snap.metrics?.realized_pnl_dollars || 0, { signed: true, symbol: sym })}   ` +
        `Unrealized: ${fmtMoney(snap.metrics?.unrealized_pnl_dollars || 0, { signed: true, symbol: sym })}`;
    }
    const st = snap.scan_stats;
    if (scanEl) {
      if (!st) scanEl.textContent = "Last scan: —";
      else {
        const last = st.last_scan_at ? new Date(st.last_scan_at).toLocaleTimeString() : "—";
        const rejectGates = Object.entries(st.rejection_by_gate || {})
          .sort((a, b) => Number(b[1]) - Number(a[1]))
          .slice(0, 4)
          .map(([k, v]) => `${k}=${v}`)
          .join(", ") || "none";
        scanEl.textContent =
          `Last scan: ${last}   Patterns found: ${st.patterns_found}   ` +
          `Trades opened: ${st.trades_opened}   Rejected: ${st.signals_rejected}   ` +
          `Scan time: ${(st.scan_duration_s || 0).toFixed(1)}s` +
          (snap.use_stream ? `   Sim days: ${st.sim_days}` : "") +
          `   Reject gates: ${rejectGates}`;
      }
    }
    if (statusEl) statusEl.textContent = snap.error || snap.status || "";
    if (startBtn) startBtn.disabled = !!snap.running;
    if (stopBtn) stopBtn.disabled = !snap.running;
    if (card) card.classList.toggle("session-idle", !!snap.session_idle);
    if (stream) {
      const blocked = snap.stream_blocked_by;
      stream.disabled = !!blocked;
      if (blocked) stream.title = `Stream in use by ${String(blocked).toUpperCase()}`;
      else stream.title = "";
    }
  }

  function closedSortValue(t, col) {
    if (col === "market") return String(t.market || "");
    if (col === "closed") return Date.parse(t.closed || "") || 0;
    if (col === "symbol") return String(t.symbol || "");
    if (col === "action") return String(t.action || "");
    if (col === "qty") return Number(t.qty) || 0;
    if (col === "entry") return Number(t.entry) || 0;
    if (col === "exit") return Number(t.exit) || 0;
    if (col === "pnl") return Number(t.pnl) || 0;
    if (col === "pnl_pct") return Number(t.pnl_pct) || 0;
    if (col === "r") return t.r == null ? Number.NEGATIVE_INFINITY : Number(t.r);
    if (col === "reason") return String(t.reason || "");
    if (col === "hold") return Number(t.days) || 0;
    if (col === "pattern") return String(t.pattern || "");
    return 0;
  }

  function closedBookStats(rows, symbol) {
    const n = rows.length;
    if (!n) return "No closed trades yet.";
    const dollars = (t) => Number(t.pnl) || 0;
    const wins = rows.filter((t) => dollars(t) > 0).length;
    const last = rows.slice().sort((a, b) => (Date.parse(b.closed || "") || 0) - (Date.parse(a.closed || "") || 0)).slice(0, 10);
    const lastSum = last.reduce((s, t) => s + dollars(t), 0);
    return `${n} closed · ${wins}/${n} wins · last 10 ${fmtMoney(lastSum, { signed: true, symbol })}`;
  }

  function renderClosed() {
    if (!closedBody) return;
    let rows = closedRows.slice();
    const sf = (closedSymbol?.value || "").trim().toLowerCase();
    const rf = (closedReason?.value || "").trim();
    const pf = (closedPattern?.value || "").trim().toLowerCase();
    const q = (closedSearch?.value || "").trim().toLowerCase();
    rows = rows.filter((t) => {
      if (sf && !String(t.symbol || "").toLowerCase().includes(sf)) return false;
      if (rf && t.reason !== rf) return false;
      if (pf && !String(t.pattern || "").toLowerCase().includes(pf)) return false;
      if (q) {
        const hay = [t.symbol, t.action, t.reason, t.pattern].map((x) => String(x || "")).join(" ").toLowerCase();
        if (!hay.includes(q)) return false;
      }
      return true;
    });
    let sortCol = closedSort.col;
    if (marketFilter === "all" && (sortCol === "pnl" || sortCol === "pnl_pct")) sortCol = "closed";
    rows.sort((a, b) => {
      const av = closedSortValue(a, sortCol);
      const bv = closedSortValue(b, sortCol);
      if (av < bv) return closedSort.desc ? 1 : -1;
      if (av > bv) return closedSort.desc ? -1 : 1;
      return 0;
    });
    const vis = visibleBooks();
    if (closedStats) {
      if (!rows.length && !closedRows.length) {
        const anyRunning = vis.some((b) => b.running);
        closedStats.textContent = anyRunning
          ? "No closed trades yet."
          : "No closed trades yet. Start US, PH, or both.";
      } else if (marketFilter === "all") {
        closedStats.textContent = `${closedRows.length} closed across books (showing ${rows.length}).`;
      } else {
        const book = vis[0];
        closedStats.textContent = closedBookStats(closedRows, book?.currency_symbol || "$");
      }
    }
    closedBody.innerHTML = "";
    if (!rows.length) {
      const tr = document.createElement("tr");
      tr.innerHTML = `<td class="empty" colspan="13">${esc(
        closedRows.length ? "No trades match filters." : "No open or closed trades. Start US, PH, or both."
      )}</td>`;
      closedBody.appendChild(tr);
      return;
    }
    rows.forEach((t) => {
      const cls = t.pnl > 0 ? "gain" : t.pnl < 0 ? "loss" : "";
      const tr = document.createElement("tr");
      tr.classList.add(`book-${t.market || "us"}`);
      tr.classList.toggle("gain-row", cls === "gain");
      tr.classList.toggle("loss-row", cls === "loss");
      const srcIdx = (lastEnvelope.books[t.market]?.closed || []).indexOf(t);
      const idx = srcIdx < 0 ? closedRows.indexOf(t) : srcIdx;
      const sym = (lastEnvelope.books[t.market] || {}).currency_symbol || "$";
      tr.innerHTML =
        `<td><span class="pill mkt-${esc(t.market)}">${esc((t.market || "").toUpperCase())}</span></td>` +
        `<td>${esc(fmtStamp(t.closed))}</td>` +
        `<td>${esc(t.symbol)}</td>` +
        `<td><span class="pill ${t.action === "SELL" ? "side-sell" : "side-buy"}">${esc(t.action)}</span></td>` +
        `<td class="num">${fmtQty(t.qty)}</td>` +
        `<td class="num">${Number(t.entry).toFixed(2)}</td>` +
        `<td class="num">${Number(t.exit).toFixed(2)}</td>` +
        `<td class="num ${cls}">${fmtMoney(t.pnl, { signed: true, symbol: sym })}</td>` +
        `<td class="num ${cls}">${fmtSigned(t.pnl_pct, 2, "%")}</td>` +
        `<td class="num">${t.r == null ? "—" : fmtSigned(t.r)}</td>` +
        `<td><span class="pill ${reasonClass(t.reason)}">${esc(fmtReason(t))}</span></td>` +
        `<td class="num">${esc(fmtHold(t.days, t.bars))}</td>` +
        `<td class="muted" title="${esc(t.pattern)}">${esc(fmtPattern(t.pattern))}</td>`;
      tr.addEventListener("dblclick", () => openTradeChart("closed", t.market, t.symbol, idx));
      closedBody.appendChild(tr);
    });
  }

  function renderPositions() {
    if (!posBody) return;
    posBody.innerHTML = "";
    const rows = [];
    for (const book of visibleBooks()) {
      const sym = book.currency_symbol || "$";
      for (const p of book.positions || []) {
        rows.push({ ...p, market: p.market || book.market, _sym: sym });
      }
    }
    if (!rows.length) {
      const tr = document.createElement("tr");
      const anyRunning = visibleBooks().some((b) => b.running) || Object.values(lastEnvelope.books || {}).some((b) => b.running);
      tr.innerHTML = `<td class="empty" colspan="15">${
        anyRunning ? "No open positions." : "No open positions. Start US, PH, or both."
      }</td>`;
      posBody.appendChild(tr);
      return;
    }
    for (const p of rows) {
      const tr = document.createElement("tr");
      const cls = p.unrl_pct > 0 ? "gain" : p.unrl_pct < 0 ? "loss" : "";
      tr.classList.add(`book-${p.market || "us"}`);
      tr.title = "Double-click to open chart";
      tr.innerHTML =
        `<td><span class="pill mkt-${esc(p.market)}">${esc((p.market || "").toUpperCase())}</span></td>` +
        `<td>${esc(p.symbol)}</td><td>${esc(p.status)}</td><td>${esc(p.action)}</td>` +
        `<td>${fmtQty(p.qty)}</td>` +
        `<td>${Number(p.entry).toFixed(2)}</td><td>${Number(p.current).toFixed(2)}</td>` +
        `<td class="${cls}">${fmtSigned(p.unrl_pct, 2, "%")}</td>` +
        `<td class="${cls}">${fmtMoney(p.mtm, { signed: true, symbol: p._sym })}</td>` +
        `<td>${p.r == null ? "—" : fmtSigned(p.r)}</td>` +
        `<td>${fmtDays(p.days)}</td><td>${p.bars == null ? "—" : p.bars}</td>` +
        `<td>${fmtMoney(p.value, { symbol: p._sym })}</td>` +
        `<td>${p.port_pct == null ? "—" : `${Number(p.port_pct).toFixed(1)}%`}</td>` +
        `<td>${esc(p.pattern)}</td>`;
      tr.addEventListener("dblclick", () => openTradeChart("open", p.market, p.symbol));
      posBody.appendChild(tr);
    }
  }

  function renderLogs() {
    if (!logsBody) return;
    logsBody.innerHTML = "";
    const lfSymbol = (logSymbol?.value || "").trim().toLowerCase();
    const lfPattern = (logPattern?.value || "").trim().toLowerCase();
    const lfStatus = (logStatus?.value || "").trim().toLowerCase();
    const lfSearch = (logSearch?.value || "").trim().toLowerCase();
    const rows = [];
    for (const book of visibleBooks()) {
      for (const row of book.signal_logs || []) rows.push({ ...row, market: row.market || book.market });
    }
    const logRows = rows.filter((row) => {
      if (lfSymbol && !String(row.symbol || "").toLowerCase().includes(lfSymbol)) return false;
      if (lfPattern && !String(row.pattern || "").toLowerCase().includes(lfPattern)) return false;
      if (lfStatus && String(row.status || "").toLowerCase() !== lfStatus) return false;
      if (lfSearch) {
        const hay = ["symbol", "timeframe", "action", "pattern", "status", "reason"]
          .map((k) => String(row[k] || "")).join(" ").toLowerCase();
        if (!hay.includes(lfSearch)) return false;
      }
      return true;
    });
    for (const row of logRows) {
      const tr = document.createElement("tr");
      tr.classList.add(`book-${row.market || "us"}`);
      const stCls = `status-${row.status || ""}`;
      tr.innerHTML =
        `<td><span class="pill mkt-${esc(row.market)}">${esc((row.market || "").toUpperCase())}</span></td>` +
        `<td>${esc(fmtTime(row.ts))}</td>` +
        `<td>${esc(row.sim_bar ? fmtTime(row.sim_bar) : "—")}</td>` +
        `<td>${esc(row.symbol)}</td>` +
        `<td>${esc(row.timeframe)}</td>` +
        `<td>${esc(row.action)}</td>` +
        `<td>${esc(row.pattern)}</td>` +
        `<td>${row.confidence == null ? "—" : Number(row.confidence).toFixed(2)}</td>` +
        `<td>${row.price == null ? "—" : Number(row.price).toFixed(2)}</td>` +
        `<td class="${stCls}">${esc(row.status)}</td>` +
        `<td title="${esc(row.reason)}">${esc(row.reason)}</td>`;
      logsBody.appendChild(tr);
    }
  }

  function renderPerfBook(id, snap) {
    const metricsEl = document.getElementById(`paper-metrics-${id}`);
    const summary = document.getElementById(`paper-summary-${id}`);
    const chart = document.getElementById(`paper-equity-chart-${id}`);
    const panel = document.querySelector(`[data-perf="${id}"]`);
    if (panel) panel.hidden = marketFilter !== "all" && marketFilter !== id;
    const m = snap.metrics || {};
    const sym = snap.currency_symbol || "$";
    if (metricsEl) {
      const exits = Object.entries(m.exit_reason_breakdown || {})
        .map(([k, v]) => `${k}=${v}`).join(", ") || "—";
      metricsEl.textContent = snap.closed_count
        ? [
            `Total P&L: ${fmtMoney(m.total_pnl_dollars, { signed: true, symbol: sym })} (${fmtSigned(m.total_pnl_pct, 2, "%")})`,
            `Realized: ${fmtMoney(m.realized_pnl_dollars, { signed: true, symbol: sym })}   Unrealized: ${fmtMoney(m.unrealized_pnl_dollars, { signed: true, symbol: sym })}`,
            `Avg R: ${fmtSigned(m.avg_r)}   Median R: ${fmtSigned(m.median_r)}   Avg hold: ${Number(m.avg_hold_bars || 0).toFixed(1)} bars`,
            `Max DD: ${fmtSigned(m.max_drawdown_pct, 2, "%")}   Sharpe: ${fmtSigned(m.sharpe_ratio)}`,
            `Exit reasons: ${exits}`,
          ].join("\n")
        : "No closed trades yet.";
    }
    if (summary) summary.textContent = snap.summary || "No closed trades yet.";
    if (chart) {
      if (snap.equity_png_b64) {
        chart.src = `data:image/png;base64,${snap.equity_png_b64}`;
        chart.hidden = false;
      } else {
        chart.hidden = true;
      }
    }
  }

  function renderPerf() {
    const books = lastEnvelope.books || {};
    BOOKS.forEach((id) => renderPerfBook(id, books[id] || {}));
  }

  function renderBlotter() {
    const books = lastEnvelope.books || {};
    closedRows = [];
    for (const book of visibleBooks()) {
      closedRows.push(...(book.closed || []));
    }
    renderPositions();
    renderClosed();
    renderLogs();
  }

  function render(envelope) {
    lastEnvelope = envelope || { books: {} };
    const clocks = envelope.clocks || {};
    const books = envelope.books || {};
    BOOKS.forEach((id) => {
      const snap = books[id] || {};
      renderClock(id, snap, clocks[id]);
      renderCard(id, snap);
    });
    applyBookLamps(envelope);
    renderBlotter();
    renderPerf();
  }

  let pollTimer = null;

  async function refresh() {
    const s = await api("/api/paper/status");
    render(s);
    return s;
  }

  async function poll() {
    const s = await refresh();
    const running = Object.values(s.books || {}).some((b) => b.running);
    if (pollTimer) clearTimeout(pollTimer);
    pollTimer = setTimeout(() => poll().catch(console.error), running ? 2000 : 5000);
  }

  const chartModal = document.getElementById("paper-chart-modal");
  const chartTitle = document.getElementById("paper-chart-title");
  const chartOhlc = document.getElementById("paper-chart-ohlc");
  const chartStatus = document.getElementById("paper-chart-status");
  const chartHost = document.getElementById("paper-chart-host");

  function fmtTvOhlc(data) {
    const o = data && data.ohlc;
    if (!o) return "";
    const sign = o.change >= 0 ? "+" : "";
    return (
      `O ${Number(o.open).toFixed(2)}  H ${Number(o.high).toFixed(2)}  ` +
      `L ${Number(o.low).toFixed(2)}  C ${Number(o.close).toFixed(2)}  ` +
      `${sign}${Number(o.change).toFixed(2)} (${sign}${Number(o.change_pct).toFixed(2)}%)`
    );
  }

  function closeTradeChart() {
    if (!chartModal) return;
    chartModal.hidden = true;
    if (window.TVChart) window.TVChart.unmount();
    if (chartOhlc) chartOhlc.textContent = "";
  }

  async function openTradeChart(side, market, symbol, index) {
    if (!chartModal) return;
    chartModal.hidden = false;
    if (chartTitle) chartTitle.textContent = `${(market || "").toUpperCase()} ${symbol || "Chart"}`;
    if (chartOhlc) chartOhlc.textContent = "";
    if (chartStatus) {
      chartStatus.hidden = false;
      chartStatus.textContent = "Loading…";
    }
    const params = new URLSearchParams({ side, market: market || "" });
    if (symbol) params.set("symbol", symbol);
    if (index != null) params.set("index", String(index));
    try {
      const data = await api(`/api/paper/chart?${params.toString()}`);
      if (chartTitle) chartTitle.textContent = data.title || symbol || "Chart";
      if (chartOhlc) {
        chartOhlc.textContent = fmtTvOhlc(data);
        chartOhlc.classList.toggle("gain", Number(data.ohlc && data.ohlc.change) >= 0);
        chartOhlc.classList.toggle("loss", Number(data.ohlc && data.ohlc.change) < 0);
      }
      if (window.TVChart && chartHost) {
        window.TVChart.mount(chartHost, data, {
          onCandle(bar) {
            if (!chartOhlc) return;
            if (!bar) {
              chartOhlc.textContent = fmtTvOhlc(data);
              chartOhlc.classList.toggle("gain", Number(data.ohlc && data.ohlc.change) >= 0);
              chartOhlc.classList.toggle("loss", Number(data.ohlc && data.ohlc.change) < 0);
              return;
            }
            const prev = (data.candles || []).findIndex((c) => c.time === bar.time);
            const prevClose = prev > 0 ? data.candles[prev - 1].close : bar.open;
            const change = bar.close - prevClose;
            const pct = prevClose ? (change / prevClose) * 100 : 0;
            const sign = change >= 0 ? "+" : "";
            chartOhlc.textContent =
              `${bar.time}  O ${Number(bar.open).toFixed(2)}  H ${Number(bar.high).toFixed(2)}  ` +
              `L ${Number(bar.low).toFixed(2)}  C ${Number(bar.close).toFixed(2)}  ` +
              `${sign}${change.toFixed(2)} (${sign}${pct.toFixed(2)}%)`;
            chartOhlc.classList.toggle("gain", change >= 0);
            chartOhlc.classList.toggle("loss", change < 0);
          },
        });
      }
      if (chartStatus) {
        chartStatus.textContent = "";
        chartStatus.hidden = true;
      }
    } catch (e) {
      if (chartStatus) {
        chartStatus.hidden = false;
        chartStatus.textContent = String(e.message || e);
      }
    }
  }

  document.getElementById("paper-chart-close")?.addEventListener("click", closeTradeChart);
  document.getElementById("paper-chart-backdrop")?.addEventListener("click", closeTradeChart);
  document.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape") closeTradeChart();
  });

  document.querySelectorAll(".book-start").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const id = btn.dataset.book;
      try {
        await api("/api/paper/start", { method: "POST", body: JSON.stringify(bookStartBody(id)) });
        await poll();
      } catch (e) {
        const statusEl = document.getElementById(`${id}-status`);
        if (statusEl) statusEl.textContent = String(e.message || e);
      }
    });
  });
  document.querySelectorAll(".book-stop").forEach((btn) => {
    btn.addEventListener("click", async () => {
      try {
        await api("/api/paper/stop", {
          method: "POST",
          body: JSON.stringify({ market: btn.dataset.book }),
        });
        await poll();
      } catch (e) {
        const statusEl = document.getElementById(`${btn.dataset.book}-status`);
        if (statusEl) statusEl.textContent = String(e.message || e);
      }
    });
  });
  document.querySelectorAll(".book-reset").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const id = btn.dataset.book;
      if (!confirm(`Wipe the ${id.toUpperCase()} paper account and signal log?`)) return;
      try {
        await api("/api/paper/reset", { method: "POST", body: JSON.stringify({ market: id }) });
        await poll();
      } catch (e) {
        const statusEl = document.getElementById(`${id}-status`);
        if (statusEl) statusEl.textContent = String(e.message || e);
      }
    });
  });
  document.getElementById("paper-start-both")?.addEventListener("click", async () => {
    try {
      await api("/api/paper/start-both", {
        method: "POST",
        body: JSON.stringify({ us: bookStartBody("us"), ph: bookStartBody("ph") }),
      });
      await poll();
    } catch (e) {
      console.error(e);
    }
  });
  document.getElementById("paper-stop-both")?.addEventListener("click", async () => {
    try {
      await api("/api/paper/stop", { method: "POST", body: JSON.stringify({ market: "all" }) });
      await poll();
    } catch (e) {
      console.error(e);
    }
  });

  document.querySelectorAll("#paper-closed th.sortable").forEach((th) => {
    th.addEventListener("click", () => {
      const col = th.dataset.col;
      if (closedSort.col === col) closedSort.desc = !closedSort.desc;
      else {
        closedSort.col = col;
        closedSort.desc = true;
      }
      renderClosed();
    });
  });
  [closedSymbol, closedPattern, closedSearch].forEach((el) => {
    el?.addEventListener("input", renderClosed);
  });
  closedReason?.addEventListener("change", renderClosed);
  [logSymbol, logPattern, logSearch].forEach((el) => {
    el?.addEventListener("input", renderLogs);
  });
  logStatus?.addEventListener("change", renderLogs);

  document.getElementById("paper-reset-logs")?.addEventListener("click", async () => {
    const m = marketFilter === "us" || marketFilter === "ph" ? marketFilter : "all";
    const label = m === "all" ? "US and PH" : m.toUpperCase();
    if (!confirm(`Clear the ${label} signal log file? Paper account stays.`)) return;
    try {
      await api("/api/paper/reset-logs", {
        method: "POST",
        body: JSON.stringify({ market: m }),
      });
      if (logsBody) logsBody.innerHTML = "";
      await poll();
    } catch (e) {
      window.alert(String(e.message || e));
    }
  });

  document.getElementById("paper-export-trades")?.addEventListener("click", async () => {
    const m = marketFilter === "us" || marketFilter === "ph" ? marketFilter : "all";
    try {
      const data = await api(`/api/paper/export?market=${encodeURIComponent(m)}`);
      const stamp = new Date().toISOString().replace(/[:.]/g, "").slice(0, 15);
      downloadText(
        `paper_trades_${m}_${stamp}.json`,
        JSON.stringify(data, null, 2),
        "application/json",
      );
    } catch (e) {
      window.alert(String(e.message || e));
    }
  });

  poll().catch(console.error);
}

document.addEventListener("DOMContentLoaded", () => {
  initBookLamps();
  if (window.TB_PAGE === "explorer") initExplorer();
  if (window.TB_PAGE === "backtest") initBacktest();
  if (window.TB_PAGE === "paper") initPaper();
});

