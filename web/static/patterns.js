/* Patterns tab: durable AI edits over the shared Pattern Editor facade.
 *
 * The page submits a request, keeps the returned durable job id, and polls it.
 * Reloading the page with ?job=<id> reconnects to the same job because the
 * PostgreSQL row — not this page or any disabled button — is the authority.
 * Validation and eligibility live server-side; this file only builds the
 * request and renders the response.
 */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const state = {
    patterns: [], versions: [], presets: [], versionId: null,
    defaultVersionId: null, generation: 0, jobId: null, timer: null,
    runs: null,
    chartContext: null, runId: null, runTimer: null,
  };
  const SPIN_KEYS = new Set([
    "session_count", "warmup_bars", "initial_capital", "position_notional",
    "txn_cost_pct", "slippage_pct", "collect_first",
  ]);
  const CHECK_KEYS = ["pattern_only", "volume_gate", "kronos_gate", "kronos_rank"];
  const TERMINAL = ["completed", "failed", "cancelled", "blocked", "interrupted"];

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
      const error = new Error(typeof detail === "string" ? detail : "Request failed");
      error.status = resp.status;
      throw error;
    }
    return body;
  }

  function setStatus(text) { $("pat-action-status").textContent = text; }

  // Reruns always use these fields; AI edits may instead reuse a saved preset.
  function openSettings() {
    const box = $("pat-settings");
    if (box) box.open = true;
  }

  function syncPresetMode() {
    const box = $("pat-settings");
    const preset = $("pat-preset");
    if (!box || !preset) return;
    const usingSaved = !!preset.value;
    box.hidden = false;
    if (usingSaved) box.open = false;
  }

  function setBaseChip(isDefault) {
    const chip = $("pat-base");
    if (!chip) return;
    if (state.versionId) {
      chip.textContent = "Base " + state.versionId.slice(0, 8) + (isDefault ? " · default" : "");
      chip.classList.add("armed");
    } else {
      chip.textContent = "No base version selected";
      chip.classList.remove("armed");
    }
  }

  function initResultsTabs() {
    const root = $("pat-results");
    if (!root) return;
    root.querySelectorAll(".tab").forEach((btn) => {
      btn.addEventListener("click", () => {
        const name = btn.dataset.tab;
        root.querySelectorAll(".tab").forEach((b) => {
          const on = b === btn;
          b.classList.toggle("active", on);
          b.setAttribute("aria-selected", on ? "true" : "false");
        });
        root.querySelectorAll(".tab-panel").forEach((panel) => {
          const on = panel.id === "pat-tab-" + name;
          panel.classList.toggle("active", on);
          panel.hidden = !on;
        });
      });
    });
  }

  // Form problems are shown next to Submit (the version-list status is far away
  // and made a blocked submit look like nothing happened).
  function formError(message) {
    const box = $("pat-error");
    box.hidden = false;
    box.classList.add("error");
    box.textContent = message;
    $("pat-state").textContent = "Not submitted.";
  }

  function clearFormError() {
    const box = $("pat-error");
    box.hidden = true;
    box.classList.remove("error");
    box.textContent = "";
  }

  // Keep the job buttons in sync wherever the durable job id is set — including
  // the ?job= reload path, where no version row is re-selected afterwards.
  function setJobButtons() {
    $("pat-again").disabled = !state.versionId;
    $("pat-cancel").disabled = !state.jobId;
  }

  function submitProblem(instruction, presetId, presetName) {
    if (!state.versionId) return "Select a base version first.";
    if (!instruction) return "Enter an instruction describing the change.";
    if (presetId) return null;  // a saved preset already carries its settings
    if (!presetName) {
      return "Select a saved preset, or name a new one to save the settings below.";
    }
    if (!$("pat-start_date").value.trim()) {
      return "Set a start date for the historical-stream preset.";
    }
    const end = $("pat-end_date") ? $("pat-end_date").value.trim() : "";
    const sessions = Number($("pat-session_count") ? $("pat-session_count").value : 0) || 0;
    if (end && sessions > 0) return "Set either an end date or a session count, not both.";
    if (!end && sessions <= 0) {
      return "Set an end date or a session count for the new preset.";
    }
    return null;
  }

  // ── catalog / versions ────────────────────────────────────────────────
  async function loadCatalog() {
    try {
      const data = await api("/api/patterns");
      state.patterns = data.patterns || [];
      const select = $("pat-pattern");
      select.innerHTML = "";
      state.patterns.forEach((p) => {
        const opt = document.createElement("option");
        opt.value = p.pattern_id;
        opt.textContent = p.display_name + " (" + p.pattern_id + ")"
          + (p.enabled ? "" : " — disabled");
        select.appendChild(opt);
      });
      if (state.patterns.length) {
        state.defaultVersionId = state.patterns[0].default_version_id;
        state.generation = state.patterns[0].generation;
        await loadVersions();
      } else {
        setStatus("No published patterns; run the bootstrap import.");
      }
    } catch (err) {
      setStatus("Catalog unavailable: " + err.message);
    }
  }

  async function loadVersions(selectVersionId) {
    const patternId = $("pat-pattern").value;
    if (!patternId) return;
    const include = $("pat-include-archived").checked ? "?include_archived=true" : "";
    try {
      const data = await api("/api/patterns/" + encodeURIComponent(patternId) + "/versions" + include);
      if ($("pat-pattern").value !== patternId) return;
      state.versions = data.versions || [];
      const current = state.patterns.find((p) => p.pattern_id === patternId);
      state.defaultVersionId = current ? current.default_version_id : null;
      state.generation = current ? current.generation : 0;
      renderVersions();
      const choose = selectVersionId
        || (state.versionId && state.versions.some((v) => v.version_id === state.versionId)
            ? state.versionId : null)
        || (!state.chartContext && (state.defaultVersionId
        || (state.versions[0] && state.versions[0].version_id)));
      if (choose) await selectVersion(choose);
      else setBaseChip(false);
    } catch (err) {
      setStatus("Versions unavailable: " + err.message);
    }
  }

  function badge(ok, text) {
    return "<span class=\"badge " + (ok ? "on" : "off") + "\">" + esc(text) + "</span>";
  }

  function renderVersions() {
    const tbody = $("pat-versions").querySelector("tbody");
    tbody.innerHTML = "";
    state.versions.forEach((v) => {
      const row = document.createElement("tr");
      row.dataset.versionId = v.version_id;
      if (v.version_id === state.versionId) row.className = "selected";
      const isDefault = v.version_id === state.defaultVersionId;
      row.innerHTML = [
        "<td>" + v.number + "</td>",
        "<td class=\"mono\">" + esc(v.version_id.slice(0, 8)) + "</td>",
        "<td>" + esc(v.actor || "") + "</td>",
        "<td>" + esc((v.created_at || "").slice(0, 19)) + "</td>",
        "<td>" + badge(v.validation === "passed", v.validation) + "</td>",
        "<td>" + (isDefault ? badge(true, "default") : "") + "</td>",
        "<td>" + (v.archived ? badge(false, "archived") : "") + "</td>",
      ].join("");
      row.addEventListener("click", () => selectVersion(v.version_id));
      tbody.appendChild(row);
    });
  }

  async function selectVersion(versionId) {
    state.versionId = versionId;
    renderComparison(null, null);
    renderVersions();
    const version = state.versions.find((v) => v.version_id === versionId) || {};
    state.jobId = version.edit_job_id || null;
    updateActionButtons(version);
    await Promise.all([loadSource(versionId), loadDetail(versionId)]);
  }

  function updateActionButtons(version) {
    const archived = !!(version && version.archived);
    const isDefault = version && version.version_id === state.defaultVersionId;
    $("pat-default").disabled = archived || isDefault;
    $("pat-again").disabled = archived;
    $("pat-delete").disabled = archived;
    $("pat-editfrom").disabled = archived;
    const replacement = $("pat-replacement");
    const others = state.versions.filter(
      (v) => v.version_id !== state.versionId && !v.archived && v.validation === "passed");
    replacement.innerHTML = "";
    others.forEach((v) => {
      const opt = document.createElement("option");
      opt.value = v.version_id;
      opt.textContent = "#" + v.number + " " + v.version_id.slice(0, 8);
      replacement.appendChild(opt);
    });
    replacement.hidden = !(isDefault && others.length);
    setBaseChip(isDefault);
    setStatus(isDefault
      ? "Current default. Deleting it requires a replacement."
      : "Selected version " + (state.versionId || "").slice(0, 8));
  }

  async function loadSource(versionId) {
    try {
      const data = await api("/api/patterns/versions/" + encodeURIComponent(versionId) + "/source");
      if (state.versionId !== versionId) return;
      const files = data.files || {};
      $("pat-source").textContent = files[data.source_path] || "—";
      $("pat-doc").textContent = files[data.documentation_path] || "—";
    } catch (err) {
      $("pat-source").textContent = "Unavailable: " + err.message;
      $("pat-doc").textContent = "—";
    }
  }

  async function loadDetail(versionId) {
    try {
      const [detail, diff] = await Promise.all([
        api("/api/patterns/versions/" + encodeURIComponent(versionId)),
        api("/api/patterns/versions/" + encodeURIComponent(versionId) + "/diff"),
      ]);
      if (state.versionId !== versionId) return;
      renderDetail(detail, diff);
      const run = (detail.backtests || []).find((r) => r.state === "completed");
      if (run) {
        const payload = await api("/api/patterns/runs/" + encodeURIComponent(run.run_id));
        if (state.versionId === versionId) renderComparison(payload, null);
      }
    } catch (err) {
      $("pat-detail").textContent = "Unavailable: " + err.message;
    }
  }

  function renderDetail(detail, diff) {
    const lines = [
      "Version #" + detail.number + "  " + detail.version_id,
      "Pattern:   " + detail.pattern_id,
      "Actor:     " + (detail.actor || "?") + "  (" + (detail.provenance || "?") + ")",
      "Created:   " + (detail.created_at || "?"),
      "Parent:    " + (detail.parent_version_id || "none"),
      "Validation:" + detail.lifecycle.validation,
      "Default run:" + (detail.lifecycle.successful_run_id || "none"),
      "Archived:  " + (detail.lifecycle.archived_at || "no"),
      "Files:     " + (detail.files || []).join(", "),
    ];
    if (detail.instruction) lines.push("Instruction: " + detail.instruction);
    if (detail.explanation) lines.push("Explanation: " + detail.explanation);
    if (detail.provider) {
      lines.push("Provider:  " + detail.provider.provider + " ("
        + detail.provider.requested_model + " -> " + (detail.provider.returned_model || "?") + ")");
    }
    lines.push("", "Reports: " + (detail.reports || []).length
      + "   Backtests: " + (detail.backtests || []).length);
    (detail.reports || []).forEach((r) => {
      lines.push("  [" + r.status + "] " + (r.report_id || "").slice(0, 8)
        + " checks=" + ((r.checks || []).length));
    });
    (detail.backtests || []).forEach((b) => {
      lines.push("  run " + (b.run_id || "").slice(0, 8) + " " + b.state
        + (b.metrics ? " trades=" + b.metrics.trade_count : ""));
    });
    $("pat-detail").textContent = lines.join("\n");
    const files = (diff && diff.files) || {};
    const text = Object.keys(files).map((name) => files[name]).join("\n");
    $("pat-diff").textContent = text || "(no differences from parent)";
  }

  async function loadPresets() {
    try {
      const data = await api("/api/backtest/presets");
      state.presets = data.presets || [];
      const select = $("pat-preset");
      select.innerHTML = '<option value="">(new preset from form)</option>';
      state.presets.forEach((p) => {
        const opt = document.createElement("option");
        opt.value = p.preset_id;
        opt.textContent = p.name + " (" + p.settings.mode + ")";
        select.appendChild(opt);
      });
    } catch (err) {
      setStatus("Presets unavailable: " + err.message);
    }
  }

  async function loadBalance() {
    const el = $("pat-balance");
    if (!el) return;
    try {
      const data = await api("/api/patterns/provider");
      el.textContent = balanceText(data);
      el.classList.toggle("balance-low", balanceIsLow(data));
    } catch (err) {
      el.textContent = "Provider balance: unavailable (" + err.message + ")";
    }
  }

  function balanceIsLow(data) {
    if (!data || !data.available || !data.balance) return true;
    if (data.balance.is_available === false) return true;
    return (data.balance.infos || []).every((i) => Number(i.total_balance) <= 0);
  }

  function balanceText(data) {
    if (!data || !data.available) {
      const state = data && data.configured === false ? "not configured" : "unavailable";
      return "Provider: " + state + " — " + ((data && data.error) || "no API key");
    }
    const balance = data.balance || {};
    const parts = (balance.infos || []).map(
      (i) => i.currency + " " + i.total_balance
        + (i.granted_balance && Number(i.granted_balance) > 0
           ? " (granted " + i.granted_balance + ")" : ""));
    const funds = balance.is_available === false ? " · INSUFFICIENT — top up" : "";
    return "Provider: " + (data.model || "deepseek") + " · Balance: "
      + (parts.join(", ") || "?") + funds;
  }

  // ── edit request ──────────────────────────────────────────────────────
  function replayValues() {
    const out = { mode: "historical-stream", timeframe: "1d" };
    ["session_count", "warmup_bars", "initial_capital", "position_notional",
     "txn_cost_pct", "slippage_pct", "collect_first", "start_date", "end_date",
     "sizing_mode", "end_policy"].forEach((key) => {
      const el = $("pat-" + key);
      if (!el) return;
      out[key] = SPIN_KEYS.has(key) ? Number(el.value || 0) : el.value;
    });
    CHECK_KEYS.forEach((key) => {
      const el = $("pat-" + key);
      if (el) out[key] = !!el.checked;
    });
    out.market = $("pat-market").value || window.TB_DEFAULT_MARKET || "us";
    out.symbols = String($("pat-symbols").value || "").replace(/,/g, " ").trim();
    out.universe = ($("pat-universe").value || "").trim() || null;
    out.chart_symbol = state.chartContext && state.chartContext.symbol;
    if (!out.start_date) out.start_date = window.TB_STREAM_START || null;
    return out;
  }

  function jobUrl() {
    const url = new URL(window.location.href);
    url.searchParams.set("job", state.jobId || "");
    window.history.replaceState(null, "", url.toString());
  }

  async function submit() {
    const instruction = $("pat-instruction").value.trim();
    const presetId = $("pat-preset").value;
    const presetName = $("pat-preset-name").value.trim();
    const problem = submitProblem(instruction, presetId, presetName);
    if (problem) {
      // A blocked submit that names a new-preset field is invisible while the
      // settings are folded, so reveal them before showing the reason.
      if (!presetId) openSettings();
      formError(problem);
      return;
    }
    clearFormError();
    $("pat-state").textContent = "Submitting…";
    $("pat-submit").disabled = true;  // fast path only; the API is idempotent
    try {
      const data = await api("/api/patterns/edits", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          pattern_id: $("pat-pattern").value,
          base_version_id: state.versionId,
          instruction: instruction,
          chart_context: state.chartContext,
          preset_id: presetId || null,
          preset_name: presetName || null,
          settings: presetId ? null : replayValues(),
        }),
      });
      state.jobId = data.job_id;
      jobUrl();
      $("pat-job-id").textContent = "job " + data.job_id.slice(0, 8);
      setJobButtons();
      startPolling();
    } catch (err) {
      formError("Could not submit: " + err.message);
      $("pat-submit").disabled = false;
    }
  }

  function startPolling() {
    stopPolling();
    state.timer = setInterval(pollJob, 1000);
    pollJob();
  }

  function stopPolling() {
    if (state.timer) { clearInterval(state.timer); state.timer = null; }
  }

  async function pollJob() {
    if (!state.jobId) return;
    try {
      const detail = await api("/api/patterns/edits/" + encodeURIComponent(state.jobId));
      renderJob(detail);
      if (TERMINAL.indexOf(detail.state) >= 0) {
        stopPolling();
        $("pat-submit").disabled = false;
        $("pat-cancel").disabled = true;
        setJobButtons();
        loadBalance();  // a generation consumes credit; refresh the displayed balance
        if (detail.generated_version_id) await loadVersions(detail.generated_version_id);
        if (detail.runs) { state.runs = detail.runs; await loadComparison(); }
      }
    } catch (err) {
      stopPolling();
      $("pat-state").textContent = "Error: " + err.message;
      $("pat-submit").disabled = false;
    }
  }

  function renderJob(detail) {
    const progress = detail.progress || {};
    const done = progress.completed_units || 0;
    const total = progress.total_units || 0;
    $("pat-progress").value = total ? Math.min(100, (done / total) * 100) : 0;
    $("pat-pct").textContent = total ? Math.round((done / total) * 100) + "%" : "—";
    $("pat-state").textContent = detail.state
      + (detail.cancel_requested ? " (cancelling…)" : "")
      + " — " + done + "/" + (total || "?") + " " + (progress.unit || "stages");
    const error = $("pat-error");
    if (detail.error) {
      error.hidden = false;
      error.classList.add("error");
      error.textContent = "[" + detail.error.code + "] " + detail.error.message
        + (detail.error.retryable ? " (retryable)" : "");
    } else {
      error.hidden = true;
      error.classList.remove("error");
    }
    $("pat-explanation").textContent = detail.explanation || "No explanation yet.";
    const diff = detail.diff || {};
    const diffText = Object.keys(diff).map((name) => diff[name]).join("\n");
    $("pat-edit-diff").textContent = diffText || "—";
    const report = detail.report;
    if (!report) {
      $("pat-diagnostics").textContent = "No validation report yet.";
    } else {
      const lines = ["Status: " + report.status];
      (report.checks || []).forEach((c) => {
        lines.push("  [" + c.outcome + "] " + c.name
          + (c.detail ? " — " + c.detail : ""));
      });
      $("pat-diagnostics").textContent = lines.join("\n");
    }
  }

  async function loadComparison() {
    const runs = state.runs || {};
    if (!runs.candidate && !runs.base) return;
    try {
      const [candidate, base] = await Promise.all([
        runs.candidate ? api("/api/patterns/runs/" + encodeURIComponent(runs.candidate)) : null,
        runs.base ? api("/api/patterns/runs/" + encodeURIComponent(runs.base)) : null,
      ]);
      renderComparison(candidate, base);
    } catch (err) {
      $("pat-comparison").textContent = "Comparison unavailable: " + err.message;
    }
  }

  function metric(payload, key) {
    if (!payload || !payload.result || !payload.result.metrics) return null;
    return payload.result.metrics[key];
  }

  function renderComparison(candidate, base) {
    const keys = ["trade_count", "win_rate", "realized_pnl", "unrealized_pnl",
                  "net_pnl", "fees", "max_drawdown_pct", "open_position_count"];
    const lines = [
      "Candidate: " + (candidate ? candidate.state : "none")
        + "   Base: " + (base ? base.state : "none"),
      "",
      ("metric").padEnd(20) + ("base").padStart(14) + ("candidate").padStart(14),
      "-".repeat(48),
    ];
    keys.forEach((key) => {
      const b = metric(base, key);
      const c = metric(candidate, key);
      lines.push(key.padEnd(20) + String(b === null ? "—" : b).padStart(14)
        + String(c === null ? "—" : c).padStart(14));
    });
    if (candidate && candidate.result) {
      lines.push("", "Zero trades: "
        + (candidate.result.zero_trades ? "yes (a valid result, not a failure)" : "no"));
    }
    $("pat-comparison").textContent = lines.join("\n");
    renderDetections(candidate);
    const tbody = $("pat-trades").querySelector("tbody");
    tbody.innerHTML = "";
    const trades = (candidate && candidate.result && candidate.result.trades) || [];
    trades.forEach((t) => {
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

  // ── lifecycle actions ─────────────────────────────────────────────────
  async function guard(fn) {
    try {
      await fn();
    } catch (err) {
      if (err.status === 409) {
        setStatus("Conflict: " + err.message + " Reloading…");
        await loadVersions(state.versionId);
      } else {
        setStatus("Error: " + err.message);
      }
    }
  }

  function selectedVersion() {
    return state.versions.find((v) => v.version_id === state.versionId) || {};
  }

  function setDefault() {
    return guard(async () => {
      const patternId = $("pat-pattern").value;
      await api("/api/patterns/versions/" + encodeURIComponent(state.versionId) + "/default", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ expected_generation: state.generation }),
      });
      setStatus("Default updated.");
      await reload(patternId);
    });
  }

  function backtestAgain() {
    return guard(async () => {
      openSettings();
      const data = await api("/api/patterns/versions/" + encodeURIComponent(state.versionId) + "/backtest", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(replayValues()),
      });
      state.runId = data.run_id;
      $("pat-again").disabled = true;
      if (state.runTimer) clearTimeout(state.runTimer);
      pollBacktest();
    });
  }

  async function pollBacktest() {
    try {
      const run = await api("/api/patterns/runs/" + encodeURIComponent(state.runId));
      setStatus("Backtest: " + run.state);
      if (TERMINAL.includes(run.state)) {
        $("pat-again").disabled = false;
        renderComparison(run, null);
        if (run.error) setStatus(run.error.message || JSON.stringify(run.error));
      } else state.runTimer = setTimeout(pollBacktest, 1000);
    } catch (err) {
      $("pat-again").disabled = false;
      setStatus("Backtest unavailable: " + err.message);
    }
  }

  function renderDetections(candidate) {
    const host = $("pat-detections");
    host.replaceChildren();
    window.TVChart.unmount();
    $("pat-chart").hidden = true;
    $("pat-chart-title").textContent = "";
    $("pat-detection-status").textContent = "No completed backtest for this version.";
    if (!candidate || !candidate.result) return;
    const detections = candidate.result.detections || [];
    $("pat-detection-status").textContent = detections.length
      ? "Select a detection to see this version's chart."
      : "No detections recorded for this run. Try other dates or symbols.";
    detections.forEach((event, index) => {
      const button = document.createElement("button");
      button.className = "btn ghost";
      button.textContent = event.symbol + " · " + event.session.slice(0, 10) + " · " + event.action;
      button.onclick = () => guard(async () => {
        const data = await api("/api/patterns/runs/" + encodeURIComponent(candidate.run_id)
          + "/chart?detection=" + index);
        $("pat-chart-title").textContent = data.title;
        $("pat-chart").hidden = false;
        window.TVChart.mount($("pat-chart"), data);
      });
      host.appendChild(button);
    });
  }

  function deleteVersion() {
    return guard(async () => {
      const replacement = $("pat-replacement");
      const useReplacement = !replacement.hidden && replacement.value;
      await api("/api/patterns/versions/" + encodeURIComponent(state.versionId) + "/archive", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          expected_generation: state.generation,
          replacement_default_version_id: useReplacement ? replacement.value : null,
        }),
      });
      setStatus("Version archived.");
      state.versionId = null;
      state.jobId = null;
      await reload($("pat-pattern").value);
    });
  }

  async function reload(patternId) {
    await loadCatalog();
    if (patternId) $("pat-pattern").value = patternId;
    await loadVersions(state.versionId || undefined);
  }

  async function applyChartHandoff(context) {
    state.chartContext = context;
    const corrections = context.manual_corrections || [];
    $("pat-correction-preview").hidden = false;
    $("pat-correction-summary").textContent = corrections.length + " corrections: " +
      corrections.map((shape) => shape.label || shape.kind).join(", ");
    $("pat-market").value = context.market || window.TB_DEFAULT_MARKET || "us";
    const candles = context.candles || [];
    if (candles.length) {
      $("pat-start_date").value = candles[0].time;
      $("pat-end_date").value = candles[candles.length - 1].time;
    }
    $("pat-pattern_only").checked = true;
    $("pat-preset-name").value = "Chart pattern edit";
    $("pat-context").textContent = context.symbol + " · " + context.pattern + " · "
      + candles.length + " bars attached. Leave Symbols blank to scan the universe."
      + (context.pattern_version_id ? "" : " Legacy trade: review the selected base version.");
    const pattern = state.patterns.find((p) => p.pattern_id === context.pattern);
    if (!pattern) throw new Error("Chart pattern is unavailable in the catalog.");
    $("pat-pattern").value = pattern.pattern_id;
    state.versionId = null;
    await loadVersions();
    if (context.pattern_version_id &&
        state.versions.some((v) => v.version_id === context.pattern_version_id)) {
      await selectVersion(context.pattern_version_id);
    } else {
      state.versionId = null;
      setBaseChip(false);
      setStatus(context.pattern_version_id
        ? "Chart version is unavailable; select a base version explicitly."
        : "Legacy chart: select a base version explicitly.");
    }
    openSettings();
    $("pat-instruction").focus?.();
  }

  async function init() {
    if (!$("pat-submit")) return;
    $('pat-preview-corrections')?.addEventListener('click', () => {
      const host = $('pat-correction-chart');
      host.hidden = !host.hidden;
      if (!host.hidden && state.chartContext) window.TVChart.mount(host, state.chartContext, {readOnly: true});
      else window.TVChart.unmount();
    });
    $("pat-pattern").addEventListener("change", () => {
      if (state.chartContext && $('pat-pattern').value !== state.chartContext.pattern) {
        if (!window.confirm('Detach the chart and its corrections to edit another pattern?')) {
          $('pat-pattern').value = state.chartContext.pattern; return;
        }
        state.chartContext = null;
        $('pat-context').textContent = '';
        $('pat-correction-preview').hidden = true;
        window.TVChart.unmount();
      }
      state.versionId = null; state.jobId = null; state.runs = null;
      loadVersions();
    });
    $("pat-include-archived").addEventListener("change", () => loadVersions());
    $("pat-submit").addEventListener("click", submit);
    $("pat-default").addEventListener("click", setDefault);
    $("pat-again").addEventListener("click", backtestAgain);
    $("pat-delete").addEventListener("click", deleteVersion);
    $("pat-editfrom").addEventListener("click", () => {
      $("pat-instruction").focus();
      setStatus("Editing from version " + (state.versionId || "").slice(0, 8));
    });
    $("pat-cancel").addEventListener("click", async () => {
      if (!state.jobId) return;
      try {
        await api("/api/patterns/edits/" + encodeURIComponent(state.jobId) + "/cancel",
          { method: "POST" });
        $("pat-state").textContent = "Cancellation requested…";
      } catch (err) { setStatus("Error: " + err.message); }
    });
    const preset = $("pat-preset");
    preset.addEventListener("change", () => {
      syncPresetMode();
      if (!preset.value) return;
      const chosen = state.presets.find((p) => p.preset_id === preset.value);
      if (!chosen) return;
      const s = chosen.settings;
      const fields = Object.assign({}, s, s.execution, s.window || {}, {symbols: (s.symbols || []).join(" ")});
      Object.entries(fields).forEach(([key, value]) => {
        const el = $("pat-" + key);
        if (!el) return;
        if (CHECK_KEYS.includes(key)) el.checked = !!value;
        else el.value = value == null ? "" : value;
      });
      $("pat-preset-name").value = chosen.name;
    });
    initResultsTabs();
    syncPresetMode();
    $("pat-start_date").value = $("pat-start_date").value || window.TB_STREAM_START || "";
    await loadCatalog();
    await loadPresets();
    loadBalance();
    const params = new URLSearchParams(window.location.search);
    const chartKey = params.get("chart");
    if (chartKey) {
      try {
        const context = JSON.parse(sessionStorage.getItem(chartKey) || "null");
        if (!context) throw new Error("Chart context has expired. Open the trade chart again.");
        await applyChartHandoff(context);
      } catch (err) { formError(err.message); }
    }
    const jobId = params.get("job");
    if (jobId) {
      state.jobId = jobId;
      $("pat-job-id").textContent = "job " + jobId.slice(0, 8);
      setJobButtons();
      startPolling();
    }
  }

  document.addEventListener("DOMContentLoaded", init);
})();
