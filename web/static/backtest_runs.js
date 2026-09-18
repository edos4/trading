/* Pinned-version / stream backtest panel for /backtest.
 *
 * Talks to the durable shared-service endpoints: submit returns a run id, the
 * page polls by that id, and a reload reconnects because the job record (not
 * this page) is the authority. Validation lives server-side; this file only
 * builds the request and renders the response.
 */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const state = { runId: null, timer: null };
  const SPIN_KEYS = new Set([
    "session_count", "warmup_bars", "initial_capital", "position_notional",
    "txn_cost_pct", "slippage_pct", "collect_first",
  ]);

  function esc(value) {
    return String(value === null || value === undefined ? "" : value)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  async function api(path, options) {
    const resp = await fetch(path, Object.assign({ credentials: "same-origin" }, options || {}));
    let body = null;
    try { body = await resp.json(); } catch (err) { body = null; }
    if (!resp.ok) {
      const detail = (body && (body.detail || body.error)) || ("HTTP " + resp.status);
      throw new Error(typeof detail === "string" ? detail : "Request failed");
    }
    return body;
  }

  function replayValues() {
    const out = {};
    ["session_count", "warmup_bars", "initial_capital", "position_notional",
     "txn_cost_pct", "slippage_pct", "collect_first", "start_date", "end_date",
     "sizing_mode", "end_policy"].forEach((key) => {
      const el = $("sr-" + key) || $("p-" + key);
      if (!el) return;
      out[key] = SPIN_KEYS.has(key) ? Number(el.value || 0) : el.value;
    });
    ["pattern_only", "volume_gate", "kronos_gate", "kronos_rank"].forEach((key) => {
      const el = $("sr-" + key);
      if (el) out[key] = !!el.checked;
    });
    return out;
  }

  function selectedVersions() {
    const select = $("sr-versions");
    if (!select) return null;
    const chosen = {};
    Array.from(select.selectedOptions).forEach((opt) => { chosen[opt.dataset.pattern] = opt.value; });
    return Object.keys(chosen).length ? chosen : null;
  }

  function requestBody() {
    const extra = $("p-extra_symbols") ? $("p-extra_symbols").value : "";
    const universe = $("p-universe") ? $("p-universe").value : "";
    const market = $("p-market") ? $("p-market").value : (window.TB_DEFAULT_MARKET || "us");
    const presetId = $("sr-preset") ? $("sr-preset").value : "";
    const body = Object.assign({
      mode: $("sr-mode").value,
      market: market || "us",
      symbols: String(extra || "").replace(/,/g, " ").trim(),
      universe: universe || null,
      versions: selectedVersions(),
      preset_id: presetId || null,
      preset_name: $("sr-preset-name") ? $("sr-preset-name").value : null,
    }, replayValues());
    return body;
  }

  function renderMetrics(payload) {
    if (!payload) return;
    const metrics = payload.metrics || {};
    const lines = [
      "Run " + payload.run_id,
      "Mode: " + (payload.mode || "?"),
      "State: " + payload.state,
      "End policy: " + (payload.end_policy || "?"),
      "Sessions/units: " + payload.completed_units + " / " + (payload.total_units || "?"),
      "",
      "Trades:      " + (metrics.trade_count === undefined ? "?" : metrics.trade_count),
      "Zero trades: " + (payload.zero_trades ? "yes (a valid result, not a failure)" : "no"),
      "Win rate:    " + (metrics.win_rate === null || metrics.win_rate === undefined ? "n/a" : (100 * metrics.win_rate).toFixed(1) + "%"),
      "Realized:    " + (metrics.realized_pnl === undefined ? "?" : metrics.realized_pnl),
      "Unrealized:  " + (metrics.unrealized_pnl === undefined ? "?" : metrics.unrealized_pnl),
      "Net P&L:     " + (metrics.net_pnl === undefined ? "?" : metrics.net_pnl),
      "Fees:        " + (metrics.fees === undefined ? "?" : metrics.fees),
      "Max DD %:    " + (metrics.max_drawdown_pct === undefined ? "?" : metrics.max_drawdown_pct),
      "Open:        " + (metrics.open_position_count === undefined ? "?" : metrics.open_position_count),
      "Currency:    " + (metrics.currency || "?"),
    ];
    $("sr-metrics").textContent = lines.join("\n");
    const tbody = $("sr-open").querySelector("tbody");
    tbody.innerHTML = "";
    (payload.open_positions || []).forEach((p) => {
      const row = document.createElement("tr");
      row.innerHTML = ["symbol", "pattern", "action", "entry_price", "qty", "unrealized_pct", "unrealized_usd"]
        .map((k) => "<td>" + esc(p[k]) + "</td>").join("");
      tbody.appendChild(row);
    });
  }

  function renderTrades(trades) {
    const tbody = $("bt-trades").querySelector("tbody");
    tbody.innerHTML = "";
    (trades || []).forEach((t) => {
      const row = document.createElement("tr");
      [t.entryDate, t.action, t.sym, t.timeframe, t.entryPrice, t.exitPrice,
       t.pnlPct, t.exitReason, t.pattern].forEach((v) => {
        const td = document.createElement("td");
        td.textContent = v === null || v === undefined ? "" : v;
        row.appendChild(td);
      });
      tbody.appendChild(row);
    });
  }

  function setProgress(payload) {
    const total = payload.total_units || 0;
    const done = payload.completed_units || 0;
    const pct = total ? Math.min(100, (done / total) * 100) : 0;
    $("sr-progress").value = pct;
    $("sr-pct").textContent = Math.round(pct) + "%";
    $("sr-state").textContent = payload.state + (payload.cancel_requested ? " (cancelling…)" : "");
    $("sr-run-id").textContent = payload.run_id ? "run " + payload.run_id : "";
  }

  function stopPolling() {
    if (state.timer) { clearInterval(state.timer); state.timer = null; }
  }

  function terminal(payload) {
    return ["completed", "failed", "cancelled", "blocked", "interrupted"].indexOf(payload.state) >= 0;
  }

  async function poll() {
    if (!state.runId) return;
    try {
      const payload = await api("/api/backtest/runs/" + encodeURIComponent(state.runId));
      setProgress(payload);
      const error = $("sr-error");
      if (payload.error) {
        error.hidden = false;
        error.textContent = "[" + payload.error.code + "] " + payload.error.message +
          (payload.error.retryable ? " (retryable)" : "");
      } else {
        error.hidden = true;
      }
      if (payload.result) {
        payload.result.run_id = payload.run_id;
        payload.result.state = payload.state;
        renderMetrics(payload.result);
        renderTrades(payload.result.trades);
      }
      if (terminal(payload)) stopPolling();
    } catch (err) {
      stopPolling();
      $("sr-state").textContent = "Error: " + err.message;
      $("sr-run-id").textContent = "Error: " + err.message;
    }
  }

  async function loadCatalog() {
    try {
      const data = await api("/api/backtest/catalog");
      const select = $("sr-versions");
      select.innerHTML = "";
      (data.patterns || []).filter((p) => p.enabled).forEach((p) => {
        const opt = document.createElement("option");
        opt.value = p.default_version_id;
        opt.dataset.pattern = p.pattern_id;
        opt.textContent = p.pattern_id + " → " + p.default_version_id.slice(0, 8);
        opt.selected = true;
        select.appendChild(opt);
      });
    } catch (err) {
      $("sr-state").textContent = "Catalog unavailable: " + err.message;
    }
  }

  async function loadPresets() {
    try {
      const data = await api("/api/backtest/presets");
      const select = $("sr-preset");
      select.innerHTML = '<option value="">(new preset from form)</option>';
      (data.presets || []).forEach((p) => {
        const opt = document.createElement("option");
        opt.value = p.preset_id;
        opt.textContent = p.name + " (" + p.settings.mode + ")";
        select.appendChild(opt);
      });
    } catch (err) {
      /* presets are optional; the run will surface the same error */
    }
  }

  async function startRun() {
    $("sr-error").hidden = true;
    $("sr-state").textContent = "Submitting…";
    try {
      const body = requestBody();
      if (!body.start_date) body.start_date = window.TB_STREAM_START || null;
      const data = await api("/api/backtest/runs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      state.runId = data.run_id;
      $("sr-state").textContent = data.state;
      $("sr-run-id").textContent = "run " + data.run_id;
      stopPolling();
      state.timer = setInterval(poll, 1000);
      await poll();
    } catch (err) {
      $("sr-state").textContent = "Error: " + err.message;
    }
  }

  async function cancelRun() {
    if (!state.runId) return;
    try {
      await api("/api/backtest/runs/" + encodeURIComponent(state.runId) + "/cancel", { method: "POST" });
      await poll();
    } catch (err) {
      $("sr-state").textContent = "Error: " + err.message;
    }
  }

  function init() {
    if (!$("sr-run")) return;
    const start = $("sr-start_date");
    if (start && !start.value && window.TB_STREAM_START) start.value = window.TB_STREAM_START;
    $("sr-run").addEventListener("click", startRun);
    $("sr-cancel").addEventListener("click", cancelRun);
    const preset = $("sr-preset");
    if (preset) {
      preset.addEventListener("change", async () => {
        if (!preset.value) return;
        try {
          const data = await api("/api/backtest/presets");
          const chosen = (data.presets || []).find((p) => p.preset_id === preset.value);
          if (!chosen) return;
          const s = chosen.settings;
          if ($("sr-mode")) $("sr-mode").value = s.mode;
          if ($("sr-end_policy")) $("sr-end_policy").value = s.execution.end_policy;
          $("sr-preset-name").value = chosen.name;
        } catch (err) { /* keep current form */ }
      });
    }
    const params = new URLSearchParams(window.location.search);
    const runId = params.get("run");
    if (runId) {
      state.runId = runId;
      state.timer = setInterval(poll, 1000);
      poll();
    }
    loadCatalog();
    loadPresets();
  }

  document.addEventListener("DOMContentLoaded", init);
})();
