/* System tab: polls /api/system and renders host resource usage. */
(function () {
  const POLL_MS = 3000;
  const TOP_N = 6;
  const $ = (id) => document.getElementById(id);

  function fmtBytes(n) {
    const v = Number(n);
    if (!Number.isFinite(v)) return "—";
    const units = ["B", "KB", "MB", "GB", "TB", "PB"];
    let i = 0;
    let x = v;
    while (x >= 1024 && i < units.length - 1) { x /= 1024; i++; }
    return `${x.toFixed(i === 0 || x >= 100 ? 0 : 1)} ${units[i]}`;
  }

  function fmtUptime(seconds) {
    let s = Math.max(0, Math.floor(Number(seconds) || 0));
    const d = Math.floor(s / 86400); s -= d * 86400;
    const h = Math.floor(s / 3600); s -= h * 3600;
    const m = Math.floor(s / 60);
    if (d) return `${d}d ${h}h ${m}m`;
    if (h) return `${h}h ${m}m`;
    return `${m}m`;
  }

  function meterClass(pct) {
    if (pct >= 90) return "crit";
    if (pct >= 75) return "warn";
    return "";
  }

  function setMeter(id, pct) {
    const el = $(id);
    if (!el) return;
    const value = Number(pct) || 0;
    el.className = `meter ${meterClass(value)}`.trim();
    if (el.firstElementChild) {
      el.firstElementChild.style.width = `${Math.max(0, Math.min(100, value))}%`;
    }
  }

  function setText(id, text) {
    const el = $(id);
    if (el) el.textContent = text;
  }

  function renderCores(cores) {
    const host = $("sys-cores");
    if (!host) return;
    host.innerHTML = "";
    for (const pct of cores || []) {
      const value = Number(pct) || 0;
      const bar = document.createElement("div");
      bar.className = "core";
      bar.title = `core ${value.toFixed(0)}%`;
      bar.dataset.level = value >= 90 ? "crit" : value >= 75 ? "warn" : "";
      const fill = document.createElement("i");
      fill.style.height = `${Math.max(3, Math.min(100, value))}%`;
      bar.appendChild(fill);
      host.appendChild(bar);
    }
  }

  function renderProcs(tbodyId, procs) {
    const body = $(tbodyId);
    if (!body) return;
    body.innerHTML = "";
    if (!procs || !procs.length) {
      const tr = document.createElement("tr");
      const td = document.createElement("td");
      td.className = "empty";
      td.colSpan = 5;
      td.textContent = "No data yet.";
      tr.appendChild(td);
      body.appendChild(tr);
      return;
    }
    for (const p of procs) {
      const tr = document.createElement("tr");
      const cells = [
        String(p.pid),
        p.name || "?",
        `${Number(p.cpu_percent || 0).toFixed(1)}`,
        `${Number(p.memory_percent || 0).toFixed(1)}`,
        fmtBytes(p.rss),
      ];
      for (let i = 0; i < cells.length; i++) {
        const td = document.createElement("td");
        td.textContent = cells[i];
        if (i >= 2) td.className = "num";
        tr.appendChild(td);
      }
      body.appendChild(tr);
    }
  }

  function render(d) {
    const cpu = d.cpu || {};
    const mem = d.memory || {};
    const swap = d.swap || {};
    const host = d.host || {};
    const net = d.network || {};
    const disks = d.disks || [];

    const cpuPct = Number(cpu.percent) || 0;
    setText("sys-cpu", `${cpuPct.toFixed(0)}%`);
    setMeter("sys-cpu-meter", cpuPct);
    setText("sys-cpu-sub", `${host.cpu_count || 0} cores · ${host.cpu_model || "unknown"}`);

    if (cpu.load) {
      setText("sys-load", cpu.load.map((v) => Number(v).toFixed(2)).join("  "));
      setText("sys-load-sub", `${cpu.count || 0} cores · 1 / 5 / 15 min`);
    } else {
      setText("sys-load", "n/a");
      setText("sys-load-sub", `${cpu.count || 0} cores`);
    }

    const memPct = Number(mem.percent) || 0;
    setText("sys-mem", `${memPct.toFixed(0)}%`);
    setMeter("sys-mem-meter", memPct);
    setText("sys-mem-sub",
      `${fmtBytes(mem.used)} / ${fmtBytes(mem.total)} · ${fmtBytes(mem.available)} free`);

    if (Number(swap.total) > 0) {
      const swapPct = Number(swap.percent) || 0;
      setText("sys-swap", `${swapPct.toFixed(0)}%`);
      setMeter("sys-swap-meter", swapPct);
      setText("sys-swap-sub", `${fmtBytes(swap.used)} / ${fmtBytes(swap.total)}`);
    } else {
      setText("sys-swap", "none");
      setMeter("sys-swap-meter", 0);
      setText("sys-swap-sub", "No swap configured");
    }

    if (disks.length) {
      setText("sys-disk", `${Number(disks[0].percent).toFixed(0)}%`);
      setMeter("sys-disk-meter", disks[0].percent);
      setText("sys-disk-sub", disks
        .map((x) => `${x.mount} ${fmtBytes(x.free)} free`)
        .join(" · "));
    }

    setText("sys-uptime", fmtUptime(host.uptime_seconds));
    setText("sys-uptime-sub", host.boot_time
      ? `boot ${new Date(host.boot_time * 1000).toLocaleString()}`
      : "");

    setText("sys-net", `${fmtBytes(net.bytes_recv)} down`);
    setText("sys-net-sub", `${fmtBytes(net.bytes_sent)} up · since boot`);

    setText("sys-host", host.hostname || "—");
    setText("sys-host-sub", `${host.platform || ""} · Python ${host.python || "?"}`);

    renderCores(cpu.per_cpu);
    const temps = d.temperatures || [];
    setText("sys-temp", temps
      .map((t) => `${t.label} ${Number(t.current).toFixed(0)}°C`)
      .join(" · "));

    const procs = d.processes || {};
    renderProcs("sys-proc-cpu", procs.by_cpu);
    renderProcs("sys-proc-mem", procs.by_mem);
  }

  async function refresh() {
    try {
      const data = await api(`/api/system?top=${TOP_N}`);
      render(data);
      setText("sys-status", `Updated ${new Date().toLocaleTimeString()}`);
    } catch (e) {
      setText("sys-status", String(e.message || e));
    }
  }

  let timer = null;
  function schedule() {
    if (timer) clearTimeout(timer);
    timer = null;
    const auto = $("sys-auto");
    if (auto && auto.checked) timer = setTimeout(tick, POLL_MS);
  }

  async function tick() {
    await refresh();
    schedule();
  }

  const refreshBtn = $("sys-refresh");
  if (refreshBtn) refreshBtn.addEventListener("click", () => { refresh(); });
  const autoEl = $("sys-auto");
  if (autoEl) autoEl.addEventListener("change", schedule);
  tick();
})();
