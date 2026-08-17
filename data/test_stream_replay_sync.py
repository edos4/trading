from __future__ import annotations

from pathlib import Path

from data.stream_server import StreamServer, _SymbolTape


def test_stream_advance_moves_all_loaded_tapes_atomically(tmp_path: Path):
    server = StreamServer(base_dir=str(tmp_path))
    rows_a = [
        {"open": 1, "high": 1, "low": 1, "close": 1, "volume": 1, "timestamp": 1},
        {"open": 2, "high": 2, "low": 2, "close": 2, "volume": 1, "timestamp": 2},
    ]
    rows_b = [
        {"open": 10, "high": 10, "low": 10, "close": 10, "volume": 1, "timestamp": 1},
        {"open": 20, "high": 20, "low": 20, "close": 20, "volume": 1, "timestamp": 2},
    ]
    server._tapes = {"A": _SymbolTape(rows_a), "B": _SymbolTape(rows_b)}
    assert server._tapes["A"].snapshot()["candle"]["timestamp"] == 1
    assert server._tapes["B"].snapshot()["candle"]["timestamp"] == 1
    server.advance()
    assert server._tapes["A"].snapshot()["candle"]["timestamp"] == 2
    assert server._tapes["B"].snapshot()["candle"]["timestamp"] == 2
