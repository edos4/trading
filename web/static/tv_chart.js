/* TradingView Lightweight Charts wrapper for paper trade rows. */

window.TVChart = (function () {
  const BG = "#131722";
  const GRID = "#2a2e39";
  const TEXT = "#d1d4dc";
  const DIM = "#787b86";
  const UP = "#26a69a";
  const DOWN = "#ef5350";

  let chart = null;
  let host = null;
  let ro = null;

  function unmount() {
    if (ro) {
      ro.disconnect();
      ro = null;
    }
    if (chart) {
      chart.remove();
      chart = null;
    }
    host = null;
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
    host = el;
    chart = LightweightCharts.createChart(el, {
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
        scaleMargins: { top: 0.08, bottom: 0.22 },
      },
      timeScale: {
        borderColor: GRID,
        rightOffset: 4,
        barSpacing: 8,
        minBarSpacing: 3,
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
    volumeSeries.priceScale().applyOptions({
      scaleMargins: { top: 0.82, bottom: 0 },
    });
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
      candleSeries.setMarkers(data.markers);
    }

    let predSeries = null;
    if ((data.pred_candles || []).length) {
      predSeries = chart.addCandlestickSeries({
        upColor: "#ce93d8",
        downColor: "#7b1fa2",
        wickUpColor: "#ce93d8",
        wickDownColor: "#7b1fa2",
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
        color: data.forecast_color || "#e040fb",
        lineWidth: 2,
        priceLineVisible: false,
        lastValueVisible: true,
        title: "Kronos close",
      });
      forecast.setData(data.forecast);
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

    if (typeof ResizeObserver !== "undefined") {
      ro = new ResizeObserver(() => {
        if (!chart || !host) return;
        chart.applyOptions({ width: host.clientWidth, height: host.clientHeight });
      });
      ro.observe(el);
    }
  }

  return { mount, unmount };
})();
