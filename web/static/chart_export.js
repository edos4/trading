/* Batch chart PNGs in one ZIP, using the same renderer as the trade modal. */
window.ChartExport = (function () {
  const encoder = new TextEncoder();
  const crcTable = Uint32Array.from({length: 256}, (_, value) => {
    for (let bit = 0; bit < 8; bit++) value = (value >>> 1) ^ (value & 1 ? 0xedb88320 : 0);
    return value >>> 0;
  });

  /* PNGs are already compressed. ZIP's store method avoids another dependency
   * and keeps the original image bytes; every entry has a standard CRC-32. */
  async function zip(files) {
    if (files.length > 65535) throw new Error("Too many files for one chart download");
    const parts = [], directory = [];
    let offset = 0, directorySize = 0;
    for (const file of files) {
      const name = encoder.encode(file.name);
      const bytes = new Uint8Array(await file.blob.arrayBuffer());
      if (name.length > 65535 || offset + bytes.length + name.length + 30 > 0xffffffff) {
        throw new Error("Chart download exceeds the 4 GB ZIP limit; narrow the filters");
      }
      let crc = 0xffffffff;
      for (const byte of bytes) crc = (crc >>> 8) ^ crcTable[(crc ^ byte) & 255];
      crc = (crc ^ 0xffffffff) >>> 0;
      const local = new Uint8Array(30), head = new DataView(local.buffer);
      head.setUint32(0, 0x04034b50, true);
      head.setUint16(4, 20, true); // ZIP 2.0
      head.setUint16(6, 0x800, true); // UTF-8 names
      head.setUint16(12, 0x21, true); // 1980-01-01
      head.setUint32(14, crc, true);
      head.setUint32(18, bytes.length, true);
      head.setUint32(22, bytes.length, true);
      head.setUint16(26, name.length, true);
      parts.push(local, name, file.blob);
      const central = new Uint8Array(46), entry = new DataView(central.buffer);
      entry.setUint32(0, 0x02014b50, true);
      entry.setUint16(4, 20, true);
      entry.setUint16(6, 20, true);
      entry.setUint16(8, 0x800, true);
      entry.setUint16(14, 0x21, true);
      entry.setUint32(16, crc, true);
      entry.setUint32(20, bytes.length, true);
      entry.setUint32(24, bytes.length, true);
      entry.setUint16(28, name.length, true);
      entry.setUint32(42, offset, true);
      directory.push(central, name);
      directorySize += central.length + name.length;
      offset += local.length + name.length + bytes.length;
    }
    if (offset + directorySize > 0xffffffff) throw new Error("Chart download exceeds the 4 GB ZIP limit; narrow the filters");
    const end = new Uint8Array(22), footer = new DataView(end.buffer);
    footer.setUint32(0, 0x06054b50, true);
    footer.setUint16(8, files.length, true);
    footer.setUint16(10, files.length, true);
    footer.setUint32(12, directorySize, true);
    footer.setUint32(16, offset, true);
    return new Blob([...parts, ...directory, end], {type: "application/zip"});
  }

  const safeName = value => String(value || "chart").replace(/[^a-zA-Z0-9_-]+/g, "_").slice(0, 80);
  const folders = {open: "open_positions", closed: "latest_trades", log: "signal_log"};

  async function download({rows, loadChart, header, label, signal, onProgress}) {
    if (!window.TVChart?.create) throw new Error("Chart renderer is not loaded; reload the page");
    const renderer = window.TVChart.create();
    const host = document.createElement("div");
    host.setAttribute("aria-hidden", "true");
    host.style.cssText = "position:fixed;left:-20000px;top:0;width:1200px;height:600px;pointer-events:none";
    document.body.appendChild(host);
    const files = [];
    const report = rows.map(row => ({...row, outcome: "not_exported"}));
    let failed = 0;
    try {
      if (document.fonts) await document.fonts.ready;
      for (const [index, row] of rows.entries()) {
        if (signal?.aborted) break;
        onProgress?.(`Creating chart ${index + 1} of ${rows.length}: ${String(row.market).toUpperCase()} ${row.symbol || "—"}…`);
        try {
          if (!row.symbol) throw new Error("This row has no symbol to chart");
          const data = await loadChart(row, signal);
          if (signal?.aborted) break;
          renderer.mount(host, data, {readOnly: true, includeDrafts: true, priceHeight: 600});
          // Let ResizeObserver, linked pane scales and primitives finish painting.
          for (let frame = 0; frame < 3; frame++) await new Promise(requestAnimationFrame);
          if (signal?.aborted) break;
          const blob = await renderer.captureBlob(header(data, row));
          const name = `${folders[row.side]}/${String(index + 1).padStart(4, "0")}_${safeName(row.market)}_${safeName(row.symbol)}_${safeName(data.timeframe)}_${safeName(row.tradeId || row.logTime || row.index)}.png`;
          files.push({name, blob});
          Object.assign(report[index], {outcome: "exported", file: name});
        } catch (err) {
          if (signal?.aborted) break;
          failed++;
          Object.assign(report[index], {outcome: "failed", error: String(err.message || err)});
        } finally {
          renderer.unmount();
        }
      }
    } finally {
      renderer.unmount();
      host.remove();
    }
    const result = {requested: rows.length, exported: files.length, failed, cancelled: !!signal?.aborted};
    if (!files.length && !failed && result.cancelled) return {...result, downloaded: false};
    onProgress?.(`Packing ${files.length} chart${files.length === 1 ? "" : "s"}…`, false);
    files.push({name: "report.json", blob: new Blob([JSON.stringify({
      created_at: new Date().toISOString(), ...result, rows: report,
    }, null, 2)], {type: "application/json"})});
    const archive = await zip(files);
    const url = URL.createObjectURL(archive);
    const link = document.createElement("a");
    link.href = url;
    const stamp = new Date().toISOString().replace(/[:.]/g, "").slice(0, 15);
    link.download = `paper_charts_${safeName(label)}_${stamp}.zip`;
    document.body.appendChild(link);
    try { link.click(); }
    finally {
      link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 60000);
    }
    return {...result, downloaded: true};
  }

  return {download};
})();
