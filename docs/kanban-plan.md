# Tasks tab (Trello-style Kanban) — implementation plan

Status: implemented (P01–P05; P05-01 seed board deliberately not adopted — the board
starts empty per §3.1). Adds a **Tasks** tab to `python main.py --web`, sitting beside the
existing **Documentation** tab, that behaves like a small Trello board: addable and
renameable lists (columns), cards with created/updated stamps, a description, subtask
checklists, and **comments** (flat, editable, with one level of replies), and
drag-and-drop of cards between lists.

Persistence is **PostgreSQL**, mirroring the pattern-editor stack
(`core/pattern_editor_db.py` + `migrations/` + `scripts/setup_editor_db.py`) under its own
`tasks` schema on the existing server. There is no SQLite store.

Login is a small set of **named users**: the primary `WEB_UI_USERNAME` / `WEB_UI_PASSWORD`
pair plus any extra logins in `WEB_UI_USERS`. **`admin` and `ed`** can both sign in
(`TBY`), and cards and comments record the **logged-in username** as their `author`.

This document is the source of truth for the work. It records the codebase map, the
locked UX/data decisions, a proposed HTTP surface, and a phased checklist with exit
gates. Proposed filenames and commands are implementation targets, not existing
capabilities.

---

## 1. Goal and scope

**In scope**

- A new authenticated page `GET /tasks` rendered from a new template, reachable from a
  new **Tasks** nav link placed immediately after **Documentation**.
- Lists (columns): create, rename, reorder, archive/delete.
- Cards: create in a list, edit title + description, show created-at / updated-at,
  move between lists, reorder within a list, archive/delete.
- Subtasks: add/rename/delete, toggle done, per-card checklist with a progress count.
- **Comments:** add, edit, delete; one level of **replies**; author + timestamp; a
  comment-count chip on the card face.
- **Authorship:** cards and comments are stamped with `author` = the logged-in username
  (`web/auth.py::require_login`).
- Drag and drop of cards across lists (and lists themselves), with persistence.
- A durable PostgreSQL store so the board survives restarts.

**Out of scope (v1)**

- Multi-user boards, sharing, or per-user permissions. Multiple **named logins** are
  supported (see §2), but the board is shared: the `author` field is populated from the
  logged-in user and is a record only — it is not per-user enforcement.
- Attachments, labels, due-date reminders, notifications, activity feed.
- Real-time collaboration (websockets). Single writer / last-write-wins is acceptable.
- Cross-tab live sync; a background poll is optional (see §8, P2).

**Non-goals that constrain design**

- No new heavyweight runtime dependency. The board is served by the existing FastAPI
  app and **PostgreSQL only**, mirroring the pattern-editor stack
  (`core/pattern_editor_db.py`, `migrations/`, `scripts/setup_*_db.py`) under its own
  `tasks` schema on the existing server. `psycopg[binary]` is already a dependency
  (`requirements.txt`).
- The tab must not break other tabs when PostgreSQL is unavailable or unmigrated: it
  degrades to a readable error (naming `scripts/setup_tasks_db.py`) like the Docs/System
  tabs do, and never blocks login or other tabs.

---

## 2. Codebase map (what this plugs into)

- **Entry point.** `main.py --web` → `web/app.py::run()` → `uvicorn` factory
  `web.app:create_app`. No `main.py` change is required; the tab is added inside
  `create_app`.
- **App + nav.** `web/app.py::create_app()` defines `ctx()` / `render()` helpers and all
  page routes. The nav lives in `web/templates/base.html` (`active == ...` classes), and
  `ctx()` injects shared flags (e.g. `hermes_enabled`). The optional Laya tab is the
  template to copy for a self-contained route module:
  `from web.laya import register_laya_routes; _register_laya(app, templates, ctx)`
  (`web/app.py:1262`, `web/laya.py:142`).
- **Static assets.** `web/static/app.css` (dark theme CSS variables at the top),
  `web/static/app.js` (shared helpers). Per-tab scripts live in `web/static/*.js`
  (e.g. `system.js`, `laya.js`, `docs.js`) and are loaded via a `{% block scripts %}`
  tag with a `?v=` cache-buster.
- **Login / identity.** `web/auth.py` accepts a set of **named users**:
  `verify_credentials` compares against the primary pair (`config.py:205`
  `web_ui_username` default `admin`; `web_ui_password`) **and** any extra login in
  `web_ui_users` (`WEB_UI_USERNAME` / `WEB_UI_PASSWORD` / `WEB_UI_USERS` in `.env`), so
  `admin` and `ed` can both sign in. `require_login` returns the matched username, and
  `web/app.py:243-249::ctx()` injects it as `user` into every template (`base.html` shows
  `{{ user }}`). This is the source of every `author` value. The primary pair also feeds
  the derived credentials (`config.py::stocks_history_auth` / `pattern_api_auth`, and the
  MCP defaults).
