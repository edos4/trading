"""Philippine Stock Exchange daily quotation: traded sessions versus no-trade rows."""
from __future__ import annotations

from datetime import date
import re
import subprocess
import tempfile
from urllib.parse import quote
from urllib.request import Request, urlopen

_TICKER = re.compile(r'^[A-Z][A-Z0-9]{0,9}$')
_FIELD = re.compile(r'^(?:-|\(?\d[\d,]*\)?|\d[\d,]*(?:\.\d+)?)$')
_UA = (
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36'
)


def quotation_url(session: date) -> str:
    """Official end-of-day report for one Philippine session."""
    return 'https://documents.pse.com.ph/market_report/' + quote(session.strftime('%B %d, %Y')) + '-EOD.pdf'


def parse_no_trade_symbols(text: str, session: date) -> set[str]:
    """Return canonical symbols whose quotation row has no open, high, low, or close.

    Preferred shares and other printed lines are included. Callers intersect this
    set with the stored universe, so an untracked line does not change coverage.
    """
    stamp = session.strftime('%B %d, %Y')
    if stamp not in text:
        raise ValueError(f'Quotation report is not for {stamp}')
    missing = set()
    for line in text.splitlines():
        tokens = line.split()
        for index, token in enumerate(tokens[:-1]):
            if not _TICKER.fullmatch(token) or not _FIELD.fullmatch(tokens[index + 1]):
                continue
            fields = tokens[index + 1:]
            if len(fields) < 8 or not all(_FIELD.fullmatch(part) for part in fields[:8]):
                continue
            if set(fields[2:6]) == {'-'}:
                missing.add(f'{token}.PS')
            break
    return missing


def _pdftotext(pdf: bytes) -> str:
    with tempfile.NamedTemporaryFile(suffix='.pdf') as handle:
        handle.write(pdf)
        handle.flush()
        completed = subprocess.run(['pdftotext', '-layout', handle.name, '-'], check=True, capture_output=True)
    return completed.stdout.decode('utf-8', errors='replace')


def no_trade_symbols(session: date, *, opener=urlopen, timeout: float = 60) -> set[str]:
    """Download the session report and list symbols that did not trade."""
    request = Request(quotation_url(session), headers={'User-Agent': _UA})
    with opener(request, timeout=timeout) as response:
        pdf = response.read()
    return parse_no_trade_symbols(_pdftotext(pdf), session)
