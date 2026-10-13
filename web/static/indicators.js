/* Indicators tab: TradingView study stacks and paper start. */

function initIndicators() {
  const sessionEl = document.getElementById("ind-session");
  if (!sessionEl) return;
  const studiesEl = document.getElementById("ind-studies");
  const savedEl = document.getElementById("ind-saved");
  const statusEl = document.getElementById("ind-stack-status");
  let catalog = [];
  let stacks = [];

  function say(el, text) {
    el.textContent = text;
  }

  function esc(value) {
    return String(value ?? "").replace(/[&<>"']/g, (ch) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[ch]));
  }

  function studyRow(study, index) {
    const row = document.createElement("div");
    row.className = "toolbar";
    row.dataset.index = String(index);
    const options = catalog.map((item) => {
      const selected = item.id === (study && study.id) ? " selected" : "";
      return `<option value="${esc(item.id)}" data-version="${esc(item.version || "last")}"${selected}>${esc(item.name)}</option>`;
    }).join("");
    const source = (study && study.source) || { type: "price", series: "close" };
    const price = ["close", "open", "high", "low", "hl2", "hlc3", "ohlc4"].map((series) => {
      const selected = source.type === "price" && source.series === series ? " selected" : "";
      return `<option value="price:${series}"${selected}>${series}</option>`;
    }).join("");
    let parents = "";
    for (let i = 0; i < index; i += 1) {
      const selected = source.type === "study" && Number(source.study_index) === i ? " selected" : "";
      parents += `<option value="study:${i}"${selected}>Study ${i + 1} plot</option>`;
    }
    row.innerHTML = `
      <label>Study ${index + 1}
        <select class="ind-id">${options}</select>
      </label>
      <label>Source
        <select class="ind-source">${price}${parents}</select>
      </label>
      <label>Inputs JSON <input class="ind-inputs" type="text" size="24" /></label>
      <button type="button" class="btn ghost ind-remove">Remove</button>`;
    row.querySelector(".ind-inputs").value = JSON.stringify((study && study.inputs) || {});
    row.querySelector(".ind-remove").addEventListener("click", () => {
      row.remove();
      renumber();
    });
    return row;
  }

  function renumber() {
    [...studiesEl.children].forEach((row, index) => {
      row.dataset.index = String(index);
      const label = row.querySelector("label");
      if (label) label.firstChild.textContent = `Study ${index + 1} `;
    });
  }

  function addStudy(study) {
    const index = studiesEl.children.length;
    studiesEl.appendChild(studyRow(study, index));
  }

  function readStudies() {
    return [...studiesEl.children].map((row, index) => {
      const select = row.querySelector(".ind-id");
      const option = select.options[select.selectedIndex];
      const sourceValue = row.querySelector(".ind-source").value;
      let source;
      if (sourceValue.startsWith("study:")) {
        source = { type: "study", study_index: Number(sourceValue.slice(6)), plot: "plot_0" };
      } else {
        source = { type: "price", series: sourceValue.slice(6) };
      }
      let inputs = {};
      const raw = row.querySelector(".ind-inputs").value.trim();
      if (raw) inputs = JSON.parse(raw);
      return {
        id: select.value,
        version: (option && option.dataset.version) || "last",
        inputs,
        source: index === 0 ? { type: "price", series: source.series || "close" } : source,
      };
    });
  }

  function payload() {
    const level = document.getElementById("ind-level").value;
    return {
      id: savedEl.value || undefined,
      name: document.getElementById("ind-name").value.trim(),
      timeframe: document.getElementById("ind-tf").value,
      studies: readStudies(),
      signal: {
        kind: document.getElementById("ind-signal").value,
        direction: document.getElementById("ind-direction").value,
        level: level === "" ? null : Number(level),
        plot: document.getElementById("ind-plot").value.trim() || "plot_0",
        study_index: Math.max(0, studiesEl.children.length - 1),
      },
      stop: {
        kind: document.getElementById("ind-stop").value,
        percent: Number(document.getElementById("ind-stop-pct").value || 2),
        plot: document.getElementById("ind-plot").value.trim() || "plot_0",
        study_index: 0,
      },
      exit_on_opposite: document.getElementById("ind-exit").checked,
    };
  }

  function fillStack(stack) {
    document.getElementById("ind-name").value = stack.name || "";
    document.getElementById("ind-tf").value = stack.timeframe || "1d";
    document.getElementById("ind-signal").value = stack.signal.kind;
    document.getElementById("ind-direction").value = stack.signal.direction;
    document.getElementById("ind-level").value = stack.signal.level == null ? "" : stack.signal.level;
    document.getElementById("ind-plot").value = stack.signal.plot || "plot_0";
    document.getElementById("ind-stop").value = stack.stop.kind;
    document.getElementById("ind-stop-pct").value = stack.stop.percent;
    document.getElementById("ind-exit").checked = !!stack.exit_on_opposite;
    studiesEl.replaceChildren();
    (stack.studies || []).forEach(addStudy);
  }

  function refreshSaved(selectId) {
    savedEl.innerHTML = `<option value="">New stack</option>` + stacks.map((stack) =>
      `<option value="${esc(stack.id)}">${esc(stack.name)}</option>`).join("");
    if (selectId) savedEl.value = selectId;
  }

  document.getElementById("ind-add").addEventListener("click", () => addStudy(null));
  savedEl.addEventListener("change", () => {
    const stack = stacks.find((item) => item.id === savedEl.value);
    if (stack) fillStack(stack);
  });
  document.getElementById("ind-save").addEventListener("click", async () => {
    try {
      const stack = await api("/api/indicators/stacks", { method: "POST", body: JSON.stringify(payload()) });
      say(statusEl, `Saved ${stack.name}.`);
      stacks = (await api("/api/indicators/stacks")).stacks;
      refreshSaved(stack.id);
    } catch (err) {
      say(statusEl, err.message);
    }
  });
  document.getElementById("ind-delete").addEventListener("click", async () => {
    if (!savedEl.value) return;
    try {
      await api("/api/indicators/stacks/" + encodeURIComponent(savedEl.value), { method: "DELETE" });
      say(statusEl, "Deleted.");
      stacks = (await api("/api/indicators/stacks")).stacks;
      refreshSaved("");
      studiesEl.replaceChildren();
      addStudy(null);
      addStudy({ source: { type: "study", study_index: 0, plot: "plot_0" } });
    } catch (err) {
      say(statusEl, err.message);
    }
  });
  document.getElementById("ind-preview").addEventListener("click", async () => {
    const out = document.getElementById("ind-preview-out");
    if (!savedEl.value) {
      say(out, "Save the stack before preview.");
      return;
    }
    try {
      const data = await api("/api/indicators/preview", {
        method: "POST",
        body: JSON.stringify({
          stack_id: savedEl.value,
          symbol: document.getElementById("ind-symbol").value.trim(),
          exchange: document.getElementById("ind-exchange").value.trim(),
        }),
      });
      const fired = data.signal ? data.signal.action : "no signal";
      say(out, `${data.symbol} bar ${data.bar_time}: ${fired}\n${JSON.stringify(data.plots, null, 2)}`);
    } catch (err) {
      say(out, err.message);
    }
  });
  document.getElementById("ind-paper").addEventListener("click", async () => {
    const el = document.getElementById("ind-paper-status");
    if (!savedEl.value) {
      say(el, "Save the stack before starting paper.");
      return;
    }
    try {
      await api("/api/paper/start", {
        method: "POST",
        body: JSON.stringify({
          market: document.getElementById("ind-market").value,
          n_symbols: Number(document.getElementById("ind-n").value),
          extra_symbols: document.getElementById("ind-extra").value,
          use_stream: document.getElementById("ind-stream").checked,
          stream_start: document.getElementById("ind-stream-start").value || null,
          volume_gate: false,
          tv_stack_id: savedEl.value,
        }),
      });
      say(el, "Paper book started. Open the Paper tab for fills.");
    } catch (err) {
      say(el, err.message);
    }
  });

  (async () => {
    try {
      const session = await api("/api/indicators/session");
      if (session.ok) {
        say(sessionEl, session.plan ? `Logged in (${session.plan}).` : "Logged in.");
      } else if (session.reason === "disabled") {
        say(sessionEl, "TradingView studies are off — set TV_SESSIONID and TV_SESSIONID_SIGN.");
      } else {
        say(sessionEl, "session expired — update TV_SESSIONID");
      }
    } catch (err) {
      say(sessionEl, err.message);
    }
    try {
      const data = await api("/api/indicators/catalog");
      catalog = [...(data.builtins || []), ...(data.scripts || [])];
    } catch (err) {
      say(statusEl, err.message);
    }
    try {
      stacks = (await api("/api/indicators/stacks")).stacks || [];
      refreshSaved("");
    } catch (err) {
      say(statusEl, err.message);
    }
    if (!studiesEl.children.length) {
      addStudy(null);
      addStudy({ source: { type: "study", study_index: 0, plot: "plot_0" } });
    }
  })();
}

document.addEventListener("DOMContentLoaded", initIndicators);
