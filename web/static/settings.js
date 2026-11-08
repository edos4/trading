/* Settings tab: dirty tracking, search and save. */
(function () {
  const form = document.getElementById("settings-form");
  if (!form) return;

  const status = document.getElementById("settings-status");
  const dirtyLabel = document.getElementById("settings-dirty");
  const search = document.getElementById("settings-search");
  const empty = document.getElementById("settings-empty");
  const saveButtons = [document.getElementById("settings-save"), document.getElementById("settings-save-bottom")];
  const resetButtons = [document.getElementById("settings-reset"), document.getElementById("settings-reset-bottom")];

  const inputs = Array.from(form.querySelectorAll("[data-setting]"));
  const fieldNodes = Array.from(form.querySelectorAll(".settings-field"));
  const groupNodes = Array.from(form.querySelectorAll(".settings-group"));
  let snapshot = new Map();
  let saving = false;

  function clearBox(name) {
    return form.querySelector(`[data-clear-for="${name}"]`);
  }

  // ── Pattern toggle lists (one checkbox per shipped detector) ────────────
  // Checked means enabled, so the field value is the muted (unticked) ids —
  // which is also what the server reports back for these fields.
  function checklistBoxes(name) {
    return Array.from(form.querySelectorAll(`[data-checklist-for="${name}"] input[data-pattern]`));
  }

  function isForcedBox(box) {
    return box.dataset.forced === "1";
  }

  function mutedIds(name) {
    return checklistBoxes(name)
      .filter((box) => isForcedBox(box) || !box.checked)
      .map((box) => box.dataset.pattern)
      .sort();
  }

  function forcedIds(name) {
    return new Set(checklistBoxes(name).filter(isForcedBox).map((box) => box.dataset.pattern));
  }

  function mutedList(raw) {
    const items = Array.isArray(raw) ? raw : String(raw == null ? "" : raw).split(",");
    return new Set(items.map((item) => String(item).trim()).filter(Boolean));
  }

  function readRaw(el) {
    const kind = el.dataset.kind;
    if (kind === "checklist") return mutedIds(el.dataset.setting).join(",");
    if (kind === "bool") return el.checked ? "on" : "off";
    if (kind === "secret") {
      if (el.value !== "") return "new:" + el.value;
      const box = clearBox(el.dataset.setting);
      return box && box.checked ? "clear" : "keep";
    }
    return el.value;
  }

  function readValue(el) {
    const kind = el.dataset.kind;
    if (kind === "checklist") {
      // Persist only the boxes the operator can change; the shipped-off ones are
      // muted regardless, and the server adds them back to the field value.
      const locked = forcedIds(el.dataset.setting);
      return mutedIds(el.dataset.setting).filter((id) => !locked.has(id)).join(",");
    }
    if (kind === "bool") return el.checked;
    if (kind === "secret") return el.value;
    if (kind === "int") {
      const n = parseInt(el.value, 10);
      return Number.isNaN(n) ? el.value : n;
    }
    if (kind === "float") {
      const n = parseFloat(el.value);
      return Number.isNaN(n) ? el.value : n;
    }
    return el.value;
  }

  function writeRaw(el, raw) {
    const kind = el.dataset.kind;
    if (kind === "checklist") {
      const muted = mutedList(raw);
      checklistBoxes(el.dataset.setting).forEach((box) => {
        box.checked = !isForcedBox(box) && !muted.has(box.dataset.pattern);
      });
      return;
    }
    if (kind === "bool") {
      el.checked = raw === "on" || raw === true;
      return;
    }
    if (kind === "secret") {
      el.value = "";
      return;
    }
    el.value = raw == null ? "" : String(raw);
  }

  function setSecretState(name, configured) {
    const field = form.querySelector(`.settings-field[data-field="${name}"]`);
    if (!field) return;
    const label = field.querySelector(".settings-secret-state");
    if (label) {
      label.textContent = configured ? "Set" : "Not set";
      label.classList.toggle("is-set", configured);
    }
    const input = field.querySelector("[data-setting]");
    if (input) {
      input.placeholder = configured
        ? "Leave blank to keep current"
        : "Not configured — enter a value";
    }
    const box = clearBox(name);
    if (box) {
      box.checked = false;
      box.disabled = !configured;
      const wrap = box.closest(".settings-clear");
      if (wrap) wrap.hidden = !configured;
    }
  }

  function takeSnapshot() {
    snapshot = new Map(inputs.map((el) => [el.dataset.setting, readRaw(el)]));
    refreshDirty();
  }

  function changedFields() {
    return inputs.filter((el) => readRaw(el) !== snapshot.get(el.dataset.setting));
  }

  function refreshDirty() {
    const changed = changedFields();
    const count = changed.length;
    if (dirtyLabel) {
      dirtyLabel.textContent = count
        ? `${count} unsaved change${count === 1 ? "" : "s"}`
        : "No unsaved changes";
    }
    saveButtons.forEach((b) => { if (b) b.disabled = saving || count === 0; });
    resetButtons.forEach((b) => { if (b) b.disabled = saving || count === 0; });
    return changed;
  }

  function setStatus(text, kind) {
    if (!status) return;
    status.textContent = text || "";
    status.classList.toggle("is-error", kind === "error");
    status.classList.toggle("is-ok", kind === "ok");
  }

  function labelFor(name) {
    const field = form.querySelector(`.settings-field[data-field="${name}"]`);
    if (!field) return name;
    const label = field.querySelector("label[for], .settings-field-title");
    return label ? label.textContent.trim() : name;
  }

  function applyServerValues(values, configured) {
    if (values) {
      inputs.forEach((el) => {
        if (el.dataset.kind === "secret") return;
        if (Object.prototype.hasOwnProperty.call(values, el.dataset.setting)) {
          writeRaw(el, values[el.dataset.setting]);
        }
      });
    }
    if (configured) {
      Object.entries(configured).forEach(([name, isSet]) => setSecretState(name, isSet));
    }
  }

  async function save(event) {
    event.preventDefault();
    if (saving) return;
    const changed = refreshDirty();
    if (!changed.length) return;

    const payload = {};
    const bad = [];
    changed.forEach((el) => {
      const value = readValue(el);
      const kind = el.dataset.kind;
      if ((kind === "int" || kind === "float") && typeof value === "string") {
        bad.push(labelFor(el.dataset.setting));
        return;
      }
      payload[el.dataset.setting] = value;
    });
    if (bad.length) {
      setStatus(`Enter a number for: ${bad.join(", ")}`, "error");
      return;
    }

    saving = true;
    refreshDirty();
    setStatus("Saving…");
    try {
      const result = await api("/api/settings", {
        method: "POST",
        body: JSON.stringify({ values: payload }),
      });
      applyServerValues(result.values, result.configured);
      takeSnapshot();
      let message = `Saved ${result.saved.length} setting${result.saved.length === 1 ? "" : "s"}.`;
      if (result.restart_required.length) {
        const names = result.restart_required.map(labelFor).join(", ");
        message += ` Restart the web service to activate: ${names}.`;
      }
      setStatus(message, "ok");
    } catch (err) {
      setStatus(err.message || "Could not save settings.", "error");
    } finally {
      saving = false;
      refreshDirty();
    }
  }

  function reset() {
    inputs.forEach((el) => writeRaw(el, snapshot.get(el.dataset.setting)));
    form.querySelectorAll("[data-clear-for]").forEach((box) => { box.checked = false; });
    setStatus("");
    refreshDirty();
  }

  function filter() {
    const query = (search.value || "").trim().toLowerCase();
    let visible = 0;
    fieldNodes.forEach((node) => {
      const match = !query || node.dataset.search.includes(query);
      node.hidden = !match;
      if (match) visible += 1;
    });
    groupNodes.forEach((group) => {
      const shown = group.querySelectorAll(".settings-field:not([hidden])").length;
      group.hidden = shown === 0;
      if (query) group.open = true;
    });
    if (empty) empty.hidden = visible !== 0;
  }

  form.addEventListener("submit", save);
  form.addEventListener("input", refreshDirty);
  form.addEventListener("change", refreshDirty);
  resetButtons.forEach((b) => b && b.addEventListener("click", reset));
  if (search) search.addEventListener("input", filter);

  window.addEventListener("beforeunload", (event) => {
    if (changedFields().length) {
      event.preventDefault();
      event.returnValue = "";
    }
  });

  takeSnapshot();
})();
