/* Kronos tab: symbol + days → actual vs base vs fine-tuned forecast chart. */
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
  const saveEl = document.getElementById("kronos-save");
  const statusEl = document.getElementById("kronos-status");
  const titleEl = document.getElementById("kronos-title");
  const hostEl = document.getElementById("kronos-chart");

  let chart = null;
  let last = null;

  function setStatus(text) {
    if (statusEl) statusEl.textContent = text || "";
  }

  function unmount() {
    if (chart) {
      chart.remove();
      chart = null;
    }
  }

  function pct(anchor, value) {
    if (!anchor) return "";
    return ((value / anchor - 1) * 100).toFixed(2);
  }

  function render(data) {
    if (typeof LightweightCharts === "undefined") {
      throw new Error("chart library failed to load");
    }
    unmount();
    hostEl.innerHTML = "";
    chart = LightweightCharts.createChart(hostEl, {
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
    });

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
  }

  function describe(data) {
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
    return `${data.symbol} ${anchorTxt} • ${bits.join(" • ")}`;
  }

  async function submit(event) {
    if (event) event.preventDefault();
    const symbol = (symbolEl.value || "").trim().toUpperCase();
    const days = parseInt(daysEl.value, 10);
    if (!symbol) { setStatus("Enter a symbol."); return; }
    if (!Number.isFinite(days) || days < 1 || days > 60) {
      setStatus("Days must be between 1 and 60."); return;
    }
    submitEl.disabled = true;
    saveEl.disabled = true;
    setStatus(`Running Kronos for ${symbol} (+${days}d)… this can take a minute.`);
    try {
      const res = await fetch("/api/kronos/forecast", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ symbol, days }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        throw new Error(data.detail || data.message || `Request failed (${res.status})`);
      }
      render(data);
      last = data;
      titleEl.textContent = `${data.symbol} — ${data.days} trading days ahead`;
      setStatus(describe(data));
      saveEl.disabled = false;
    } catch (err) {
      setStatus(String(err.message || err));
    } finally {
      submitEl.disabled = false;
    }
  }

  function saveChart() {
    if (!chart || !last) return;
    try {
      const url = chart.takeScreenshot().toDataURL("image/png");
      const a = document.createElement("a");
      a.href = url;
      a.download = `${last.symbol}_kronos_${last.days}d.png`;
      a.click();
    } catch (err) {
      setStatus(`Could not save chart: ${err.message || err}`);
    }
  }

  if (form) form.addEventListener("submit", submit);
  if (saveEl) saveEl.addEventListener("click", saveChart);
})();