- **PostgreSQL store precedent.** `core/pattern_editor_db.py` — psycopg3 with
  `dict_row`, **one connection per operation**, `SET LOCAL search_path TO {schema},
  pg_catalog` with bounded `statement_timeout`/`lock_timeout`, `schema_name()`
  validation, error mapping (`DatabaseUnavailable` / `MigrationRequired` / `Conflict`),
  and `migrate()` that takes a `pg_advisory_xact_lock`, creates `schema_migrations`, and
  verifies SHA-256 checksums. Migrations live in `migrations/pattern_editor/*.sql`
  (contiguous from `0001`; the runner owns `schema_migrations`). `scripts/setup_editor_db.py`
  is the idempotent "ensure database + migrate" bootstrap. Copy this shape for tasks.
- **Store accessor precedent.** `core/pattern_edit_store.py::open_pattern_store()` — a
  module-level singleton accessor for the store. Mirror it with `open_task_store()`.
- **Settings.** `config.py` uses a settings object with per-feature blocks and URLs;
  `pattern_editor_database_url` (empty ⇒ reuse `data/db.py::build_dsn()`) and
  `pattern_editor_schema` are the model. `.env.example` documents each.
- **Page/API test precedent.** `web/test_docs_api.py`, `web/test_system_api.py`,
  `web/test_laya_api.py` use `fastapi.testclient.TestClient` + `unittest`, hitting the
  page for the nav link and the `/api/*` routes for behavior (these need no PostgreSQL).
  `conftest.py::editor_store_factory` is the model for a PostgreSQL-backed fixture: read
  an explicit `*_TEST_DATABASE_URL` (skip when unset), `migrate()` a disposable
  `<prefix>_test_<uuid>` schema, and drop only that schema on teardown.
- **Vendored JS precedent.** `web/static/vendor/lightweight-charts.standalone.production.js`
  is committed, so a small vendored drag-and-drop library is consistent with the repo
  (see §6.3).

---

## 3. UX specification

### 3.1 Lists (columns)

- The board starts empty on a fresh install with a single prompt: “Add your first list”.
- **Add list:** an “+ Add list” affordance at the right edge of the board; opens an
  inline input (Enter commits, Esc cancels). New list is appended as the last column.
- **Rename:** click the list title → it becomes an editable input in place (Enter
  commits, Esc cancels, blur commits). Renaming never touches cards.
- **Reorder:** drag the list header horizontally to reorder columns.
- **Delete/archive:** a per-list “…” menu with **Archive list**. Archived lists are
  hidden, not destroyed; an “Archived lists” drawer offers **Restore**. A list can only
  be hard-deleted from the archive drawer, and only when it is empty.
- Empty list shows a “No cards yet” placeholder that is a valid drop target.

### 3.2 Cards

Each card stores `title`, `description`, `author` (creator), `created_at`, `updated_at`,
an ordered subtask list, and a comment thread. On the board a card shows: title, a subtask
progress chip (`2/5`) when it has subtasks, a comment-count chip (`n`) when it has
comments, an attribution initial (creator, hover/label detail), an “edited” hint when
`updated_at > created_at`, and, on hover, quick actions (edit, archive).

- **Create:** a “+ Add a card” composer at the bottom of each list (textarea → Enter
  creates and keeps the composer open for rapid entry; Esc closes). New card appended to
  that list.
- **Open detail:** clicking a card opens a detail panel/modal with:
  - editable **title** (click to edit),
  - editable **description** (multi-line, plain text; Markdown is out of scope for v1),
  - **Created** (“Created by <author> · <created_at>”) and **Updated** timestamps
    (rendered in local time, from stored UTC),
  - **Author** shown as the creator; it is set at create and is immutable (editing a
    card does not change it),
  - **Subtasks** checklist: checkbox per subtask, add-subtask input, rename, delete,
  - **Comments** (see §3.4),
  - **Move to list** select (keyboard/accessible alternative to drag),
  - **Archive card** / **Delete card**.
- **Timestamps:** `created_at` set on insert; `updated_at` bumped on any card field
  change. Toggling a subtask and adding/editing/deleting a comment bump the parent
  card’s `updated_at` (they are visible changes to the card). Displayed as
  `YYYY-MM-DD HH:MM` with a relative hint (“2h ago”) in the detail view.

### 3.3 Subtasks

- Belong to exactly one card and are ordered (`position`).
- Fields: `text`, `done` (bool), `created_at`.
- Toggling is instant (optimistic UI); the progress chip on the card updates.
- "Add subtask" at the end of the checklist; Enter adds and keeps focus for the next.
- A card with all subtasks done shows a distinct done state on the board chip.

### 3.4 Comments

- Belong to exactly one card; ordered by `created_at` (newest-first at the top level).
- Fields: `author`, `body`, `parent_id` (null for a top-level comment), `created_at`,
  `updated_at`.
- **Top-level comments** show author, `YYYY-MM-DD HH:MM` + relative hint, body (plain
  text; Markdown out of scope), and **Edit** / **Delete** / **Reply** actions.
- **Replies** nest exactly one level under their parent, ordered oldest-first and
  visually indented. A reply has no **Reply** action (no third level).
