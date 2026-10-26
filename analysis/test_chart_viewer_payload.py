from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import numpy as np
import pytest

from analysis.chart_renderer import ChartRenderer, _chart_session_day, build_trade_viewer_payload
from data.history import bars_to_df


@pytest.mark.parametrize("title_case", [False, True])
def test_date_only_history_keeps_session_candles_and_entry_pivot(title_case) -> None:
    dates = pd.bdate_range("2024-03-04", periods=10)
    rows = [{"date": day.date().isoformat(), "open": 100 + i, "high": 101 + i,
             "low": 99 + i, "close": 100 + i, "volume": 1000}
            for i, day in enumerate(dates)]
    # The real history adapter represents date-only response rows as UTC
    # midnights. These are session labels, including across the DST change.
    frame = bars_to_df(rows)
    if title_case:
        frame.columns = frame.columns.str.title()
    original = frame.copy()
    payload = build_trade_viewer_payload(
        frame, symbol="TEST", action="BUY", entry_time="2024-03-08T00:00:00+00:00",
        annotations=[{"type": "marker", "date": "2024-03-08", "price": 104.,
                      "label": "Saved pivot"}],
    )

    expected = dates.strftime("%Y-%m-%d").tolist()
    assert [row["time"] for row in payload["candles"]] == expected
    assert all(pd.Timestamp(row["time"]).dayofweek < 5 for row in payload["candles"])
    assert ChartRenderer()._prepare_df(frame).index.equals(dates)
    assert [_chart_session_day(stamp, "America/New_York") for stamp in frame.index] == list(dates)
    markers = {marker["text"]: marker for marker in payload["markers"]}
    assert markers["BUY"]["time"] == markers["Saved pivot"]["time"] == "2024-03-08"
    candle = next(row for row in payload["candles"] if row["time"] == markers["BUY"]["time"])
    assert candle["close"] == markers["Saved pivot"]["price"] == 104.
    pd.testing.assert_frame_equal(frame, original)


def test_mixed_session_labels_and_real_prints_sort_and_keep_latest_session() -> None:
    stamps = [
        ("2024-03-11", 10.), ("2024-03-11T13:30:00+00:00", 11.),
        ("2024-03-12", 20.), ("2024-03-12T01:00:00+00:00", 12.),
        ("2024-03-12T13:30:00+00:00", 21.), ("2024-03-13", 30.),
        ("2024-03-13T13:30:00+00:00", 31.),
    ]
    rows = [{"date": stamp, "open": close, "high": close + 1,
             "low": close - 1, "close": close, "volume": 1000}
            for stamp, close in reversed(stamps)]
    frame = bars_to_df(rows)
    original = frame.copy()
    payload = build_trade_viewer_payload(
        frame, symbol="TEST", action="BUY", entry_time="2024-03-12T01:00:00+00:00",
        annotations=[{"type": "marker", "date": "2024-03-11", "price": 12.,
                      "label": "Saved pivot"}],
    )

    assert [(row["time"], row["close"]) for row in payload["candles"]] == [
        ("2024-03-11", 12.), ("2024-03-12", 21.), ("2024-03-13", 31.),
    ]
    assert {marker["time"] for marker in payload["markers"]} == {"2024-03-11"}
    pd.testing.assert_frame_equal(frame, original)


def test_invalid_session_reprint_keeps_the_latest_valid_candle() -> None:
    idx = pd.bdate_range("2024-01-02", periods=40)
    close = 100 + np.arange(40) * .25 + np.sin(np.arange(40) / 2) * 3
    original = pd.DataFrame({
        "open": close - .4, "high": close + .6, "low": close - .6,
        "close": close, "volume": 1000,
    }, index=idx)
    reprint = original.iloc[[10]].copy()
    reprint.index += pd.Timedelta(hours=4)
    reprint["close"] = np.nan
    df = pd.concat([original, reprint])

    actual = build_trade_viewer_payload(df, symbol="TEST", trendlines=True)
    expected = build_trade_viewer_payload(original, symbol="TEST", trendlines=True)

    assert actual["candles"] == expected["candles"]
    assert actual["trendlines"] == expected["trendlines"]


