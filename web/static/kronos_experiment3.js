/* Experiment 3: read-only, persisted forecasts and two actual daily closes. */
(function () {
  const section = document.getElementById("kronos-experiment3");
  if (!section) return;
  const status = document.getElementById("experiment3-status");
  const scores = document.getElementById("experiment3-scores");
  const refresh = document.getElementById("experiment3-refresh");
  const select = document.getElementById("experiment3-symbol");
  const host = document.getElementById("experiment3-chart");
  const body = document.querySelector("#experiment3-table tbody");
  const reportBody = document.querySelector("#experiment3-direction-report tbody");
  let results = [];
  let chart = null;
  const price = (value) => Number.isFinite(value) ? value.toFixed(2) : "Pending";
  const directionName = (value) => ({ up: "↑ Up", down: "↓ Down", flat: "→ Flat" }[value] || "Pending");
  function prediction(point, result) {
    const outcome = result.correct === null ? "Pending" : result.correct ? "Correct" : "Incorrect";
    return `${price(point.value)} · ${directionName(result.predicted)} · ${outcome}`;
  }

  function draw() {
    if (chart) chart.remove();
    chart = null;
    host.replaceChildren();
    const row = results.find((r) => r.symbol === select.value);
    if (!row) return;
    // Merge only observed closes into the white line. No synthetic future points.
    const history = new Map((row.history || []).map((p) => [p.time, p]));
    (row.actuals || []).forEach((p) => history.set(p.time, p));
    const data = { ...row, history: [...history.values()].sort((a, b) => a.time.localeCompare(b.time)) };
    const panel = window.KronosCharts.makePanel(data);
    const directions = document.createElement("p");
    directions.className = "small";
    directions.textContent = row.direction_results.map((result) =>
      `+${result.day}D direction vs Nov 10: actual ${directionName(result.actual)}, ` +
      `base ${directionName(result.base.predicted)} (${result.base.correct === null ? "Pending" : result.base.correct ? "Correct" : "Incorrect"}), ` +
      `fine-tuned ${directionName(result.finetuned.predicted)} (${result.finetuned.correct === null ? "Pending" : result.finetuned.correct ? "Correct" : "Incorrect"})`
    ).join(" · ");
    panel.section.appendChild(directions);
    host.appendChild(panel.section);
    chart = window.KronosCharts.drawChart(panel.host, data);
    panel.save.disabled = false;
    panel.save.addEventListener("click", () => window.KronosCharts.saveChart(row.symbol, 2, chart));
  }

  function render(payload) {
    results = payload.results || [];
    select.disabled = !results.length;
    const selected = select.value;
    select.replaceChildren();
    body.replaceChildren();
    reportBody.replaceChildren();
    scores.textContent = "";
    if (payload.status === "not_started") {
      status.textContent = "Experiment 3 has no saved predictions yet.";
      draw();
      return;
    }
    const saved = payload.metadata;
    const state = payload.status === "complete" ? "Complete; collection stopped."
      : payload.status === "expired_incomplete" ? "Collection ended with missing actuals."
      : "Waiting for the two completed session closes.";
    status.textContent = `${results.length} frozen forecasts · ${payload.actual_count}/100 actual closes saved. ${state} Forecasts saved ${saved.generated_at}.`;
    (payload.direction_report || []).forEach((row) => {
      const tr = document.createElement("tr");
      const values = [row.date, `+${row.day}D`, `${row.evaluated}/${row.expected}`, row.pending];
      ["base", "finetuned"].forEach((model) => {
        const result = row[model];
        values.push(...(row.evaluated
          ? [result.correct, result.incorrect, `${result.accuracy_pct.toFixed(1)}%`]
          : ["Pending", "Pending", "Pending"]));
      });
      values.forEach((value, i) => {
        const td = document.createElement("td");
        td.textContent = String(value);
        if (i >= 4) td.style.color = i < 7 ? "#2962ff" : "#ef5350";
        tr.appendChild(td);
      });
      reportBody.appendChild(tr);
    });
    ["base", "finetuned"].forEach((model) => {
      const entries = (payload.scores[model] || []).map((s) => s.count
        ? `+${s.day}D: ${s.mape_pct.toFixed(2)}% mean absolute error, ${s.direction_accuracy_pct.toFixed(1)}% direction correct (${s.direction_correct}/${s.count})`
        : `+${s.day}D: awaiting actuals`);
      const line = document.createElement("div");
      line.style.color = model === "base" ? "#2962ff" : "#ef5350";
      line.textContent = `${model === "base" ? "Base" : "Fine-tuned"} — ${entries.join(" · ")}`;
      scores.appendChild(line);
    });
    results.forEach((row) => {
      const option = document.createElement("option");
      option.value = row.symbol;
      option.textContent = `${row.rank}. ${row.symbol}`;
      select.appendChild(option);
      const tr = document.createElement("tr");
      const actual = Object.fromEntries((row.actuals || []).map((p) => [p.time, p.value]));
      const values = [row.rank, row.symbol, price(row.anchor.value)];
      [0, 1].forEach((i) => {
        const directions = row.direction_results[i];
        values.push(prediction(row.base[i], directions.base), prediction(row.finetuned[i], directions.finetuned),
          directions.actual === null ? "Pending" : `${price(actual[saved.target_dates[i]])} · ${directionName(directions.actual)}`);
      });
      values.forEach((value, i) => {
        const td = document.createElement("td");
        if (i === 1) {
          const button = document.createElement("button");
          button.type = "button";
          button.className = "btn";
          button.textContent = value;
          button.addEventListener("click", () => { select.value = row.symbol; draw(); });
          td.appendChild(button);
        } else {
          td.textContent = String(value);
          if ([3, 6].includes(i)) td.style.color = "#2962ff";
          if ([4, 7].includes(i)) td.style.color = "#ef5350";
          if ([5, 8].includes(i)) td.style.color = "#ffffff";
        }
        tr.appendChild(td);
      });
      body.appendChild(tr);
    });
    if (results.some((r) => r.symbol === selected)) select.value = selected;
    draw();
  }

  async function load() {
    refresh.disabled = true;
    try {
      const response = await fetch("/api/kronos/experiment3", { credentials: "same-origin", cache: "no-store" });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.detail || "Could not load saved experiment");
      render(payload);
    } catch (error) {
      status.textContent = `Experiment 3: ${error.message || error}`;
    } finally {
      refresh.disabled = false;
    }
  }
  select.addEventListener("change", draw);
  refresh.addEventListener("click", load);
  load();
})();