- A composer (textarea + **Comment**) sits at the bottom; `Cmd/Ctrl+Enter` submits.
  Choosing **Reply** prefills “replying to …” and targets that comment as `parent_id`.
- **Delete** on a top-level comment warns that its replies are removed too.
- The card face shows a comment-count chip (all comments, replies included); it is
  hidden at zero.
- Author is the logged-in username (`web/auth.py::require_login`) — the **same identity**
  used for card authorship. Each comment and reply carries its own `author` (a reply has
  no separately inherited author). The shared login makes this effectively constant
  today, but the column future-proofs MCP/agent-authored comments.

### 3.5 Drag and drop

- Drag a **card** onto another list (moves it, appended or dropped at the hovered index)
  or within its own list (reorders).
- Drag a **list** by its header to reorder columns.
- Drop targets: the cards container and the “+ Add a card” footer of every list.
- Optimistic UI: the card moves immediately; the server call persists `list_id` +
  `position`. On failure the card snaps back and a toast/`status` line explains.
- Touch support is required (operator uses a phone per `system_stats.py`), so pick a
  library that handles touch/pointer events, not bare HTML5 DnD.

### 3.6 Accessibility and mobile

- Every drag operation has a non-drag equivalent: card detail has **Move to list**, and
  list reorder has “move left/right” in the list menu.
- Checkboxes are real `<input type="checkbox">` with labels; the board is usable with
  keyboard focus and a screen reader at a basic level.
- Columns are horizontally scrollable on narrow screens; the board uses
  `overflow-x: auto` rather than squeezing columns.

---

## 4. Data model and persistence

### 4.1 Store modules (`core/task_db.py`, `core/task_store.py`)

Mirror the pattern-editor PostgreSQL stack:

- **`core/task_db.py`** — connection and migration support, mirroring
  `core/pattern_editor_db.py`: task-local `TaskError` / `TaskConflict` /
  `TaskDatabaseUnavailable` / `TaskMigrationRequired`, `MIGRATIONS =
  .../migrations/tasks`, `schema_name()` (accepts `tasks` or `tasks_*`, ≤63 chars),
  `configured_dsn()` (⇒ `settings.task_database_url or data.db.build_dsn()`), a
  `connection(dsn, schema="tasks")` context manager (new connection per operation,
  `dict_row`, same bounded `statement_timeout`/`lock_timeout`, `SET LOCAL search_path TO
  {schema}, pg_catalog`), and `migrate(dsn, schema)` (advisory lock + checksum-verified
  `schema_migrations`, contiguous migrations from `0001`).
- **`core/task_store.py`** — `TaskStore(dsn=None, schema="tasks")` with a `connect()`
  context manager and small, explicit methods for lists, cards, subtasks, and comments
  (create/rename/move/archive/delete, reorder, cascade delete). Validation lives here
  (trim, length caps, unknown id → not found). `open_task_store()` is the module
  singleton accessor. The web layer owns one store; the store performs no network or
  business logic beyond validation and persistence.

### 4.2 Schema (`migrations/tasks/0001_tasks.sql`)

The migration runner creates and owns `schema_migrations`; the file contains only DDL,
in the style of `migrations/pattern_editor/0001_registry.sql` (`clock_timestamp()`
defaults, `CHECK` constraints, `plpgsql` triggers):

```sql
CREATE TABLE task_lists (
    id         TEXT PRIMARY KEY,           -- uuid4 hex
    title      TEXT NOT NULL CHECK (length(btrim(title)) BETWEEN 1 AND 200),
    position   DOUBLE PRECISION NOT NULL,  -- fractional rank, ascending
    archived   BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE task_cards (
    id          TEXT PRIMARY KEY,          -- uuid4 hex
    list_id     TEXT NOT NULL REFERENCES task_lists(id) ON DELETE CASCADE,
    title       TEXT NOT NULL CHECK (length(btrim(title)) BETWEEN 1 AND 200),
    description TEXT NOT NULL DEFAULT '' CHECK (length(description) <= 20000),
    author      TEXT NOT NULL,             -- logged-in username at create time
    position    DOUBLE PRECISION NOT NULL, -- fractional rank within list
    archived    BOOLEAN NOT NULL DEFAULT FALSE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX idx_task_cards_list ON task_cards(list_id, position);

CREATE TABLE task_subtasks (
    id         TEXT PRIMARY KEY,
    card_id    TEXT NOT NULL REFERENCES task_cards(id) ON DELETE CASCADE,
    text       TEXT NOT NULL CHECK (length(btrim(text)) BETWEEN 1 AND 500),
    done       BOOLEAN NOT NULL DEFAULT FALSE,
    position   DOUBLE PRECISION NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX idx_task_subtasks_card ON task_subtasks(card_id, position);

CREATE TABLE task_comments (
    id         TEXT PRIMARY KEY,
    card_id    TEXT NOT NULL REFERENCES task_cards(id) ON DELETE CASCADE,
    parent_id  TEXT REFERENCES task_comments(id) ON DELETE CASCADE,
    author     TEXT NOT NULL,              -- logged-in username at create time
    body       TEXT NOT NULL CHECK (length(btrim(body)) BETWEEN 1 AND 20000),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    CHECK (parent_id IS NULL OR parent_id <> id)
);
CREATE INDEX idx_task_comments_card   ON task_comments(card_id, created_at);
CREATE INDEX idx_task_comments_parent ON task_comments(parent_id);

-- Replies are one level deep and must share their parent's card.
CREATE FUNCTION task_comment_reply_depth() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.parent_id IS NOT NULL THEN
        IF EXISTS (SELECT 1 FROM task_comments
                   WHERE id = NEW.parent_id AND parent_id IS NOT NULL) THEN
            RAISE EXCEPTION 'replies may only be one level deep' USING ERRCODE='23514';
        END IF;
        IF (SELECT card_id FROM task_comments WHERE id = NEW.parent_id) <> NEW.card_id THEN
            RAISE EXCEPTION 'reply parent belongs to another card' USING ERRCODE='23514';
        END IF;
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER task_comment_reply_depth BEFORE INSERT OR UPDATE ON task_comments
    FOR EACH ROW EXECUTE FUNCTION task_comment_reply_depth();
```

