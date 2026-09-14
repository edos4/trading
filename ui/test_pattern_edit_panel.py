"""Native mapping tests; display integration is a separately configured check."""
from types import SimpleNamespace
from ui.tv_chart import TradingViewChart


def test_edit_mapping_uses_viewport_offset_and_rejects_forecasts():
    chart=object.__new__(TradingViewChart)
    chart._plot=(10,20,210,220)
    chart._start=3;chart._visible=2
    chart._candles=[{'time':str(i)} for i in range(6)]
    chart._candles[4]['predicted']=True
    assert chart._editable_candle(SimpleNamespace(x=20,y=100))['time']=='3'
    assert chart._editable_candle(SimpleNamespace(x=160,y=100)) is None
    assert chart._editable_candle(SimpleNamespace(x=20,y=221)) is None
    assert chart._editable_candle(SimpleNamespace(x=0,y=100)) is None
