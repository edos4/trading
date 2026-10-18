/* Laya tab: Assess / History / Evaluation / Model status.
 * Existing visual design + shared helpers. Text rendered as text (textContent),
 * never untrusted HTML. Client request ID + server idempotency prevents
 * duplicate submission. Stale responses never replace the current view.
 * Polling is cancelled on navigation.
 */
(function () {
  "use strict";
  let pollTimer = null;
  let currentJob = null;
  let currentSelection = 0;
  let historyOffset = 0;
  const historyLimit = 20;
  let lastDetail = null;

  function el(id) { return document.getElementById(id); }
  function setText(node, value) { if (node) node.textContent = value == null ? "" : String(value); }

  async function api(path, opts) {
    const res = await fetch(path, opts);
    const body = await res.json().catch(() => ({}));
    if (!res.ok) {
      const msg = (body && (body.detail || body.error_detail || body.error_code)) || ("HTTP " + res.status);
      throw new Error(String(msg));
    }
    return body;
  }

  async function refreshStatus() {
    const pill = el("laya-state-pill");
    const detail = el("laya-status-detail");
    const pre = el("laya-model-pre");
    try {
      const s = await api("/api/laya/status");
      setText(pill, "State: " + (s.state || "unknown"));
      setText(detail, s.reason || (s.model ? "Model: " + JSON.stringify(s.model) : ""));
      setText(pre, JSON.stringify(s, null, 2));
    } catch (e) {
      setText(pill, "State: failed");
      setText(detail, String((e && e.message) || e));
      setText(pre, "Status unavailable: " + String((e && e.message) || e));
    }
  }

  function showPanel(name) {
    document.querySelectorAll("[data-ltab]").forEach((b) => {
      const on = b.getAttribute("data-ltab") === name;
      b.classList.toggle("active", on);
      b.setAttribute("aria-selected", on ? "true" : "false");
    });
    document.querySelectorAll("[data-lpanel]").forEach((p) => {
      p.hidden = p.getAttribute("data-lpanel") !== name;
    });
    if (name === "history") loadHistory();
    if (name === "model") refreshStatus();
  }

  function stopPolling() {
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
    currentJob = null;
    const c = el("laya-cancel");
    if (c) c.disabled = true;
  }

  async function pollJob(jobId, selection) {
    let body;
    try {
      body = await api("/api/laya/jobs/" + encodeURIComponent(jobId));
    } catch (e) {
      if (selection !== currentSelection) return; // stale
      setText(el("laya-job-status"), "Job poll failed: " + e.message);
      return;
    }
    if (selection !== currentSelection) return; // stale response guard
    const state = body.state;
    setText(el("laya-job-status"), "Job " + jobId + ": " + state +
      (body.error_detail ? " — " + body.error_detail : ""));
    if (state === "succeeded") {
      stopPolling();
      const aid = body.assessment_id || (body.assessment && body.assessment.assessment_id);
      if (aid) loadDetail(aid, selection);
      loadHistory();
    } else if (state === "failed" || state === "cancelled" || state === "interrupted") {
      stopPolling();
    }
  }

  async function runAssessment() {
    const selection = ++currentSelection;
    stopPolling();
    currentSelection = selection;
    const task = el("laya-task").value;
    const payload = {
      market: el("laya-market").value,
      symbol: el("laya-symbol").value.trim(),
      exchange: el("laya-exchange").value.trim(),
      timeframe: el("laya-timeframe").value,
      task_id: task,
      input_mode: task === "event-review-v1" ? "text" : "candidate",
      source_text: el("laya-text").value,
      source_title: el("laya-title").value,
      publisher: el("laya-publisher").value,
      language: el("laya-language").value || "en",
      candidate_index: parseInt(el("laya-candidate-index").value || "0", 10),
      idempotency_key: "laya-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 10),
    };
    setText(el("laya-job-status"), "Submitting…");
    setText(el("laya-result"), "Running… (queued/loading/running)");
    let submitted;
    try {
      submitted = await api("/api/laya/assessments", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
    } catch (e) {
      if (selection !== currentSelection) return;
      setText(el("laya-job-status"), "Submit failed: " + e.message);
      return;
    }
    if (selection !== currentSelection) return;
    currentJob = submitted.job_id;
    setText(el("laya-job-status"), "Job " + submitted.job_id + ": " + submitted.state);
    el("laya-cancel").disabled = false;
    pollTimer = setInterval(() => pollJob(submitted.job_id, selection), 1500);
    pollJob(submitted.job_id, selection);
  }

  async function loadDetail(assessmentId, selection) {
    let view;
    try {
      view = await api("/api/laya/assessments/" + encodeURIComponent(assessmentId));
    } catch (e) {
      if (selection !== undefined && selection !== currentSelection) return;
      setText(el("laya-detail"), "Detail failed: " + e.message);
      setText(el("laya-result"), "Detail failed: " + e.message);
      return;
    }
    if (selection !== undefined && selection !== currentSelection) return;
    lastDetail = view;
    const preview = view.input ? JSON.stringify(view.input, null, 2) : "(no saved input)";
    setText(el("laya-preview"), preview.slice(0, 4000));
    const lines = [];
    lines.push("Assessment: " + view.assessment_id);
    lines.push("Market/symbol: " + view.market + " / " + view.symbol + "  as_of: " + view.as_of);
    lines.push("Task: " + view.task_id + "  calibration: " + view.calibration_status);
    lines.push("Elapsed: " + view.elapsed_ms + "ms" + (view.truncated ? "  WARNING: truncated" : ""));
    if (view.result && view.result.answers) {
      lines.push("");
      lines.push("Model assessment (typed answers; NOT trade approval):");
      for (const a of view.result.answers) {
        lines.push("  - " + a.qid + " [" + a.kind + "]: " + a.answer +
          " (answer_confidence=" + a.answer_confidence + ")");
      }
      if (view.result.warnings && view.result.warnings.length) {
        lines.push("Warnings: " + view.result.warnings.join("; "));
      }
    } else {
      lines.push("Result pending or unavailable.");
    }
    if (view.decisions && view.decisions.length) {
      lines.push("");
      lines.push("Policy decisions:");
      for (const d of view.decisions) {
        lines.push("  - " + d.mode + " → " + d.outcome + " (" + (d.policy_id || "") + ")");
      }
    }
    lines.push("");
    lines.push("Engine eligibility and policy outcome are separate from 'supports candidate'.");
    setText(el("laya-result"), lines.join("\n"));
    setText(el("laya-detail"), JSON.stringify(view, null, 2).slice(0, 8000));
  }

  async function loadHistory() {
    const tbody = document.querySelector("#laya-h-table tbody");
    const market = el("laya-h-market").value;
    const symbol = el("laya-h-symbol").value.trim();
    const task = el("laya-h-task").value;
    const params = new URLSearchParams({
      limit: String(historyLimit), offset: String(historyOffset),
    });
    if (market) params.set("market", market);
    if (symbol) params.set("symbol", symbol);
    if (task) params.set("task_id", task);
    let body;
    try {
      body = await api("/api/laya/assessments?" + params.toString());
    } catch (e) {
      setText(el("laya-h-page"), "History failed: " + e.message);
      return;
    }
    if (tbody) {
      tbody.innerHTML = "";
      for (const row of body.assessments || []) {
        const tr = document.createElement("tr");
        const cells = [row.assessment_id, row.market, row.symbol, row.task_id, row.as_of,
          row.complete ? "complete" : "pending"];
        for (const value of cells) {
          const td = document.createElement("td");
          td.textContent = String(value == null ? "" : value);
          tr.appendChild(td);
        }
        tr.style.cursor = "pointer";
        tr.addEventListener("click", () => loadDetail(row.assessment_id));
        tbody.appendChild(tr);
      }
    }
    setText(el("laya-h-page"), "Showing " + (body.assessments || []).length +
      " of " + body.total + " (offset " + body.offset + ")");
  }

  function bind() {
    document.querySelectorAll("[data-ltab]").forEach((b) => {
      b.addEventListener("click", () => showPanel(b.getAttribute("data-ltab")));
    });
    el("laya-run").addEventListener("click", runAssessment);
    el("laya-cancel").addEventListener("click", async () => {
      if (!currentJob) return;
      try {
        await api("/api/laya/jobs/" + encodeURIComponent(currentJob) + "/cancel", { method: "POST" });
      } catch (e) {
        setText(el("laya-job-status"), "Cancel failed: " + e.message);
        return;
      }
      setText(el("laya-job-status"), "Cancellation requested.");
    });
    el("laya-task").addEventListener("change", () => {
      const isText = el("laya-task").value === "event-review-v1";
      el("laya-text-fields").hidden = !isText;
      el("laya-candidate-fields").hidden = isText;
    });
    el("laya-h-refresh").addEventListener("click", () => { historyOffset = 0; loadHistory(); });
    el("laya-h-prev").addEventListener("click", () => {
      historyOffset = Math.max(0, historyOffset - historyLimit); loadHistory();
    });
    el("laya-h-next").addEventListener("click", () => {
      historyOffset += historyLimit; loadHistory();
    });
    el("laya-export").addEventListener("click", async () => {
      if (!lastDetail) return;
      try {
        const body = await api("/api/laya/assessments/" +
          encodeURIComponent(lastDetail.assessment_id) + "/export");
        setText(el("laya-detail"), JSON.stringify(body, null, 2).slice(0, 8000));
      } catch (e) {
        setText(el("laya-detail"), "Export failed: " + e.message);
      }
    });
    el("laya-rerun").addEventListener("click", async () => {
      if (!lastDetail || !lastDetail.input) return;
      const inp = lastDetail.input;
      const payload = {
        market: inp.market, symbol: inp.symbol, exchange: inp.exchange || "NASDAQ",
        timeframe: inp.timeframe || "1d", task_id: inp.question_schema_id,
        rerun_of: lastDetail.assessment_id,
        idempotency_key: "laya-" + Date.now().toString(36) + "-" +
          Math.random().toString(36).slice(2, 10),
      };
      if (inp.documents && inp.documents.length) {
        setText(el("laya-detail"), "Rerun for text mode: paste the source text in Assess first.");
        return;
      }
      payload.input_mode = "candidate";
      try {
        const submitted = await api("/api/laya/assessments", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });
        setText(el("laya-detail"), "Rerun submitted as " + submitted.assessment_id +
          " (linked to " + lastDetail.assessment_id + ").");
      } catch (e) {
        setText(el("laya-detail"), "Rerun failed: " + e.message);
      }
    });
    window.addEventListener("beforeunload", stopPolling);
    document.addEventListener("visibilitychange", () => {
      if (document.hidden) stopPolling();
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", () => { bind(); refreshStatus(); });
  } else {
    bind();
    refreshStatus();
  }
})();
