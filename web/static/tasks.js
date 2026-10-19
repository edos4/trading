/* Tasks (Kanban) board.
 * Plain DOM (no framework) + vendored SortableJS for pointer/touch drag and
 * drop. All text is rendered with textContent, never untrusted HTML. Every
 * mutation reloads the board from /api/tasks/board so positions/order stay in
 * sync; a failed call surfaces {error_code, detail} and reverts.
 */
(function () {
  "use strict";

  var boardEl = document.getElementById("kanban-board");
  if (!boardEl) return;

  var statusEl = document.getElementById("kanban-status");
  var dialog = document.getElementById("kanban-dialog");
  var archivedPanel = document.getElementById("kanban-archived");
  var archivedWrap = document.getElementById("kanban-archived-list");
  var toggleArchivedBtn = document.getElementById("kanban-toggle-archived");
  var exportBtn = document.getElementById("kanban-export");

  var state = { lists: [], includeArchived: false, card: null, comments: [], replyTo: null };

  function el(id) { return document.getElementById(id); }
  function setStatus(msg) { if (statusEl) statusEl.textContent = msg || ""; }

  function h(tag, props, children) {
    var node = document.createElement(tag);
    if (props) {
      Object.keys(props).forEach(function (key) {
        var value = props[key];
        if (key === "class") node.className = value;
        else if (key === "text") node.textContent = value == null ? "" : String(value);
        else if (key === "dataset") Object.assign(node.dataset, value);
        else if (key.slice(0, 2) === "on" && typeof value === "function") {
          node.addEventListener(key.slice(2), value);
        } else if (value != null && value !== false) {
          node.setAttribute(key, value === true ? "" : value);
        }
      });
    }
    (children || []).forEach(function (child) {
      if (child == null) return;
      node.appendChild(typeof child === "string" ? document.createTextNode(child) : child);
    });
    return node;
  }

  async function api(path, method, body) {
    var res = await fetch(path, {
      method: method || "GET",
      headers: body ? { "Content-Type": "application/json" } : undefined,
      body: body ? JSON.stringify(body) : undefined,
    });
    var data = await res.json().catch(function () { return {}; });
    if (!res.ok) {
      var msg = (data && (data.detail || data.error_code)) || ("HTTP " + res.status);
      throw new Error(String(msg));
    }
    return data;
  }

  function pad(n) { return String(n).padStart(2, "0"); }
  function fmt(iso) {
    if (!iso) return "";
    var d = new Date(iso);
    if (isNaN(d.getTime())) return iso;
    return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate())
      + " " + pad(d.getHours()) + ":" + pad(d.getMinutes());
  }
  function rel(iso) {
    if (!iso) return "";
    var d = new Date(iso);
    if (isNaN(d.getTime())) return "";
    var s = Math.round((Date.now() - d.getTime()) / 1000);
    if (s < 60) return "just now";
    if (s < 3600) return Math.floor(s / 60) + "m ago";
    if (s < 86400) return Math.floor(s / 3600) + "h ago";
    return Math.floor(s / 86400) + "d ago";
  }
  function initial(name) { return (name || "?").trim().charAt(0); }

  /* ── board read/render ────────────────────────────────────────────────── */
  async function loadBoard() {
    var suffix = state.includeArchived ? "?include_archived=1" : "";
    var data = await api("/api/tasks/board" + suffix);
    state.lists = (data && data.lists) || [];
    render();
  }

  function render() {
    boardEl.textContent = "";
    state.lists.forEach(function (list) { boardEl.appendChild(renderList(list)); });
    boardEl.appendChild(renderAddList());
    initSortables();
    if (state.includeArchived) renderArchived();
    else if (archivedPanel) archivedPanel.hidden = true;
  }

  function renderList(list) {
    var cards = h("div", { class: "kanban-cards", dataset: { list: list.id } });
    if (!list.cards.length) {
      cards.appendChild(h("p", { class: "kanban-empty", text: "No cards yet" }));
    }
    list.cards.forEach(function (card) { cards.appendChild(renderCard(card)); });

    var title = h("span", { class: "kanban-list-title", text: list.title,
      onclick: function () { renameList(list, title); } });
    var menuBox = h("div", { class: "menu", hidden: true }, [
      h("button", { class: "btn ghost", type: "button", text: "Rename", onclick: function () { hideMenus(); renameList(list, title); } }),
      h("button", { class: "btn ghost", type: "button", text: "Move left", onclick: function () { hideMenus(); moveList(list, -1); } }),
      h("button", { class: "btn ghost", type: "button", text: "Move right", onclick: function () { hideMenus(); moveList(list, 1); } }),
      h("button", { class: "btn ghost", type: "button", text: "Archive list", onclick: function () { hideMenus(); archiveList(list); } }),
    ]);
    var menu = h("div", { class: "kanban-list-menu" }, [
      h("button", { class: "btn ghost", type: "button", text: "…", "aria-label": "List menu",
        onclick: function (ev) { ev.stopPropagation(); toggleMenu(menuBox); } }),
      menuBox,
    ]);

    var head = h("div", { class: "kanban-list-head" }, [title, menu]);

    var composer = h("div", { hidden: true });
    var textarea = h("textarea", { rows: 2, maxlength: 200, placeholder: "Card title" });
    var addBtn = h("button", { class: "btn primary", type: "button", text: "Add card",
      onclick: function () { submitCard(list, textarea, composer); } });
    composer.appendChild(textarea);
    composer.appendChild(h("div", { class: "toolbar" }, [
      addBtn,
      h("button", { class: "btn ghost", type: "button", text: "Cancel", onclick: function () { composer.hidden = true; } }),
    ]));
    textarea.addEventListener("keydown", function (ev) {
      if (ev.key === "Enter" && !ev.shiftKey) { ev.preventDefault(); submitCard(list, textarea, composer); }
      if (ev.key === "Escape") { composer.hidden = true; }
    });

    var addCard = h("button", { class: "btn ghost kanban-add-card", type: "button", text: "+ Add a card",
      onclick: function () { composer.hidden = false; textarea.value = ""; textarea.focus(); } });

    return h("section", { class: "kanban-list", dataset: { list: list.id } }, [
      head, cards, addCard, composer,
    ]);
  }

  function renderCard(card) {
    var chips = h("div", { class: "kanban-chips" });
    var subs = card.subtasks || [];
    if (subs.length) {
      var doneCount = subs.filter(function (s) { return s.done; }).length;
      chips.appendChild(h("span", {
        class: "kanban-chip" + (doneCount === subs.length ? " done" : ""),
        text: doneCount + "/" + subs.length,
      }));
    }
    if (card.comment_count) {
      chips.appendChild(h("span", { class: "kanban-chip", text: card.comment_count + " comment" + (card.comment_count === 1 ? "" : "s") }));
    }
    if (card.author) {
      chips.appendChild(h("span", { class: "kanban-avatar", text: initial(card.author), title: "Created by " + card.author }));
    }
    var nodes = [h("div", { class: "kanban-card-title", text: card.title })];
    if (chips.childNodes.length) nodes.push(chips);
    return h("article", { class: "kanban-card", dataset: { id: card.id },
      onclick: function () { openCard(card.id); } }, nodes);
  }

  function renderAddList() {
    var input = h("input", { type: "text", maxlength: 200, placeholder: "List name" });
    var box = h("div", { hidden: true }, [input,
      h("div", { class: "toolbar" }, [
        h("button", { class: "btn primary", type: "button", text: "Add list", onclick: createList }),
        h("button", { class: "btn ghost", type: "button", text: "Cancel", onclick: function () { box.hidden = true; } }),
      ])]);
    input.addEventListener("keydown", function (ev) {
      if (ev.key === "Enter") { ev.preventDefault(); createList(); }
      if (ev.key === "Escape") box.hidden = true;
    });
    function createList() {
      var title = input.value.trim();
      if (!title) return;
      withStatus(async function () {
        await api("/api/tasks/lists", "POST", { title: title });
        box.hidden = true; input.value = "";
        await loadBoard();
      });
    }
    return h("div", { class: "kanban-add-list" }, [
      h("button", { class: "btn ghost kanban-add-list", type: "button", text: "+ Add list",
        onclick: function () { box.hidden = false; input.focus(); } }),
      box,
    ]);
  }

  function renderArchived() {
    if (!archivedPanel) return;
    archivedPanel.hidden = false;
    archivedWrap.textContent = "";
    var archived = state.lists.filter(function (l) { return l.archived; });
    if (!archived.length) {
      archivedWrap.appendChild(h("p", { class: "kanban-empty", text: "No archived lists." }));
      return;
    }
    archived.forEach(function (list) {
      var del = h("button", { class: "btn ghost", type: "button", text: "Delete",
        disabled: list.cards.length > 0, title: list.cards.length ? "List is not empty" : "Delete permanently",
        onclick: function () { withStatus(async function () {
          await api("/api/tasks/lists/" + encodeURIComponent(list.id), "DELETE"); await loadBoard(); }); } });
      var restore = h("button", { class: "btn ghost", type: "button", text: "Restore",
        onclick: function () { withStatus(async function () {
          await api("/api/tasks/lists/" + encodeURIComponent(list.id), "PATCH", { archived: false }); await loadBoard(); }); } });
      archivedWrap.appendChild(h("div", { class: "kanban-archived-item" }, [
        h("span", { text: list.title }), restore, del]));
    });
  }

  /* ── list actions ─────────────────────────────────────────────────────── */
  function renameList(list, node) {
    var input = h("input", { type: "text", maxlength: 200, value: list.title });
    node.replaceWith(input);
    input.focus();
    input.select();
    function commit() {
      var title = input.value.trim();
      if (!title || title === list.title) { render(); return; }
      withStatus(async function () {
        await api("/api/tasks/lists/" + encodeURIComponent(list.id), "PATCH", { title: title });
        await loadBoard();
      });
    }
    input.addEventListener("keydown", function (ev) {
      if (ev.key === "Enter") { ev.preventDefault(); commit(); }
      if (ev.key === "Escape") render();
    });
    input.addEventListener("blur", commit);
  }

  function listNeighbour(list, direction) {
    var visible = state.lists.filter(function (l) { return !l.archived; });
    var idx = visible.findIndex(function (l) { return l.id === list.id; });
    return visible[idx + direction] || null;
  }

  function moveList(list, direction) {
    var neighbour = listNeighbour(list, direction);
    if (!neighbour) return;
    var body = direction < 0
      ? { list_id: list.id, before_id: neighbour.id }
      : { list_id: list.id, after_id: neighbour.id };
    withStatus(async function () {
      await api("/api/tasks/lists/reorder", "POST", body);
      await loadBoard();
    });
  }

  function archiveList(list) {
    withStatus(async function () {
      await api("/api/tasks/lists/" + encodeURIComponent(list.id), "PATCH", { archived: true });
      await loadBoard();
    });
  }

  /* ── card composer + detail dialog ────────────────────────────────────── */
  function submitCard(list, textarea, composer) {
    var title = textarea.value.trim();
    if (!title) return;
    withStatus(async function () {
      await api("/api/tasks/cards", "POST", { list_id: list.id, title: title, description: "" });
      textarea.value = ""; composer.hidden = true;
      await loadBoard();
    }, { keepStatus: true });
  }

  async function openCard(cardId) {
    var card = null;
    state.lists.some(function (l) {
      return (l.cards || []).some(function (c) { if (c.id === cardId) { card = c; return true; } return false; });
    });
    if (!card) return;
    state.card = card;
    state.replyTo = null;
    setStatus("");
    el("kd-title").value = card.title;
    el("kd-description").value = card.description || "";
    el("kd-comment-input").value = "";
    el("kd-reply-note").hidden = true;
    renderSubtasks();
    renderMoveSelect();
    try {
      var data = await api("/api/tasks/cards/" + encodeURIComponent(cardId) + "/comments");
      state.comments = (data && data.comments) || [];
    } catch (e) {
      state.comments = [];
      setStatus("Could not load comments: " + (e.message || e));
    }
    renderComments();
    if (typeof dialog.showModal === "function") dialog.showModal(); else dialog.setAttribute("open", "");
  }

  function renderMoveSelect() {
    var select = el("kd-move");
    select.textContent = "";
    state.lists.filter(function (l) { return !l.archived; }).forEach(function (l) {
      var option = h("option", { value: l.id, text: l.title });
      if (state.card && state.card.list_id === l.id) option.selected = true;
      select.appendChild(option);
    });
  }

  function renderSubtasks() {
    var wrap = el("kd-subtasks");
    wrap.textContent = "";
    var subs = (state.card && state.card.subtasks) || [];
    var doneCount = subs.filter(function (s) { return s.done; }).length;
    el("kd-sub-progress").textContent = subs.length ? doneCount + "/" + subs.length + " done" : "";
    subs.forEach(function (sub) {
      var checkbox = h("input", { type: "checkbox" });
      checkbox.checked = !!sub.done;
      checkbox.addEventListener("change", function () {
        withStatus(async function () {
          await api("/api/tasks/subtasks/" + encodeURIComponent(sub.id), "PATCH", { done: checkbox.checked });
          await refreshCard();
        });
      });
      var label = h("span", { class: sub.done ? "done" : "", text: sub.text,
        ondblclick: function () { editSubtask(sub, label); } });
      var del = h("button", { class: "btn ghost", type: "button", text: "✕", "aria-label": "Delete subtask",
        onclick: function () { withStatus(async function () {
          await api("/api/tasks/subtasks/" + encodeURIComponent(sub.id), "DELETE"); await refreshCard(); }); } });
      wrap.appendChild(h("li", {}, [h("label", {}, [checkbox, label]), del]));
    });
  }

  function editSubtask(sub, node) {
    var input = h("input", { type: "text", maxlength: 500, value: sub.text });
    node.replaceWith(input);
    input.focus();
    function commit() {
      var text = input.value.trim();
      if (!text || text === sub.text) { renderSubtasks(); return; }
      withStatus(async function () {
        await api("/api/tasks/subtasks/" + encodeURIComponent(sub.id), "PATCH", { text: text });
        await refreshCard();
      });
    }
    input.addEventListener("keydown", function (ev) {
      if (ev.key === "Enter") { ev.preventDefault(); commit(); }
      if (ev.key === "Escape") renderSubtasks();
    });
    input.addEventListener("blur", commit);
  }

  async function refreshCard() {
    await loadBoard();
    var card = null;
    state.lists.some(function (l) {
      return (l.cards || []).some(function (c) { if (c.id === state.card.id) { card = c; return true; } return false; });
    });
    if (card) {
      state.card = card;
      el("kd-meta").textContent = "";
      renderSubtasks();
    }
  }

  function renderComments() {
    var wrap = el("kd-comments");
    wrap.textContent = "";
    if (!state.comments.length) {
      wrap.appendChild(h("p", { class: "kanban-empty", text: "No comments yet." }));
      renderMeta();
      return;
    }
    var tops = state.comments.filter(function (c) { return !c.parent_id; })
      .sort(function (a, b) { return b.created_at.localeCompare(a.created_at); });
    tops.forEach(function (top) {
      wrap.appendChild(renderComment(top, false));
      state.comments.filter(function (c) { return c.parent_id === top.id; })
        .sort(function (a, b) { return a.created_at.localeCompare(b.created_at); })
        .forEach(function (reply) { wrap.appendChild(renderComment(reply, true)); });
    });
    renderMeta();
  }

  function renderComment(comment, isReply) {
    var body = h("div", { class: "kanban-comment-body", text: comment.body });
    var actions = h("div", { class: "kanban-comment-actions" }, [
      h("button", { type: "button", text: "Edit", onclick: function () { editComment(comment, body); } }),
      h("button", { type: "button", text: "Delete", onclick: function () {
        if (!confirm("Delete this comment" + (comment.parent_id ? "" : " and its replies") + "?")) return;
        withStatus(async function () {
          await api("/api/tasks/comments/" + encodeURIComponent(comment.id), "DELETE");
          await reloadComments();
        });
      } }),
    ]);
    if (!isReply) {
      actions.appendChild(h("button", { type: "button", text: "Reply", onclick: function () { startReply(comment); } }));
    }
    return h("div", { class: "kanban-comment" + (isReply ? " reply" : "") }, [
      h("div", { class: "kanban-comment-head" }, [
        h("span", { class: "kanban-comment-author", text: comment.author }),
        h("span", { text: fmt(comment.created_at) + (comment.updated_at !== comment.created_at ? " · edited" : "") }),
        h("span", { text: rel(comment.created_at) }),
      ]),
      body, actions,
    ]);
  }

  function editComment(comment, bodyNode) {
    var textarea = h("textarea", { rows: 2, maxlength: 20000, value: comment.body });
    bodyNode.replaceWith(textarea);
    textarea.focus();
    var save = h("button", { class: "btn primary", type: "button", text: "Save", onclick: commit });
    var cancel = h("button", { class: "btn ghost", type: "button", text: "Cancel", onclick: renderComments });
    textarea.after(h("div", { class: "toolbar" }, [save, cancel]));
    function commit() {
      var value = textarea.value.trim();
      if (!value) return;
      withStatus(async function () {
        await api("/api/tasks/comments/" + encodeURIComponent(comment.id), "PATCH", { body: value });
        await reloadComments();
      });
    }
  }

  function startReply(comment) {
    state.replyTo = comment.id;
    var note = el("kd-reply-note");
    note.hidden = false;
    note.textContent = "Replying to " + comment.author + " …";
    el("kd-reply-cancel").hidden = false;
    el("kd-comment-input").focus();
  }

  function cancelReply() {
    state.replyTo = null;
    el("kd-reply-note").hidden = true;
    el("kd-reply-cancel").hidden = true;
  }

  async function reloadComments() {
    var data = await api("/api/tasks/cards/" + encodeURIComponent(state.card.id) + "/comments");
    state.comments = (data && data.comments) || [];
    renderComments();
    await loadBoard();
    var card = null;
    state.lists.some(function (l) {
      return (l.cards || []).some(function (c) { if (c.id === state.card.id) { card = c; return true; } return false; });
    });
    if (card) state.card = card;
  }

  function renderMeta() {
    if (!state.card) return;
    el("kd-meta").textContent = "Created by " + state.card.author + " · " + fmt(state.card.created_at)
      + " (updated " + rel(state.card.updated_at) + ")";
  }

  function postComment() {
    var input = el("kd-comment-input");
    var body = input.value.trim();
    if (!body || !state.card) return;
    var payload = { body: body };
    if (state.replyTo) payload.parent_id = state.replyTo;
    withStatus(async function () {
      await api("/api/tasks/cards/" + encodeURIComponent(state.card.id) + "/comments", "POST", payload);
      input.value = "";
      cancelReply();
      await reloadComments();
    });
  }

  /* ── drag and drop (SortableJS) ───────────────────────────────────────── */
  function initSortables() {
    if (typeof window.Sortable === "undefined") return;
    boardEl.querySelectorAll(".kanban-cards").forEach(function (container) {
      window.Sortable.create(container, {
        group: "kanban-cards",
        animation: 120,
        ghostClass: "kanban-sortable-ghost",
        draggable: ".kanban-card",
        onEnd: onCardDrop,
      });
    });
    window.Sortable.create(boardEl, {
      animation: 120,
      draggable: ".kanban-list",
      handle: ".kanban-list-head",
      ghostClass: "kanban-sortable-ghost",
      onEnd: onListDrop,
    });
  }

  function onCardDrop(evt) {
    var cardId = evt.item.dataset.id;
    var listId = evt.to.dataset.list || (evt.to.closest(".kanban-list") || {}).dataset?.list;
    if (!cardId || !listId) { loadBoard(); return; }
    var prev = evt.item.previousElementSibling;
    var next = evt.item.nextElementSibling;
    var body = { list_id: listId };
    if (prev && prev.dataset.id) body.after_id = prev.dataset.id;
    else if (next && next.dataset.id) body.before_id = next.dataset.id;
    withStatus(async function () {
      await api("/api/tasks/cards/" + encodeURIComponent(cardId) + "/move", "POST", body);
      await loadBoard();
    });
  }

  function onListDrop(evt) {
    var listId = evt.item.dataset.list;
    if (!listId) { loadBoard(); return; }
    var prev = evt.item.previousElementSibling;
    var next = evt.item.nextElementSibling;
    var body = { list_id: listId };
    if (prev && prev.dataset.list) body.after_id = prev.dataset.list;
    else if (next && next.dataset.list) body.before_id = next.dataset.list;
    if (!body.after_id && !body.before_id) return;
    withStatus(async function () {
      await api("/api/tasks/lists/reorder", "POST", body);
      await loadBoard();
    });
  }

  /* ── helpers ──────────────────────────────────────────────────────────── */
  async function withStatus(op) {
    try { setStatus(""); await op(); }
    catch (e) { setStatus(String((e && e.message) || e)); try { await loadBoard(); } catch (_) { /* ignore */ } }
  }

  function toggleMenu(box) {
    var open = !box.hidden;
    hideMenus();
    box.hidden = open;
  }
  function hideMenus() {
    document.querySelectorAll(".kanban-list-menu .menu").forEach(function (m) { m.hidden = true; });
  }
  document.addEventListener("click", function (ev) {
    if (!ev.target.closest || !ev.target.closest(".kanban-list-menu")) hideMenus();
  });

  /* ── wiring ───────────────────────────────────────────────────────────── */
  el("kd-close").addEventListener("click", function () { dialog.close(); });
  dialog.addEventListener("close", function () { state.card = null; cancelReply(); });
  el("kd-subtask-add").addEventListener("click", function () {
    var input = el("kd-subtask-input");
    var text = input.value.trim();
    if (!text || !state.card) return;
    withStatus(async function () {
      await api("/api/tasks/cards/" + encodeURIComponent(state.card.id) + "/subtasks", "POST", { text: text });
      input.value = "";
      await refreshCard();
    });
  });
  el("kd-subtask-input").addEventListener("keydown", function (ev) {
    if (ev.key === "Enter") { ev.preventDefault(); el("kd-subtask-add").click(); }
  });
  el("kd-comment-add").addEventListener("click", postComment);
  el("kd-reply-cancel").addEventListener("click", cancelReply);
  el("kd-comment-input").addEventListener("keydown", function (ev) {
    if (ev.key === "Enter" && (ev.metaKey || ev.ctrlKey)) { ev.preventDefault(); postComment(); }
  });
  el("kd-title").addEventListener("blur", function () {
    if (!state.card) return;
    var title = el("kd-title").value.trim();
    if (!title || title === state.card.title) return;
    withStatus(async function () {
      await api("/api/tasks/cards/" + encodeURIComponent(state.card.id), "PATCH", { title: title });
      await loadBoard();
    });
  });
  el("kd-description").addEventListener("blur", function () {
    if (!state.card) return;
    var description = el("kd-description").value;
    if (description === (state.card.description || "")) return;
    withStatus(async function () {
      await api("/api/tasks/cards/" + encodeURIComponent(state.card.id), "PATCH", { description: description });
      await loadBoard();
    });
  });
  el("kd-move").addEventListener("change", function () {
    if (!state.card) return;
    var listId = el("kd-move").value;
    if (!listId || listId === state.card.list_id) return;
    withStatus(async function () {
      await api("/api/tasks/cards/" + encodeURIComponent(state.card.id) + "/move", "POST", { list_id: listId });
      await loadBoard();
      dialog.close();
    });
  });
  el("kd-archive").addEventListener("click", function () {
    if (!state.card) return;
    withStatus(async function () {
      await api("/api/tasks/cards/" + encodeURIComponent(state.card.id), "PATCH", { archived: true });
      dialog.close();
      await loadBoard();
    });
  });
  el("kd-delete").addEventListener("click", function () {
    if (!state.card || !confirm("Delete this card? Its subtasks and comments are removed.")) return;
    withStatus(async function () {
      await api("/api/tasks/cards/" + encodeURIComponent(state.card.id), "DELETE");
      dialog.close();
      await loadBoard();
    });
  });
  if (toggleArchivedBtn) {
    toggleArchivedBtn.addEventListener("click", function () {
      state.includeArchived = !state.includeArchived;
      withStatus(loadBoard);
    });
  }
  if (exportBtn) {
    exportBtn.addEventListener("click", function () {
      window.open("/api/tasks/export", "_blank");
    });
  }

  withStatus(loadBoard);
})();
