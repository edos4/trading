"""Unit tests for PSE Edge history fallback (Yahoo *.PS is a YHD stub)."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from data import pse_edge, tv_client
from data.pse_edge import _parse_chart_date, _parse_directory_html
from data.tv_client import TVClient, _candles_from_yahoo_payload
import pandas as pd


BDO_DIR_HTML = """
<td><a href="#company" onclick="cmDetail('260','468');return false;">BDO Unibank, Inc.</a></td>
<td class="alignC"><a href="#company" onclick="cmDetail('260','468');return false;">BDO</a></td>
<td class="alignC"><a href="#company" onclick="cmDetail('111','222');return false;">SMPH</a></td>
"""

YHD_STUB = {
    "chart": {
        "result": [
            {
                "meta": {"exchangeName": "YHD", "symbol": "BDO.PS"},
                "indicators": {"quote": [{}]},
            }
        ],
        "error": None,
    }
}


def test_parse_directory_picks_exact_ticker():
    found = _parse_directory_html(BDO_DIR_HTML)
    assert found["BDO"] == ("260", "468")
    assert found["SMPH"] == ("111", "222")
    assert "BDO UNIBANK" not in found


def test_parse_chart_date_manila():
    ts = _parse_chart_date("Aug 12, 2026 00:00:00")
    assert ts is not None
    assert ts.tzinfo == ZoneInfo("Asia/Manila")
    assert ts.date().isoformat() == "2026-08-12"


def test_yahoo_yhd_stub_is_empty():
    assert _candles_from_yahoo_payload(YHD_STUB, "BDO.PS", "1d") == []
    assert _candles_from_yahoo_payload(None, "BDO.PS", "1d") == []
    assert _candles_from_yahoo_payload({"chart": {"result": None}}, "BDO.PS", "1d") == []


def test_ph_chart_uses_edge_not_yahoo():
    edge_bar = tv_client.OHLCVCandle(
        125.0, 127.0, 124.0, 126.0, 1_000_000.0,
        timestamp=datetime(2026, 8, 12, tzinfo=ZoneInfo("Asia/Manila")),
    )
    client = TVClient(screener="philippines", exchange="PSE")
    with (
        patch.object(tv_client.urllib.request, "urlopen") as yahoo,
        patch("data.pse_edge.fetch_history", return_value=[edge_bar]) as edge,
    ):
        out = client._fetch_history_chart("BDO", "1d")

    yahoo.assert_not_called()
    edge.assert_called_once()
    assert len(out) == 1
    assert out[0].close == 126.0


def test_us_chart_does_not_call_edge():
    client = TVClient(screener="america", exchange="NASDAQ")

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return json.dumps(YHD_STUB).encode()

    with (
        patch.object(tv_client.urllib.request, "urlopen", return_value=_Resp()),
        patch("data.pse_edge.fetch_history") as edge,
    ):
        out = client._fetch_history_chart("AAPL", "1d")

    edge.assert_not_called()
    assert out == []


def test_fetch_daily_parses_chart_payload(tmp_path: Path, monkeypatch):
    pse_edge.reset_for_tests()
    monkeypatch.setattr(pse_edge, "_DIR_CACHE", tmp_path / "dir.json")
    monkeypatch.setattr(pse_edge, "_HIST_CACHE_DIR", tmp_path / "ohlcv")
    monkeypatch.setattr(pse_edge, "_MIN_INTERVAL_SECONDS", 0.0)

    calls = []

    def fake_request(url, *, data, content_type, referer, timeout=30):
        calls.append(url)
        if "search.ax" in url:
            return BDO_DIR_HTML.encode()
        payload = {
            "chartData": [
                {
                    "OPEN": 129.6,
                    "HIGH": 129.9,
                    "LOW": 126.7,
                    "CLOSE": 127.8,
                    "VALUE": 317972124.0,
                    "CHART_DATE": "Jan 02, 2024 00:00:00",
                }
            ]
        }
        return json.dumps(payload).encode()

    monkeypatch.setattr(pse_edge, "_request", fake_request)
    candles = pse_edge.fetch_daily("BDO")
    assert len(candles) == 1
    assert candles[0].close == 127.8
    assert abs(candles[0].volume - (317972124.0 / 127.8)) < 1e-6
    assert tmp_path.joinpath("dir.json").exists()


def test_fetch_directory_paginates(tmp_path: Path, monkeypatch):
    pse_edge.reset_for_tests()
    monkeypatch.setattr(pse_edge, "_DIR_CACHE", tmp_path / "dir.json")
    monkeypatch.setattr(pse_edge, "_MIN_INTERVAL_SECONDS", 0.0)
    pages = []

    def fake_request(url, *, data, content_type, referer, timeout=30):
        pages.append(data.decode())
        if len(pages) == 1:
            return BDO_DIR_HTML.encode()
        return b"<table></table>"

    monkeypatch.setattr(pse_edge, "_request", fake_request)
    mapping = pse_edge.fetch_directory(force=True)
    assert mapping["BDO"] == ("260", "468")
    assert mapping["SMPH"] == ("111", "222")
    assert len(pages) >= 2


def test_fetch_daily_chunked_merges_years(tmp_path: Path, monkeypatch):
    pse_edge.reset_for_tests()
    monkeypatch.setattr(pse_edge, "_DIR_CACHE", tmp_path / "dir.json")
    monkeypatch.setattr(pse_edge, "_HIST_CACHE_DIR", tmp_path / "ohlcv")
    monkeypatch.setattr(pse_edge, "_MIN_INTERVAL_SECONDS", 0.0)

    def fake_request(url, *, data, content_type, referer, timeout=30):
        if "search.ax" in url:
            return BDO_DIR_HTML.encode()
        payload = data.decode()
        year = "2024" if "01-01-2024" in payload or "2024" in payload else "2025"
        # startDate is mm-dd-YYYY
        if '"startDate": "01-01-2024"' in payload or "01-01-2024" in payload:
            chart_date = "Jan 02, 2024 00:00:00"
            close = 100.0
        else:
            chart_date = "Jan 02, 2025 00:00:00"
            close = 110.0
        return json.dumps(
            {
                "chartData": [
                    {
                        "OPEN": close,
                        "HIGH": close,
                        "LOW": close,
                        "CLOSE": close,
                        "VALUE": close * 1000,
                        "CHART_DATE": chart_date,
                    }
                ]
            }
        ).encode()

    monkeypatch.setattr(pse_edge, "_request", fake_request)
    candles = pse_edge.fetch_daily_chunked(
        "BDO", start_year=2024, end=datetime(2025, 1, 3).date(),
    )
    assert len(candles) == 2
    assert [c.close for c in candles] == [100.0, 110.0]


def test_ph_peso_adv_ranks_banks_over_pennies():
    df = pd.DataFrame(
        {
            "name": ["C", "BDO", "LC"],
            "exchange": ["PSE", "PSE", "PSE"],
            "close": [0.22, 126.0, 0.222],
            "volume": [99_510_000, 1_111_670, 99_510_000],
            "Value.Traded": [220_000, 140_070_420, 22_091_220],
            "average_volume_10d_calc": [80_000_000, 2_498_595, 81_152_000],
        }
    )
    adv = tv_client._ph_peso_adv(df)
    ranked = df.assign(_adv=adv).sort_values("_adv", ascending=False)
    assert list(ranked["name"]) == ["BDO", "LC", "C"]
    assert float(ranked.iloc[0]["_adv"]) >= 5_000_000


def test_ph_universe_drops_share_volume_pennies(monkeypatch):
    df = pd.DataFrame(
        {
            "name": ["C", "VVT", "BDO", "SMPH", "ICT"],
            "exchange": ["PSE", "PSE", "PSE", "PSE", "PSE"],
            "close": [0.22, 0.01, 126.0, 18.2, 965.0],
            "volume": [99_000_000, 80_000_000, 1_111_670, 21_021_400, 548_830],
            "Value.Traded": [200_000, 50_000, 140_070_420, 382_589_480, 529_620_950],
            "average_volume_10d_calc": [80e6, 70e6, 2.5e6, 8.1e6, 1.3e6],
        }
    )
    monkeypatch.setattr(
        tv_client, "_get_scanner_data", lambda _q: (len(df), df.copy())
    )
    rows = TVClient.fetch_top_symbols_with_exchanges(
        3, "philippines", min_value=5_000_000, exchange="PSE"
    )
    names = [s for s, _ex in rows]
    assert names[0] in {"ICT", "BDO", "SMPH"}
    assert "C" not in names and "VVT" not in names