def test_trendlines_fit_only_candles_displayed_by_the_viewer() -> None:
    idx = pd.bdate_range("2024-01-02", periods=80)
    close = 100 + np.arange(80) * 0.25 + np.sin(np.arange(80) / 2) * 3
    df = pd.DataFrame({
        "open": close - 0.4, "high": close + 0.6, "low": close - 0.6,
        "close": close, "volume": 1000,
    }, index=idx)
    # The viewer omits these candles. Fitting their invisible highs/lows, or
    # including their positions in a slope, moves the rail on its time scale.
    df.loc[idx[12:28], "open"] = np.nan
    charted = build_trade_viewer_payload(df, symbol="TEST", trendlines=True)
    expected = build_trade_viewer_payload(df.dropna(), symbol="TEST", trendlines=True)

    assert charted["candles"] == expected["candles"]
    assert expected["trendlines"]
    assert charted["trendlines"] == expected["trendlines"]


def test_duplicate_trade_level_retains_saved_annotation_reason() -> None:
    idx = pd.bdate_range("2024-01-02", periods=20)
    df = pd.DataFrame({
        "open": 1.0, "high": 1.1, "low": 0.9, "close": 1.0, "volume": 1000,
    }, index=idx)
    payload = build_trade_viewer_payload(df, symbol="TEST", stop=0.86, annotations=[{
        "type": "hline", "price": 0.8600000143051147, "label": "stop",
        "reason": "Below the saved swing low.", "color": "#ef5350",
    }])

    assert len(payload["levels"]) == 1
    assert payload["levels"][0]["reason"] == "Below the saved swing low."


def test_pattern_hlines_do_not_duplicate_trade_levels() -> None:
    from patterns.base_pattern import ann_hline

    idx = pd.bdate_range("2024-01-02", periods=40)
    close = pd.Series(range(100, 140), index=idx, dtype=float)
    df = pd.DataFrame({
        "open": close - 0.4,
        "high": close + 0.8,
        "low": close - 0.9,
        "close": close,
        "volume": 1_000_000,
    }, index=idx)

    payload = build_trade_viewer_payload(
        df,
        symbol="SNTI",
        action="BUY",
        entry=1.11,
        stop=0.86,
        target=1.34,
        exit_price=1.0545,
        exit_reason="stop_loss",
        annotations=[
            ann_hline(1.090000033378601, "level", "#ffeb3b"),
            # float32 prices captured from the signal's chart annotations
            ann_hline(0.8600000143051147, "stop", "#ef5350"),
            ann_hline(1.340000033378601, "target", "#26a69a"),
        ],
    )

    titles = [level["title"] for level in payload["levels"]]
    assert titles.count("stop") == 1
    assert titles.count("target") == 1
    assert titles.count("level") == 1


def test_viewer_payload_has_candles_and_levels() -> None:
    idx = pd.bdate_range("2024-01-02", periods=40)
    close = pd.Series(range(100, 140), index=idx, dtype=float)
    df = pd.DataFrame({
        "open": close - 0.4,
        "high": close + 0.8,
        "low": close - 0.9,
        "close": close,
        "volume": 1_000_000,
    }, index=idx)

    payload = build_trade_viewer_payload(
        df,
        symbol="AAPL",
        timeframe="1d",
        pattern="pattern_003_double_bottom",
        action="BUY",
        entry=120.0,
        stop=110.0,
        target=140.0,
        current=139.0,
        entry_time=datetime(2024, 2, 1),
    )
    assert payload["symbol"] == "AAPL"
    assert "chart_png_b64" not in payload
    assert len(payload["candles"]) == 40
    assert payload["candles"][0]["time"] == "2024-01-02"
    assert payload["candles"][-1]["close"] == 139.0
    assert len(payload["volume"]) == 40
    assert "ema20" not in payload
    assert "ema50" not in payload
    assert payload["rsi14"]
    assert payload["rsi14"][-1]["time"] == payload["candles"][-1]["time"]
    assert all(0.0 <= row["value"] <= 100.0 for row in payload["rsi14"])
    titles = {level["title"] for level in payload["levels"]}
    assert titles == {"entry", "stop", "target", "last"}
    assert payload["markers"]
    assert payload["markers"][0]["shape"] == "arrowUp"


