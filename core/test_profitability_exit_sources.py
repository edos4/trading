"""Distinguish pattern orders from the independent paper safety overlay."""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.usefixtures("confirmed_buy_dii")
from core.test_position_protection import book_with_position
from core.test_patternsv2_exits import candle


@pytest.mark.parametrize('short',[False,True])
@pytest.mark.parametrize('tighter_pattern',[False,True])
def test_exit_evidence_identifies_the_binding_order(short,tighter_pattern):
    book,p=book_with_position(short,fee=.001,slip=0)
    if tighter_pattern:
        p.stop_loss=102 if short else 98
    closing=candle(100,105,95,100,1)
    assert book.on_bar('TEST',closing,'1d')==[p]
    source=p.position_marks[-1]['exit_check']['trigger_source']
    assert source==('pattern_hard_stop' if tighter_pattern else 'paper_protection')
    assert 'combined_exit_policy' in book.snapshot_metrics()['execution_context']
