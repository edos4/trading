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
    $("pat-again").disabled = !state.jobId;
    $("pat-cancel").disabled = !state.jobId;
  }

  function submitProblem(instruction, presetId, presetName) {
    if (!state.versionId) return "Select a base version first.";
    if (!instruction) return "Enter an instruction describing the change.";
    if (presetId) return null;  // a saved preset already carries its settings
    if (!presetName) {
      return "Select a saved preset, or name a new one to save the settings below.";
    }
    const symbols = String($("pat-symbols").value || "").trim();
    const universe = String($("pat-universe").value || "").trim();
    if (!symbols && !universe) {
      return "Enter at least one symbol (or a universe) for the new preset.";
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
      state.versions = data.versions || [];
      const current = state.patterns.find((p) => p.pattern_id === patternId);
      state.defaultVersionId = current ? current.default_version_id : null;
      state.generation = current ? current.generation : 0;
      renderVersions();
      const choose = selectVersionId
        || (state.versionId && state.versions.some((v) => v.version_id === state.versionId)
            ? state.versionId : null)
        || state.defaultVersionId
        || (state.versions[0] && state.versions[0].version_id);
      if (choose) await selectVersion(choose);
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
    renderVersions();
    const version = state.versions.find((v) => v.version_id === versionId) || {};
    state.jobId = version.edit_job_id || state.jobId;
    updateActionButtons(version);
    await Promise.all([loadSource(versionId), loadDetail(versionId)]);
  }

  function updateActionButtons(version) {
    const archived = !!(version && version.archived);
    const isDefault = version && version.version_id === state.defaultVersionId;
    $("pat-default").disabled = archived || isDefault;
    $("pat-again").disabled = !state.jobId;
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
    setStatus(isDefault
      ? "Current default. Deleting it requires a replacement."
      : "Selected version " + (state.versionId || "").slice(0, 8));
  }

  async function loadSource(versionId) {
    try {
      const data = await api("/api/patterns/versions/" + encodeURIComponent(versionId) + "/source");
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
      renderDetail(detail, diff);
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
      const data = await api("/api/patterns/edits/" + encodeURIComponent(state.jobId) + "/retry",
        { method: "POST" });
      state.jobId = data.job_id;
      jobUrl();
      setJobButtons();
      startPolling();
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

  async function init() {
    if (!$("pat-submit")) return;
    $("pat-pattern").addEventListener("change", () => {
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
      if (!preset.value) return;
      const chosen = state.presets.find((p) => p.preset_id === preset.value);
      if (!chosen) return;
      const s = chosen.settings;
      if ($("pat-market")) $("pat-market").value = s.market;
      if ($("pat-end_policy")) $("pat-end_policy").value = s.execution.end_policy;
      if ($("pat-initial_capital")) $("pat-initial_capital").value = s.execution.initial_capital;
      $("pat-preset-name").value = chosen.name;
    });
    $("pat-start_date").value = $("pat-start_date").value || window.TB_STREAM_START || "";
    await loadCatalog();
    await loadPresets();
    await loadBalance();
    const params = new URLSearchParams(window.location.search);
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