def test_viewer_payload_dedupes_duplicate_session_bars() -> None:
    """A tape with two bars per session (04:00 UTC + 13:30 UTC) must not
    produce duplicate/non-monotonic candle times — LightweightCharts rejects
    them and the chart renders blank."""
    # July dates = EDT (UTC-4): both UTC stamps land on the same NY date.
    raw = [
        ("2024-07-01 04:00", 10.0),
        ("2024-07-01 13:30", 10.5),
        ("2024-07-02 04:00", 10.6),
        ("2024-07-02 13:30", 11.0),
        ("2024-07-03 13:30", 11.4),
    ]
    ts = [datetime.fromisoformat(d).replace(tzinfo=timezone.utc) for d, _ in raw]
    closes = [c for _, c in raw]
    df = pd.DataFrame(
        {
            "open": closes,
            "high": [c + 0.2 for c in closes],
            "low": [c - 0.2 for c in closes],
            "close": closes,
            "volume": [1_000_000] * len(closes),
        },
        index=pd.DatetimeIndex(ts),
    )

    payload = build_trade_viewer_payload(
        df,
        symbol="ALHC",
        timeframe="1d",
        action="SELL",
        entry=11.0,
        stop=12.0,
        target=9.0,
        current=10.5,
        session_tz="America/New_York",
    )
    times = [c["time"] for c in payload["candles"]]
    assert times == ["2024-07-01", "2024-07-02", "2024-07-03"]
    assert len(times) == len(set(times))
    assert all(a < b for a, b in zip(times, times[1:]))
    # The kept bar per session is the later 13:30 UTC one.
    closes_by_time = {c["time"]: c["close"] for c in payload["candles"]}
    assert closes_by_time["2024-07-01"] == 10.5
    assert closes_by_time["2024-07-02"] == 11.0


def test_action_marker_stays_on_its_session_date() -> None:
    """13:30 UTC is closer to the next midnight than to its own session open."""
    ts = pd.DatetimeIndex([
        datetime(2026, 9, 14, 13, 30, tzinfo=timezone.utc),
        datetime(2026, 9, 15, 13, 30, tzinfo=timezone.utc),
        datetime(2026, 9, 16, 13, 30, tzinfo=timezone.utc),
    ])
    closes = [1.725, 1.17, 1.24]
    df = pd.DataFrame({
        "open": closes, "high": [c + 0.1 for c in closes],
        "low": [c - 0.1 for c in closes], "close": closes,
        "volume": [1_000_000] * 3,
    }, index=ts)
    payload = build_trade_viewer_payload(
        df, symbol="CNTB", action="SELL", session_tz="America/New_York",
        entry=1.725, entry_time="2026-09-14T13:30:00+00:00",
        exit_price=1.2285, exit_time="2026-09-16T13:30:00+00:00",
        exit_reason="trailing_stop",
    )
    texts = {m["text"]: m["time"] for m in payload["markers"]}
    assert texts["SELL"] == "2026-09-14"
    assert texts["trailing_stop"] == "2026-09-16"


def test_pattern_start_is_a_yellow_marker_on_the_first_anchor() -> None:
    from patterns.base_pattern import ann_marker, ann_segment
    idx = pd.bdate_range("2024-01-02", periods=30)
    df = pd.DataFrame({
        "open": 10.0, "high": 11.0, "low": 9.0, "close": 10.0, "volume": 1000,
    }, index=idx)
    start = str(idx[4].date())
    payload = build_trade_viewer_payload(
        df, symbol="TEST", pattern="pattern_010_pennant", action="SELL",
        annotations=[
            ann_marker(start, 10.0, "pole start", "#ffeb3b"),
            ann_marker(str(idx[8].date()), 12.0, "pole", "#ffeb3b"),
            ann_segment(start, str(idx[8].date()), 10.0, 12.0, "#ffeb3b", label="Pole"),
            ann_marker(str(idx[14].date()), 9.0, "entry", "#ffeb3b"),
        ],
        entry_time=idx[14],
    )
    starts = [m for m in payload["markers"] if str(m["text"]).startswith("Pattern start")]
    assert len(starts) == 1
    assert starts[0]["time"] == start
    assert starts[0]["color"] == "#ffeb3b"
    assert starts[0]["price"] == 10.0
    assert payload["pattern_note"] == f"Yellow: detected pattern · started {start}"


def test_pattern_geometry_keeps_old_trade_in_view():
    from patterns.base_pattern import ann_segment, ann_marker, ann_hline
    idx = pd.bdate_range("2020-01-01", periods=800)
    df = pd.DataFrame({"open": 100., "high": 110., "low": 90., "close": 101., "volume": 1000}, index=idx)
    payload = build_trade_viewer_payload(
        df, symbol="TEST", action="BUY", entry_time=idx[50], exit_time=idx[60],
        annotations=[
            ann_segment(str(idx[10].date()), str(idx[50].date()), 95, 105, "#ff9800"),
            ann_marker(str(idx[10].date()), 95, "L1", "#26a69a"),
            ann_hline(105, "neckline", "#ff9800"),
        ],
    )
    assert payload["segments"][0]["data"] == [
        {"time": str(idx[10].date()), "value": 95.},
        {"time": str(idx[50].date()), "value": 105.},
    ]
    assert payload["candles"][0]["time"] <= str(idx[10].date())
    assert payload["candles"][-1]["time"] == str(idx[80].date())
    assert any(m["text"] == "First bottom (L1)" for m in payload["markers"])
    assert any(l["title"] == "Neckline" for l in payload["levels"])


