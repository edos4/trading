/* TradingView Lightweight Charts wrapper for paper trade rows. */

window.TVChart = (function () {
  const BG = "#131722";
  const GRID = "#2a2e39";
  const TEXT = "#d1d4dc";
  const DIM = "#787b86";
  const UP = "#26a69a";
  const DOWN = "#ef5350";
  const RSI = "#7e57c2";

  /* Vertical bands as fractions of the price chart, top to bottom: candles,
   * then volume. RSI gets its own chart underneath (see mount). */
  const PRICE_BAND = { top: 0.10, bottom: 0.70 };
  const VOLUME_BAND = { top: 0.80, bottom: 1 };
  const RSI_PANE_HEIGHT = 170;
  const RSI_OVERBOUGHT = 70;
  const RSI_OVERSOLD = 30;
  /* A hair either side of 0-100 so the line and guides never touch the edge. */
  const RSI_FLOOR = -2;
  const RSI_CEILING = 102;

  const margins = band => ({ top: band.top, bottom: 1 - band.bottom });

  let chart = null;
  let rsiChart = null;
  let cleanup = [];
  let tools = null;
  let currentDraftKey = null;
  let preview = false;
  const drafts = new Map();

  function unmount() {
    cleanup.forEach(fn => fn()); cleanup = [];
    tools = null;
    preview = false;
    if (rsiChart) {
      rsiChart.remove();
      rsiChart = null;
    }
    if (chart) {
      chart.remove();
      chart = null;
    }
  }

  /* Keep the price pane and the RSI pane on the same bars. */
  function linkTimeScales(from, to) {
    let syncing = false;
    from.timeScale().subscribeVisibleLogicalRangeChange((range) => {
      if (!range || syncing) return;
      syncing = true;
      try { to.timeScale().setVisibleLogicalRange(range); } finally { syncing = false; }
    });
    to.timeScale().subscribeVisibleLogicalRangeChange((range) => {
      if (!range || syncing) return;
      syncing = true;
      try { from.timeScale().setVisibleLogicalRange(range); } finally { syncing = false; }
    });
  }

  function fmtTime(time) {
    if (time == null) return "";
    if (typeof time === "string") return time;
    if (typeof time === "number") {
      return new Date(time * 1000).toISOString().slice(0, 10);
    }
    if (time.year) {
      const m = String(time.month).padStart(2, "0");
      const d = String(time.day).padStart(2, "0");
      return `${time.year}-${m}-${d}`;
    }
    return String(time);
  }

  function mount(el, data, hooks) {
    unmount();
    if (!el || typeof LightweightCharts === "undefined") {
      throw new Error("TradingView chart library is not loaded");
    }
    const candles = data.candles || [];
    if (candles.length < 2) {
      throw new Error("not enough bars to chart");
    }
    el.innerHTML = "";
    el.style.display = "";
    el.style.flexDirection = "";
    const rsi = data.rsi14 || [];
    const identity = data.trade_id || data.entry_time;
    const draftKey = identity ? 'pattern-draft-' + JSON.stringify([
      data.replay_id || 'paper', data.market, data.symbol, data.timeframe,
      data.pattern, data.pattern_version_id, identity,
    ]) : null;
    currentDraftKey = draftKey;
    preview = !!hooks?.readOnly;
    function correct() {
      if (!data.pattern) { window.alert('Select a chart with a target pattern to submit corrections.'); return; }
      if (tools?.pending()) { window.alert('Finish or cancel the current drawing first.'); return; }
      try {
        const key = 'pattern-chart-' + Date.now();
        sessionStorage.setItem(key, JSON.stringify({...data, manual_corrections: tools?.snapshot() || []}));
        window.location.href = '/patterns?chart=' + encodeURIComponent(key);
      } catch (err) { window.alert('Could not open pattern editor: ' + err.message); }
    }
    if (!hooks?.readOnly) {
      const menu = document.createElement("button");
      menu.type = "button";
      menu.textContent = "Correct pattern…";
      menu.className = "btn";
      menu.style.cssText = "position:fixed;z-index:10000";
      menu.hidden = true;
      document.body.appendChild(menu);
      const hide = () => { menu.hidden = true; };
      const context = (event) => {
        event.preventDefault();
        menu.style.left = Math.min(event.clientX, window.innerWidth - 160) + "px";
        menu.style.top = Math.min(event.clientY, window.innerHeight - 45) + "px";
        menu.hidden = false;
        menu.focus();
      };
      menu.onclick = correct;
      el.addEventListener("contextmenu", context);
      document.addEventListener("click", hide);
      menu.onkeydown = (event) => { if (event.key === "Escape") hide(); };
      cleanup.push(() => {
        el.removeEventListener("contextmenu", context);
        document.removeEventListener("click", hide);
        menu.remove();
      });
    }
    /* Lightweight Charts v4 has no sub-panes, so an RSI series needs its own
     * chart under the price chart. Only split the host when there is RSI data. */
    let priceEl = el;
    let rsiEl = null;
    if (rsi.length) {
      el.innerHTML = "";
      el.style.display = "flex";
      el.style.flexDirection = "column";
      priceEl = el.ownerDocument.createElement("div");
      priceEl.style.cssText = "flex:1;min-height:0";
      rsiEl = el.ownerDocument.createElement("div");
      rsiEl.style.cssText = `height:${RSI_PANE_HEIGHT}px;flex:none`;
      el.appendChild(priceEl);
      el.appendChild(rsiEl);
    }
    chart = LightweightCharts.createChart(priceEl, {
      autoSize: true,
      layout: {
        background: { color: BG },
        textColor: TEXT,
        fontFamily: "Trebuchet MS, Roboto, sans-serif",
        fontSize: 12,
      },
      grid: {
        vertLines: { color: GRID },
        horzLines: { color: GRID },
      },
      crosshair: {
        mode: LightweightCharts.CrosshairMode.Normal,
        vertLine: { color: DIM, width: 1, style: 3, labelBackgroundColor: "#363a45" },
        horzLine: { color: DIM, width: 1, style: 3, labelBackgroundColor: "#363a45" },
      },
      rightPriceScale: {
        borderColor: GRID,
        scaleMargins: margins(PRICE_BAND),
      },
      timeScale: {
        borderColor: GRID,
        rightOffset: 4,
        barSpacing: 8,
        minBarSpacing: 0.5,
        // The RSI pane below owns the date axis when it is present.
        visible: !rsiEl,
      },
      handleScroll: { mouseWheel: true, pressedMouseMove: true, horzTouchDrag: true },
      handleScale: { axisPressedMouseMove: true, mouseWheel: true, pinch: true },
    });

    const candleSeries = chart.addCandlestickSeries({
      upColor: UP,
      downColor: DOWN,
      wickUpColor: UP,
      wickDownColor: DOWN,
      borderVisible: false,
    });
    candleSeries.setData(candles);



    const volumeSeries = chart.addHistogramSeries({
      priceFormat: { type: "volume" },
      priceScaleId: "",
    });
    volumeSeries.priceScale().applyOptions({ scaleMargins: margins(VOLUME_BAND) });
    volumeSeries.setData(data.volume || []);

    for (const level of data.levels || []) {
      candleSeries.createPriceLine({
        price: level.price,
        color: level.color,
        lineWidth: 1,
        lineStyle: LightweightCharts.LineStyle.Dashed,
        axisLabelVisible: true,
        title: level.title,
      });
    }
    if ((data.markers || []).length) {
      candleSeries.setMarkers(data.markers.filter(marker => marker.price == null));
      for (const marker of data.markers.filter(marker => marker.price != null)) {
        const pivot = chart.addLineSeries({
          color: marker.color, lineVisible: false,
          priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false,
        });
        pivot.setData([{ time: marker.time, value: marker.price }]);
        // Parts carrying a reason get their label from PatternNotes, which lays
        // it out together with the reason; the library would draw it alone.
        pivot.setMarkers([{ ...marker, text: marker.reason ? "" : (marker.text || "") }]);
      }
    }

    if (window.PatternNotes) {
      const notes = window.PatternNotes.collect(data);
      if (notes.length) candleSeries.attachPrimitive(window.PatternNotes.create(notes, PRICE_BAND));
    }

    for (const segment of data.segments || []) {
      if (!segment.data || segment.data.length < 2) continue;
      const line = chart.addLineSeries({
        color: segment.color, lineWidth: Math.max(1, Math.min(4, Math.round(segment.width || 2))),
        lineStyle: segment.style === "--" ? LightweightCharts.LineStyle.Dashed : LightweightCharts.LineStyle.Solid,
        priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false,
      });
      line.setData(segment.data);
      if (segment.label) {
        const point = segment.data[Math.floor((segment.data.length - 1) / 2)];
        line.setMarkers([{
          time: point.time,
          position: /lower|support/i.test(segment.label) ? "belowBar" : "aboveBar",
          shape: "circle", size: 0, color: segment.color,
          text: segment.reason ? "" : segment.label,
        }]);
      }
    }

    /* RSI only starts once its warm-up window has filled, so the series has
     * fewer bars than the candles. Pad the missing bars with whitespace: both
     * charts then share one time scale and the panes stay aligned. */
    const rsiValues = new Map(rsi.map((row) => [row.time, row.value]));
    const rsiData = [];
    const rsiSeen = new Set();
    for (const row of candles.concat(data.pred_candles || [])) {
      if (rsiSeen.has(row.time)) continue;
      rsiSeen.add(row.time);
      const value = rsiValues.get(row.time);
      rsiData.push(value == null ? { time: row.time } : { time: row.time, value });
    }

    let predSeries = null;
    let rsiSeries = null;
    if ((data.pred_candles || []).length) {
      predSeries = chart.addCandlestickSeries({
        upColor: "#ffeb3b",
        downColor: "#f9a825",
        wickUpColor: "#ffeb3b",
        wickDownColor: "#f9a825",
        borderVisible: false,
        title: "Kronos",
      });
      predSeries.setData(
        (data.pred_candles || []).map((row) => ({
          time: row.time,
          open: row.open,
          high: row.high,
          low: row.low,
          close: row.close,
        }))
      );
    }
    if ((data.forecast || []).length) {
      const forecast = chart.addLineSeries({
        color: data.forecast_color || "#ffeb3b",
        lineWidth: 2,
        priceLineVisible: false,
        lastValueVisible: true,
        title: "Kronos close",
      });
      forecast.setData(data.forecast);
    }
    chart.subscribeCrosshairMove((param) => {
      if (!hooks || typeof hooks.onCandle !== "function") return;
      if (!param || param.time == null) {
        hooks.onCandle(null);
        return;
      }
      const bar = param.seriesData.get(candleSeries) || (predSeries && param.seriesData.get(predSeries));
      if (!bar) {
        hooks.onCandle(null);
        return;
      }
      const predicted = !!(predSeries && param.seriesData.get(predSeries) && !param.seriesData.get(candleSeries));
      hooks.onCandle({
        time: fmtTime(param.time),
        open: bar.open,
        high: bar.high,
        low: bar.low,
        close: bar.close,
        predicted,
      });
    });
    chart.timeScale().fitContent();

    if (rsiEl) {
      rsiChart = LightweightCharts.createChart(rsiEl, {
        autoSize: true,
        layout: {
          background: { color: BG },
          textColor: TEXT,
          fontFamily: "Trebuchet MS, Roboto, sans-serif",
          fontSize: 11,
        },
        grid: { vertLines: { color: GRID }, horzLines: { color: GRID } },
        crosshair: {
          mode: LightweightCharts.CrosshairMode.Normal,
          vertLine: { color: DIM, width: 1, style: 3, labelBackgroundColor: "#363a45" },
          horzLine: { color: DIM, width: 1, style: 3, labelBackgroundColor: "#363a45" },
        },
        rightPriceScale: {
          borderColor: GRID,
          // The range is pinned just past 0-100, so the axis never prints
          // impossible values the way a stretched overlay scale would.
          scaleMargins: { top: 0, bottom: 0 },
        },
        timeScale: {
          borderColor: GRID,
          rightOffset: 4,
          barSpacing: 8,
          minBarSpacing: 0.5,
        },
        handleScroll: { mouseWheel: true, pressedMouseMove: true, horzTouchDrag: true },
        handleScale: { axisPressedMouseMove: true, mouseWheel: true, pinch: true },
      });
      rsiSeries = rsiChart.addLineSeries({
        color: RSI,
        lineWidth: 1,
        priceLineVisible: false,
        lastValueVisible: true,
        crosshairMarkerVisible: false,
        priceFormat: { type: "price", precision: 1, minMove: 0.1 },
      });
      rsiSeries.applyOptions({
        autoscaleInfoProvider: () => ({
          priceRange: { minValue: RSI_FLOOR, maxValue: RSI_CEILING },
        }),
      });
      for (const level of [RSI_OVERBOUGHT, RSI_OVERSOLD]) {
        rsiSeries.createPriceLine({
          price: level,
          color: DIM,
          lineWidth: 1,
          lineStyle: LightweightCharts.LineStyle.Dashed,
          axisLabelVisible: true,
          title: String(level),
        });
      }
      rsiSeries.setData(rsiData);
      rsiChart.timeScale().fitContent();
      linkTimeScales(chart, rsiChart);
      // Hovering the oscillator still reports the candle under the cursor.
      rsiChart.subscribeCrosshairMove((param) => {
        if (!hooks || typeof hooks.onCandle !== "function") return;
        const time = param && param.time != null ? fmtTime(param.time) : null;
        const bar = time == null ? null : candles.find((c) => c.time === time);
        hooks.onCandle(bar ? { ...bar } : null);
      });
    }

    if (window.ChartDraw) {
      const panes = [{ el: priceEl, chart, series: candleSeries, pane: "price" }];
      if (rsiSeries) panes.push({ el: rsiEl, chart: rsiChart, series: rsiSeries, pane: "rsi" });
      let saved = data.manual_corrections || [];
      if (!hooks?.readOnly && draftKey) {
        try { saved = JSON.parse(sessionStorage.getItem(draftKey)) || drafts.get(draftKey) || saved; }
        catch (_) { saved = drafts.get(draftKey) || saved; }
      }
      tools = window.ChartDraw.attach({
        host: el,
        panes,
        times: candles.map((row) => row.time),
        shapes: saved, readOnly: !!hooks?.readOnly, onCorrect: correct,
        onChange(shapes) {
          if (!draftKey || hooks?.readOnly) return;
          drafts.set(draftKey, shapes);
          try { sessionStorage.setItem(draftKey, JSON.stringify(shapes)); }
          catch (_) { /* In-memory drafts still survive modal close/reopen. */ }
        },
      });
      const drawingTools = tools;
      cleanup.push(() => drawingTools.destroy());
      if (!draftKey && !hooks?.readOnly) {
        const warn = event => {
          if (drawingTools.snapshot().length) { event.preventDefault(); event.returnValue = ''; }
        };
        window.addEventListener('beforeunload', warn);
        cleanup.push(() => window.removeEventListener('beforeunload', warn));
      }
    }
  }

  /* Both panes stacked, so a capture still shows the whole chart. */
  function capture() {
    const top = chart.takeScreenshot();
    if (!rsiChart) return top.toDataURL("image/png");
    const bottom = rsiChart.takeScreenshot();
    const out = document.createElement("canvas");
    out.width = Math.max(top.width, bottom.width);
    out.height = top.height + bottom.height;
    const ctx = out.getContext("2d");
    ctx.drawImage(top, 0, 0);
    ctx.drawImage(bottom, 0, top.height);
    return out.toDataURL("image/png");
  }

  function canClose() {
    if (preview) return true;
    const count = tools ? tools.snapshot().length : 0;
    if (tools && tools.pending() && !window.confirm('Discard the unfinished drawing? Completed corrections are kept.')) return false;
    if (currentDraftKey || !count) return true;
    return window.confirm('This chart has no stable identity. Discard its corrections?');
  }
  return { mount, unmount, capture, canClose };
})();
