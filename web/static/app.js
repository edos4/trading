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
    const data = await api(`/api/symbols?n=${encodeURIComponent(n)}`);
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
  poll().catch(console.error);
}

/* ── Paper ────────────────────────────────────────────────────────────── */
function initPaper() {
  const status = document.getElementById("paper-status");
  const equity = document.getElementById("paper-equity");
  const exposure = document.getElementById("paper-exposure");
  const scan = document.getElementById("paper-scan");
  const summary = document.getElementById("paper-summary");
  const chart = document.getElementById("paper-equity-chart");
  const startBtn = document.getElementById("paper-start");
  const stopBtn = document.getElementById("paper-stop");
  const streamCheck = document.getElementById("paper-stream");
  const streamDateWrap = document.getElementById("stream-date-wrap");
  const posBody = document.querySelector("#paper-pos tbody");
  const closedBody = document.querySelector("#paper-closed tbody");
  const logsBody = document.querySelector("#paper-logs tbody");

  streamCheck.onchange = () => {
    streamDateWrap.hidden = !streamCheck.checked;
  };

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

  function fmtTime(iso) {
    if (!iso) return "—";
    try {
      return new Date(iso).toLocaleString();
    } catch {
      return iso;
    }
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

  function fmtMoney(n, { signed = false, digits = 0 } = {}) {
    const v = Number(n);
    if (!Number.isFinite(v)) return "—";
    const body = Math.abs(v).toLocaleString(undefined, {
      minimumFractionDigits: digits,
      maximumFractionDigits: digits,
    });
    if (signed) return `${v >= 0 ? "+" : "-"}$${body}`;
    return v < 0 ? `-$${body}` : `$${body}`;
  }

  function fmtSigned(n, digits = 2, suffix = "") {
    const v = Number(n);
    if (!Number.isFinite(v)) return "—";
    return `${v >= 0 ? "+" : ""}${v.toFixed(digits)}${suffix}`;
  }

  function render(s) {
    status.textContent = s.status || "";
    startBtn.disabled = !!s.running;
    stopBtn.disabled = !s.running;
    equity.textContent =
      `Cash: $${Number(s.cash).toLocaleString(undefined, { maximumFractionDigits: 2 })}   ` +
      `Equity: $${Number(s.equity).toLocaleString(undefined, { maximumFractionDigits: 2 })}   ` +
      `Open: ${s.open_count}   Closed: ${s.closed_count}`;
    const exp = s.exposure || {};
    exposure.textContent =
      `Long exposure: ${(exp.long_pct || 0).toFixed(1)}%   ` +
      `Short exposure: ${(exp.short_pct || 0).toFixed(1)}%   ` +
      `Net exposure: ${(exp.net_pct || 0) >= 0 ? "+" : ""}${(exp.net_pct || 0).toFixed(1)}%`;
    const st = s.scan_stats;
    if (!st) {
      scan.textContent = "Last scan: —";
    } else {
      const last = st.last_scan_at
        ? new Date(st.last_scan_at).toLocaleTimeString()
        : "—";
      scan.textContent =
        `Last scan: ${last}   Patterns found: ${st.patterns_found}   ` +
        `Trades opened: ${st.trades_opened}   Rejected: ${st.signals_rejected}   ` +
        `Vol reject: ${st.volume_gate_rejected || 0}   ` +
        `Scan time: ${(st.scan_duration_s || 0).toFixed(1)}s` +
        (s.use_stream ? `   Sim days: ${st.sim_days}` : "");
    }
    summary.textContent = s.summary || "";
    if (s.equity_png_b64) {
      chart.src = `data:image/png;base64,${s.equity_png_b64}`;
      chart.hidden = false;
    }
    posBody.innerHTML = "";
    for (const p of s.positions || []) {
      const tr = document.createElement("tr");
      const cls = p.unrl_pct > 0 ? "gain" : p.unrl_pct < 0 ? "loss" : "";
      tr.innerHTML =
        `<td>${p.symbol}</td><td>${p.status}</td><td>${p.action}</td>` +
        `<td>${fmtQty(p.qty)}</td>` +
        `<td>${Number(p.entry).toFixed(2)}</td><td>${Number(p.current).toFixed(2)}</td>` +
        `<td class="${cls}">${fmtSigned(p.unrl_pct, 2, "%")}</td>` +
        `<td class="${cls}">${fmtMoney(p.mtm, { signed: true })}</td>` +
        `<td>${p.r == null ? "—" : fmtSigned(p.r)}</td>` +
        `<td>${fmtDays(p.days)}</td>` +
        `<td>${fmtMoney(p.value)}</td>` +
        `<td>${p.port_pct == null ? "—" : `${Number(p.port_pct).toFixed(1)}%`}</td>` +
        `<td>${p.pattern}</td>`;
      posBody.appendChild(tr);
    }
    closedBody.innerHTML = "";
    for (const t of s.closed || []) {
      const tr = document.createElement("tr");
      const cls = t.pnl_pct > 0 ? "gain" : t.pnl_pct < 0 ? "loss" : "";
      tr.innerHTML =
        `<td>${t.opened}</td><td>${t.closed}</td><td>${fmtDays(t.days)}</td>` +
        `<td>${t.symbol}</td><td>${t.action}</td><td>${fmtQty(t.qty)}</td>` +
        `<td>${Number(t.entry).toFixed(2)}</td><td>${Number(t.exit).toFixed(2)}</td>` +
        `<td class="${cls}">${fmtSigned(t.pnl_pct, 2, "%")}</td>` +
        `<td class="${cls}">${fmtMoney(t.pnl, { signed: true })}</td>` +
        `<td>${t.r == null ? "—" : fmtSigned(t.r)}</td>` +
        `<td>${t.reason}</td><td>${t.pattern}</td>`;
      closedBody.appendChild(tr);
    }
    logsBody.innerHTML = "";
    for (const row of s.signal_logs || []) {
      const tr = document.createElement("tr");
      const stCls = `status-${row.status || ""}`;
      tr.innerHTML =
        `<td>${fmtTime(row.ts)}</td>` +
        `<td>${row.symbol || ""}</td>` +
        `<td>${row.timeframe || ""}</td>` +
        `<td>${row.action || ""}</td>` +
        `<td>${row.pattern || ""}</td>` +
        `<td>${row.confidence == null ? "—" : Number(row.confidence).toFixed(2)}</td>` +
        `<td>${row.price == null ? "—" : Number(row.price).toFixed(2)}</td>` +
        `<td class="${stCls}">${row.status || ""}</td>` +
        `<td title="${(row.reason || "").replace(/"/g, "&quot;")}">${row.reason || ""}</td>`;
      logsBody.appendChild(tr);
    }
  }

  async function poll() {
    const s = await api("/api/paper/status");
    render(s);
    setTimeout(() => poll().catch(console.error), s.running ? 2000 : 5000);
  }

  document.getElementById("paper-start").onclick = async () => {
    try {
      await api("/api/paper/start", {
        method: "POST",
        body: JSON.stringify({
          n_symbols: Number(document.getElementById("paper-n").value || 100),
          use_stream: document.getElementById("paper-stream").checked,
          kronos_gate: document.getElementById("paper-kronos").checked,
          kronos_rank: document.getElementById("paper-kronos-rank").checked,
          volume_gate: document.getElementById("paper-volume").checked,
          stream_start: document.getElementById("paper-stream-start").value || null,
        }),
      });
    } catch (e) {
      status.textContent = String(e.message || e);
    }
  };
  document.getElementById("paper-stop").onclick = () =>
    api("/api/paper/stop", { method: "POST" }).catch((e) => {
      status.textContent = String(e.message || e);
    });
  document.getElementById("paper-reset").onclick = async () => {
    if (!confirm("Wipe the paper trading account and start fresh?")) return;
    try {
      await api("/api/paper/reset", { method: "POST" });
    } catch (e) {
      status.textContent = String(e.message || e);
    }
  };
  poll().catch(console.error);
}

document.addEventListener("DOMContentLoaded", () => {
  if (window.TB_PAGE === "explorer") initExplorer();
  if (window.TB_PAGE === "backtest") initBacktest();
  if (window.TB_PAGE === "paper") initPaper();
});
