/* Kronos tab: comma-separated symbols + days → one forecast chart per symbol. */
(function () {
  const BG = "#131722";
  const GRID = "#2a2e39";
  const TEXT = "#d1d4dc";
  const DIM = "#787b86";
  const ACTUAL = "#d1d4dc";
  const BASE = "#2962ff";
  const FT = "#ef5350";

  const form = document.getElementById("kronos-form");
  const symbolEl = document.getElementById("kronos-symbol");
  const daysEl = document.getElementById("kronos-days");
  const submitEl = document.getElementById("kronos-submit");
  const topForm = document.getElementById("kronos-top-form");
  const topNEl = document.getElementById("kronos-top-n");
  const topDaysEl = document.getElementById("kronos-top-days");
  const topSubmitEl = document.getElementById("kronos-top-submit");
  const statusEl = document.getElementById("kronos-status");
  const resultsEl = document.getElementById("kronos-results");
  const saveEl = document.getElementById("kronos-save");
  const saveDaysEl = document.getElementById("kronos-save-days");
  const refreshEl = document.getElementById("kronos-winrate-refresh");
  const historyStatusEl = document.getElementById("kronos-history-status");
  const winrateSummaryEl = document.getElementById("kronos-winrate-summary");
  const winrateHostEl = document.getElementById("kronos-winrate-chart");
  const predictionsBodyEl = document.querySelector("#kronos-predictions tbody");
  const predictionsCountEl = document.getElementById("kronos-predictions-count");
  const exp2RunEl = document.getElementById("kronos-exp2-run");
  const exp2RefreshEl = document.getElementById("kronos-exp2-refresh");
  const exp2StatusEl = document.getElementById("kronos-exp2-status");
  const exp2SummaryEl = document.getElementById("kronos-exp2-summary");
  const exp2AvgEl = document.getElementById("kronos-exp2-avg");
  const exp2BodyEl = document.querySelector("#kronos-exp2 tbody");
  const exp2GradedOnlyEl = document.getElementById("kronos-exp2-graded-only");

  const MAX_TOP = 100;
  const WINRATE_EPOCH = Date.UTC(2020, 0, 1) / 1000;
  let winrateChart = null;
  let experimentData = null;
  // Two trading weeks of actual closes are visible on open; the rest of the
  // ~5-month history stays loaded so the chart can still be zoomed out.
  const VISIBLE_HISTORY_BARS = 10;
  let charts = [];

  function setStatus(text) {
    if (statusEl) statusEl.textContent = text || "";
  }

  function unmountAll() {
    charts.forEach((c) => {
      try { c.chart.remove(); } catch (err) { /* already detached */ }
    });
    charts = [];
    if (resultsEl) resultsEl.innerHTML = "";
  }

  function pct(anchor, value) {
    if (!anchor) return "";
    return ((value / anchor - 1) * 100).toFixed(2);
  }

  function chartOptions() {
    return {
      autoSize: true,
      layout: {
        background: { color: BG },
        textColor: TEXT,
        fontFamily: "Trebuchet MS, Roboto, sans-serif",
        fontSize: 12,
      },
      grid: { vertLines: { color: GRID }, horzLines: { color: GRID } },
      crosshair: {
        mode: LightweightCharts.CrosshairMode.Normal,
        vertLine: { color: DIM, width: 1, style: 3, labelBackgroundColor: "#363a45" },
        horzLine: { color: DIM, width: 1, style: 3, labelBackgroundColor: "#363a45" },
      },
      rightPriceScale: { borderColor: GRID, scaleMargins: { top: 0.12, bottom: 0.12 } },
      timeScale: { borderColor: GRID, rightOffset: 3, barSpacing: 7, minBarSpacing: 0.5 },
      handleScroll: { mouseWheel: true, pressedMouseMove: true, horzTouchDrag: true },
      handleScale: { axisPressedMouseMove: true, mouseWheel: true, pinch: true },
    };
  }

  function drawChart(hostEl, data) {
    if (typeof LightweightCharts === "undefined") {
      throw new Error("chart library failed to load");
    }
    const chart = LightweightCharts.createChart(hostEl, chartOptions());

    const actualSeries = chart.addLineSeries({ color: ACTUAL, lineWidth: 2, title: "actual" });
    actualSeries.setData((data.history || []).map((r) => ({ time: r.time, value: r.value })));

    const anchor = data.anchor ? { time: data.anchor.time, value: data.anchor.value } : null;
    const lines = { base: BASE, finetuned: FT };
    Object.keys(lines).forEach((tag) => {
      const rows = data[tag] || [];
      if (!rows.length) return;
      const series = chart.addLineSeries({
        color: lines[tag],
        lineWidth: 2,
        lineStyle: tag === "base" ? LightweightCharts.LineStyle.Dashed : LightweightCharts.LineStyle.Solid,
        priceLineVisible: false,
        title: tag,
      });
      series.setData((anchor ? [anchor] : []).concat(
        rows.map((r) => ({ time: r.time, value: r.value })),
      ));
    });

    // Open on the last two trading weeks plus the full forecast horizon rather
    // than fitting the whole ~5-month history, which starts far too zoomed out.
    const historyLen = (data.history || []).length;
    const forecastLen = Math.max(
      (data.base || []).length,
      (data.finetuned || []).length,
    );
    if (historyLen || forecastLen) {
      chart.timeScale().setVisibleLogicalRange({
        from: Math.max(0, historyLen - VISIBLE_HISTORY_BARS),
        to: historyLen + forecastLen + 1,
      });
    }
    return chart;
  }

  function describe(data) {
    if (data.errors && data.errors.data) return `${data.symbol}: ${data.errors.data}`;
    const bits = [];
    const a = data.anchor ? data.anchor.value : null;
    ["base", "finetuned"].forEach((tag) => {
      const rows = data[tag] || [];
      if (!rows.length) {
        if (data.errors && data.errors[tag]) bits.push(`${tag}: failed`);
        return;
      }
      const lastValue = rows[rows.length - 1].value;
      bits.push(`${tag} +${data.days}d ${lastValue.toFixed(2)} (${pct(a, lastValue)}%)`);
    });
    const anchorTxt = data.anchor ? `anchor ${data.anchor.time} @ ${data.anchor.value.toFixed(2)}` : "";
    return [anchorTxt, bits.join(" • ")].filter(Boolean).join(" • ") || "no forecast";
  }

  function makePanel(data) {
    const section = document.createElement("section");
    section.className = "panel";

    const head = document.createElement("div");
    head.className = "toolbar";
    const title = document.createElement("h2");
    title.style.margin = "0";
    title.textContent = `${data.symbol} — ${data.days} trading days ahead`;
    const save = document.createElement("button");
    save.type = "button";
    save.className = "btn";
    save.textContent = "Save chart";
    save.disabled = true;
    head.appendChild(title);
    head.appendChild(save);

    const status = document.createElement("p");
    status.className = "status";
    status.textContent = describe(data);

    const host = document.createElement("div");
    host.className = "tv-chart-host";
    host.style.minHeight = "460px";

    section.appendChild(head);
    section.appendChild(status);
    section.appendChild(host);
    return { section, host, save };
  }

  function saveChart(symbol, days, chart) {
    try {
      const url = chart.takeScreenshot().toDataURL("image/png");
      const a = document.createElement("a");
      a.href = url;
      a.download = `${symbol}_kronos_${days}d.png`;
      a.click();
    } catch (err) {
      setStatus(`Could not save ${symbol} chart: ${err.message || err}`);
    }
  }

  function render(payload) {
    unmountAll();
    const results = payload.results || [];
    results.forEach((data) => {
      const { section, host, save } = makePanel(data);
      resultsEl.appendChild(section);
      if ((data.base || []).length || (data.finetuned || []).length) {
        const chart = drawChart(host, data);
        charts.push({ chart, symbol: data.symbol, days: data.days });
        save.disabled = false;
        save.addEventListener("click", () => saveChart(data.symbol, data.days, chart));
      }
    });
    const failed = results.filter((r) => !(r.base || []).length && !(r.finetuned || []).length);
    const summary = `${results.length} symbol(s) rendered`;
    setStatus(failed.length ? `${summary} • ${failed.length} failed` : summary);
  }

  function parseSymbols(raw) {
    const seen = [];
    (raw || "").split(",").forEach((chunk) => {
      const sym = chunk.replace(/\s+/g, "").toUpperCase();
      if (sym && seen.indexOf(sym) === -1) seen.push(sym);
    });
    return seen;
  }

  function readDays(el) {
    const input = el || daysEl;
    const days = parseInt(input.value, 10);
    if (!Number.isFinite(days) || days < 1 || days > 60) {
      setStatus("Days must be between 1 and 60.");
      return null;
    }
    return days;
  }

  async function run(body, label) {
    submitEl.disabled = true;
    if (topSubmitEl) topSubmitEl.disabled = true;
    unmountAll();
    setStatus(`${label}… this can take a minute.`);
    try {
      const res = await fetch("/api/kronos/forecast", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        throw new Error(data.detail || data.message || `Request failed (${res.status})`);
      }
      render(data);
      return data;
    } catch (err) {
      setStatus(String(err.message || err));
      return null;
    } finally {
      submitEl.disabled = false;
      if (topSubmitEl) topSubmitEl.disabled = false;
    }
  }

  async function submit(event) {
    if (event) event.preventDefault();
    const symbols = parseSymbols(symbolEl.value);
    if (!symbols.length) { setStatus("Enter at least one symbol."); return; }
    const days = readDays(daysEl);
    if (days === null) return;
    await run({ symbols, days }, `Running Kronos for ${symbols.join(", ")} (+${days}d)`);
  }

  async function submitTop(event) {
    if (event) event.preventDefault();
    const n = parseInt(topNEl.value, 10);
    if (!Number.isFinite(n) || n < 1 || n > MAX_TOP) {
      setStatus(`Top must be between 1 and ${MAX_TOP}.`);
      return;
    }
    const days = readDays(topDaysEl || daysEl);
    if (days === null) return;
    const data = await run(
      { top: n, days },
      `Running Kronos for the top ${n} NASDAQ stocks (+${days}d)`,
    );
    if (data && Array.isArray(data.symbols) && data.symbols.length) {
      symbolEl.value = data.symbols.join(", ");
    }
  }

  function setHistoryStatus(text) {
    if (historyStatusEl) historyStatusEl.textContent = text || "";
  }

  function fmtRate(value) {
    return value === null || value === undefined ? "n/a" : `${value.toFixed(1)}%`;
  }

  function winratePoints(model) {
    return ((model && model.days) || [])
      .filter((row) => row.n > 0 && row.win_rate_pct !== null && row.win_rate_pct !== undefined)
      .map((row) => ({ time: WINRATE_EPOCH + row.day * 86400, value: row.win_rate_pct }));
  }

  function renderWinRate(stats) {
    if (!winrateHostEl) return;
    if (winrateChart) {
      try { winrateChart.remove(); } catch (err) { /* already detached */ }
      winrateChart = null;
    }
    winrateHostEl.innerHTML = "";
    const base = (stats && stats.models && stats.models.base) || { days: [], overall: {} };
    const fine = (stats && stats.models && stats.models.finetuned) || { days: [], overall: {} };
    const saved = (stats && stats.saved) || {};
    if (winrateSummaryEl) {
      winrateSummaryEl.textContent = [
        `saved ${saved.predictions || 0} prediction(s) across ${saved.symbols || 0} symbol(s)`,
        `graded ${(stats && stats.graded) || 0}`,
        `pending ${(stats && stats.pending) || 0}`,
        `base ${fmtRate(base.overall && base.overall.win_rate_pct)}`,
        `fine-tuned ${fmtRate(fine.overall && fine.overall.win_rate_pct)}`,
      ].join(" • ");
    }
    const basePoints = winratePoints(base);
    const finePoints = winratePoints(fine);
    if (!basePoints.length && !finePoints.length) {
      setHistoryStatus(saved.predictions
        ? "No graded predictions yet — refresh after the forecast sessions print."
        : "No saved forecasts yet.");
      return;
    }
    if (typeof LightweightCharts === "undefined") {
      setHistoryStatus("Chart library failed to load.");
      return;
    }
    const chart = LightweightCharts.createChart(winrateHostEl, {
      autoSize: true,
      layout: {
        background: { color: BG },
        textColor: TEXT,
        fontFamily: "Trebuchet MS, Roboto, sans-serif",
        fontSize: 12,
      },
      grid: { vertLines: { color: GRID }, horzLines: { color: GRID } },
      rightPriceScale: { borderColor: GRID },
      timeScale: {
        borderColor: GRID,
        tickMarkFormatter: (time) => `D${Math.round((time - WINRATE_EPOCH) / 86400)}`,
      },
      localization: {
        timeFormatter: (time) => `Day ${Math.round((time - WINRATE_EPOCH) / 86400)}`,
      },
    });
    const zeroToHundred = () => ({ priceRange: { minValue: 0, maxValue: 100 } });
    const add = (points, color, title, dashed) => {
      if (!points.length) return;
      const series = chart.addLineSeries({
        color,
        lineWidth: 2,
        title,
        lineStyle: dashed ? LightweightCharts.LineStyle.Dashed : LightweightCharts.LineStyle.Solid,
        priceLineVisible: false,
        autoscaleInfoProvider: zeroToHundred,
      });
      series.setData(points);
    };
    add(basePoints, BASE, "base", true);
    add(finePoints, FT, "fine-tuned", false);
    const times = basePoints.concat(finePoints).map((point) => point.time);
    const first = Math.min.apply(null, times);
    const last = Math.max.apply(null, times);
    const fifty = chart.addLineSeries({
      color: DIM,
      lineWidth: 1,
      lineStyle: LightweightCharts.LineStyle.Dashed,
      title: "50%",
      priceLineVisible: false,
      lastValueVisible: false,
      autoscaleInfoProvider: zeroToHundred,
    });
    fifty.setData(first === last
      ? [{ time: first, value: 50 }]
      : [{ time: first, value: 50 }, { time: last, value: 50 }]);
    chart.timeScale().fitContent();
    winrateChart = chart;
  }

  async function historyRequest(url, options) {
    const res = await fetch(url, Object.assign({ credentials: "same-origin" }, options || {}));
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      throw new Error(data.detail || data.message || `Request failed (${res.status})`);
    }
    return data;
  }

  function fmtPrice(value) {
    return (typeof value === "number" && Number.isFinite(value))
      ? value.toFixed(2)
      : String(value ?? "");
  }

  function renderPredictions(data) {
    if (!predictionsBodyEl) return;
    predictionsBodyEl.innerHTML = "";
    const rows = (data && data.rows) || [];
    if (predictionsCountEl) {
      predictionsCountEl.textContent =
        `${rows.length} of ${(data && data.total) || 0} row(s)`;
    }
    rows.forEach((r) => {
      const actual = (r.actual_close === null || r.actual_close === undefined)
        ? "—"
        : `${r.actual_date || "?"} @ ${fmtPrice(r.actual_close)}`;
      const cells = [
        (r.saved_at || "").slice(0, 19).replace("T", " "),
        r.symbol,
        `${r.anchor_date || "?"} @ ${fmtPrice(r.anchor_close)}`,
        r.model,
        String(r.day_ahead),
        `${r.forecast_date || "?"} @ ${fmtPrice(r.predicted_close)}`,
        actual,
        r.correct === true ? "win" : (r.correct === false ? "loss" : "pending"),
      ];
      const tr = document.createElement("tr");
      cells.forEach((cell) => {
        const td = document.createElement("td");
        td.textContent = cell;
        tr.appendChild(td);
      });
      predictionsBodyEl.appendChild(tr);
    });
  }

  async function loadPredictions() {
    if (!predictionsBodyEl) return;
    try {
      renderPredictions(await historyRequest("/api/kronos/predictions?limit=500"));
    } catch (err) {
      setHistoryStatus(String(err.message || err));
    }
  }

  async function loadWinRate() {
    if (!winrateHostEl) return;
    try {
      renderWinRate(await historyRequest("/api/kronos/winrate"));
    } catch (err) {
      setHistoryStatus(String(err.message || err));
    }
  }

  async function refreshWinRate(event) {
    if (event) event.preventDefault();
    if (refreshEl) refreshEl.disabled = true;
    setHistoryStatus("Grading settled sessions…");
    try {
      const data = await historyRequest("/api/kronos/winrate/refresh", { method: "POST" });
      renderWinRate(data);
      await loadPredictions();
      setHistoryStatus(`Graded ${data.graded_now || 0} prediction(s).`);
    } catch (err) {
      setHistoryStatus(String(err.message || err));
    } finally {
      if (refreshEl) refreshEl.disabled = false;
    }
  }

  async function saveToday(event) {
    if (event) event.preventDefault();
    if (!saveEl) return;
    const days = readDays(saveDaysEl || daysEl);
    if (days === null) return;
    saveEl.disabled = true;
    if (refreshEl) refreshEl.disabled = true;
    unmountAll();
    setStatus(`Saving tracked symbols (+${days}d)… this can take a minute.`);
    try {
      const data = await historyRequest("/api/kronos/save", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ days }),
      });
      render(data);
      const saved = data.saved || {};
      await loadWinRate();
      await loadPredictions();
      setHistoryStatus(
        `Saved ${saved.predictions || 0} prediction(s) for ` +
        `${(saved.symbols || []).length} symbol(s).`,
      );
    } catch (err) {
      setStatus(String(err.message || err));
      setHistoryStatus(String(err.message || err));
    } finally {
      saveEl.disabled = false;
      if (refreshEl) refreshEl.disabled = false;
    }
  }

  function setExp2Status(text) {
    if (exp2StatusEl) exp2StatusEl.textContent = text || "";
  }

  function fmtClose(value) {
    return (typeof value === "number" && Number.isFinite(value))
      ? value.toFixed(2)
      : "—";
  }

  function fmtAccuracy(value) {
    return (typeof value === "number" && Number.isFinite(value))
      ? `${value.toFixed(2)}%`
      : "—";
  }

  function renderExperiment(data) {
    if (!exp2BodyEl) return;
    experimentData = data || null;
    const summary = (data && data.summary) || {};
    const saved = summary.saved || {};
    const models = summary.models || {};
    const base = models.base || {};
    const fine = models.finetuned || {};
    if (exp2SummaryEl) {
      exp2SummaryEl.textContent = [
        `${saved.predictions || 0} row(s) across ${saved.symbols || 0} symbol(s)`,
        `graded ${summary.graded || 0}`,
        `pending ${summary.pending || 0}`,
        saved.last_anchor ? `last anchor ${saved.last_anchor}` : "",
        saved.last_graded_anchor
          ? `last graded anchor ${saved.last_graded_anchor}`
          : "no graded rows yet",
        saved.last_saved_at
          ? `last run ${saved.last_saved_at.slice(0, 19).replace("T", " ")}`
          : "",
      ].filter(Boolean).join(" • ");
    }
    if (exp2AvgEl) {
      exp2AvgEl.textContent =
        `Avg Base Accuracy: ${fmtAccuracy(base.avg_accuracy_pct)} • ` +
        `Avg Finetuned Accuracy: ${fmtAccuracy(fine.avg_accuracy_pct)}`;
    }
    renderExperimentRows();
  }

  function renderExperimentRows() {
    if (!exp2BodyEl) return;
    const allRows = (experimentData && experimentData.rows) || [];
    const gradedOnly = !!(exp2GradedOnlyEl && exp2GradedOnlyEl.checked);
    const rows = gradedOnly
      ? allRows.filter((r) => r.actual_close !== null && r.actual_close !== undefined)
      : allRows;
    exp2BodyEl.innerHTML = "";
    if (!rows.length) {
      const tr = document.createElement("tr");
      const td = document.createElement("td");
      td.colSpan = 9;
      td.className = "muted";
      td.textContent = gradedOnly
        ? "No graded rows yet — the predicted sessions have not closed."
        : "No Experiment 2 rows yet.";
      tr.appendChild(td);
      exp2BodyEl.appendChild(tr);
      return;
    }
    rows.forEach((r) => {
      const pending = r.actual_close === null || r.actual_close === undefined;
      const actual = pending
        ? (r.forecast_date ? `pending ${r.forecast_date}` : "pending")
        : fmtClose(r.actual_close);
      const cells = [
        r.symbol,
        r.anchor_date || "—",
        r.forecast_date || "—",
        `+${r.day_ahead}D`,
        actual,
        fmtClose(r.base_close),
        fmtClose(r.finetuned_close),
        fmtAccuracy(r.base_accuracy_pct),
        fmtAccuracy(r.finetuned_accuracy_pct),
      ];
      const tr = document.createElement("tr");
      cells.forEach((cell) => {
        const td = document.createElement("td");
        td.textContent = cell;
        tr.appendChild(td);
      });
      exp2BodyEl.appendChild(tr);
    });
  }

  async function loadExperiment() {
    if (!exp2BodyEl) return;
    try {
      renderExperiment(await historyRequest("/api/kronos/experiment2?limit=2000"));
    } catch (err) {
      setExp2Status(String(err.message || err));
    }
  }

  async function runExperiment(event) {
    if (event) event.preventDefault();
    if (exp2RunEl) exp2RunEl.disabled = true;
    if (exp2RefreshEl) exp2RefreshEl.disabled = true;
    setExp2Status("Running Experiment 2 for the tracked symbols (+5d)… this can take a minute.");
    try {
      const data = await historyRequest("/api/kronos/experiment2/run", { method: "POST" });
      const saved = data.saved || {};
      await loadExperiment();
      setExp2Status(
        `Saved ${saved.predictions || 0} row(s) for ` +
        `${(saved.symbols || []).length} symbol(s); graded ${data.graded_now || 0}.`,
      );
    } catch (err) {
      setExp2Status(String(err.message || err));
    } finally {
      if (exp2RunEl) exp2RunEl.disabled = false;
      if (exp2RefreshEl) exp2RefreshEl.disabled = false;
    }
  }

  async function refreshExperiment(event) {
    if (event) event.preventDefault();
    if (exp2RefreshEl) exp2RefreshEl.disabled = true;
    setExp2Status("Grading settled Experiment 2 rows…");
    try {
      const data = await historyRequest(
        "/api/kronos/experiment2/refresh", { method: "POST" });
      await loadExperiment();
      setExp2Status(`Graded ${data.graded_now || 0} row(s).`);
    } catch (err) {
      setExp2Status(String(err.message || err));
    } finally {
      if (exp2RefreshEl) exp2RefreshEl.disabled = false;
    }
  }

  if (form) form.addEventListener("submit", submit);
  if (topForm) topForm.addEventListener("submit", submitTop);
  if (saveEl) saveEl.addEventListener("click", saveToday);
  if (refreshEl) refreshEl.addEventListener("click", refreshWinRate);
  if (exp2RunEl) exp2RunEl.addEventListener("click", runExperiment);
  if (exp2RefreshEl) exp2RefreshEl.addEventListener("click", refreshExperiment);
  if (exp2GradedOnlyEl) {
    exp2GradedOnlyEl.addEventListener("change", renderExperimentRows);
  }
  loadWinRate();
  loadPredictions();
  loadExperiment();
  // The cron writes new rows daily; keep an open tab current without a reload.
  window.setInterval(() => {
    if (!document.hidden) loadExperiment();
  }, 300000);
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) loadExperiment();
  });
})();