Foreign keys cascade: deleting a list cascades its cards, a card cascades its subtasks
and comments, and a top-level comment cascades its replies. `author` is stored on both
`task_cards` and `task_comments` and is never accepted from the client (see §4.4).

### 4.3 Ordering strategy

- **Fractional ranks (`DOUBLE PRECISION`)**: a card dropped between `a` and `b` gets
  `(a + b) / 2`; dropped at the end gets `max(position) + 1024`. This makes a move a
  single `UPDATE`, no renumbering.
- Guard against float exhaustion: if the gap to a neighbour is below a threshold
  (e.g. `1e-6`), renumber the whole list/board in one transaction (`position = i * 1024`).
  This is the one case that revisits siblings, and it stays O(n) for a single board.
- Comments are ordered by `created_at`, not a rank, so they never need renumbering.

### 4.4 Concurrency, validation, robustness

- **Single writer assumption** (one web process). Each operation opens its own
  connection (editor style); writes run inside a transaction. PostgreSQL handles
  concurrent readers/writers. No optimistic-concurrency tokens in v1.
- Server-side validation: non-empty, trimmed `title` (max ~200 chars), `description`
  (max ~20k chars), `text` (max ~500 chars), comment `body` (max ~20k chars). Reject
  unknown ids with 404, bad bodies with 422/400, mirroring the Laya error shape
  (`web/laya.py::_err`, `{error_code, detail}`). A reply whose parent is itself a reply,
  or belongs to another card, is rejected (422/409) — also enforced by the trigger.
- `author` is **always** taken from the authenticated session (`require_login`), never
  from the request body; it is written at insert and never updated.
- `created_at` / `updated_at` are server-generated (`clock_timestamp()`), never trusted
  from the client.
- Failure mapping: `TaskDatabaseUnavailable` → 503, `TaskMigrationRequired` → 503
  (“run `scripts/setup_tasks_db.py`”), `TaskConflict` → 409. The other tabs keep working
  (same philosophy as `web/system_stats.py`, which degrades when `psutil` is missing).

### 4.5 Config and example file

Add to `config.py` under a new “Web UI — Tasks (Kanban) tab” block:

```python
# ── Web UI · Tasks (Kanban) tab ─────────────────────────────────────────
# Empty reuses the existing PostgreSQL DATABASE_URL / db_* settings.
task_database_url: str = ""
# Tasks schema. Production uses `tasks`; tests point this at tasks_test_<uuid>.
task_schema: str = "tasks"
# Show the Tasks tab (a clone that never wants a board can hide it).
tasks_enabled: bool = True
```

Document all three in `.env.example` beside the `PATTERN_EDITOR_*` block.

Add `scripts/setup_tasks_db.py` mirroring `scripts/setup_editor_db.py`: when
`task_database_url` names a dedicated database, ensure it exists, then apply pending
migrations with `migrate()`. There is **no catalog seeding**. The script is idempotent
and safe to run on every deploy.

---

## 5. HTTP API surface

All routes require login (`Depends(require_login)`) and are registered by a new
`web/tasks.py::register_task_routes(app, templates, ctx)` called from `create_app`
next to `_register_laya`. JSON errors use `{error_code, detail}`.

