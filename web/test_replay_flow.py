"""Export/upload/reload/chart regression without a live server or account."""
import asyncio
import json
import math
from copy import deepcopy

import httpx
import pandas as pd
import pytest

from utils.trade_export import build_paper_trade_export


def test_export_replay_roundtrip_and_chart(monkeypatch, tmp_path):
    from web import replay_store
    from web.app import create_app
    from web.auth import require_login

    annotations = [{"type": "segment", "start_date": "2024-01-02", "end_date": "2024-01-12", "start_price": 10., "end_price": 12., "color": "#ff9800"}]
    trade = dict(market='us', symbol='TEST', action='BUY', pattern='test', timeframe='1d', entry=12., stop=9., target=15., current=13., exit=14., reason='take_profit', opened='2026-09-10T00:00:00+00:00', sim_opened='2024-01-12T00:00:00+00:00', sim_closed='2024-01-19T00:00:00+00:00', chart_annotations=annotations)
    export = build_paper_trade_export({'books': {'us': dict(market='us', positions=[trade], closed=[trade])}}, market='us')
    monkeypatch.setattr(replay_store, 'REPLAY_PATH', tmp_path / 'replay.json')
    idx = pd.bdate_range('2024-01-01', periods=40)
    df = pd.DataFrame(dict(open=11., high=14., low=9., close=12., volume=1000), index=idx)
    monkeypatch.setattr('data.history.load_daily_ohlcv_df', lambda *a, **kw: df)
    # Keep mocked I/O inline: sandboxed event loops cannot wake from worker threads.
    async def inline_io(fn, *args, **kwargs):
        return fn(*args, **kwargs)
    monkeypatch.setattr(asyncio, 'to_thread', inline_io)
    app = create_app()
    async def logged_in():
        return 'test'
    app.dependency_overrides[require_login] = logged_in

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            assert (await client.post('/api/replay/upload', json=export)).status_code == 200
            loaded = (await client.get('/api/replay/load')).json()['replay']
            replay_id = loaded.pop('correction_replay_id')
            assert replay_id
            assert loaded == json.loads(json.dumps(export))
            assert (await client.get('/api/replay/load')).json()['replay']['correction_replay_id'] == replay_id
            uploaded = await client.post('/api/replay/upload', json=export)
            assert uploaded.json()['replay_id'] != replay_id
            for side, key in [('open', 'open_positions'), ('closed', 'closed_trades')]:
                row = loaded['books'][0][key][0]
                response = await client.post('/api/replay/chart', json={
                    'market': row['market'], 'symbol': row['symbol'], 'side': side,
                    'action': row['action'], 'pattern': row['pattern'],
                    'entry': row['entry'], 'entry_time': row['sim_opened'],
                    'exit': row.get('exit'), 'exit_time': row.get('sim_closed'),
                    'chart_annotations': row['chart_annotations'],
                })
                assert response.status_code == 200, response.text
                payload = response.json()
                assert len(payload['candles']) > 2
                assert payload['replay_cutoff'] == row['sim_opened']
                assert payload['candles'][-1]['time'] > row['sim_opened'][:10]
                assert payload['segments'][0]['data'] == [{'time': '2024-01-02', 'value': 10.}, {'time': '2024-01-12', 'value': 12.}]
                assert any(m['time'] == '2024-01-12' and m['text'] == 'BUY' for m in payload['markers'])
    asyncio.run(run())


