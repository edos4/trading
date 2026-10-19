"""Tasks board repository (Trello-style lists/cards/subtasks/comments).

PostgreSQL only, mirroring the pattern editor's store shape: one connection per
operation, all writes in a transaction, server-generated timestamps, and
validation here (the web layer owns authentication). Positions are fractional
ranks (``DOUBLE PRECISION``) so a move is a single ``UPDATE``; a collapsing gap
triggers an O(n) renumber of the affected scope.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
import uuid

from core.task_db import (
    TaskConflict, TaskError, TaskMigrationRequired, TaskNotFound,
    configured_dsn, connection, schema_name, verify_migrations,
)

STEP = 1024.0
MIN_GAP = 1e-6


def uid() -> str:
    return uuid.uuid4().hex


def _ser(row) -> dict:
    """dict(sqlite Row) → JSON-friendly dict with ISO timestamps."""
    out = dict(row)
    for key in ("created_at", "updated_at"):
        value = out.get(key)
        if isinstance(value, datetime):
            out[key] = value.isoformat()
    return out


def _text(value, limit, field, *, allow_empty=False) -> str:
    text = value if isinstance(value, str) else ""
    text = text.strip()
    if not text and not allow_empty:
        raise TaskError(f"{field} must not be empty")
    if len(text) > limit:
        raise TaskError(f"{field} must be at most {limit} characters")
    return text


def _description(value) -> str:
    text = value if isinstance(value, str) else ""
    if len(text) > 20000:
        raise TaskError("description must be at most 20000 characters")
    return text


def _require(con, table, identity, label):
    row = con.execute(f"SELECT * FROM {table} WHERE id=%s", (identity,)).fetchone()
    if row is None:
        raise TaskNotFound(f"Unknown {label}")
    return row


def _next_position(con, table, scope_col=None, scope_val=None) -> float:
    if scope_col:
        row = con.execute(
            f"SELECT COALESCE(MAX(position), 0) AS m FROM {table} WHERE {scope_col}=%s",
            (scope_val,)).fetchone()
    else:
        row = con.execute(f"SELECT COALESCE(MAX(position), 0) AS m FROM {table}").fetchone()
    return float(row["m"]) + STEP


def _ordered(con, table, scope_col, scope_val, exclude=None):
    query = f"SELECT id, position FROM {table} WHERE {scope_col}=%s"
    args = [scope_val]
    if exclude is not None:
        query += " AND id<>%s"
        args.append(exclude)
    query += " ORDER BY position, id"
    return con.execute(query, args).fetchall()


def _place(con, table, scope_col, scope_val, item_id, before_id=None, after_id=None) -> None:
    """Set ``item_id``'s rank so it sits before/after the given neighbour.

    ``before_id`` is the item it should precede, ``after_id`` the item it should
    follow. With neither it is appended. A sub-threshold gap renumbers the scope.
    """
    rows = _ordered(con, table, scope_col, scope_val, exclude=item_id)
    ids = [r["id"] for r in rows]
    idx = len(ids)
    if after_id and after_id in ids:
        idx = ids.index(after_id) + 1
    elif before_id and before_id in ids:
        idx = ids.index(before_id)
    prev = float(rows[idx - 1]["position"]) if idx > 0 else None
    nxt = float(rows[idx]["position"]) if idx < len(rows) else None
    if prev is not None and nxt is not None and (nxt - prev) < MIN_GAP:
        order = ids[:idx] + [item_id] + ids[idx:]
        for i, rid in enumerate(order):
            con.execute(f"UPDATE {table} SET position=%s WHERE id=%s", ((i + 1) * STEP, rid))
        return
    if prev is None and nxt is None:
        pos = STEP
    elif prev is None:
        pos = nxt - STEP
    elif nxt is None:
        pos = prev + STEP
    else:
        pos = (prev + nxt) / 2.0
    con.execute(f"UPDATE {table} SET position=%s WHERE id=%s", (pos, item_id))


def _touch_card(con, card_id) -> None:
    con.execute("UPDATE task_cards SET updated_at=clock_timestamp() WHERE id=%s", (card_id,))


class TaskStore:
    """Thread-safe-enough via per-operation connections; created without DDL."""

    def __init__(self, dsn=None, schema="tasks"):
        self._dsn = configured_dsn() if dsn is None else dsn
        if not isinstance(self._dsn, str) or not self._dsn.strip():
            from core.task_db import TaskDatabaseUnavailable

            raise TaskDatabaseUnavailable("Configure PostgreSQL for the Tasks board")
        self.schema = schema_name(schema)

    @contextmanager
    def connect(self):
        with connection(self._dsn, self.schema) as con:
            verify_migrations(con)
            yield con

    # ── health / reads ─────────────────────────────────────────────────────
    def ping(self) -> bool:
        with self.connect() as con:
            con.execute("SELECT 1")
        return True

    def board(self, include_archived: bool = False) -> dict:
        with self.connect() as con:
            lists = [dict(r) for r in con.execute(
                "SELECT * FROM task_lists ORDER BY position, id")]
            cards = [dict(r) for r in con.execute(
                "SELECT c.*, (SELECT count(*) FROM task_comments m WHERE m.card_id=c.id)"
                " AS comment_count FROM task_cards c ORDER BY c.position, c.id")]
            subs = [dict(r) for r in con.execute(
                "SELECT * FROM task_subtasks ORDER BY position, id")]
        subs_by_card: dict[str, list] = {}
        for sub in subs:
            subs_by_card.setdefault(sub["card_id"], []).append(_ser(sub))
        cards_by_list: dict[str, list] = {}
        for card in cards:
            if card["archived"] and not include_archived:
                continue
            item = _ser(card)
            item["subtasks"] = subs_by_card.get(item["id"], [])
            cards_by_list.setdefault(item["list_id"], []).append(item)
        out = []
        for lst in lists:
            if lst["archived"] and not include_archived:
                continue
            item = _ser(lst)
            item["cards"] = cards_by_list.get(item["id"], [])
            out.append(item)
        return {"lists": out}

    def get_card(self, card_id) -> dict:
        with self.connect() as con:
            card = dict(_require(con, "task_cards", card_id, "card"))
            card["comment_count"] = con.execute(
                "SELECT count(*) AS n FROM task_comments WHERE card_id=%s",
                (card_id,)).fetchone()["n"]
            card["subtasks"] = [_ser(r) for r in con.execute(
                "SELECT * FROM task_subtasks WHERE card_id=%s ORDER BY position, id",
                (card_id,)).fetchall()]
        return _ser(card)

    # ── lists ──────────────────────────────────────────────────────────────
    def create_list(self, title) -> dict:
        title = _text(title, 200, "title")
        with self.connect() as con:
            lid = uid()
            pos = _next_position(con, "task_lists")
            con.execute("INSERT INTO task_lists(id, title, position) VALUES(%s, %s, %s)",
                        (lid, title, pos))
            return _ser(_require(con, "task_lists", lid, "list"))

    def update_list(self, list_id, *, title=None, archived=None) -> dict:
        with self.connect() as con:
            _require(con, "task_lists", list_id, "list")
            sets, args = [], []
            if title is not None:
                sets.append("title=%s")
                args.append(_text(title, 200, "title"))
            if archived is not None:
                sets.append("archived=%s")
                args.append(bool(archived))
            if sets:
                sets.append("updated_at=clock_timestamp()")
                args.append(list_id)
                con.execute(f"UPDATE task_lists SET {', '.join(sets)} WHERE id=%s", args)
            return _ser(_require(con, "task_lists", list_id, "list"))

    def reorder_list(self, list_id, *, before_id=None, after_id=None) -> dict:
        with self.connect() as con:
            _require(con, "task_lists", list_id, "list")
            _place(con, "task_lists", "id", list_id, list_id,
                   before_id=before_id, after_id=after_id)
            return _ser(_require(con, "task_lists", list_id, "list"))

    def delete_list(self, list_id) -> None:
        with self.connect() as con:
            _require(con, "task_lists", list_id, "list")
            count = con.execute("SELECT count(*) AS n FROM task_cards WHERE list_id=%s",
                                (list_id,)).fetchone()["n"]
            if count:
                raise TaskConflict("List is not empty; delete or move its cards first")
            con.execute("DELETE FROM task_lists WHERE id=%s", (list_id,))

    # ── cards ──────────────────────────────────────────────────────────────
    def create_card(self, list_id, title, description, author) -> dict:
        title = _text(title, 200, "title")
        description = _description(description)
        author = _text(author, 200, "author")
        with self.connect() as con:
            _require(con, "task_lists", list_id, "list")
            cid = uid()
            pos = _next_position(con, "task_cards", "list_id", list_id)
            con.execute(
                "INSERT INTO task_cards(id, list_id, title, description, author, position)"
                " VALUES(%s, %s, %s, %s, %s, %s)",
                (cid, list_id, title, description, author, pos))
            return self._card(con, cid)

    def update_card(self, card_id, *, title=None, description=None, archived=None) -> dict:
        with self.connect() as con:
            _require(con, "task_cards", card_id, "card")
            sets, args = [], []
            if title is not None:
                sets.append("title=%s")
                args.append(_text(title, 200, "title"))
            if description is not None:
                sets.append("description=%s")
                args.append(_description(description))
            if archived is not None:
                sets.append("archived=%s")
                args.append(bool(archived))
            if sets:
                sets.append("updated_at=clock_timestamp()")
                args.append(card_id)
                con.execute(f"UPDATE task_cards SET {', '.join(sets)} WHERE id=%s", args)
            return self._card(con, card_id)

    def move_card(self, card_id, list_id, *, before_id=None, after_id=None) -> dict:
        with self.connect() as con:
            _require(con, "task_cards", card_id, "card")
            _require(con, "task_lists", list_id, "list")
            con.execute(
                "UPDATE task_cards SET list_id=%s, updated_at=clock_timestamp() WHERE id=%s",
                (list_id, card_id))
            _place(con, "task_cards", "list_id", list_id, card_id,
                   before_id=before_id, after_id=after_id)
            return self._card(con, card_id)

    def delete_card(self, card_id) -> None:
        with self.connect() as con:
            _require(con, "task_cards", card_id, "card")
            con.execute("DELETE FROM task_cards WHERE id=%s", (card_id,))

    def _card(self, con, card_id) -> dict:
        card = dict(_require(con, "task_cards", card_id, "card"))
        card["comment_count"] = con.execute(
            "SELECT count(*) AS n FROM task_comments WHERE card_id=%s",
            (card_id,)).fetchone()["n"]
        card["subtasks"] = [_ser(r) for r in con.execute(
            "SELECT * FROM task_subtasks WHERE card_id=%s ORDER BY position, id",
            (card_id,)).fetchall()]
        return _ser(card)

    # ── subtasks ───────────────────────────────────────────────────────────
    def add_subtask(self, card_id, text) -> dict:
        text = _text(text, 500, "text")
        with self.connect() as con:
            _require(con, "task_cards", card_id, "card")
            sid = uid()
            pos = _next_position(con, "task_subtasks", "card_id", card_id)
            con.execute(
                "INSERT INTO task_subtasks(id, card_id, text, position) VALUES(%s,%s,%s,%s)",
                (sid, card_id, text, pos))
            _touch_card(con, card_id)
            return _ser(_require(con, "task_subtasks", sid, "subtask"))

    def update_subtask(self, subtask_id, *, text=None, done=None) -> dict:
        with self.connect() as con:
            sub = _require(con, "task_subtasks", subtask_id, "subtask")
            sets, args = [], []
            if text is not None:
                sets.append("text=%s")
                args.append(_text(text, 500, "text"))
            if done is not None:
                sets.append("done=%s")
                args.append(bool(done))
            if sets:
                args.append(subtask_id)
                con.execute(f"UPDATE task_subtasks SET {', '.join(sets)} WHERE id=%s", args)
            _touch_card(con, sub["card_id"])
            return _ser(_require(con, "task_subtasks", subtask_id, "subtask"))

    def delete_subtask(self, subtask_id) -> None:
        with self.connect() as con:
            sub = _require(con, "task_subtasks", subtask_id, "subtask")
            con.execute("DELETE FROM task_subtasks WHERE id=%s", (subtask_id,))
            _touch_card(con, sub["card_id"])

    # ── comments ───────────────────────────────────────────────────────────
    def list_comments(self, card_id) -> list[dict]:
        with self.connect() as con:
            _require(con, "task_cards", card_id, "card")
            rows = con.execute(
                "SELECT * FROM task_comments WHERE card_id=%s ORDER BY created_at, id",
                (card_id,)).fetchall()
        return [_ser(r) for r in rows]

    def add_comment(self, card_id, author, body, parent_id=None) -> dict:
        body = _text(body, 20000, "body")
        author = _text(author, 200, "author")
        with self.connect() as con:
            _require(con, "task_cards", card_id, "card")
            if parent_id:
                parent = _require(con, "task_comments", parent_id, "comment")
                if parent["card_id"] != card_id:
                    raise TaskError("reply parent belongs to another card")
                if parent["parent_id"] is not None:
                    raise TaskError("replies may only be one level deep")
            mid = uid()
            con.execute(
                "INSERT INTO task_comments(id, card_id, parent_id, author, body)"
                " VALUES(%s, %s, %s, %s, %s)",
                (mid, card_id, parent_id, author, body))
            _touch_card(con, card_id)
            return _ser(_require(con, "task_comments", mid, "comment"))

    def update_comment(self, comment_id, body) -> dict:
        body = _text(body, 20000, "body")
        with self.connect() as con:
            row = _require(con, "task_comments", comment_id, "comment")
            con.execute(
                "UPDATE task_comments SET body=%s, updated_at=clock_timestamp() WHERE id=%s",
                (body, comment_id))
            _touch_card(con, row["card_id"])
            return _ser(_require(con, "task_comments", comment_id, "comment"))

    def delete_comment(self, comment_id) -> None:
        with self.connect() as con:
            row = _require(con, "task_comments", comment_id, "comment")
            con.execute("DELETE FROM task_comments WHERE id=%s", (comment_id,))
            _touch_card(con, row["card_id"])

    # ── export ─────────────────────────────────────────────────────────────
    def export(self) -> dict:
        """Whole board including comments and authors (backup / agent feed)."""
        with self.connect() as con:
            lists = [_ser(r) for r in con.execute(
                "SELECT * FROM task_lists ORDER BY position, id")]
            cards = [dict(r) for r in con.execute(
                "SELECT * FROM task_cards ORDER BY position, id")]
            subs = [dict(r) for r in con.execute(
                "SELECT * FROM task_subtasks ORDER BY position, id")]
            comments = [_ser(r) for r in con.execute(
                "SELECT * FROM task_comments ORDER BY created_at, id")]
        subs_by_card: dict[str, list] = {}
        for sub in subs:
            subs_by_card.setdefault(sub["card_id"], []).append(_ser(sub))
        comments_by_card: dict[str, list] = {}
        for comment in comments:
            comments_by_card.setdefault(comment["card_id"], []).append(comment)
        cards_by_list: dict[str, list] = {}
        for card in cards:
            item = _ser(card)
            item["subtasks"] = subs_by_card.get(item["id"], [])
            item["comments"] = comments_by_card.get(item["id"], [])
            cards_by_list.setdefault(item["list_id"], []).append(item)
        for lst in lists:
            lst["cards"] = cards_by_list.get(lst["id"], [])
        return {"lists": lists}


_store: TaskStore | None = None


def open_task_store(*, dsn=None, schema=None) -> TaskStore:
    """The configured Tasks store (module singleton unless overridden)."""
    from config import settings

    if dsn is not None or schema is not None:
        return TaskStore(dsn=dsn, schema=schema or settings.task_schema)
    global _store
    if _store is None:
        _store = TaskStore(schema=settings.task_schema)
    return _store