| Method | Path | Purpose |
| --- | --- | --- |
| GET  | `/tasks` | Render the board page. |
| GET  | `/api/tasks/board` | One payload: all lists (archived flag included) with nested cards (each with `author` + `comment_count`) + subtasks, ordered. |
| POST | `/api/tasks/lists` | Create a list `{title}`. |
| PATCH| `/api/tasks/lists/{list_id}` | Rename `{title}` and/or `{archived}`. |
| POST | `/api/tasks/lists/reorder` | `{list_id, before_id \| after_id}` → set fractional position. |
| DELETE | `/api/tasks/lists/{list_id}` | Hard-delete (only when empty). |
| POST | `/api/tasks/cards` | Create `{list_id, title, description?}`; `author` = session username. |
| PATCH| `/api/tasks/cards/{card_id}` | Edit `{title?, description?, archived?}`; bumps `updated_at`; leaves `author` unchanged. |
| POST | `/api/tasks/cards/{card_id}/move` | `{list_id, before_id \| after_id}` → move + reposition in one write. |
| DELETE | `/api/tasks/cards/{card_id}` | Delete card (cascades subtasks + comments). |
| POST | `/api/tasks/cards/{card_id}/subtasks` | Add subtask `{text}`. |
| PATCH| `/api/tasks/subtasks/{subtask_id}` | Edit `{text?, done?}`; bumps parent card `updated_at`. |
| DELETE | `/api/tasks/subtasks/{subtask_id}` | Delete subtask. |
| POST | `/api/tasks/subtasks/reorder` | Optional in v1 (see §8); reorder within a card. |
| GET  | `/api/tasks/cards/{card_id}/comments` | Flat list for a card (fields incl. `parent_id`); the client groups replies. |
| POST | `/api/tasks/cards/{card_id}/comments` | Add `{body, parent_id?}`; `author` = session username. |
| PATCH| `/api/tasks/comments/{comment_id}` | Edit `{body}`; bumps comment + parent card `updated_at`. |
| DELETE | `/api/tasks/comments/{comment_id}` | Delete (cascades replies). |

Notes

- `GET /api/tasks/board` is the single board read; the client re-renders from it after a
  mutation response (each mutation returns the affected rows) to avoid stale UI. A full
  reload is cheap for realistic board sizes. It carries per-card `author` and
  `comment_count` (a cheap count) so the attribution and chip render without loading
  comment bodies; bodies load on demand from `/api/tasks/cards/{id}/comments` when the
  detail panel opens.
- `author` on a card or comment is set from the session (`Depends(require_login)`), never
  from the request body; `PATCH /api/tasks/cards/{card_id}` does not change a card’s
  `author`.
- Keep the route module self-contained (like `web/laya.py`) so a board problem cannot
  regress other tabs.

---

## 6. Frontend implementation

### 6.1 Template `web/templates/tasks.html`

- `{% extends "base.html" %}`, `{% block title %}Tasks — Trading Bot{% endblock %}`,
  a `page-head` with the board title and an “Archived lists” toggle, the board
  container `#board`, and a card-detail `<dialog>` / hidden panel for accessibility
  (including the Comments section markup).
- `{% block scripts %}<script src="/static/tasks.js?v=1"></script>{% endblock %}`.

### 6.2 `web/static/tasks.js`

- Loads `/api/tasks/board`, renders columns and cards, and owns all mutations.
- Small `api(path, method, body)` helper that surfaces `{error_code, detail}` to the
  status line and reverts optimistic moves on failure.
- Rendering is plain DOM (matching `system.js`/`laya.js` style), no framework.
- Detail modal handles title/description editing, checklist, timestamps, move-to-list,
  the “Created by <author>” line, and the **Comments** section: comment-count chip on the
  card face; comments listed newest-first with one level of indented replies; **Edit** /
  **Delete** / **Reply**, a composer (textarea + **Comment**, `Cmd/Ctrl+Enter` submits),
  and the “replying to …” state. Optimistic where easy; revert on error like the DnD path.

### 6.3 Drag and drop

**Decision (implemented):** vendored **SortableJS 1.15.6** (MIT) at
`web/static/vendor/sortable.min.js`, loaded only on the Tasks tab. Chosen over the native
Pointer Events alternative for reliable touch/pointer drag. Every drag also has a
click/keyboard equivalent (**Move to list**, list **Move left/right**). Cards use one
Sortable with `group: "kanban-cards"`; lists use a second Sortable bound to `#kanban-board`
with `handle: ".kanban-list-head"`.

- **Recommended:** vendor **SortableJS** (`web/static/vendor/sortable.min.js`, ~45 KB,
  MIT) and use `group: "cards"` for cross-list moves plus a separate sortable for
  columns. It handles touch/pointer, animation, and clone-while-dragging, and lets us
  persist on the `onEnd` event with `(from list, to list, index)`.
  Rationale: the repo already vendors `lightweight-charts`, so this is consistent and
  avoids hand-rolling touch drag. It is loaded only on the Tasks tab.
- **Alternative (no new asset):** native Pointer Events drag with a manually drawn ghost.
  More code, weaker touch behavior, no added dependency. Choose this only if vendoring
  an asset is unacceptable.
- On drop: optimistically move the node, then call `/api/tasks/cards/{id}/move` (or the
  list reorder route) and, on error, move the node back and reload the board.

### 6.4 CSS (`web/static/app.css`)

