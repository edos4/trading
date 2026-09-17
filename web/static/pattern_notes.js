/* Pattern part notes for the Lightweight Charts viewer.
 *
 * The chart library draws one line of marker text and gives no control over
 * its position, so a part that carries a "why this part" reason is rendered
 * entirely by this series primitive: one block holding the part label and the
 * reason under it, placed clear of the price anchor and of every other block.
 * The library's own marker text is blanked for those parts in tv_chart.js.
 */
window.PatternNotes = (function () {
  const FONT_NOTE = "10px 'Trebuchet MS', Roboto, sans-serif";
  const FONT_LABEL = "bold 12px 'Trebuchet MS', Roboto, sans-serif";
  const NOTE_LINE = 12;
  const LABEL_LINE = 14;
  const GAP = 4;            // clearance between the label and its reason
  const PAD = 4;            // block padding around the text
  /* The library draws a marker's shape itself, centred about this far beyond
   * the price (a default-size circle spans roughly 6..28px past it). */
  const ANCHOR_GAP = 30;
  const MAX_WIDTH = 190;
  const MAX_LINES = 3;
  const NUDGE = 12;         // step used to slide a block clear of another
  const NUDGE_TRIES = 6;
  const TEXT = "rgba(190,194,206,0.95)";
  const BG = "rgba(19,23,34,0.86)";

  function wrap(context, font, text, limit) {
    context.font = font;
    const words = String(text).split(/\s+/);
    const out = [];
    let line = "";
    for (const word of words) {
      const candidate = line ? `${line} ${word}` : word;
      if (line && context.measureText(candidate).width > MAX_WIDTH) {
        out.push(line);
        line = word;
        if (out.length === limit) break;
      } else {
        line = candidate;
      }
    }
    if (line && out.length < limit) out.push(line);
    return out;
  }

  function width(context, font, rows) {
    context.font = font;
    let widest = 0;
    for (const row of rows) widest = Math.max(widest, context.measureText(row).width);
    return widest;
  }

  /* One entry per labeled part that carries a reason. */
  function collect(data) {
    const notes = [];
    for (const marker of data.markers || []) {
      if (!marker.reason || marker.price == null || !marker.time) continue;
      notes.push({
        time: marker.time,
        price: marker.price,
        label: marker.text || "",
        color: marker.color,
        reason: marker.reason,
        below: marker.position !== "aboveBar",
      });
    }
    for (const segment of data.segments || []) {
      if (!segment.reason || !(segment.data || []).length || !segment.label) continue;
      const point = segment.data[Math.floor((segment.data.length - 1) / 2)];
      notes.push({
        time: point.time,
        price: point.value,
        label: segment.label,
        color: segment.color,
        reason: segment.reason,
        // The library puts a segment label below the line only for rail/support.
        below: /lower|support/i.test(segment.label),
      });
    }
    for (const level of data.levels || []) {
      if (!level.reason) continue;
      notes.push({ price: level.price, reason: level.reason, color: level.color, axis: true });
    }
    return notes;
  }

  function overlaps(placed, left, top, w, h) {
    for (const box of placed) {
      if (left < box.left + box.w && box.left < left + w
          && top < box.top + box.h && box.top < top + h) return true;
    }
    return false;
  }

  function create(notes) {
    let series = null;
    let chart = null;

    /* The label with its reason underneath, both clear of the anchor. */
    function drawPart(context, item, x, y, placed, pane) {
      const labelRows = wrap(context, FONT_LABEL, item.label, 1);
      const noteRows = wrap(context, FONT_NOTE, item.reason, MAX_LINES);
      if (!labelRows.length) return;
      const textWidth = Math.max(
        width(context, FONT_LABEL, labelRows),
        noteRows.length ? width(context, FONT_NOTE, noteRows) : 0,
      );
      const w = textWidth + PAD * 2;
      const h = labelRows.length * LABEL_LINE + GAP
        + noteRows.length * NOTE_LINE + PAD * 2;
      const left = Math.min(Math.max(x - w / 2, 2), Math.max(2, pane.width - w - 2));
      const below = y + ANCHOR_GAP;
      const above = y - ANCHOR_GAP - h;
      // Prefer the label's own side; use the other one when it would not fit.
      const natural = item.below ? below : above;
      let top = natural;
      if (top + h > pane.height - 2 && above >= 2) top = above;
      else if (top < 2 && below + h <= pane.height - 2) top = below;
      top = Math.min(Math.max(top, 2), Math.max(2, pane.height - h - 2));
      for (let i = 0; i < NUDGE_TRIES && overlaps(placed, left, top, w, h); i++) {
        top += item.below ? NUDGE : -NUDGE;
      }
      placed.push({ left, top, w, h });
      if (Math.abs(top - natural) > 8) {
        // Pushed clear of a neighbour: keep the block tied to its anchor.
        context.strokeStyle = "rgba(150,155,170,0.5)";
        context.lineWidth = 1;
        context.beginPath();
        context.moveTo(x, y + (item.below ? 26 : -26));
        context.lineTo(left + w / 2, item.below ? top : top + h);
        context.stroke();
      }

      context.fillStyle = BG;
      context.fillRect(left, top, w, h);
      context.textAlign = "center";
      context.textBaseline = "top";
      const mid = left + w / 2;
      let lineTop = top + PAD;
      context.font = FONT_LABEL;
      context.fillStyle = item.color || "#ffeb3b";
      for (const row of labelRows) {
        context.fillText(row, mid, lineTop);
        lineTop += LABEL_LINE;
      }
      context.font = FONT_NOTE;
      context.fillStyle = TEXT;
      lineTop += GAP;
      for (const row of noteRows) {
        context.fillText(row, mid, lineTop);
        lineTop += NOTE_LINE;
      }
    }

    /* A pattern price line's own title is drawn on the right axis. */
    function drawAxisNote(context, item, y, placed, pane) {
      const rows = wrap(context, FONT_NOTE, item.reason, MAX_LINES);
      if (!rows.length) return;
      const w = width(context, FONT_NOTE, rows) + PAD * 2;
      const h = rows.length * NOTE_LINE + PAD * 2;
      const left = Math.max(2, pane.width - w - 8);
      // The library centres its own title box on the line; sit just below it.
      let top = y + 13;
      for (let i = 0; i < NUDGE_TRIES && overlaps(placed, left, top, w, h); i++) top += NUDGE;
      placed.push({ left, top, w, h });
      context.fillStyle = BG;
      context.fillRect(left, top, w, h);
      context.font = FONT_NOTE;
      context.fillStyle = TEXT;
      context.textAlign = "left";
      context.textBaseline = "top";
      let lineTop = top + PAD;
      for (const row of rows) {
        context.fillText(row, left + PAD, lineTop);
        lineTop += NOTE_LINE;
      }
    }

    function draw(target) {
      if (!series || !chart) return;
      target.useMediaCoordinateSpace(({ context, mediaSize }) => {
        context.save();
        const placed = [];
        const pinned = [];
        for (const item of notes) {
          const y = series.priceToCoordinate(item.price);
          if (y == null) continue;
          if (item.axis) {
            drawAxisNote(context, item, y, placed, mediaSize);
            continue;
          }
          const x = chart.timeScale().timeToCoordinate(item.time);
          if (x == null) continue;
          pinned.push({ item, x, y });
        }
        // Place the topmost block first so neighbours slide downward, not over.
        pinned.sort((a, b) => a.y - b.y);
        for (const entry of pinned) {
          drawPart(context, entry.item, entry.x, entry.y, placed, mediaSize);
        }
        context.restore();
      });
    }

    return {
      attached(param) {
        series = param.series;
        chart = param.chart;
      },
      detached() {
        series = null;
        chart = null;
      },
      updateAllViews() {},
      paneViews() {
        return [{ renderer: () => ({ draw }) }];
      },
    };
  }

  return { collect, create };
})();