def test_weekly_export_replay_chart_draws_weekly_candles_and_rails(monkeypatch):
    from web.app import create_app
    from web.auth import require_login

    idx = (pd.bdate_range("2024-01-01", periods=200, tz="America/New_York")
           + pd.Timedelta(hours=9, minutes=30)).tz_convert("UTC")
    close = [100 + (i // 5) * .25 + math.sin((i // 5) / 2) * 3 + (i % 5) * .04
             for i in range(200)]
    df = pd.DataFrame({
        "open": [c - .4 for c in close], "high": [c + .6 for c in close],
        "low": [c - .6 for c in close], "close": close, "volume": 1000,
    }, index=idx)
    annotations = [
        {"type": "segment", "start_date": "2024-01-04", "end_date": "2024-01-11",
         "start_price": 99., "end_price": 100., "label": "Saved weekly segment"},
        {"type": "path", "label": "Saved weekly path", "points": [
            {"date": "2024-01-04", "price": 99.}, {"date": "2024-01-11", "price": 101.},
            {"date": "2024-01-18", "price": 100.},
        ]},
        {"type": "marker", "date": "2024-01-11", "price": 101., "label": "Saved weekly pivot"},
        {"type": "marker", "date": "2023-12-28", "price": 999., "label": "Outside history"},
        {"type": "segment", "start_date": "2023-12-28", "end_date": "2024-01-11",
         "start_price": 999., "end_price": 100., "label": "Missing weekly anchor"},
    ]
    original_annotations = deepcopy(annotations)
    trade = dict(market="us", symbol="TEST", action="BUY", pattern="test", timeframe="1W",
                 entry=close[30], sim_opened=idx[30].isoformat(), chart_annotations=annotations)
    exported = build_paper_trade_export({"books": {"us": dict(market="us", positions=[trade])}}, market="us")
    row = exported["books"][0]["open_positions"][0]
    assert row["timeframe"] == "1W"
    monkeypatch.setattr("data.history.load_daily_ohlcv_df", lambda *args, **kwargs: df)

    async def inline_io(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", inline_io)
    app = create_app()

    async def logged_in():
        return "test"

    app.dependency_overrides[require_login] = logged_in

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/api/replay/chart", json={
                "market": row["market"], "symbol": row["symbol"], "side": "open",
                "timeframe": row["timeframe"], "action": row["action"],
                "entry": row["entry"], "entry_time": row["sim_opened"],
                "chart_annotations": row["chart_annotations"],
            })
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["timeframe"] == "1W"
        assert len(payload["candles"]) == 40
        assert all(pd.Timestamp(c["time"]).dayofweek == 4 for c in payload["candles"])
        first = payload["candles"][0]
        assert first["time"] == "2024-01-05"
        assert first["open"] == pytest.approx(df.iloc[0]["open"])
        assert first["high"] == pytest.approx(df.iloc[:5]["high"].max())
        assert first["low"] == pytest.approx(df.iloc[:5]["low"].min())
        assert first["close"] == pytest.approx(df.iloc[4]["close"])
        assert payload["volume"][0]["value"] == 5000
        # Monday's trade belongs to its own week's aggregate, rather than the
        # closer Friday of the preceding week. Thursday labels from old saved
        # weekly resampling identify this same bucket without changing prices.
        assert any(m["time"] == "2024-02-16" and m["text"] == "BUY" for m in payload["markers"])
        segments = {segment["label"]: segment["data"] for segment in payload["segments"]}
        assert segments["Saved weekly segment"] == [
            {"time": "2024-01-05", "value": 99.}, {"time": "2024-01-12", "value": 100.},
        ]
        assert segments["Saved weekly path"] == [
            {"time": "2024-01-05", "value": 99.}, {"time": "2024-01-12", "value": 101.},
            {"time": "2024-01-19", "value": 100.},
        ]
        assert any(m["time"] == "2024-01-12" and m["price"] == 101.
                   and m["text"] == "Saved weekly pivot" for m in payload["markers"])
        assert "Missing weekly anchor" not in segments
        assert not any(m["text"] == "Outside history" for m in payload["markers"])
        assert row["chart_annotations"] == original_annotations
        assert annotations == original_annotations
        assert payload["trendlines"]
        dates = {c["time"] for c in payload["candles"]}
        assert all(p["time"] in dates for rail in payload["trendlines"] for p in rail["data"])

    asyncio.run(run())
