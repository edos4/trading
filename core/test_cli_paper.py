"""CLI flags for paper stream / collect-first / trade export."""

from __future__ import annotations

import argparse

from main import (
    _COLLECT_FIRST_USE_DEFAULT,
    _parse_args,
    _resolve_collect_first,
    _resolve_n_symbols,
    parse_stream_date,
)


USER_CMD = [
    "--paper",
    "--symbols=500",
    "--pattern-only",
    "--collect-first=4",
    "--stream=01/05/2026",
    "--duration-days=30",
    "--export-trades-log=output_trades.json",
]


def test_parse_stream_date_slash_is_us_mdy():
    assert parse_stream_date("01/05/2026") == "2026-01-05"
    assert parse_stream_date("1/5/2026") == "2026-01-05"
    assert parse_stream_date("2026-01-05") == "2026-01-05"


def test_parse_stream_date_rejects_garbage():
    try:
        parse_stream_date("not-a-date")
    except argparse.ArgumentTypeError as exc:
        assert "invalid stream date" in str(exc)
    else:
        raise AssertionError("expected ArgumentTypeError")


def test_user_paper_stream_command():
    args = _parse_args(USER_CMD)
    assert args.paper == 50
    assert args.symbols == 500
    assert args.pattern_only is True
    assert args.collect_first == 4
    assert args.stream == "01/05/2026"
    assert args.duration_days == 30
    assert args.export_trades_log == "output_trades.json"
    assert _resolve_n_symbols(args, args.paper) == 500
    assert _resolve_collect_first(args) == (True, 4)
    assert parse_stream_date(args.stream) == "2026-01-05"


def test_paper_positional_n_without_symbols():
    args = _parse_args(["--paper", "200"])
    assert args.paper == 200
    assert args.symbols is None
    assert _resolve_n_symbols(args, args.paper) == 200


def test_collect_first_bare_flag_uses_env_default_n():
    args = _parse_args(["--paper", "--collect-first"])
    assert args.collect_first == _COLLECT_FIRST_USE_DEFAULT
    enabled, top_n = _resolve_collect_first(args)
    assert enabled is True
    assert top_n is None


def test_collect_first_bare_with_top_n_flag():
    args = _parse_args(["--paper", "--collect-first", "--collect-first-top-n", "4"])
    assert args.collect_first == _COLLECT_FIRST_USE_DEFAULT
    assert _resolve_collect_first(args) == (True, 4)


def test_stream_without_date_is_empty_string():
    args = _parse_args(["--paper", "--stream"])
    assert args.stream == ""


def test_duration_days_flag():
    args = _parse_args(["--paper", "--duration-days=30"])
    assert args.duration_days == 30
