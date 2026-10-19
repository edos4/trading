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

  const MAX_TOP = 100;
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

    chart.timeScale().fitContent();
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

  if (form) form.addEventListener("submit", submit);
  if (topForm) topForm.addEventListener("submit", submitTop);
})();
