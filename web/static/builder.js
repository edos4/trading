/* Visual indicator builder: condition blocks, drag/drop, preview, backtest. */
(function () {
  "use strict";

  const state = {
    catalog: [],
    operators: [],
    groups: [],
    patterns: [],
    rule: null,
    savedId: "",
    generation: null,
    dirty: false,
    saved: [],
    sortables: [],
  };

  const $ = (id) => document.getElementById(id);
  const MAX_DEPTH = 4;

  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = String(text);
    return node;
  }

  function say(node, text, error) {
    if (!node) return;
    node.textContent = text == null ? "" : String(text);
    node.classList.toggle("error", !!error);
  }

  function entryOf(id) {
    return state.catalog.find((item) => item.id === id) || null;
  }

  function paramDefaults(entry) {
    const params = {};
    (entry && entry.params ? entry.params : []).forEach((param) => {
      params[param.name] = param.default;
    });
    return params;
  }

  function indicatorOperand(id) {
    const entry = entryOf(id) || (state.catalog.length ? state.catalog[0] : null);
    if (!entry) return { source: "indicator", id: "rsi", params: { period: 14 }, field: "value", offset: 0 };
    return {
      source: "indicator",
      id: entry.id,
      params: paramDefaults(entry),
      field: entry.outputs[0] || "value",
      offset: 0,
    };
  }

  function defaultCondition() {
    return {
      kind: "condition",
      left: indicatorOperand("rsi"),
      operator: "gte",
      right: { source: "value", value: 30 },
    };
  }

  function defaultRule() {
    return {
      schema_version: 1,
      name: "indicator1",
      timeframe: "1d",
      direction: "long",
      root: { kind: "group", op: "AND", children: [defaultCondition()] },
      exit: {
        on_false: true,
        stop: { kind: "percent", percent: 5, atr_period: 14, atr_mult: 2 },
        target: { kind: "r_multiple", r: 2, percent: 10 },
        max_hold_bars: null,
      },
      attachments: { patternsv2: { pattern: "all", position: "off", mode: "filter" } },
    };
  }

  function isGroup(node) {
    return node && node.kind === "group";
  }

  function getNode(path) {
    if (path === "root") return state.rule.root;
    let node = state.rule.root;
    path.split(".").slice(1).forEach((raw) => {
      if (!node || !node.children) return;
      node = node.children[Number(raw)];
    });
    return node;
  }

  function parentPathOf(path) {
    const cut = path.lastIndexOf(".");
    return cut < 0 ? "" : path.slice(0, cut);
  }

  function indexOfPath(path) {
    return Number(path.split(".").pop());
  }

  function markDirty(value) {
    state.dirty = !!value;
    const status = $("builder-status");
    if (status) status.textContent = state.dirty ? "Unsaved changes." : "";
    const save = $("builder-save");
    if (save) save.classList.toggle("primary", state.dirty);
  }

  // ── render ────────────────────────────────────────────────────────────────
  function renderSaved(selected) {
    const select = $("builder-saved");
    if (!select) return;
    select.textContent = "";
    const blank = el("option", null, state.saved.length ? "Select a saved indicator" : "No saved indicators");
    blank.value = "";
    select.appendChild(blank);
    state.saved.forEach((rule) => {
      const option = el("option", null,
        `${rule.name} · ${rule.timeframe} · ${rule.direction} · ${rule.conditions} cond`);
      option.value = rule.id;
      select.appendChild(option);
    });
    if (selected && state.saved.some((rule) => rule.id === selected)) {
      select.value = selected;
    }
  }

  function renderEditor() {
    const root = $("builder-root");
    if (!root || !state.rule) return;
    root.textContent = "";
    root.appendChild(renderGroup(state.rule.root, "root", 0));
    initSortables();
  }

  function renderConnector(group) {
    const chip = el("button", "builder-connector", group.op);
    chip.type = "button";
    chip.title = "Toggle AND / OR for this block";
    chip.addEventListener("click", () => {
      group.op = group.op === "AND" ? "OR" : "AND";
      markDirty(true);
      renderEditor();
    });
    return chip;
  }

  function blockLabel(path) {
    const parent = getNode(parentPathOf(path));
    const index = indexOfPath(path);
    if (!parent || !parent.children) return "Block";
    let ordinal = 0;
    for (let i = 0; i <= index && i < parent.children.length; i += 1) {
      if (isGroup(parent.children[i])) ordinal += 1;
    }
    return `Block ${Math.max(0, ordinal - 1)}`;
  }

  function renderGroup(group, path, depth) {
    const wrap = el("div", "builder-group");
    wrap.dataset.path = path;
    wrap.dataset.depth = String(depth);

    const head = el("div", "builder-group-head");
    if (depth > 0) {
      const handle = el("span", "builder-handle", "⠿");
      handle.title = "Drag this block";
      head.appendChild(handle);
    }
    head.appendChild(el("strong", "builder-group-title",
      depth === 0 ? "Rule" : blockLabel(path)));
    head.appendChild(renderConnector(group));

    const actions = el("span", "builder-group-actions");
    const addCondition = el("button", "btn ghost", "+ Condition");
    addCondition.type = "button";
    addCondition.title = "Add another condition to this block.";
    addCondition.addEventListener("click", () => addConditionTo(group));
    actions.appendChild(addCondition);
    if (depth < MAX_DEPTH - 1) {
      const addGroup = el("button", "btn ghost", "+ Group");
      addGroup.type = "button";
      addGroup.title = "Add a nested group with its own AND/OR connector.";
      addGroup.addEventListener("click", () => addGroupTo(group));
      actions.appendChild(addGroup);
    }
    if (depth > 0) {
      const up = el("button", "btn ghost", "▲");
      up.type = "button";
      up.title = "Move block up";
      up.addEventListener("click", () => moveWithin(path, -1));
      const down = el("button", "btn ghost", "▼");
      down.type = "button";
      down.title = "Move block down";
      down.addEventListener("click", () => moveWithin(path, 1));
      const remove = el("button", "btn danger", "×");
      remove.type = "button";
      remove.title = "Remove block";
      remove.addEventListener("click", () => removeNode(path));
      actions.appendChild(up);
      actions.appendChild(down);
      actions.appendChild(remove);
    }
    head.appendChild(actions);
    wrap.appendChild(head);

    const children = el("div", "builder-children");
    children.dataset.path = path;
    group.children.forEach((child, index) => {
      const childPath = `${path}.${index}`;
      children.appendChild(isGroup(child)
        ? renderGroup(child, childPath, depth + 1)
        : renderCondition(child, childPath));
      if (index < group.children.length - 1) {
        children.appendChild(renderConnector(group));
      }
    });
    if (!group.children.length) {
      children.appendChild(el("p", "muted small", "No conditions yet — add one."));
    }
    wrap.appendChild(children);
    return wrap;
  }

  function renderOperand(operand, onChange) {
    const box = el("span", "builder-operand");
    const entry = entryOf(operand.id) || state.catalog[0];

    const select = el("select", "builder-indicator");
    select.title = "Indicator (and output) whose value is compared on the left side.";
    const groups = {};
    state.catalog.forEach((item) => {
      if (!groups[item.group]) {
        groups[item.group] = el("optgroup");
        groups[item.group].label = item.group;
        select.appendChild(groups[item.group]);
      }
      const option = el("option", null, item.label);
      option.value = item.id;
      groups[item.group].appendChild(option);
    });
    select.value = operand.id;
    select.addEventListener("change", () => {
      const next = entryOf(select.value);
      operand.id = select.value;
      operand.params = paramDefaults(next);
      operand.field = next && next.outputs[0] ? next.outputs[0] : "value";
      operand.offset = 0;
      markDirty(true);
      renderEditor();
      onChange && onChange();
    });
    box.appendChild(select);

    const params = entry && entry.params ? entry.params : [];
    if (params.length) {
      const details = el("details", "builder-params");
      const summary = el("summary", "builder-chip");
      const chipText = () => `(${params.map((param) =>
        operand.params[param.name] != null ? operand.params[param.name] : param.default).join(", ")})`;
      summary.textContent = chipText();
      summary.title = "Click to edit indicator parameters";
      details.appendChild(summary);
      params.forEach((param) => {
        const wrap = el("label", "builder-param", param.label);
        const input = el("input");
        input.type = "number";
        input.min = param.min;
        input.max = param.max;
        input.step = param.kind === "int" ? "1" : "0.1";
        input.value = operand.params[param.name] != null ? operand.params[param.name] : param.default;
        input.title = `${param.label} for ${entry ? entry.label : operand.id}`;
        input.addEventListener("input", () => {
          operand.params[param.name] = Number(input.value);
          summary.textContent = chipText();
          markDirty(true);
        });
        wrap.appendChild(input);
        details.appendChild(wrap);
      });
      box.appendChild(details);
    }

    if (entry && entry.outputs && entry.outputs.length > 1) {
      const wrap = el("label", "builder-param", "output");
      const out = el("select");
      out.title = "Which output of this indicator to compare.";
      entry.outputs.forEach((name) => {
        const option = el("option", null, name);
        option.value = name;
        out.appendChild(option);
      });
      out.value = operand.field || entry.outputs[0];
      out.addEventListener("change", () => {
        operand.field = out.value;
        markDirty(true);
      });
      wrap.appendChild(out);
      box.appendChild(wrap);
    }

    const shift = el("label", "builder-param", "bars ago");
    const shiftInput = el("input");
    shiftInput.type = "number";
    shiftInput.min = "0";
    shiftInput.max = "500";
    shiftInput.step = "1";
    shiftInput.value = operand.offset || 0;
    shiftInput.title = "Compare this value N bars ago (0 = current bar).";
    shiftInput.addEventListener("input", () => {
      operand.offset = Math.max(0, Number(shiftInput.value) || 0);
      markDirty(true);
    });
    shift.appendChild(shiftInput);
    box.appendChild(shift);
    return box;
  }

  function renderCondition(condition, path) {
    const row = el("div", "builder-condition");
    row.dataset.path = path;

    const handle = el("span", "builder-handle", "⠿");
    handle.title = "Drag this condition";
    row.appendChild(handle);
    row.appendChild(renderOperand(condition.left));

    const operators = el("select", "builder-operator");
    operators.title = "Comparison: >, >=, <, <=, =, !=, or a crossing event.";
    state.operators.forEach((operator) => {
      const option = el("option", null, operator.label);
      option.value = operator.id;
      operators.appendChild(option);
    });
    operators.value = condition.operator;
    operators.addEventListener("change", () => {
      condition.operator = operators.value;
      markDirty(true);
    });
    row.appendChild(operators);

    const mode = el("select", "builder-right-mode");
    mode.title = "Compare the left side with a fixed number or another indicator.";
    [["value", "value"], ["indicator", "indicator"]].forEach(([value, label]) => {
      const option = el("option", null, label);
      option.value = value;
      mode.appendChild(option);
    });
    mode.value = condition.right.source === "indicator" ? "indicator" : "value";
    mode.addEventListener("change", () => {
      if (mode.value === "indicator") {
        condition.right = indicatorOperand("sma");
      } else {
        condition.right = { source: "value", value: 30 };
      }
      markDirty(true);
      renderEditor();
    });
    row.appendChild(mode);

    if (condition.right.source === "indicator") {
      row.appendChild(renderOperand(condition.right));
    } else {
      const input = el("input", "builder-value");
      input.type = "number";
      input.step = "any";
      input.title = "Fixed number the left side is compared with.";
      input.value = condition.right.value != null ? condition.right.value : 0;
      input.addEventListener("input", () => {
        condition.right.value = Number(input.value);
        markDirty(true);
      });
      row.appendChild(input);
    }

    const actions = el("span", "builder-condition-actions");
    const up = el("button", "btn ghost", "▲");
    up.type = "button";
    up.title = "Move up";
    up.addEventListener("click", () => moveWithin(path, -1));
    const down = el("button", "btn ghost", "▼");
    down.type = "button";
    down.title = "Move down";
    down.addEventListener("click", () => moveWithin(path, 1));
    const remove = el("button", "btn danger", "×");
    remove.type = "button";
    remove.title = "Remove condition";
    remove.addEventListener("click", () => removeNode(path));
    actions.appendChild(up);
    actions.appendChild(down);
    actions.appendChild(remove);
    row.appendChild(actions);
    return row;
  }

  // ── tree edits ────────────────────────────────────────────────────────────
  function addConditionTo(group) {
    group.children.push(defaultCondition());
    markDirty(true);
    renderEditor();
  }

  function addGroupTo(group) {
    group.children.push({ kind: "group", op: "AND", children: [defaultCondition()] });
    markDirty(true);
    renderEditor();
  }

  function removeNode(path) {
    const parentPath = parentPathOf(path);
    const parent = getNode(parentPath);
    if (!parent || !parent.children) return;
    parent.children.splice(indexOfPath(path), 1);
    markDirty(true);
    renderEditor();
  }

  function moveWithin(path, delta) {
    const parent = getNode(parentPathOf(path));
    if (!parent || !parent.children) return;
    const index = indexOfPath(path);
    const target = index + delta;
    if (target < 0 || target >= parent.children.length) return;
    const [node] = parent.children.splice(index, 1);
    parent.children.splice(target, 0, node);
    markDirty(true);
    renderEditor();
  }

  function initSortables() {
    state.sortables.forEach((sortable) => {
      try { sortable.destroy(); } catch (err) { /* already gone */ }
    });
    state.sortables = [];
    if (typeof window.Sortable === "undefined") return;
    document.querySelectorAll("#builder-root .builder-children").forEach((container) => {
      state.sortables.push(window.Sortable.create(container, {
        group: "rule-blocks",
        animation: 120,
        handle: ".builder-handle",
        draggable: ".builder-condition, .builder-group",
        ghostClass: "builder-ghost",
        onEnd: onDrop,
      }));
    });
  }

  function onDrop(evt) {
    const fromPath = evt.item && evt.item.dataset ? evt.item.dataset.path : "";
    const toPath = evt.to && evt.to.dataset ? evt.to.dataset.path : "";
    if (!fromPath || !toPath) return renderEditor();
    if (toPath === fromPath || toPath.indexOf(`${fromPath}.`) === 0) {
      return renderEditor(); // never drop a block inside itself
    }
    const sourceParent = getNode(parentPathOf(fromPath));
    const target = getNode(toPath);
    if (!sourceParent || !target || !target.children) return renderEditor();
    const sourceIndex = indexOfPath(fromPath);
    const node = sourceParent.children[sourceIndex];
    if (!node) return renderEditor();
    sourceParent.children.splice(sourceIndex, 1);
    // `newIndex` counts connector chips too; `newDraggableIndex` counts only
    // conditions/groups, which is what the rule tree stores.
    const rawIndex = evt.newDraggableIndex != null ? evt.newDraggableIndex : evt.newIndex;
    const insertAt = Math.max(0, Math.min(rawIndex, target.children.length));
    target.children.splice(insertAt, 0, node);
    markDirty(true);
    renderEditor();
  }

  // ── controls ──────────────────────────────────────────────────────────────
  function fillControls() {
    const rule = state.rule;
    if (!rule) return;
    $("builder-name").value = rule.name || "";
    $("builder-timeframe").value = rule.timeframe || "1d";
    $("builder-direction").value = rule.direction || "long";
    const exit = rule.exit || {};
    $("builder-on-false").checked = exit.on_false !== false;
    const stop = exit.stop || {};
    $("builder-stop-kind").value = stop.kind || "percent";
    $("builder-stop-percent").value = stop.percent != null ? stop.percent : 5;
    $("builder-stop-atr-mult").value = stop.atr_mult != null ? stop.atr_mult : 2;
    $("builder-stop-atr-period").value = stop.atr_period != null ? stop.atr_period : 14;
    const target = exit.target || {};
    $("builder-target-kind").value = target.kind || "r_multiple";
    $("builder-target-r").value = target.r != null ? target.r : 2;
    $("builder-target-percent").value = target.percent != null ? target.percent : 10;
    $("builder-max-hold").value = exit.max_hold_bars != null ? exit.max_hold_bars : "";
    const attach = (rule.attachments && rule.attachments.patternsv2) || {};
    $("builder-attach-position").value = attach.position || "off";
    $("builder-attach-pattern").value = attach.pattern || "all";
    $("builder-attach-mode").value = attach.mode || "filter";
    updateAttachmentVisibility();
    updateExitVisibility();
  }

  function populatePatternOptions() {
    const select = $("builder-attach-pattern");
    if (!select) return;
    select.textContent = "";
    const all = el("option", null, "All Patternsv2 patterns");
    all.value = "all";
    select.appendChild(all);
    state.patterns.forEach((pattern) => {
      const option = el("option", null, pattern.label || pattern.id);
      option.value = pattern.id;
      select.appendChild(option);
    });
  }

  function updateExitVisibility() {
    const stopKind = $("builder-stop-kind").value;
    $("builder-stop-percent").closest("label").hidden = stopKind !== "percent";
    $("builder-stop-atr-mult").closest("label").hidden = stopKind !== "atr";
    $("builder-stop-atr-period").closest("label").hidden = stopKind !== "atr";
    const targetKind = $("builder-target-kind").value;
    $("builder-target-r").closest("label").hidden = targetKind !== "r_multiple";
    $("builder-target-percent").closest("label").hidden = targetKind !== "percent";
  }

  function updateAttachmentVisibility() {
    const position = $("builder-attach-position").value;
    const mode = $("builder-attach-mode");
    const pattern = $("builder-attach-pattern");
    const before = position === "before";
    if (before) mode.value = "filter";
    mode.disabled = before || position === "off";
    pattern.disabled = position === "off";
    const target = pattern.value === "all"
      ? "Patternsv2"
      : (pattern.selectedOptions[0] ? pattern.selectedOptions[0].textContent : pattern.value);
    const allPatterns = pattern.value === "all";
    $("builder-attach-hint").textContent = position === "before"
      ? (allPatterns
        ? "Before: Patternsv2 detection is skipped on bars where this rule is false. Filter mode only."
        : `Before: ${target} signals are dropped on bars where this rule is false; other patterns still run. Filter mode only.`)
      : position === "after"
        ? (allPatterns
          ? "After: every Patternsv2 pattern detects first; this rule then filters or shadows their signals."
          : `After: ${target} detects first; this rule then filters or shadows its signals.`)
        : "Off: Patternsv2 runs without this rule. Paper's Rule filter can still select it manually.";
    const modeHint = $("builder-attach-mode-hint");
    if (modeHint) {
      if (position === "off") {
        modeHint.textContent = "Mode is ignored while the attachment is Off.";
      } else if (before) {
        modeHint.textContent =
          "Before placement always uses Filter; there is no signal to shadow yet.";
      } else if (mode.value === "filter") {
        modeHint.textContent =
          "Filter: reject the selected pattern's signals while this rule is false.";
      } else {
        modeHint.textContent =
          "Shadow: never block; record rule_evidence on each signal so you can compare.";
      }
    }
  }

  function bindExitControls() {
    $("builder-name").addEventListener("input", () => {
      state.rule.name = $("builder-name").value;
      markDirty(true);
    });
    $("builder-timeframe").addEventListener("change", () => {
      state.rule.timeframe = $("builder-timeframe").value;
      markDirty(true);
    });
    $("builder-direction").addEventListener("change", () => {
      state.rule.direction = $("builder-direction").value;
      markDirty(true);
    });
    $("builder-on-false").addEventListener("change", () => {
      state.rule.exit.on_false = $("builder-on-false").checked;
      markDirty(true);
    });
    $("builder-stop-kind").addEventListener("change", () => {
      state.rule.exit.stop.kind = $("builder-stop-kind").value;
      updateExitVisibility();
      markDirty(true);
    });
    $("builder-stop-percent").addEventListener("input", () => {
      state.rule.exit.stop.percent = Number($("builder-stop-percent").value);
      markDirty(true);
    });
    $("builder-stop-atr-mult").addEventListener("input", () => {
      state.rule.exit.stop.atr_mult = Number($("builder-stop-atr-mult").value);
      markDirty(true);
    });
    $("builder-stop-atr-period").addEventListener("input", () => {
      state.rule.exit.stop.atr_period = Number($("builder-stop-atr-period").value);
      markDirty(true);
    });
    $("builder-target-kind").addEventListener("change", () => {
      state.rule.exit.target.kind = $("builder-target-kind").value;
      updateExitVisibility();
      markDirty(true);
    });
    $("builder-target-r").addEventListener("input", () => {
      state.rule.exit.target.r = Number($("builder-target-r").value);
      markDirty(true);
    });
    $("builder-target-percent").addEventListener("input", () => {
      state.rule.exit.target.percent = Number($("builder-target-percent").value);
      markDirty(true);
    });
    $("builder-max-hold").addEventListener("input", () => {
      const raw = $("builder-max-hold").value;
      state.rule.exit.max_hold_bars = raw === "" ? null : Number(raw);
      markDirty(true);
    });
    $("builder-attach-position").addEventListener("change", () => {
      state.rule.attachments.patternsv2.position = $("builder-attach-position").value;
      if ($("builder-attach-position").value === "before") {
        state.rule.attachments.patternsv2.mode = "filter";
      }
      updateAttachmentVisibility();
      markDirty(true);
    });
    $("builder-attach-pattern").addEventListener("change", () => {
      state.rule.attachments.patternsv2.pattern = $("builder-attach-pattern").value;
      updateAttachmentVisibility();
      markDirty(true);
    });
    $("builder-attach-mode").addEventListener("change", () => {
      state.rule.attachments.patternsv2.mode = $("builder-attach-mode").value;
      updateAttachmentVisibility();
      markDirty(true);
    });
  }

  // ── persistence ───────────────────────────────────────────────────────────
  async function loadSavedList(selected) {
    const res = await api("/api/rules");
    state.saved = res.rules || [];
    renderSaved(selected);
  }

  function loadRule(payload, savedId, generation) {
    const attachment = (payload.attachments && payload.attachments.patternsv2) || {};
    payload.attachments = {
      patternsv2: {
        pattern: attachment.pattern || "all",
        position: attachment.position || "off",
        mode: attachment.mode || "filter",
      },
    };
    state.rule = payload;
    state.savedId = savedId || "";
    state.generation = generation == null ? null : Number(generation);
    fillControls();
    renderEditor();
    markDirty(false);
  }

  async function openSaved(id) {
    if (!id) return;
    try {
      const row = await api(`/api/rules/${encodeURIComponent(id)}`);
      const payload = Object.assign({}, row.payload, { name: row.name });
      loadRule(payload, row.id, row.generation);
      say($("builder-list-status"), "");
    } catch (err) {
      say($("builder-list-status"), err.message, true);
    }
  }

  async function saveRule() {
    try {
      const body = {
        name: $("builder-name").value.trim(),
        payload: state.rule,
      };
      if (state.savedId) {
        body.rule_id = state.savedId;
        if (state.generation != null) body.expected_generation = state.generation;
      }
      const row = await api("/api/rules", { method: "POST", body: JSON.stringify(body) });
      state.rule = Object.assign({}, row.payload, { name: row.name });
      loadRule(state.rule, row.id, row.generation);
      await loadSavedList(row.id);
      say($("builder-status"), `Saved ${row.name}.`);
    } catch (err) {
      say($("builder-status"), err.message, true);
    }
  }

  async function deleteRule() {
    if (!state.savedId) {
      say($("builder-status"), "Select a saved indicator first.", true);
      return;
    }
    if (!window.confirm("Delete this saved indicator?")) return;
    try {
      await api(`/api/rules/${encodeURIComponent(state.savedId)}`, { method: "DELETE" });
      state.savedId = "";
      state.generation = null;
      await loadSavedList("");
      say($("builder-status"), "Deleted.");
    } catch (err) {
      say($("builder-status"), err.message, true);
    }
  }

  // ── preview / backtest / paper ────────────────────────────────────────────
  function fmt(value) {
    if (value == null) return "—";
    const number = Number(value);
    if (!Number.isFinite(number)) return String(value);
    return Math.abs(number) >= 1000 ? number.toFixed(1) : number.toFixed(4).replace(/0+$/, "").replace(/\.$/, "");
  }

  async function previewRule() {
    const status = $("builder-preview-status");
    try {
      say(status, "Running…");
      const res = await api("/api/rules/preview", {
        method: "POST",
        body: JSON.stringify({
          payload: state.rule,
          symbol: $("builder-preview-symbol").value.trim(),
          market: $("builder-preview-market").value,
          bars: Number($("builder-preview-bars").value || 120),
        }),
      });
      const body = $("builder-preview-table").querySelector("tbody");
      body.textContent = "";
      res.bars.forEach((row) => {
        const tr = el("tr", row.matched ? "gain-row" : null);
        tr.appendChild(el("td", null, row.date));
        tr.appendChild(el("td", "num", row.close));
        tr.appendChild(el("td", null, row.matched ? "yes" : "no"));
        const evidence = (row.conditions || []).map((item) =>
          `${item.label}: ${fmt(item.left)} ${item.pass ? "✓" : "✗"}`).join("; ");
        tr.appendChild(el("td", "small", evidence));
        body.appendChild(tr);
      });
      $("builder-preview-wrap").hidden = false;
      say(status, `${res.matched_count} match(es) in ${res.bars.length} bars · ` +
        `${res.available_bars} available · needs ${res.required_bars}.`);
    } catch (err) {
      say(status, err.message, true);
    }
  }

  async function runBacktest() {
    const status = $("builder-bt-status");
    if (!state.savedId) {
      say(status, "Save the indicator before running a backtest.", true);
      return;
    }
    try {
      say(status, "Running…");
      const res = await api(`/api/rules/${encodeURIComponent(state.savedId)}/backtest`, {
        method: "POST",
        body: JSON.stringify({
          symbols: $("builder-bt-symbols").value,
          market: $("builder-bt-market").value,
          start_date: $("builder-bt-start").value || null,
          end_date: $("builder-bt-end").value || null,
          position_notional: Number($("builder-bt-notional").value || 10000),
        }),
      });
      const body = $("builder-bt-table").querySelector("tbody");
      body.textContent = "";
      res.trades.forEach((trade) => {
        const tr = el("tr", trade.pnl >= 0 ? "gain-row" : "loss-row");
        tr.appendChild(el("td", null, trade.symbol));
        tr.appendChild(el("td", null, trade.entry_date));
        tr.appendChild(el("td", null, trade.exit_date));
        tr.appendChild(el("td", "num", trade.entry));
        tr.appendChild(el("td", "num", trade.exit));
        tr.appendChild(el("td", "num", trade.bars));
        tr.appendChild(el("td", null, trade.reason));
        tr.appendChild(el("td", "num", `${trade.pnl_pct >= 0 ? "+" : ""}${trade.pnl_pct.toFixed(2)}%`));
        body.appendChild(tr);
      });
      $("builder-bt-wrap").hidden = false;
      const matchesBody = $("builder-bt-matches").querySelector("tbody");
      matchesBody.textContent = "";
      (res.matches || []).slice(0, 200).forEach((row) => {
        const tr = el("tr", row.matched ? "gain-row" : null);
        tr.appendChild(el("td", null, row.symbol));
        tr.appendChild(el("td", null, row.date));
        tr.appendChild(el("td", "num", row.close));
        tr.appendChild(el("td", null, row.matched ? "match" : "—"));
        matchesBody.appendChild(tr);
      });
      $("builder-bt-matches-wrap").hidden = false;
      const s = res.summary || {};
      $("builder-bt-summary").textContent =
        `${s.trades || 0} trade(s) · win rate ${s.win_rate || 0}% · ` +
        `avg ${s.avg_pnl_pct || 0}% · expectancy $${s.expectancy || 0} · ` +
        `total $${s.total_pnl || 0} · max DD $${s.max_drawdown || 0}` +
        ((res.matches || []).length > 200 ? " · match timeline truncated to 200 rows" : "") +
        (res.errors && res.errors.length ? ` · ${res.errors.length} symbol error(s)` : "");
      say(status, "");
    } catch (err) {
      say(status, err.message, true);
    }
  }

  async function startPaper() {
    const status = $("builder-paper-status");
    if (!state.savedId) {
      say(status, "Save the indicator before starting a paper book.", true);
      return;
    }
    try {
      const attachment = (state.rule.attachments && state.rule.attachments.patternsv2) || {};
      const placement = attachment.position || "off";
      const body = {
        market: $("builder-paper-market").value,
        n_symbols: Number($("builder-paper-n").value || 50),
        extra_symbols: $("builder-paper-extra").value || "",
        use_stream: $("builder-paper-stream").checked,
        volume_gate: false,
      };
      if (placement === "off") {
        body.rule_id = state.savedId;
      } else {
        body.patterns_v2 = true;
        body.rule_filter_id = state.savedId;
        body.rule_filter_mode = attachment.mode || "filter";
        body.rule_filter_position = placement;
      }
      await api("/api/paper/start", {
        method: "POST",
        body: JSON.stringify(body),
      });
      say(status, placement === "off"
        ? "Rule-strategy paper book started. Open the Paper tab for fills."
        : `Patternsv2 + rule (${placement}) paper book started. Open the Paper tab for fills.`);
    } catch (err) {
      say(status, err.message, true);
    }
  }

  // ── boot ──────────────────────────────────────────────────────────────────
  async function init() {
    bindExitControls();
    try {
      const catalog = await api("/api/rules/catalog");
      state.catalog = catalog.indicators || [];
      state.operators = catalog.operators || [];
      state.groups = catalog.groups || [];
      state.patterns = catalog.patternsv2_patterns || [];
      populatePatternOptions();
      say($("builder-session"), `${state.catalog.length} indicators available`);
    } catch (err) {
      say($("builder-session"), err.message, true);
    }
    try {
      await loadSavedList("");
    } catch (err) {
      say($("builder-list-status"), err.message, true);
    }
    loadRule(defaultRule(), "", null);

    $("builder-new").addEventListener("click", () => {
      if (state.dirty && !window.confirm("Discard unsaved changes?")) return;
      loadRule(defaultRule(), "", null);
      $("builder-saved").value = "";
    });
    $("builder-duplicate").addEventListener("click", () => {
      const copy = JSON.parse(JSON.stringify(state.rule));
      copy.name = `${state.rule.name || "indicator"} copy`;
      loadRule(copy, "", null);
      $("builder-saved").value = "";
      markDirty(true);
    });
    $("builder-saved").addEventListener("change", (event) => {
      if (state.dirty && !window.confirm("Discard unsaved changes?")) {
        event.target.value = state.savedId || "";
        return;
      }
      openSaved(event.target.value);
    });
    $("builder-save").addEventListener("click", saveRule);
    $("builder-delete").addEventListener("click", deleteRule);
    $("builder-add-block").addEventListener("click", () => addGroupTo(state.rule.root));
    $("builder-add-condition").addEventListener("click", () => addConditionTo(state.rule.root));
    $("builder-preview").addEventListener("click", previewRule);
    $("builder-bt-run").addEventListener("click", runBacktest);
    $("builder-paper-start").addEventListener("click", startPaper);

    window.addEventListener("beforeunload", (event) => {
      if (!state.dirty) return;
      event.preventDefault();
      event.returnValue = "";
    });
  }

  document.addEventListener("DOMContentLoaded", init);
})();