Add a small, prefixed block (`.kanban-*`, `.kcard-*`, `.klist-*`, `.kanban-comment*`)
reusing the existing theme variables (`--panel`, `--border`, `--accent`, `--muted`,
`--ok`). Because `app.css` is shared, bump `?v=` in `base.html`
(`/static/app.css?v=25` → `v=26`) so clients pick it up. Keep the board horizontally
scrollable and columns a fixed width.

---

## 7. Navigation and app wiring

1. `web/app.py`: `from web.tasks import register_task_routes`; inside `create_app`,
   after `_register_laya(...)`, call `register_task_routes(app, templates, ctx)` — only
   when `settings.tasks_enabled`.
2. `web/templates/base.html`: add
   `<a href="/tasks" class="{% if active == 'tasks' %}active{% endif %}">Tasks</a>`
   immediately after the Documentation link (last position), wrapped in
   `{% if tasks_enabled %}` so a clone can hide it. Expose `tasks_enabled` through
   `ctx()` the same way `hermes_enabled` is (`web/app.py:242`).
3. Bump asset cache-busters touched: `app.css?v=26`, new `tasks.js?v=1`; vendored
   `sortable.min.js` referenced with a version if desired.
4. Degradation: when PostgreSQL is unavailable or unmigrated, `/tasks` renders a
   readable error naming `scripts/setup_tasks_db.py`, and every other tab keeps serving.
5. Update `README.md` (tab list plus a PostgreSQL/setup note: `scripts/setup_tasks_db.py`,
   `TASKS_DATABASE_URL` / `TASKS_SCHEMA`). The Documentation tab renders `README.md`, so
   the tab list there should mention Tasks.
6. Login is configured by `WEB_UI_USERNAME` / `WEB_UI_PASSWORD` (primary) and
   `WEB_UI_USERS` (extra named logins) in `.env`; the operator runs with `admin` and `ed`
   both able to sign in (`TBY`), so no code change is needed beyond the setting. Keep the
   real passwords in `.env`; this document records the users for reference only, and the
   password should be rotated if the repo is ever shared.

---

## 8. Suggested additions (beyond the stated requirements)

Ordered by value; each is independently shippable. P1 = in v1 scope, P2 = near-term
follow-up, P3 = optional/later.

**P1 — cheap, high-value**

- **Card labels/tags** with colors, filterable on the board. Small extra table or a
  `tag_ids` join; enables “show only bugs” style views.
- **Due dates** on cards with an overdue highlight. Timestamps already exist; this is one
  nullable column plus UI.
- **Keyboard-first flow:** `n` = new card in focused list, `/` = search, `Enter` = open
  selected card. Pairs well with the operator’s desktop use.
- **Board search / filter box** that filters cards by title, description, subtask, and
  comment text client-side.
- **Subtask reordering** within a card (same fractional-rank approach).
- **Subtasks as “Done” rollup:** when all subtasks complete, offer to move the card to a
  chosen “Done” list (Trello’s “card is complete” behavior), opt-in per board.

**P2 — operational fit with this repo**

- **Deep links to the rest of the app.** Cards can optionally hold a typed reference
  (`link`): symbol (`AAPL`), a pattern/version, or a backtest run id — clicking opens
  the Explorer, Patterns, or Backtest tab pre-filtered. This is where a project task
  board earns its place inside a trading bot rather than living in a separate app.
- **A default seed board** created lazily on first open (e.g. `Backlog / Doing /
  Done`), so the tab is not empty on day one. Seeding is idempotent.
- **Activity timestamps everywhere** and a compact per-card “last updated” sort.
- **Per-list card count** and board totals in the header.
- **JSON export / import** of the whole board (`GET /api/tasks/export`, comments and
  authors included) for backup and for feeding a task list into an AI agent — consistent
  with the MCP surface described in `docs/mcp-plan.md`.
- **MCP tools** (`tasks_board`, `tasks_add_card`, `tasks_move_card`,
  `tasks_add_comment`) in `mcp_server/`, so an agent can read and comment on the board
  through the same authenticated API. Fits the existing “one owner of state, thin typed
  wrapper” decision in `docs/mcp-plan.md`.

**P3 — optional**

- **Card attachments** (chart PNGs, logs) stored on disk under `data/cache/tasks/`.
- **Multiple boards** (a `board_id` column; board switcher in the header).
- **Named logins / members:** multiple named logins already exist (`WEB_UI_USERS`), so
  the `author` columns give per-user attribution today; a real permission model (per-user
  boards/roles) is the remaining step.
- **Lightweight live refresh** (poll `/api/tasks/board` every N seconds when the tab is
  visible, or an SSE endpoint) for two viewers.
- **WIP limits** per list and a simple cycle-time metric (created → Done) using the
  existing timestamps.

---

## 9. Phased plan

Resume protocol: start at the first unchecked task whose dependencies are done; check a
task only when its deliverable and verification are complete. Status values: `not
started`, `in progress`, `blocked`, `complete`.

### P01 — Store, migrations, and contracts (PostgreSQL) · Depends on: none

**Status:** complete · **Progress:** 5/5

