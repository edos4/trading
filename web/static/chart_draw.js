/* Manual chart annotations: trendlines and dotted vertical lines, with undo.
 *
 * Shapes are stored by (time, value) plus the pane they belong to, so they stay
 * anchored to their bars while the chart is panned or zoomed. A trendline drawn
 * on the RSI pane is anchored to RSI values and drawn by that pane's primitive;
 * a vertical line spans every pane. Rendering goes through series primitives,
 * which keeps drawings on the chart canvas instead of an HTML overlay.
 */
window.ChartDraw = (function () {
  const LINE = "rgba(66,165,245,0.95)";
  const DRAFT = "rgba(66,165,245,0.55)";
  const VERT = "rgba(66,165,245,0.8)";
  const HANDLE = "rgba(144,202,249,0.95)";
  const MIN_DRAG = 4;      // px a trendline must span to count as a drawing
  const MAX_HISTORY = 50;

  const BTN = "padding:3px 8px;border:1px solid #2a2e39;border-radius:4px;"
    + "font:11px 'Trebuchet MS',Roboto,sans-serif;color:#d1d4dc;background:#1e222d;"
    + "cursor:pointer;user-select:none;line-height:1.4;";
  const BTN_ON = "padding:3px 8px;border:1px solid #42a5f5;border-radius:4px;"
    + "font:11px 'Trebuchet MS',Roboto,sans-serif;color:#0b0e14;background:#42a5f5;"
    + "cursor:pointer;user-select:none;line-height:1.4;";

  /* One primitive per pane; `painter.request` is filled in from attached().
   * Each end of a trendline names its own pane, so a line can run from the
   * price pane into the RSI pane. Endpoints are solved in a stacked coordinate
   * space (pane offset + local y) and every pane draws the same line; each
   * canvas clips it to itself, which is what joins the two panes seamlessly. */
  function createPrimitive(state, painter, panes, myPane) {
    let series = null;
    let chart = null;

    function paneOf(name) {
      return panes.find((entry) => entry.pane === name) || panes[0];
    }

    function point(time, value, paneName) {
      const entry = paneOf(paneName);
      const x = chart.timeScale().timeToCoordinate(time);
      const y = entry.series.priceToCoordinate(value);
      return x == null || y == null ? null : { x, y: entry.el.offsetTop + y };
    }

    function dot(context, at) {
      context.fillStyle = HANDLE;
      context.beginPath();
      context.arc(at.x, at.y, 2.5, 0, Math.PI * 2);
      context.fill();
    }

    function paint(context, height, shape, draft) {
      context.save();
      if (shape.kind === "vert") {
        // A time marker belongs to every pane.
        const x = chart.timeScale().timeToCoordinate(shape.time);
        if (x != null) {
          context.strokeStyle = draft ? DRAFT : VERT;
          context.lineWidth = 1;
          context.setLineDash([4, 4]);
          context.beginPath();
          context.moveTo(x, 0);
          context.lineTo(x, height);
          context.stroke();
        }
        context.restore();
        return;
      }
      const a = point(shape.t1, shape.p1, shape.pane1);
      const b = point(shape.t2, shape.p2, shape.pane2);
      if (a && b) {
        const top = paneOf(myPane).el.offsetTop;
        context.strokeStyle = draft ? DRAFT : LINE;
        context.lineWidth = 1.5;
        context.beginPath();
        context.moveTo(a.x, a.y - top);
        context.lineTo(b.x, b.y - top);
        context.stroke();
        if (!draft) {
          dot(context, { x: a.x, y: a.y - top });
          dot(context, { x: b.x, y: b.y - top });
        }
      }
      context.restore();
    }

    return {
      attached(param) {
        series = param.series;
        chart = param.chart;
        painter.request = param.requestUpdate;
      },
      detached() {
        series = null;
        chart = null;
        painter.request = () => {};
      },
      updateAllViews() {},
      paneViews() {
        return [{
          renderer: () => ({
            draw: (target) => {
              if (!series || !chart) return;
              target.useMediaCoordinateSpace(({ context, mediaSize }) => {
                for (const shape of state.shapes) paint(context, mediaSize.height, shape, false);
                if (state.draft) paint(context, mediaSize.height, state.draft, true);
              });
            },
          }),
        }];
      },
    };
  }

  /* panes: [{el, chart, series, pane}] — top pane first. */
  function attach(opts) {
    const { host, panes } = opts;
    const times = opts.times || [];
    const state = { shapes: [], draft: null };
    const history = [];
    const painters = panes.map(() => ({ request: () => {} }));
    let tool = null;

    const redraw = () => { for (const painter of painters) painter.request(); };
    const remember = () => {
      history.push(state.shapes.slice());
      if (history.length > MAX_HISTORY) history.shift();
    };

    /* ── toolbar ────────────────────────────────────────────────────────── */
    host.style.position = "relative";
    const bar = document.createElement("div");
    bar.style.cssText = "position:absolute;top:6px;left:6px;z-index:5;display:flex;"
      + "gap:4px;align-items:center;font:11px 'Trebuchet MS',Roboto,sans-serif;";
    const buttons = {};
    function addButton(key, label, title, action) {
      const el = document.createElement("button");
      el.type = "button";
      el.textContent = label;
      el.title = title;
      el.style.cssText = BTN;
      el.addEventListener("pointerdown", (event) => event.stopPropagation());
      el.addEventListener("click", action);
      bar.appendChild(el);
      if (key) buttons[key] = el;
      return el;
    }
    addButton("trend", "Trend", "Trendline: drag between two points (RSI pane too)",
              () => setTool("trend"));
    addButton("vert", "VLine", "Dotted vertical line: drag or click a bar", () => setTool("vert"));
    addButton(null, "Undo", "Undo the last drawing (Ctrl+Z)", undo);
    addButton(null, "Clear", "Remove every drawing", clear);
    host.appendChild(bar);

    function paintButtons() {
      buttons.trend.style.cssText = tool === "trend" ? BTN_ON : BTN;
      buttons.vert.style.cssText = tool === "vert" ? BTN_ON : BTN;
    }

    /* ── tools ──────────────────────────────────────────────────────────── */
    function setTool(next) {
      tool = tool === next ? null : next;
      // Drawing drags must not pan the chart underneath, in any pane.
      for (const entry of panes) {
        entry.chart.applyOptions({
          handleScroll: { mouseWheel: true, pressedMouseMove: !tool, horzTouchDrag: !tool },
        });
      }
      state.draft = null;
      paintButtons();
      redraw();
    }

    function undo() {
      if (!history.length) return;
      state.shapes = history.pop();
      state.draft = null;
      redraw();
    }

    function clear() {
      if (!state.shapes.length) return;
      remember();
      state.shapes = [];
      state.draft = null;
      redraw();
    }

    /* ── pointer → (time, value, pane) ──────────────────────────────────── */
    function locate(event, entry) {
      const bounds = entry.el.getBoundingClientRect();
      const x = event.clientX - bounds.left;
      const y = event.clientY - bounds.top;
      const value = entry.series.coordinateToPrice(y);
      const logical = entry.chart.timeScale().coordinateToLogical(x);
      if (value == null || logical == null) return null;
      const index = Math.max(0, Math.min(times.length - 1, Math.round(logical)));
      return { time: times[index], price: Number(value), pane: entry.pane };
    }

    let start = null;
    let startX = 0;

    function onDown(event, entry) {
      if (!tool || event.button !== 0) return;
      const at = locate(event, entry);
      if (!at) return;
      event.preventDefault();
      event.stopPropagation();
      start = at;
      startX = event.clientX;
      state.draft = tool === "trend"
        ? { kind: "trend", pane1: at.pane, pane2: at.pane, t1: at.time, p1: at.price,
            t2: at.time, p2: at.price }
        : { kind: "vert", time: at.time };
      redraw();
    }

    function onMove(event, entry) {
      if (!start) return;
      const at = locate(event, entry);
      if (!at) return;
      // The moving end follows the pane under the cursor, so a trendline can
      // be dragged from the price pane into the RSI pane and back.
      state.draft = state.draft.kind === "trend"
        ? { kind: "trend", pane1: start.pane, pane2: at.pane, t1: start.time,
            p1: start.price, t2: at.time, p2: at.price }
        : { kind: "vert", time: at.time };
      redraw();
    }

    function onUp(event) {
      if (!start) return;
      const draft = state.draft;
      start = null;
      state.draft = null;
      if (draft) {
        const moved = Math.abs(event.clientX - startX) >= MIN_DRAG;
        if (draft.kind === "vert" || moved) {
          remember();
          state.shapes.push(draft);
        }
      }
      redraw();
    }

    function onKey(event) {
      const tag = (event.target && event.target.tagName) || "";
      if (/INPUT|TEXTAREA|SELECT/.test(tag)) return;
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "z") {
        event.preventDefault();
        undo();
      } else if (event.key === "Escape" && tool) {
        setTool(null);
      }
    }

    const detach = [];
    panes.forEach((entry, index) => {
      const down = (event) => onDown(event, entry);
      const move = (event) => onMove(event, entry);
      entry.el.addEventListener("pointerdown", down, true);
      entry.el.addEventListener("pointermove", move, true);
      entry.el.addEventListener("pointerup", onUp, true);
      detach.push(() => {
        entry.el.removeEventListener("pointerdown", down, true);
        entry.el.removeEventListener("pointermove", move, true);
        entry.el.removeEventListener("pointerup", onUp, true);
      });
      entry.series.attachPrimitive(createPrimitive(state, painters[index], panes, entry.pane));
    });
    document.addEventListener("keydown", onKey);

    return {
      destroy() {
        detach.forEach((fn) => fn());
        document.removeEventListener("keydown", onKey);
        if (bar.parentNode) bar.parentNode.removeChild(bar);
        state.shapes = [];
        state.draft = null;
      },
    };
  }

  return { attach };
})();
