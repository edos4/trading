"""Concurrent paper controllers cannot reset or overwrite each other's book."""
from __future__ import annotations

import asyncio
import pytest
from core.paper_ownership import PaperLease, PaperBusy, exclusive_paper_run


def test_competing_lease_fails_and_owner_release_allows_retry(tmp_path):
    resource=str(tmp_path/'book')
    with PaperLease(resource):
        with pytest.raises(PaperBusy):
            with PaperLease(resource):
                pytest.fail('a second owner was admitted')
    with PaperLease(resource):
        pass


def test_cli_lease_precedes_any_mutation():
    from core.market import get_market
    entered=[]
    @exclusive_paper_run
    async def run(*,market='us',use_stream=False):
        entered.append('mutated')
    resource='ledger:'+str(get_market('us').paper_account_path.resolve())
    with PaperLease(resource):
        with pytest.raises(PaperBusy):
            asyncio.run(run(market='us'))
    assert not entered
    asyncio.run(run(market='us'))
    assert entered==['mutated']


def test_incompatible_port_is_never_killed(monkeypatch):
    from core.paper_books import PaperBook
    book=PaperBook('us')
    monkeypatch.setattr(book,'_port_open',lambda *args:True)
    monkeypatch.setattr(book,'_probe_stream_status',lambda: {'market':'ph','start_date':'other'})
    monkeypatch.setattr(book,'_kill_whatever_is_on',lambda *args: pytest.fail('killed another process'))
    assert 'already occupied' in book._ensure_stream_server(start_date='2025-11-03')
