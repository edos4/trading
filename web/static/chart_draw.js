/* Manual corrections use actual candle times and pane values, never pixels. */
window.ChartDraw = (function () {
  const BLUE = '#42a5f5', SELECTED = '#90caf9';
  const copy = value => JSON.parse(JSON.stringify(value));

  function attach({host, panes, times = [], shapes = [], onChange = () => {}, onCorrect,
                   readOnly = false}) {
    let drawings = copy(shapes), draft = null, selected = null, tool = null, drag = null;
    const history = [], cleanup = [], painters = [];
    const originalScroll = panes.map(p => ({...(p.chart.options?.().handleScroll || {})}));
    const paneOf = name => panes.find(p => p.pane === name) || panes[0];
    const redraw = () => painters.forEach(p => p.request());
    const remember = () => { history.push(copy(drawings)); if (history.length > 50) history.shift(); };
    const snapshot = () => copy(drawings);
    const changed = () => { onChange(snapshot()); redraw(); };
    const fresh = kind => ({id: window.crypto?.randomUUID?.() || `${Date.now()}-${Math.random()}`, kind, label: ''});
    function point(p) {
      const pane = paneOf(p.pane);
      const x = pane.chart.timeScale().timeToCoordinate(p.time);
      const y = pane.series.priceToCoordinate(p.value);
      return x == null || y == null ? null : {x, y: pane.el.offsetTop + y};
    }
    function coordinates(shape) {
      if (shape.kind === 'vert') {
        const x = panes[0].chart.timeScale().timeToCoordinate(shape.time);
        return x == null ? [] : [{x, y: panes[0].el.offsetTop},
          {x, y: panes.at(-1).el.offsetTop + panes.at(-1).el.clientHeight}];
      }
      return shape.points.map(point);
    }
    function paint(context, shape, top) {
      const pts = coordinates(shape);
      if (!pts.length || pts.some(p => !p)) return;
      context.save();
      context.strokeStyle = shape.id === selected ? SELECTED : BLUE;
      context.fillStyle = context.strokeStyle;
      context.lineWidth = 2;
      context.setLineDash(shape.kind === 'vert' ? [4, 4] : []);
      context.beginPath();
      pts.forEach((p, i) => context[i ? 'lineTo' : 'moveTo'](p.x, p.y - top));
      context.stroke();
      if (shape.kind !== 'vert') for (const p of pts) {
        context.beginPath(); context.arc(p.x, p.y - top, 3, 0, Math.PI * 2); context.fill();
      }
      if (shape.label) {
        context.font = '12px sans-serif';
        context.fillText(shape.label, pts[0].x + 6, pts[0].y - top + 15);
      }
      context.restore();
    }
    for (const pane of panes) {
      const painter = {request: () => {}};
      painters.push(painter);
      const primitive = {
        attached(p) { painter.request = p.requestUpdate; },
        detached() { painter.request = () => {}; },
        updateAllViews() {},
        paneViews() { return [{renderer: () => ({draw(target) {
          target.useMediaCoordinateSpace(({context}) => {
            drawings.forEach(s => paint(context, s, pane.el.offsetTop));
            if (draft) paint(context, draft, pane.el.offsetTop);
          });
        }})}]; },
      };
      pane.series.attachPrimitive(primitive);
      cleanup.push(() => pane.series.detachPrimitive(primitive));
    }
    const bar = document.createElement('div');
    bar.style.cssText = 'display:flex;flex-wrap:wrap;gap:4px;align-items:center;position:absolute;top:3px;left:6px;z-index:5;max-width:95%;background:#131722';
    host.style.position = 'relative';
    const buttons = {};
    function button(label, action, key) {
      const el = document.createElement('button');
      el.type = 'button'; el.textContent = label; el.className = 'btn';
      el.style.cssText = 'font:11px sans-serif;padding:3px 6px';
      el.addEventListener('click', action);
      bar.appendChild(el);
      if (key) buttons[key] = el;
      return el;
    }
    const status = document.createElement('span');
    status.style.cssText = 'color:#90caf9;font:11px sans-serif';
    status.setAttribute('role', 'status');
    status.textContent = 'Blue: your corrections.';
    function applyScroll() {
      panes.forEach((p, i) => {
        const base = {...(originalScroll[i] || {mouseWheel: true, pressedMouseMove: true, horzTouchDrag: true})};
        if (tool) { base.pressedMouseMove = false; base.horzTouchDrag = false; }
        p.chart.applyOptions({handleScroll: base});
      });
    }
    function setTool(next) {
      cancel(); tool = tool === next ? null : next;
      applyScroll();
      Object.entries(buttons).forEach(([key, b]) => {
        b.setAttribute('aria-pressed', String(key === tool));
        b.style.backgroundColor = key === tool ? BLUE : '';
        b.style.color = key === tool ? '#131722' : '';
      });
      status.textContent = tool === 'pattern' ? 'Click price pivots left to right; Enter to finish.' : 'Blue: your corrections.';
    }
    function cancel() {
      if (drag?.before) drawings = drag.before;
      drag = null; draft = null; redraw();
    }
    function finish() {
      if (!draft || draft.kind !== 'pattern') return;
      if (draft.points.length < 2) { status.textContent = 'Add at least two points.'; return; }
      remember(); drawings.push(draft); selected = draft.id; draft = null; changed();
    }
    function undo() {
      cancel(); if (!history.length) return;
      drawings = history.pop();
      if (!drawings.some(s => s.id === selected)) selected = null;
      changed();
    }
    function remove() {
      if (!drawings.some(s => s.id === selected)) return;
      remember(); drawings = drawings.filter(s => s.id !== selected); selected = null; changed();
    }
    if (!readOnly) {
      button('Select', () => setTool('select'), 'select');
      button('Draw pattern', () => setTool('pattern'), 'pattern');
      button('Finish', finish);
      button('Trend', () => setTool('trend'), 'trend');
      button('VLine', () => setTool('vert'), 'vert');
      button('Undo', undo);
      button('Clear', () => { cancel(); if (drawings.length) { remember(); drawings = []; selected = null; changed(); } });
      button('Delete', remove);
      const label = document.createElement('input');
      label.type = 'text'; label.maxLength = 200; label.placeholder = 'Selected shape label';
      label.setAttribute('aria-label', 'Selected correction label'); label.style.width = '135px';
      label.addEventListener('change', () => {
        const shape = drawings.find(s => s.id === selected);
        if (shape) { remember(); shape.label = label.value; changed(); }
        else status.textContent = 'Use Select and click a correction to label it.';
      });
      bar.appendChild(label);
      if (onCorrect) button('Correct pattern…', () => {
        if (draft || drag) { status.textContent = 'Finish or cancel the current drawing first.'; return; }
        onCorrect();
      });
      bar.appendChild(status); host.appendChild(bar);
      function locate(event) {
        const pane = panes.find(p => {
          const b = p.el.getBoundingClientRect(); return event.clientY >= b.top && event.clientY <= b.bottom;
        });
        if (!pane) return null;
        const b = pane.el.getBoundingClientRect(), x = event.clientX - b.left;
        const time = pane.chart.timeScale().coordinateToTime(x);
        const normalized = typeof time === 'object' && time ?
          `${time.year}-${String(time.month).padStart(2,'0')}-${String(time.day).padStart(2,'0')}` :
          typeof time === 'number' ? new Date(time * 1000).toISOString().slice(0,10) : time;
        const value = pane.series.coordinateToPrice(event.clientY - b.top);
        if (!times.includes(normalized) || value == null || !Number.isFinite(value)) return null;
        return {time: normalized, value, pane: pane.pane};
      }
      function hit(event) {
        const b = panes[0].el.getBoundingClientRect();
        const cursor = {x: event.clientX - b.left, y: event.clientY - b.top + panes[0].el.offsetTop};
        for (const shape of [...drawings].reverse()) {
          const pts = coordinates(shape);
          for (let i = 0; i < pts.length; i++) {
            const p = pts[i]; if (!p) continue;
            if (Math.hypot(p.x-cursor.x, p.y-cursor.y) < 9) return {shape, index: i};
          }
          for (let i = 1; i < pts.length; i++) {
            const a = pts[i-1], b = pts[i]; if (!a || !b) continue;
            const dx=b.x-a.x, dy=b.y-a.y;
            const t=Math.max(0,Math.min(1,((cursor.x-a.x)*dx+(cursor.y-a.y)*dy)/(dx*dx+dy*dy || 1)));
            if (Math.hypot(cursor.x-a.x-t*dx,cursor.y-a.y-t*dy) < 7)
              return {shape, index: shape.kind === 'vert' ? 0 : null};
          }
        }
        return null;
      }
      function down(event) {
        if (!tool || event.button !== 0 || bar.contains(event.target)) return;
        host.focus(); event.preventDefault(); event.stopPropagation();
        if (tool === 'select') {
          const found = hit(event); selected = found?.shape.id || null;
          label.value = found?.shape.label || '';
          if (found && found.index !== null) drag = {id: selected, index: found.index, before: snapshot()};
          redraw(); return;
        }
        const at = locate(event);
        if (!at) { status.textContent = 'Use an actual candle, not forecast or empty space.'; return; }
        if (!draft && drawings.length >= 100) { status.textContent = 'Maximum 100 corrections.'; return; }
        if (tool === 'pattern') {
          if (at.pane !== 'price') { status.textContent = 'Pattern pivots belong on price candles.'; return; }
          if (draft && (draft.points.length >= 100 || times.indexOf(at.time) <= times.indexOf(draft.points.at(-1).time))) {
            status.textContent = 'Use increasing bar times (maximum 100 points).'; return;
          }
          if (!draft) draft = {...fresh('pattern'), points: []};
          draft.points.push(at); redraw(); return;
        }
        draft = tool === 'vert' ? {...fresh('vert'), time: at.time} : {...fresh('trend'), points: [at, copy(at)]};
        drag = {startX: event.clientX, startY: event.clientY}; redraw();
      }
      function move(event) {
        if (!drag) return;
        const at = locate(event); if (!at) return;
        if (drag.id) {
          const shape = drawings.find(s => s.id === drag.id);
          if (shape.kind === 'vert') shape.time = at.time;
          else {
            if (shape.kind === 'pattern' && (at.pane !== 'price' ||
                (drag.index > 0 && times.indexOf(at.time) <= times.indexOf(shape.points[drag.index-1].time)) ||
                (drag.index < shape.points.length-1 && times.indexOf(at.time) >= times.indexOf(shape.points[drag.index+1].time)))) return;
            shape.points[drag.index] = at;
          }
        } else if (draft.kind === 'vert') draft.time = at.time;
        else draft.points[1] = at;
        redraw();
      }
      function up(event) {
        if (!drag) return;
        if (drag.id) { history.push(drag.before); if (history.length > 50) history.shift(); }
        else if (draft && (draft.kind === 'vert' || Math.hypot(event.clientX-drag.startX,event.clientY-drag.startY) >= 4)) {
          remember(); drawings.push(draft); selected = draft.id;
        }
        drag = null; draft = null; changed();
      }
      function key(event) {
        const typing = /INPUT|TEXTAREA|SELECT/.test(event.target?.tagName || '');
        if (typing && !host.contains(event.target)) return;
        if (event.key === 'Escape' && (tool || draft || drag) && !typing) {
          event.preventDefault(); event.stopImmediatePropagation(); setTool(null); return;
        }
        if (typing || !host.contains(event.target)) return;
        if (event.key === 'Enter') { event.preventDefault(); event.stopImmediatePropagation(); finish(); }
        else if (event.key === 'Delete' || event.key === 'Backspace') { event.preventDefault(); remove(); }
        else if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'z') { event.preventDefault(); undo(); }
      }
      host.tabIndex = 0;
      function listen(el, name, fn, capture = false) {
        el.addEventListener(name, fn, capture); cleanup.push(() => el.removeEventListener(name, fn, capture));
      }
      panes.forEach(p => listen(p.el, 'pointerdown', down, true));
      listen(document, 'pointermove', move, true); listen(document, 'pointerup', up, true);
      listen(document, 'pointercancel', cancel, true); listen(document, 'keydown', key, true);
    }
    return {
      snapshot,
      pending: () => !!(draft || drag),
      load(shapes) { cancel(); drawings = copy(shapes); history.length = 0; selected = null; changed(); },
      destroy() {
        cancel(); cleanup.forEach(fn => fn()); bar.remove();
        panes.forEach((p, i) => { if (originalScroll[i]) p.chart.applyOptions({handleScroll: originalScroll[i]}); });
      },
    };
  }
  return {attach};
})();