- [x] **P01-01:** Define the task/list/card/subtask/comment typed shapes and the
  fractional-rank ordering rules; document the “renumber when a gap collapses” threshold.
- [x] **P01-02:** Add `migrations/tasks/0001_tasks.sql` — `task_lists`, `task_cards`
  (incl. `author`), `task_subtasks`, `task_comments` (incl. `author`), indexes, and the
  one-level reply trigger (in the style of `migrations/pattern_editor/0001_registry.sql`).
- [x] **P01-03:** Implement `core/task_db.py` + `core/task_store.py` (connection,
  migration runner, `TaskStore` CRUD, reorder, cascade delete, comments) mirroring
  `core/pattern_editor_db.py` and `core/pattern_edit_store.py::open_pattern_store`.
- [x] **P01-04:** Add `task_database_url` + `task_schema` + `tasks_enabled` to
  `config.py` and `.env.example`; add `scripts/setup_tasks_db.py`.
- [x] **P01-05:** Add the `conftest.py::task_store_factory` fixture (disposable
  `tasks_test_<uuid>` schema from `TASKS_TEST_DATABASE_URL`) and `core/test_task_store.py`.

**Exit gate:** Store tests pass against a disposable PostgreSQL schema and a fresh
connection serves the same board; no web code yet.

### P02 — HTTP API · Depends on: P01

**Status:** complete · **Progress:** 3/3

- [x] **P02-01:** Implement `web/tasks.py::register_task_routes` with the §5 routes
  (including the comment routes) and the `{error_code, detail}` error shape; set `author`
  from `Depends(require_login)` on card and comment create, and leave it untouched on
  `PATCH`.
- [x] **P02-02:** Validate inputs (non-empty titles/bodies, length caps, unknown ids →
  404, bad reply parent → 422/409) and return a readable 503 on database or migration
  failure.
- [x] **P02-03:** API tests via `TestClient` covering board read, each mutation, move
  between lists, comment CRUD, reply-depth rejection, authorship, and error paths.

**Exit gate:** `web/test_tasks_api.py` passes and `/api/tasks/board` round-trips a full
board (with authors and comment counts).

### P03 — Page, nav, and board rendering · Depends on: P02

**Status:** complete · **Progress:** 4/4

- [x] **P03-01:** Add `web/templates/tasks.html` (including the detail-panel comments
  markup) and the `active="tasks"` route.
- [x] **P03-02:** Register the module in `create_app` and add the **Tasks** nav link
  after **Documentation**, gated by `tasks_enabled` (expose it via `ctx()`).
- [x] **P03-03:** Implement `web/static/tasks.js`: fetch board, render lists/cards,
  subtask chips, the comment-count chip, and card attribution; create/rename list,
  create/edit/delete card, checklist CRUD, the comments UI (add/edit/delete/reply), and
  the “Created by <author>” detail line.
- [x] **P03-04:** Add `.kanban-*` / `.kanban-comment*` styles to `app.css` and bump
  `app.css?v`.

**Exit gate:** A test asserts the page renders and the nav link is present on other
tabs (`web/test_tasks_api.py`, mirroring `web/test_docs_api.py`).

### P04 — Drag and drop · Depends on: P03

**Status:** complete · **Progress:** 3/3

- [x] **P04-01:** Vendor the chosen DnD library (recommended SortableJS) under
  `web/static/vendor/`, or implement the native Pointer Events alternative; record the
  decision in §6.3.
- [x] **P04-02:** Wire card drag between/within lists → `/api/tasks/cards/{id}/move`,
  with optimistic move and revert-on-failure.
- [x] **P04-03:** Wire list reorder → `/api/tasks/lists/reorder`; verify touch/pointer
  behavior and the keyboard “move to list” fallback.

**Exit gate:** Cards and lists reorder and persist across a page reload; a forced API
error reverts the UI.

### P05 — Hardening and polish · Depends on: P04

**Status:** complete · **Progress:** 4/4

- [x] **P05-01:** Seed a default `Backlog / Doing / Done` board idempotently on first
  open — **not adopted**: §3.1 keeps a fresh board empty, and the plan made seeding
  opt-in.
- [x] **P05-02:** Empty states, loading state, error/status line, archived-list drawer
  with restore.
- [x] **P05-03:** `GET /api/tasks/export` JSON export (backup / agent feed), comments
  and authors included.
- [x] **P05-04:** Update `README.md` tab list and this document’s status; browser smoke
  via the existing Playwright harness in `web/test_e2e_playwright.py`, including
  add/edit/reply/delete comment.

**Exit gate:** Feature complete against §3–§6; docs updated; smoke passes.

---

## 10. Testing and verification

- **Unit/store:** new `core/test_task_store.py` driven by a `task_store_factory`
  fixture mirroring `conftest.py::editor_store_factory`: read **`TASKS_TEST_DATABASE_URL`**
  (skip when unset), `migrate()` a disposable `tasks_test_<uuid>` schema, and
  `DROP SCHEMA ... CASCADE` only that generated schema on teardown. It **never** derives
  the DSN from application settings or `.env`. Cover create/rename/move/archive, cascade
  delete, ordering after interleaved moves, gap-collapse renumbering, reopening the
  store, and comment threading (reject ≥2 levels and cross-card replies).
