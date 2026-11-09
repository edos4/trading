"""The quotation parser keeps exchange no-trade rows out of the freshness denominator."""
from __future__ import annotations

from datetime import date
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from data.pse_quotation import parse_no_trade_symbols, quotation_url


def test_quotation_url_matches_the_published_filename():
    assert quotation_url(date(2026, 11, 6)).endswith('November%2006%2C%202026-EOD.pdf')


def test_no_trade_rows_are_symbols_without_a_printed_ohlc():
    text = """
November 06, 2026
BDO UNIBANK              BDO              124.7  125  121.8  125.2  121.6  125  6,142,890  763,387,393  462,692,974
CITYSTATE BANK           CSB              12.46  13.68  -  -  -  -  -  -  -
FINANCIALS SECTOR TOTAL                                          34,407,115  1,461,996,123
"""
    assert parse_no_trade_symbols(text, date(2026, 11, 6)) == {'CSB.PS'}
    with pytest.raises(ValueError, match='November 07, 2026'):
        parse_no_trade_symbols(text, date(2026, 11, 7))


def test_november_6_report_covers_the_audited_no_trade_list():
    pdf = Path('docs/verification/sources/pse-2026-11-06.pdf')
    report = Path('docs/verification/pse-latest-session-2026-11-06.json')
    if shutil.which('pdftotext') is None or not pdf.exists():
        pytest.skip('quotation text extractor or source report is unavailable')
    text = subprocess.run(['pdftotext', '-layout', str(pdf), '-'], check=True, capture_output=True).stdout.decode()
    found = parse_no_trade_symbols(text, date(2026, 11, 6))
    expected = set(json.loads(report.read_text())['nontraded_report_rows'])
    assert expected <= found
    assert 'BDO.PS' not in found
