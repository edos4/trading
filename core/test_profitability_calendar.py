"""Sparse tapes may not disappear from the replay calendar."""
from __future__ import annotations

import pytest

from data.stream_server import StreamServer, _SymbolTape


@pytest.mark.parametrize('days_a,days_b,expected', [
    ([1,3],[2,3],[2,3]), ([1],[2,3],[2,3]), ([1,4],[2,3,4],[2,3,4]),
])
def test_advance_uses_union_including_tapes_missing_current_bar(days_a,days_b,expected):
    server=StreamServer(market='us')
    try:
        server._tapes={name:_SymbolTape([{'timestamp':i} for i in days])
                       for name,days in [('A',days_a),('B',days_b)]}
        server._asof_ts=1
        for day in expected:
            assert server.advance()>0
            assert server._asof_ts==day
        assert server.advance()==0
        assert server._asof_ts==expected[-1]
    finally:
        server._load_pool.shutdown(wait=True)
