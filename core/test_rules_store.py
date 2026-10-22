"""PostgreSQL CRUD tests for visual indicator rules."""
from __future__ import annotations

import pytest

from core.rules_db import RuleConflict, RulesError
from core.rules_store import RulesStore


def payload(name="indicator1", value=30, attachment=None):
    body = {
        "schema_version": 1,
        "name": name,
        "timeframe": "1d",
        "direction": "long",
        "root": {"kind": "group", "op": "AND", "children": [{
            "kind": "condition",
            "left": {"source": "indicator", "id": "rsi", "params": {"period": 14}},
            "operator": "gte",
            "right": {"source": "value", "value": value},
        }]},
        "exit": {
            "on_false": True,
            "stop": {"kind": "percent", "percent": 5},
            "target": {"kind": "r_multiple", "r": 2},
        },
    }
    if attachment is not None:
        body["attachments"] = {"patternsv2": attachment}
    return body


def test_save_list_get_update_delete(rules_store_factory) -> None:
    store: RulesStore = rules_store_factory()
    created = store.save_rule("indicator1", payload())
    assert created["name"] == "indicator1"
    assert created["generation"] == 0
    assert created["payload"]["name"] == "indicator1"

    listed = store.list_rules()
    assert [row["name"] for row in listed] == ["indicator1"]
    assert listed[0]["conditions"] == 1
    assert listed[0]["timeframe"] == "1d"

    fetched = store.get_rule(created["id"])
    assert fetched["id"] == created["id"]

    updated = store.save_rule(
        "indicator1", payload(value=50), rule_id=created["id"],
        expected_generation=0,
    )
    assert updated["generation"] == 1
    assert updated["payload"]["root"]["children"][0]["right"]["value"] == 50

    with pytest.raises(RuleConflict):
        store.save_rule("indicator1", payload(value=60), rule_id=created["id"],
                        expected_generation=0)

    store.delete_rule(created["id"])
    assert store.get_rule(created["id"]) is None


def test_duplicate_name_is_a_conflict(rules_store_factory) -> None:
    store = rules_store_factory()
    store.save_rule("dup", payload("dup"))
    with pytest.raises(RuleConflict):
        store.save_rule("DUP", payload("DUP"))


def test_patternsv2_attachment_is_saved_and_summarized(rules_store_factory) -> None:
    store = rules_store_factory()
    created = store.save_rule(
        "attached",
        payload("attached",
                attachment={"pattern": "double_bottom",
                            "position": "before", "mode": "filter"}),
    )
    assert created["payload"]["attachments"]["patternsv2"]["pattern"] == "double_bottom"
    listed = store.list_rules()
    assert listed[0]["patternsv2"] == {
        "pattern": "double_bottom", "position": "before", "mode": "filter"}


def test_invalid_attachment_is_rejected(rules_store_factory) -> None:
    store = rules_store_factory()
    with pytest.raises(RulesError):
        store.save_rule(
            "bad attachment",
            payload("bad attachment",
                    attachment={"position": "sideways", "mode": "filter"}),
        )
    with pytest.raises(RulesError):
        store.save_rule(
            "bad mode",
            payload("bad mode",
                    attachment={"position": "after", "mode": "sometimes"}),
        )
    with pytest.raises(RulesError):
        store.save_rule(
            "bad pattern",
            payload("bad pattern",
                    attachment={"pattern": "not_a_pattern",
                                "position": "after", "mode": "filter"}),
        )


def test_invalid_payload_is_rejected(rules_store_factory) -> None:
    store = rules_store_factory()
    with pytest.raises(RulesError):
        store.save_rule("", payload(""))
    with pytest.raises(RulesError):
        store.save_rule("bad", {"schema_version": 1, "name": "bad",
                                "root": {"kind": "group", "op": "AND", "children": []}})
    with pytest.raises(RulesError):
        store.save_rule("bad", {**payload("bad"), "schema_version": 2})

    unknown = payload("unknown")
    unknown["root"]["children"][0]["left"]["id"] = "nope"
    with pytest.raises(RulesError):
        store.save_rule("unknown", unknown)

    bad_param = payload("bad param")
    bad_param["root"]["children"][0]["left"]["params"] = {"period": 0}
    with pytest.raises(RulesError):
        store.save_rule("bad param", bad_param)


def test_unknown_delete_is_an_error(rules_store_factory) -> None:
    store = rules_store_factory()
    with pytest.raises(RulesError):
        store.delete_rule("missing")


def test_delete_refused_while_in_use(rules_store_factory, monkeypatch) -> None:
    store = rules_store_factory()
    created = store.save_rule("in use", payload("in use"))
    monkeypatch.setattr("core.rules_store.rule_in_use_market", lambda rule_id: "us")
    with pytest.raises(RuleConflict):
        store.delete_rule(created["id"])
    assert store.get_rule(created["id"]) is not None


def test_rule_in_use_scans_running_books(monkeypatch) -> None:
    import threading

    from core.rules_store import rule_in_use

    class Book:
        def __init__(self, market, rule_id="", filter_id="", running=True,
                     filter_mode="shadow"):
            self.market = market
            self.rule_id = rule_id
            self.rule_filter_id = filter_id
            self.rule_filter_mode = filter_mode
            self.running = running
            self.lock = threading.Lock()

    class Manager:
        books = {"us": Book("us", rule_id="strategy"),
                 "ph": Book("ph", filter_id="filter")}

    import core.paper_books as paper_books_module

    monkeypatch.setattr(paper_books_module, "paper_books", Manager())
    assert rule_in_use("strategy") is True
    assert rule_in_use("filter") is True
    assert rule_in_use("other") is False

    Manager.books["ph"].rule_filter_mode = "off"
    assert rule_in_use("filter") is False
    Manager.books["ph"].rule_filter_mode = "shadow"

    Manager.books["us"].running = False
    Manager.books["ph"].running = False
    assert rule_in_use("strategy") is False
    assert rule_in_use("filter") is False


def test_migrate_is_idempotent(rules_store_factory) -> None:
    from core.rules_db import migrate

    assert migrate(rules_store_factory.dsn, rules_store_factory.schema) == []