- **API:** new `web/test_tasks_api.py` (unittest + `TestClient`) covering board read,
  every mutation, comment CRUD, and error paths. `web/test_docs_api.py::test_nav_link_present_on_other_pages`
  is the model for the nav-link assertion (no PostgreSQL needed).
- **Authorship:** a test asserts that a card and a comment created through the API carry
  the **logged-in username** as `author` (the test client authenticates as a configured
  user, e.g. `ed`), and that editing a card does not change its `author`.
- **Rendering:** assert `/tasks` returns 200 and the nav link appears.
- **Persistence:** write, close, and reopen the store and read the same board (the
  editor store tests are the model for the PostgreSQL lifecycle).
- **Manual/Playwright:** create list → add card → add subtasks → comment → reply → drag
  to another list → reload → confirm position and thread; then force an API failure and
  confirm the UI reverts.
- **Degradation:** with PostgreSQL unreachable or unmigrated, `/tasks` renders a readable
  error naming `scripts/setup_tasks_db.py`, and every other tab still serves.

Run the focused suites with the repo’s existing test runner (pytest/unittest), e.g.
`TASKS_TEST_DATABASE_URL=... python -m pytest web/test_tasks_api.py core/test_task_store.py`.

---

## 11. Decisions, risks, and open questions

**Decisions to confirm during implementation**

1. **DnD library vs native.** Recommend vendoring SortableJS for reliable touch
   behavior (consistent with the vendored `lightweight-charts`); fall back to native
   Pointer Events only if adding an asset is off the table.
2. **Persistence: PostgreSQL.** The Tasks board uses PostgreSQL only, mirroring the
   pattern-editor stack, under its own `tasks` schema on the existing server
   (`task_database_url` empty ⇒ reuse `DATABASE_URL` / `db_*`). Rationale: an explicit
   operator requirement, consistency with the editor, one migration/backup story, and
   MCP/agent access through the same store. There is no SQLite store and no
   `data/cache/tasks.sqlite3`.
3. **Comment threading.** Flat, editable comments with **exactly one level of replies**,
   enforced by a trigger; a reply’s parent must be a top-level comment on the same card.
4. **Login and authorship.** The dashboard accepts multiple named logins: the primary
   `WEB_UI_USERNAME` / `WEB_UI_PASSWORD` plus extra users in `WEB_UI_USERS` (the operator
   runs with `admin` and `ed`, both `TBY`). `author` on a card (creator) and on a comment
   = the logged-in username (`web/auth.py::require_login`), so `admin` and `ed` are
   attributed distinctly. The board is still shared (no per-user permissions); the card
   `author` is the creator and is immutable.
5. **Comment/subtask edits bump the card.** Adding/editing/deleting a comment and
   toggling a subtask bump the parent card’s `updated_at`.
6. **Delete vs archive.** v1 archives by default and only hard-deletes on explicit
   request; lists hard-delete only when empty. Deleting a top-level comment also removes
   its replies.
7. **Seed board on first open.** Default `Backlog / Doing / Done` unless the operator
   wants an empty start.

**Risks and mitigations**

- *Shared CSS regression.* Keep all new rules under a `kanban-`/`k.*` prefix and bump the
  cache-buster; the Tasks styles are additive only.
- *Float rank exhaustion.* Detected and repaired by the renumber pass (§4.3); add a test.
- *Touch DnD flakiness.* Covered by choosing a pointer-aware library and by keeping the
  non-drag “move to list” fallback.
- *PostgreSQL availability now gates the Tasks tab.* Mitigate by degrading to a readable
  error (naming `scripts/setup_tasks_db.py`) and never blocking login or other tabs.
- *Another schema to migrate on deploy.* Mitigate with idempotent
  `scripts/setup_tasks_db.py`, run by the deploy script.
- *Credential named in a committed doc.* The real password lives in the gitignored
  `.env`; `docs/kanban-plan.md` records the current pair for reference only and should be
  updated (or the password rotated) if the repository is ever shared publicly.
- *Single-writer assumption.* Documented; if the app is ever run with multiple workers
  against one database, PostgreSQL still serializes writers, but cross-process ordering
  races would need optimistic tokens (deferred).

**Resolved during implementation**

1. **SortableJS is vendored** (`web/static/vendor/sortable.min.js`, 1.15.6, MIT) for
   reliable touch/pointer drag, consistent with the vendored `lightweight-charts`. The
   non-drag equivalents (`Move to list`, list `Move left/right`) are kept.
2. **The tab ships visible** (`TASKS_ENABLED` defaults `true`); a clone can hide it with
   `TASKS_ENABLED=false`.
3. **v1 scope as built:** lists/cards/subtasks, comments (one-level replies), drag and
   drop, archived-list drawer, and JSON export. Labels, due dates, symbol/run deep links,
   and MCP tools remain §8 follow-ups.
