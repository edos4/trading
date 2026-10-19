"""Tasks board store tests.

PostgreSQL-backed; the ``task_store_factory`` fixture skips unless
``TASKS_TEST_DATABASE_URL`` points at a disposable database.
"""
from __future__ import annotations

import pytest

from core.task_db import TaskConflict, TaskError, TaskNotFound


def test_create_list_card_subtask(task_store_factory):
    store = task_store_factory()
    assert store.board() == {"lists": []}

    lst = store.create_list("Backlog")
    card = store.create_card(lst["id"], "First", "desc", "ed")
    assert card["author"] == "ed"
    assert card["comment_count"] == 0

    sub = store.add_subtask(card["id"], "step 1")
    store.update_subtask(sub["id"], done=True)

    board = store.board()
    assert [l["title"] for l in board["lists"]] == ["Backlog"]
    cards = board["lists"][0]["cards"]
    assert len(cards) == 1
    assert cards[0]["subtasks"][0]["done"] is True


def test_authorship_of_cards_and_comments(task_store_factory):
    store = task_store_factory()
    lst = store.create_list("L")
    card = store.create_card(lst["id"], "T", "", "admin")
    comment = store.add_comment(card["id"], "ed", "hello")
    assert card["author"] == "admin"
    assert comment["author"] == "ed"
    # Editing a card never changes its author.
    assert store.update_card(card["id"], title="T2")["author"] == "admin"
    # Editing a comment bumps its own and the card's updated_at.
    before = store.get_card(card["id"])["updated_at"]
    edited = store.update_comment(comment["id"], "hello again")
    assert edited["body"] == "hello again"
    assert store.get_card(card["id"])["updated_at"] > before


def test_move_between_lists_and_ordering(task_store_factory):
    store = task_store_factory()
    a = store.create_list("A")
    b = store.create_list("B")
    c1 = store.create_card(a["id"], "one", "", "ed")
    c2 = store.create_card(a["id"], "two", "", "ed")
    c3 = store.create_card(a["id"], "three", "", "ed")

    store.move_card(c3["id"], a["id"], before_id=c1["id"])
    assert [c["title"] for c in store.board()["lists"][0]["cards"]] == ["three", "one", "two"]

    store.move_card(c2["id"], b["id"])
    board = {l["title"]: [c["title"] for c in l["cards"]] for l in store.board()["lists"]}
    assert board["A"] == ["three", "one"]
    assert board["B"] == ["two"]


def test_gap_collapse_renumbering(task_store_factory):
    store = task_store_factory()
    lst = store.create_list("L")
    first = store.create_card(lst["id"], "first", "", "ed")
    last = store.create_card(lst["id"], "last", "", "ed")
    prev = first
    for i in range(60):
        mid = store.create_card(lst["id"], f"m{i}", "", "ed")
        store.move_card(mid["id"], lst["id"], after_id=prev["id"], before_id=last["id"])
        prev = mid
    cards = store.board()["lists"][0]["cards"]
    assert len(cards) == 62
    positions = [c["position"] for c in cards]
    assert positions == sorted(positions)
    assert len(set(positions)) == len(positions)


def test_delete_card_cascades(task_store_factory):
    store = task_store_factory()
    lst = store.create_list("L")
    card = store.create_card(lst["id"], "T", "", "ed")
    store.add_subtask(card["id"], "s")
    store.add_comment(card["id"], "ed", "c")
    store.delete_card(card["id"])
    assert store.board()["lists"][0]["cards"] == []
    with store.connect() as con:
        assert con.execute("SELECT count(*) AS n FROM task_subtasks").fetchone()["n"] == 0
        assert con.execute("SELECT count(*) AS n FROM task_comments").fetchone()["n"] == 0


def test_delete_list_only_when_empty(task_store_factory):
    store = task_store_factory()
    lst = store.create_list("L")
    store.create_card(lst["id"], "T", "", "ed")
    with pytest.raises(TaskConflict):
        store.delete_list(lst["id"])
    empty = store.create_list("E")
    store.delete_list(empty["id"])
    assert "E" not in [l["title"] for l in store.board()["lists"]]


def test_comment_threading_rules(task_store_factory):
    store = task_store_factory()
    lst = store.create_list("L")
    card = store.create_card(lst["id"], "T", "", "ed")
    other = store.create_card(lst["id"], "U", "", "ed")
    top = store.add_comment(card["id"], "ed", "top")
    reply = store.add_comment(card["id"], "admin", "reply", parent_id=top["id"])
    assert reply["parent_id"] == top["id"]
    with pytest.raises(TaskError):
        store.add_comment(card["id"], "ed", "deep", parent_id=reply["id"])
    with pytest.raises(TaskError):
        store.add_comment(other["id"], "ed", "cross", parent_id=top["id"])
    # Deleting the top-level comment removes its replies.
    store.delete_comment(top["id"])
    with store.connect() as con:
        assert con.execute("SELECT count(*) AS n FROM task_comments").fetchone()["n"] == 0


def test_reopen_preserves_board(task_store_factory):
    store = task_store_factory()
    lst = store.create_list("Persist")
    store.create_card(lst["id"], "keep", "", "ed")
    reopened = task_store_factory()
    board = reopened.board()
    assert [l["title"] for l in board["lists"]] == ["Persist"]
    assert board["lists"][0]["cards"][0]["title"] == "keep"


def test_validation_and_not_found(task_store_factory):
    store = task_store_factory()
    with pytest.raises(TaskError):
        store.create_list("   ")
    lst = store.create_list("L")
    with pytest.raises(TaskNotFound):
        store.create_card("nope", "T", "", "ed")
    card = store.create_card(lst["id"], "T", "", "ed")
    with pytest.raises(TaskError):
        store.add_comment(card["id"], "ed", "  ")
    with pytest.raises(TaskNotFound):
        store.add_comment("missing", "ed", "x")


def test_archive_card_hidden_unless_requested(task_store_factory):
    store = task_store_factory()
    lst = store.create_list("L")
    card = store.create_card(lst["id"], "T", "", "ed")
    store.update_card(card["id"], archived=True)
    assert store.board()["lists"][0]["cards"] == []
    assert len(store.board(include_archived=True)["lists"][0]["cards"]) == 1