def test_trade_preserves_annotations_through_ledger_roundtrip():
    from core.backtester import _open_trade
    from core.paper_trader import _trade_to_dict, _trade_from_dict
    from data.tv_client import OHLCVCandle
    from patterns.base_pattern import TradeSignal, ann_segment
    annotations = [ann_segment("2024-01-01", "2024-01-10", 10, 12, "#ff9800")]
    signal = TradeSignal(symbol="TEST", action="BUY", pattern="test", timeframe="1d", confidence=1, price=12, qty=1, chart_annotations=annotations)
    candle = OHLCVCandle(open=12, high=13, low=11, close=12, volume=100, timestamp=datetime(2024, 1, 10))
    trade = _open_trade(signal, candle, 10)
    assert _trade_from_dict(_trade_to_dict(trade)).chart_annotations == annotations
    signal.chart_annotations[0]["start_price"] = 99
    assert trade.chart_annotations[0]["start_price"] == 10


def test_legacy_trade_without_anchors_does_not_redetect():
    idx = pd.bdate_range("2024-01-01", periods=40)
    df = pd.DataFrame({"Open": 10, "High": 11, "Low": 9, "Close": 10, "Volume": 100}, index=idx)
    payload = build_trade_viewer_payload(df, symbol="TEST", pattern="legacy", entry_time=idx[20])
    assert payload['segments'] == []


def test_desktop_pattern_segment_interpolates_when_panned():
    from ui.tv_chart import TradingViewChart
    from unittest.mock import Mock
    chart = object.__new__(TradingViewChart)
    chart._canvas = Mock()
    chart._candles = [{"time": str(i)} for i in range(10)]
    chart._payload = {"segments": [{"data": [{"time": "0", "value": 10}, {"time": "9", "value": 19}], "color": "#ff9800"}]}
    chart._start = 3
    chart._visible = 4
    chart._plot = (0, 0, 400, 100)
    chart._price_lo, chart._price_hi = 10, 20
    chart._draw_pattern_segments(chart._candles[3:7])
    args = chart._canvas.create_line.call_args.args
    assert args == (50, 70, 350, 40)


@pytest.mark.parametrize('timeframe,freq', [('1d', 'B'), ('1W', 'W-FRI')])
def test_dii_precrop_alignment_and_future_causality(timeframe, freq):
    from analysis.indicator_engine import IndicatorEngine
    idx = pd.date_range('2018-01-01', periods=400, freq=freq)
    values = np.arange(400.) + 100
    df = pd.DataFrame(dict(open=values, high=values+1, low=values-1, close=values, volume=100), index=idx)
    df.loc[idx[250], 'high'] = np.inf
    original = df.copy()
    payload = build_trade_viewer_payload(df, symbol='TEST', timeframe=timeframe)
    direct = IndicatorEngine(df).directional_imbalance_index()
    assert payload['dii']['length'] == 10 and payload['dii']['period'] == 70
    for field, series in direct.items():
        points = payload['dii'][field]
        assert [row['time'] for row in points] == [row['time'] for row in payload['candles']]
        for point in points:
            expected = series.iloc[idx.get_loc(pd.Timestamp(point['time']))]
            if np.isfinite(expected): assert point['value'] == expected
            else: assert 'value' not in point
    assert 'value' in payload['dii']['bulls'][0]  # warmup precedes visible crop
    prefix = build_trade_viewer_payload(df.iloc[:350], symbol='TEST', timeframe=timeframe)
    for field in direct:
        full = {row['time']: row for row in payload['dii'][field]}
        assert all(row == full[row['time']] for row in prefix['dii'][field] if row['time'] in full)
    import json
    json.dumps(payload, allow_nan=False)
    pd.testing.assert_frame_equal(df, original)


def test_dii_session_dedup_prefers_valid_reprint():
    idx = pd.bdate_range('2024-01-01', periods=120)
    df = pd.DataFrame(dict(open=10., high=11., low=9., close=10., volume=100), index=idx)
    bad = df.iloc[[50]].copy()
    bad.index += pd.Timedelta(hours=4)
    bad['high'] = np.nan
    actual = build_trade_viewer_payload(pd.concat([df, bad]), symbol='TEST')
    assert actual['dii'] == build_trade_viewer_payload(df, symbol='TEST')['dii']
