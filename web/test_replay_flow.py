"""Export/upload/reload/chart regression without a live server or account."""
import asyncio
import json

import httpx
import pandas as pd

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
            assert loaded == json.loads(json.dumps(export))
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
                assert payload['segments'][0]['data'] == [{'time': '2024-01-02', 'value': 10.}, {'time': '2024-01-12', 'value': 12.}]
                assert any(m['time'] == '2024-01-12' and m['text'] == 'BUY' for m in payload['markers'])
    asyncio.run(run())
