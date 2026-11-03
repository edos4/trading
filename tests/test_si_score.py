"""Deterministic scorer for the self-improving loop (si.sh)."""

from __future__ import annotations

import json

import pytest

from scripts.si_score import (
    append_leaderboard,
    best_score,
    compare_scores,
    main,
    score_export,
)


def _export(*, patterns_v2: bool = True, rs=(1.0, -1.0, 0.5, -1.0)) -> dict:
    trades = [
        {
            "market": "us",
            "symbol": f"SYM{i}",
            "action": "BUY" if i % 2 == 0 else "SELL",
            "pattern": "double_bottom" if i % 3 else "double_top",
            "r": r,
            "pnl": 100.0 * r,
            "pnl_pct": r * 3,
            "hold_bars": 4,
            "exit_reason": "take_profit" if r > 0 else "stop_loss",
            "sim_closed": f"2026-01-{i + 1:02d}T14:30:00+00:00",
        }
        for i, r in enumerate(rs)
    ]
    return {
        "purpose": "paper_trade_evaluation",
        "exported_at": "2026-01-10T00:00:00+00:00",
        "filter": "us",
        "books": [
            {
                "market": "us",
                "settings": {"patterns_v2": patterns_v2},
                "closed_count": len(trades),
                "closed_trades": trades,
            }
        ],
    }


def test_score_tracks_win_rate_pf_and_expectancy():
    score = score_export(_export(), min_closed=4)
    assert score["trades"] == 4
    assert score["wins"] == 2
    assert score["win_rate_pct"] == 50.0
    assert score["profit_factor"] == pytest.approx(0.75)
    assert score["expectancy_r"] == pytest.approx(-0.125)
    assert score["exit_reason_mix"] == {"stop_loss": 2, "take_profit": 2}
    assert score["valid"] is True


def test_score_is_invalid_without_patternsv2_or_enough_trades():
    assert score_export(_export(patterns_v2=False), min_closed=4)["valid"] is False
    assert score_export(_export(), min_closed=10)["valid"] is False


def test_assert_patternsv2_exits_nonzero_when_off(tmp_path):
    path = tmp_path / "pt.json"
    path.write_text(json.dumps(_export(patterns_v2=False)), encoding="utf-8")
    assert main(["assert-patternsv2", "--export", str(path)]) == 1

    path.write_text(json.dumps(_export()), encoding="utf-8")
    assert main(["assert-patternsv2", "--export", str(path)]) == 0


def test_compare_accepts_a_verified_improvement():
    reference = score_export(_export(rs=(1.0, -1.0, 0.5, -1.0)), min_closed=4)
    new = score_export(
        _export(rs=(1.0, 1.0, 0.5, -1.0, -1.0)), min_closed=4
    )
    verdict = compare_scores(new, reference)
    assert verdict["improved"] is True, verdict["reasons"]


def test_compare_rejects_win_rate_bought_with_a_collapsed_trade_count():
    reference = score_export(_export(rs=(1.0, -1.0, 0.5, -1.0)), min_closed=4)
    new = score_export(_export(rs=(2.0, 2.0)), min_closed=2)
    verdict = compare_scores(new, reference)
    assert verdict["improved"] is False
    assert any("trade count" in reason for reason in verdict["reasons"])


def test_compare_rejects_a_lower_win_rate_or_profit_factor():
    reference = score_export(_export(rs=(1.0, 1.0, -1.0, -1.0)), min_closed=4)
    lower_wr = score_export(_export(rs=(1.0, -1.0, -1.0, -1.0)), min_closed=4)
    assert compare_scores(lower_wr, reference)["improved"] is False

    lower_pf = score_export(_export(rs=(0.2, 0.2, -1.0, -1.0)), min_closed=4)
    assert compare_scores(lower_pf, reference)["improved"] is False


def test_compare_rejects_an_equal_rerun():
    reference = score_export(_export(rs=(1.0, 1.0, 0.5, -1.0)), min_closed=4)
    same = score_export(_export(rs=(1.0, 1.0, 0.5, -1.0)), min_closed=4)
    verdict = compare_scores(same, reference)
    assert verdict["improved"] is False
    assert any("strict" in reason for reason in verdict["reasons"])


def test_best_score_picks_the_highest_win_rate(tmp_path):
    accepted = tmp_path / "accepted"
    accepted.mkdir()
    low = score_export(_export(rs=(1.0, -1.0, -1.0, -1.0)), min_closed=4)
    high = score_export(_export(rs=(1.0, 1.0, 1.0, -1.0)), min_closed=4)
    (accepted / "01.json").write_text(json.dumps(low), encoding="utf-8")
    (accepted / "02.json").write_text(json.dumps(high), encoding="utf-8")

    found = best_score(tmp_path)
    assert found is not None
    assert found[0].name == "02.json"


def test_leaderboard_appends_header_once(tmp_path):
    board = tmp_path / "leaderboard.md"
    append_leaderboard(board, score=None, label="01", status="FAILED", note="no export")
    append_leaderboard(board, score=None, label="02", status="INVALID", note="small n")
    text = board.read_text(encoding="utf-8")
    assert text.count("| Run | When |") == 1
    assert "| 01 |" in text and "| 02 |" in text
