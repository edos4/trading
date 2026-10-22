"""CRUD store for visual indicator rules (PostgreSQL, JSONB payload)."""
from __future__ import annotations

from contextlib import contextmanager
import uuid

import psycopg
from psycopg.types.json import Jsonb

from core.rule_contracts import RulePayload
from core.rules_db import (
    RuleConflict, RulesError, connection, verify_migrations,
)

_COLUMNS = "id, name, generation, payload, created_at, updated_at"


def uid() -> str:
    return uuid.uuid4().hex


def _iso(value):
    return value.isoformat() if value is not None else None


def _row(row) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "generation": int(row["generation"]),
        "payload": row["payload"],
        "created_at": _iso(row["created_at"]),
        "updated_at": _iso(row["updated_at"]),
    }


def _name(value: str) -> str:
    name = (value or "").strip()
    if not name:
        raise RulesError("An indicator needs a name")
    if len(name) > 80:
        raise RulesError("The name must be 80 characters or fewer")
    return name


def _validate(name: str, payload: dict) -> tuple[str, RulePayload]:
    name = _name(name)
    if not isinstance(payload, dict):
        raise RulesError("The rule payload must be a JSON object")
    data = dict(payload)
    data["name"] = name
    data.setdefault("schema_version", 1)
    try:
        rule = RulePayload.model_validate(data)
        from analysis.rule_catalog import validate_rule

        validate_rule(rule)
        return name, rule
    except Exception as exc:  # pydantic/catalog -> a user-facing message
        errors = getattr(exc, "errors", None)
        if callable(errors):
            first = (errors() or [{}])[0]
            location = ".".join(str(part) for part in first.get("loc", ()) if part != "")
            message = first.get("msg") or "Invalid rule"
            raise RulesError(f"{location}: {message}" if location else message) from None
        raise RulesError(str(exc) or "Invalid rule") from None


def _summary(payload: dict) -> dict:
    counts = {"blocks": 0, "conditions": 0}

    def walk(node):
        if not isinstance(node, dict):
            return
        if node.get("kind") == "condition":
            counts["conditions"] += 1
            return
        counts["blocks"] += 1
        for child in node.get("children") or []:
            walk(child)

    walk((payload or {}).get("root") or {})
    attachment = (((payload or {}).get("attachments") or {}).get("patternsv2") or {})
    return {
        "timeframe": (payload or {}).get("timeframe") or "1d",
        "direction": (payload or {}).get("direction") or "long",
        "blocks": counts["blocks"],
        "conditions": counts["conditions"],
        "patternsv2": {
            "position": attachment.get("position") or "off",
            "mode": attachment.get("mode") or "filter",
        },
    }


class RulesStore:
    def __init__(self, dsn: str | None = None, schema: str = "rules"):
        from config import settings
        from data.db import build_dsn

        self.dsn = dsn or settings.rules_database_url or build_dsn()
        self.schema = schema

    @contextmanager
    def connect(self):
        with connection(self.dsn, self.schema) as con:
            verify_migrations(con)
            yield con

    def ping(self) -> bool:
        with self.connect() as con:
            con.execute("SELECT 1")
        return True

    # ── reads ────────────────────────────────────────────────────────────
    def list_rules(self) -> list[dict]:
        with self.connect() as con:
            rows = con.execute(
                f"SELECT {_COLUMNS} FROM indicator_rules ORDER BY lower(name)"
            ).fetchall()
        return [
            {
                "id": row["id"],
                "name": row["name"],
                "generation": int(row["generation"]),
                "updated_at": _iso(row["updated_at"]),
                **_summary(row["payload"]),
            }
            for row in rows
        ]

    def get_rule(self, rule_id: str) -> dict | None:
        with self.connect() as con:
            row = con.execute(
                f"SELECT {_COLUMNS} FROM indicator_rules WHERE id=%s",
                ((rule_id or "").strip(),),
            ).fetchone()
        return _row(row) if row is not None else None

    # ── writes ───────────────────────────────────────────────────────────
    def save_rule(self, name: str, payload: dict, *, rule_id: str | None = None,
                  expected_generation: int | None = None) -> dict:
        name, rule = _validate(name, payload)
        body = Jsonb(rule.model_dump(mode="json"))
        try:
            with self.connect() as con:
                if not rule_id:
                    duplicate = con.execute(
                        "SELECT id FROM indicator_rules WHERE lower(name)=lower(%s)",
                        (name,),
                    ).fetchone()
                    if duplicate is not None:
                        raise RuleConflict(f"A rule named {name!r} already exists")
                    new_id = uid()
                    con.execute(
                        "INSERT INTO indicator_rules(id, name, generation, payload) "
                        "VALUES (%s, %s, 0, %s)",
                        (new_id, name, body),
                    )
                    row = con.execute(
                        f"SELECT {_COLUMNS} FROM indicator_rules WHERE id=%s",
                        (new_id,),
                    ).fetchone()
                    return _row(row)

                row = con.execute(
                    "SELECT * FROM indicator_rules WHERE id=%s FOR UPDATE",
                    (rule_id,),
                ).fetchone()
                if row is None:
                    raise RulesError("Rule not found")
                if (expected_generation is not None
                        and int(row["generation"]) != int(expected_generation)):
                    raise RuleConflict("Rule changed; reload before retrying")
                generation = int(row["generation"]) + 1
                con.execute(
                    "UPDATE indicator_rules SET name=%s, generation=%s, payload=%s, "
                    "updated_at=clock_timestamp() WHERE id=%s",
                    (name, generation, body, rule_id),
                )
                row = con.execute(
                    f"SELECT {_COLUMNS} FROM indicator_rules WHERE id=%s",
                    (rule_id,),
                ).fetchone()
                return _row(row)
        except psycopg.errors.UniqueViolation:
            raise RuleConflict(f"A rule named {name!r} already exists") from None

    def delete_rule(self, rule_id: str) -> None:
        rule_id = (rule_id or "").strip()
        market = rule_in_use_market(rule_id)
        if market:
            raise RuleConflict(
                f"Stop the {market.upper()} paper book using this indicator "
                f"before deleting it")
        with self.connect() as con:
            result = con.execute(
                "DELETE FROM indicator_rules WHERE id=%s", (rule_id,),
            )
            if result.rowcount == 0:
                raise RulesError("Rule not found")


def rule_in_use_market(rule_id: str) -> str:
    """Market of the running book using this rule, or "" when unused."""
    rule_id = (rule_id or "").strip()
    if not rule_id:
        return ""
    from core.paper_books import paper_books

    for book in paper_books.books.values():
        market = getattr(book, "market", "") or ""
        with book.lock:
            if not book.running:
                continue
            if getattr(book, "rule_id", "") == rule_id:
                return market
            if (getattr(book, "rule_filter_id", "") == rule_id
                    and getattr(book, "rule_filter_mode", "off") != "off"):
                return market
    return ""


def rule_in_use(rule_id: str) -> bool:
    """True while a running paper book uses this rule as strategy or filter."""
    return bool(rule_in_use_market(rule_id))


_store: RulesStore | None = None


def open_rules_store(*, dsn: str | None = None, schema: str | None = None) -> RulesStore:
    """Process-wide store; tests pass a dsn/schema and get a fresh instance."""
    global _store
    from config import settings

    if dsn is not None or schema is not None:
        _store = RulesStore(dsn=dsn, schema=schema or settings.rules_schema)
        return _store
    if _store is None:
        _store = RulesStore(schema=settings.rules_schema)
    return _store
