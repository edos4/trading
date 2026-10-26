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
  let diiChart = null;
  let diiLegend = null;
  let cleanup = [];
  let tools = null;
  let currentDraftKey = null;
  let preview = false;
  const drafts = new Map();
  /* Overlays the reader can hide. The state lives here rather than in the
   * panes, so hiding a pane and re-mounting the chart are the same operation
   * and the choice sticks for every chart opened afterwards, including after a
   * reload or a jump to another tab. */
  const view = { patterns: true, rsi: true, dii: true };
  try {
    const saved = JSON.parse(localStorage.getItem("chart-view") || "null");
    for (const key of Object.keys(view)) {
      if (typeof saved?.[key] === "boolean") view[key] = saved[key];
    }
  } catch (_) { /* storage unavailable: start with every overlay shown */ }
  let lastMount = null;

  function unmount() {
    cleanup.forEach(fn => fn()); cleanup = [];
    lastMount = null;
    tools = null;
    diiLegend = null;
    preview = false;
    if (rsiChart) {
      rsiChart.remove();
      rsiChart = null;
    }
    if (diiChart) { diiChart.remove(); diiChart = null; }
    if (chart) {
      chart.remove();
      chart = null;
    }
  }

  /* Matching logical ranges also need matching plot widths: each chart sizes
   * its price axis from its own labels, otherwise the same bar has a different
   * pixel x in each pane. Recheck after labels or the host size change. */
  function linkTimeScales(...charts) {
    let syncing = false, aligning = false;
    const scales = charts.map(chart => chart.timeScale());
    const widths = charts.map(chart => chart.paneSize().width);
    const pending = new Map();
    const sameRange = (a, b) => a && b &&
      Math.abs(a.from - b.from) < 1e-9 && Math.abs(a.to - b.to) < 1e-9;
    let linkedRange = scales[0].getVisibleLogicalRange();
    function syncRange(scale, range, force = false) {
      if (!range || syncing || aligning) return;
      const current = scale.getVisibleLogicalRange();
      const queued = pending.get(scale);
      if (!force && (sameRange(queued, range) || (!queued && sameRange(current, range)))) return;
      pending.set(scale, range);
      syncing = true;
      try { scale.setVisibleLogicalRange(range); } finally { syncing = false; }
    }
    function alignWidths() {
      if (aligning) return;
      aligning = true;
      try {
        const axes = charts.map(chart => chart.priceScale("right"));
        const width = Math.max(...axes.map(axis => axis.width()));
        if (width > 0) for (const axis of axes) {
          if (axis.options().minimumWidth < width) axis.applyOptions({minimumWidth: width});
        }
        charts.forEach((chart, index) => { widths[index] = chart.paneSize().width; });
      } finally { aligning = false; }
      scales.forEach(scale => syncRange(scale, linkedRange));
    }
    const handlers = charts.map((chart, index) => range => {
      if (!range || syncing || aligning) return;
      const width = chart.paneSize().width;
      const queued = pending.get(scales[index]);
      const reflected = sameRange(queued, range);
      pending.delete(scales[index]);
      // Axis/host resizing changes the native range at the old bar spacing.
      // Keep the user's last range while both panes adopt their new widths.
      // A mirrored API range is applied on a later paint; its callback must
      // not replace a newer user pan on the other pane.
      if (!reflected && (width === widths[index] || !linkedRange)) linkedRange = range;
      widths[index] = width;
      alignWidths();
      // A user can move this pane before its queued mirror has painted.
      // Replace that older action even when the current range already fits.
      if (queued && !reflected) syncRange(scales[index], linkedRange, true);
    });
    scales.forEach((scale, index) => scale.subscribeVisibleLogicalRangeChange(handlers[index]));
    alignWidths();
    return () => {
      scales.forEach((scale, index) => scale.unsubscribeVisibleLogicalRangeChange(handlers[index]));
    };
  }

  // Canvas primitives share the chart's coordinates, resize and screenshot lifecycle.
  function attachPainter(series, paint, zOrder = "normal") {
    const primitive = {
      attached() {}, detached() {}, updateAllViews() {},
      paneViews: () => [{ zOrder: () => zOrder, renderer: () => ({
        draw: target => target.useMediaCoordinateSpace(scope => {
          scope.context.save();
          try { paint(scope); } finally { scope.context.restore(); }
        }),
      }) }],
    };
    series.attachPrimitive(primitive);
    cleanup.push(() => series.detachPrimitive(primitive));
  }

  function paintImbalance(context, points, upSeries, timeScale) {
    const polygon = (a, b) => {
      const orange = a.up + b.up > a.down + b.down;
      const y = [a.up, b.up, b.down, a.down].map(value => upSeries.priceToCoordinate(value));
      if (y.some(value => value == null) || Math.max(...y) === Math.min(...y)) return;
      const gradient = context.createLinearGradient(0, Math.min(...y), 0, Math.max(...y));
      gradient.addColorStop(0, orange ? "rgba(251,140,0,0.55)" : "rgba(41,98,255,0.55)");
      gradient.addColorStop(1, orange ? "rgba(251,140,0,0.02)" : "rgba(41,98,255,0.02)");
      context.fillStyle = gradient;
      context.beginPath(); context.moveTo(a.x, y[0]); context.lineTo(b.x, y[1]);
      context.lineTo(b.x, y[2]); context.lineTo(a.x, y[3]); context.closePath(); context.fill();
    };
    let previous = null;
    for (const row of points) {
      const x = timeScale.timeToCoordinate(row.time);
      if (x == null || !Number.isFinite(row.up) || !Number.isFinite(row.down)) { previous = null; continue; }
      const point = {x, up: row.up, down: row.down};
      if (previous) {
        const a = previous.up - previous.down, b = point.up - point.down;
        if (a * b < 0) {
          const fraction = a / (a - b);
          const value = previous.up + fraction * (point.up - previous.up);
          const cross = {x: previous.x + fraction * (point.x - previous.x), up: value, down: value};
          polygon(previous, cross); polygon(cross, point);
        } else polygon(previous, point);
      }
      previous = point;
    }
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

  /* A rail has only its two endpoint samples. Lightweight Charts autoscales
   * samples inside the viewport, so zooming between both endpoints otherwise
   * omits the visible rail entirely and can clip it off the price pane. Include
   * its interpolated visible extent without extending the line. Clip in pixel
   * coordinates: logical ranges describe bar slots, while the physical plot
   * edges include part of the adjacent slots. */
  function railAutoscale(chart, points) {
    const timeScale = chart.timeScale();
    return original => {
      const info = original();
      // timeScale.width() is zero while this pane's date axis is hidden.
      const width = chart.paneSize().width;
      if (!(width > 0)) return info;
      // The original range rounds to bar slots and can include an endpoint
      // already outside the physical plot. Only visible geometry sets bounds.
      let minValue = Infinity, maxValue = -Infinity;
      for (let i = 1; i < points.length; i++) {
        const a = points[i - 1], b = points[i];
        const x0 = timeScale.timeToCoordinate(a.time), x1 = timeScale.timeToCoordinate(b.time);
        if (x0 == null || x1 == null) continue;
        if (x1 <= x0) continue;
        const left = Math.max(0, x0), right = Math.min(width, x1);
        if (left > right) continue;
        const valueAt = x => a.value + (b.value - a.value) * (x - x0) / (x1 - x0);
        const first = valueAt(left), last = valueAt(right);
        minValue = Math.min(minValue, first, last);
        maxValue = Math.max(maxValue, first, last);
      }
      if (!Number.isFinite(minValue) || !Number.isFinite(maxValue)) return null;
      return {...info, priceRange: {minValue, maxValue}};
    };
  }

  /* Rails are fitted in actual-candle indices. An overlay can introduce an
   * intervening date into the library's shared time scale; preserve the fitted
   * price at every candle rather than letting that date shift the whole rail. */
  function railSamples(points, candles, candleIndices, extraTimes) {
    if (!extraTimes.some(time => points[0].time < time && time < points.at(-1).time)) return points;
    const sampled = [];
    for (let i = 1; i < points.length; i++) {
      const a = points[i - 1], b = points[i];
      const start = candleIndices.get(a.time), end = candleIndices.get(b.time);
      if (start == null || end == null || end <= start) return points;
      if (!sampled.length) sampled.push(a);
      for (let j = start + 1; j < end; j++) sampled.push({
        time: candles[j].time, value: a.value + (b.value - a.value) * (j - start) / (end - start),
      });
      sampled.push(b);
    }
    return sampled;
  }

  /* Hiding an overlay keeps its data: re-mount the same payload with the
   * toggled flag, preserving the visible bar range so the view does not jump. */
  function toggleView(key) {
    const target = lastMount;
    if (!target) return;
    const range = chart ? chart.timeScale().getVisibleLogicalRange() : null;
    view[key] = !view[key];
    try { localStorage.setItem("chart-view", JSON.stringify(view)); }
    catch (_) { /* storage unavailable: the choice lasts for this page */ }
    mount(target.el, target.data, target.hooks, range);
  }

  function viewToggles(host, available) {
    const items = [
      ["patterns", "Patterns", "Show or hide pattern overlays and corrections", available.patterns],
      ["rsi", "RSI", "Show or hide the RSI(14) pane", available.rsi],
      ["dii", "DII", "Show or hide the Directional Imbalance Index pane", available.dii],
    ].filter(item => item[3]);
    if (!items.length) return null;
    const bar = host.ownerDocument.createElement("div");
    bar.className = "chart-view-toggles";
    bar.style.cssText = "display:flex;gap:4px";
    for (const [key, label, hint] of items) {
      const button = host.ownerDocument.createElement("button");
      button.type = "button";
      button.textContent = label;
      button.title = hint;
      button.className = "btn";
      button.style.cssText = "font:11px sans-serif;padding:2px 6px";
      const on = view[key];
      button.setAttribute("aria-pressed", String(on));
      if (on) { button.style.backgroundColor = "#42a5f5"; button.style.color = "#131722"; }
      button.addEventListener("click", () => toggleView(key));
      bar.appendChild(button);
    }
    return bar;
  }

  function mount(el, data, hooks, restoreRange) {
    unmount();
    if (!el || typeof LightweightCharts === "undefined") {
      throw new Error("TradingView chart library is not loaded");
    }
    const candles = data.candles || [];
    if (candles.length < 2) {
      throw new Error("not enough bars to chart");
    }
    lastMount = {el, data, hooks};
    el.innerHTML = "";
    el.style.display = "";
    el.style.flexDirection = "";
    el.style.position = "relative";
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
    if (!hooks?.readOnly && view.patterns) {
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
     * chart under the price chart. Only split the host when a pane is shown. */
    const showRsi = view.rsi && rsi.length > 0;
    const showDii = view.dii && !!data.dii;
    let priceEl = el;
    let rsiEl = null;
    let diiEl = null;
    if (showRsi || showDii) {
      el.innerHTML = "";
      el.style.display = "flex";
      el.style.flexDirection = "column";
      priceEl = el.ownerDocument.createElement("div");
      priceEl.style.cssText = "flex:1;min-height:0";
      el.appendChild(priceEl);
      if (showRsi) {
        rsiEl = el.ownerDocument.createElement("div");
        rsiEl.style.cssText = `height:${RSI_PANE_HEIGHT}px;flex:none`;
        el.appendChild(rsiEl);
      }
      if (showDii) {
        diiEl = el.ownerDocument.createElement("div");
        diiEl.style.cssText = "height:220px;flex:none;position:relative";
        el.appendChild(diiEl);
      }
      priceEl.style.minHeight = "240px";
      el.style.overflowY = "auto";
    }
    const toggles = viewToggles(el, {
      patterns: !!((data.segments || []).length || (data.trendlines || []).length
        || (data.markers || []).length || (data.manual_corrections || []).length || data.pattern),
      rsi: rsi.length > 0,
      dii: !!data.dii,
    });
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
        visible: !rsiEl && !diiEl,
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
    if (view.patterns && (data.markers || []).length) {
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

    if (view.patterns && window.PatternNotes) {
      const notes = window.PatternNotes.collect(data);
      if (notes.length) candleSeries.attachPrimitive(window.PatternNotes.create(notes, PRICE_BAND));
    }

    const automaticRails = new Set(view.patterns ? data.trendlines || [] : []);
    const overlays = view.patterns ? [...(data.segments || []), ...automaticRails] : [];
    const candleIndices = new Map(candles.map((row, index) => [row.time, index]));
    const priceTimes = [...new Set(candles.concat(
      data.volume || [], data.pred_candles || [], data.forecast || [],
      view.patterns ? (data.markers || []).filter(marker => marker.price != null) : [],
      overlays.flatMap(segment => segment.data || []),
    ).map(row => row.time))].sort();
    const extraTimes = priceTimes.filter(time => !candleIndices.has(time));
    for (const segment of overlays) {
      if (!segment.data || segment.data.length < 2) continue;
      const points = automaticRails.has(segment)
        ? railSamples(segment.data, candles, candleIndices, extraTimes)
        : segment.data;
      const line = chart.addLineSeries({
        color: segment.color, lineWidth: Math.max(1, Math.min(4, Math.round(segment.width || 2))),
        lineStyle: segment.style === "--"
          ? LightweightCharts.LineStyle.Dashed
          : segment.style === ":"
            ? LightweightCharts.LineStyle.Dotted
            : LightweightCharts.LineStyle.Solid,
        priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false,
        autoscaleInfoProvider: automaticRails.has(segment)
          ? railAutoscale(chart, points)
          : undefined,
      });
      line.setData(points);
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

    /* RSI starts after warm-up and forecasts can add dates without predicted
     * candles. Pad every price-pane date with whitespace so both panes use the
     * same logical indices when fitting, zooming or panning the chart. */
    const rsiValues = new Map(rsi.map((row) => [row.time, row.value]));
    const rsiData = priceTimes.map(time => {
      const value = rsiValues.get(time);
      return value == null ? {time} : {time, value};
    });

    let predSeries = null;
    let rsiSeries = null;
    if ((data.pred_candles || []).length) {
      predSeries = chart.addCandlestickSeries({
        upColor: "#ffeb3b",
        downColor: "#f9a825",
        wickUpColor: "#ffeb3b",
        wickDownColor: "#f9a825",
        borderVisible: false,
        title: "Forecast",
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
        title: "Forecast close",
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
          visible: !diiEl,
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
      if (!diiEl) cleanup.push(linkTimeScales(chart, rsiChart));
      // Hovering the oscillator still reports the candle under the cursor.
      rsiChart.subscribeCrosshairMove((param) => {
        if (!hooks || typeof hooks.onCandle !== "function") return;
        const time = param && param.time != null ? fmtTime(param.time) : null;
        const bar = time == null ? null : candles.find((c) => c.time === time);
        hooks.onCandle(bar ? { ...bar } : null);
      });
    }

    if (diiEl) {
      const dii = data.dii;
      const colors = { up: "#FB8C00", down: "#2962FF" };
      diiChart = LightweightCharts.createChart(diiEl, {
        autoSize: true,
        layout: { background: { color: BG }, textColor: TEXT, fontSize: 11 },
        grid: { vertLines: { visible: false }, horzLines: { visible: false } },
        rightPriceScale: { borderColor: GRID, scaleMargins: { top: 0.18, bottom: 0.12 } },
        timeScale: { borderColor: GRID, rightOffset: 4, barSpacing: 8, minBarSpacing: 0.5 },
        crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
      });
      const countSeries = {};
      for (const [field, color] of Object.entries(colors)) {
        const series = diiChart.addLineSeries({ color, lineWidth: 1, title: field, priceLineVisible: false,
          priceFormat: { type: "price", precision: 0, minMove: 1 }, lastValueVisible: false });
        series.setData(dii[field] || []);
        countSeries[field] = series;
        const band = chart.addLineSeries({ color, lineWidth: 1, priceLineVisible: false,
          lastValueVisible: false, crosshairMarkerVisible: false });
        band.setData(dii[field === "up" ? "upper" : "lower"] || []);
      }
      const downValues = new Map((dii.down || []).map(row => [row.time, row.value]));
      const countPoints = (dii.up || []).map(row => ({time: row.time, up: row.value, down: downValues.get(row.time)}));
      attachPainter(countSeries.up, ({context}) => {
        paintImbalance(context, countPoints, countSeries.up, diiChart.timeScale());
      }, "bottom");
      const lastTime = candles[candles.length - 1]?.time;
      const latest = field => (dii[field] || []).find(row => row.time === lastTime)?.value;
      attachPainter(countSeries.up, ({context, mediaSize}) => {
        const x = diiChart.timeScale().timeToCoordinate(lastTime);
        if (x == null || x < 0 || x > mediaSize.width) return;
        context.font = "11px sans-serif";
        for (const [field, percent] of [["up", "bulls"], ["down", "bears"]]) {
          const value = latest(field), share = latest(percent);
          if (!Number.isFinite(value)) continue;
          const y = countSeries[field].priceToCoordinate(value);
          if (y == null) continue;
          context.fillStyle = colors[field];
          context.fillText(Number.isFinite(share) ? `${share.toFixed(2)}%` : "—", Math.min(x + 8, mediaSize.width - 55),
            y + (latest("up") === latest("down") ? (field === "up" ? -7 : 13) : (value > latest(field === "up" ? "down" : "up") ? -7 : 13)));
        }
      });
      const extrema = Object.fromEntries(["upper", "lower"].map(field =>
        [field, new Map((dii[field] || []).map(row => [row.time, row.value]))]));
      attachPainter(candleSeries, ({context}) => {
        context.font = "10px sans-serif";
        context.textAlign = "center";
        // Like Pine's last-bar labels, mark only events in the displayed calculation period.
        for (const bar of candles.slice(-dii.period)) {
          const x = chart.timeScale().timeToCoordinate(bar.time);
          if (x == null) continue;
          for (const [field, price, extreme, offset] of [
            ["up", bar.high, extrema.upper.get(bar.time), -8],
            ["down", bar.low, extrema.lower.get(bar.time), 14],
          ]) {
            if (!Number.isFinite(extreme) || price !== extreme) continue;
            const y = candleSeries.priceToCoordinate(price);
            if (y == null) continue;
            context.fillStyle = colors[field]; context.fillText("+1", x, y + offset);
          }
        }
        context.textAlign = "start";
      });
      const legend = el.ownerDocument.createElement("div");
      legend.style.cssText = "position:absolute;left:8px;top:4px;z-index:2;color:#d1d4dc;pointer-events:none;font-size:11px";
      diiEl.appendChild(legend);
      diiLegend = legend;
      const readout = time => {
        const value = field => (dii[field] || []).find(row => row.time === time)?.value;
        const fmt = (field, percent = false) => Number.isFinite(value(field))
          ? value(field).toFixed(percent ? 1 : 0) + (percent ? "%" : "") : "—";
        const ready = Number.isFinite(value("up")) && Number.isFinite(value("down"));
        legend.textContent = `DII (${dii.length}, ${dii.period}) · ${time} · ${time === candles[candles.length - 1]?.time ? "last displayed bar · " : ""}Up ${fmt("up")} · Down ${fmt("down")} · Bulls ${fmt("bulls", true)} · Bears ${fmt("bears", true)}${ready ? "" : " · warmup / unavailable"}`;
      };
      readout(candles[candles.length - 1]?.time);
      diiChart.timeScale().fitContent();
      cleanup.push(linkTimeScales(...[chart, rsiChart, diiChart].filter(Boolean)));
      const paneCharts = [chart, rsiChart, diiChart].filter(Boolean);
      let syncingCrosshair = false;
      for (const source of paneCharts) {
        const handler = param => {
          if (syncingCrosshair) return;
          syncingCrosshair = true;
          try {
            const time = param?.time == null ? null : fmtTime(param.time);
            const bar = candles.find(row => row.time === time);
            readout(time || candles[candles.length - 1]?.time);
            if (source === diiChart && typeof hooks?.onCandle === "function") hooks.onCandle(bar || null);
            for (const target of paneCharts) {
              if (target === source) continue;
              if (!bar) target.clearCrosshairPosition();
              else if (target === chart) target.setCrosshairPosition(bar.close, time, candleSeries);
              else if (target === rsiChart) target.setCrosshairPosition(rsiValues.get(time) ?? 50, time, rsiSeries);
              else target.setCrosshairPosition((dii.up || []).find(row => row.time === time)?.value ?? 0, time, countSeries.up);
            }
          } finally { syncingCrosshair = false; }
        };
        source.subscribeCrosshairMove(handler);
        cleanup.push(() => source.unsubscribeCrosshairMove(handler));
      }
    }

    if (window.ChartDraw && view.patterns) {
      const panes = [{ el: priceEl, chart, series: candleSeries, pane: "price" }];
      if (rsiSeries) panes.push({ el: rsiEl, chart: rsiChart, series: rsiSeries, pane: "rsi" });
      let saved = data.manual_corrections || [];
      if (!hooks?.readOnly && draftKey) {
        try { saved = JSON.parse(sessionStorage.getItem(draftKey)) || drafts.get(draftKey) || saved; }
        catch (_) { saved = drafts.get(draftKey) || saved; }
      }
      tools = window.ChartDraw.attach({
        host: el,
        leading: toggles,
        panes,
        times: candles.map((row) => row.time),
        shapes: saved, readOnly: !!hooks?.readOnly, onCorrect: correct,
        guidance: (data.trendlines || []).some(line => line.data?.length >= 2)
          ? 'Blue rails: solid confirmed · dashed broken · dotted <3 touches. Corrections have point handles.'
          : undefined,
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

    if (toggles && !tools) {
      toggles.style.cssText += ";position:absolute;top:4px;left:6px;z-index:6;"
        + "background:#131722;padding:2px;border:1px solid " + GRID + ";border-radius:4px";
      el.appendChild(toggles);
    }

    if (restoreRange) chart.timeScale().setVisibleLogicalRange(restoreRange);
  }

  /* Both panes stacked, so a capture still shows the whole chart. */
  function capture() {
    const shots = [chart, rsiChart, diiChart].filter(Boolean).map(pane => pane.takeScreenshot());
    const out = document.createElement("canvas");
    out.width = Math.max(...shots.map(shot => shot.width));
    out.height = shots.reduce((height, shot) => height + shot.height, 0);
    const ctx = out.getContext("2d");
    let y = 0;
    for (const shot of shots) { ctx.drawImage(shot, 0, y); y += shot.height; }
    if (diiLegend) {
      const top = shots.slice(0, -1).reduce((height, shot) => height + shot.height, 0);
      ctx.fillStyle = TEXT; ctx.font = "11px sans-serif";
      ctx.fillText(diiLegend.textContent, 8, top + 14);
    }
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
