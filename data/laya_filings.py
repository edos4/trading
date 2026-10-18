"""Filing-text evidence for Laya event review (the profitable loop).

A text decision model only earns its keep on real language. For US swing
trading, the language that predicts losses is event text: offerings,
dilution, adverse disclosures. EDGAR supplies it free with verifiable
timestamps, which is exactly what causal historical experiments need:

- ``acceptanceDateTime`` is the availability time: a filing is eligible only
  if accepted no later than the decision cutoff (publication date alone is
  insufficient where ingestion was delayed or the document was revised).
- Each accession number is a stored revision: a corrected filing cannot
  replace the version originally observed.

Forms watched (offering/dilution/adverse surface):

- S-1/S-3/S-4/F-1/F-3 — registration statements (incoming supply).
- 424B1–424B5 — pricing prospectuses (the dilution terms themselves).
- 8-K items 1.01/1.02/2.02/3.02/7.01/8.01 — entry agreements, termination,
  results, unregistered sales, Reg-FD, other material events.
- 6-K — foreign private issuer material events (no item codes; take all).
- 144 — notices of proposed insider sale.

Document URLs follow the Archives pattern::

    https://www.sec.gov/Archives/edgar/data/{CIK}/{ACCESSION}/{PRIMARY_DOC}

where ACCESSION has its dashes stripped. SEC throttle: max 10 requests per
second; document fetches are paced. A descriptive User-Agent is required.
"""
from __future__ import annotations

import html as _html
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from html.parser import HTMLParser

from data.edgar_client import EdgarClient
from utils.logger import log

_ARCHIVES_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{doc}"
FETCH_TIMEOUT = 20
_FETCH_PACING_S = 0.2

# Registration + pricing + material-event surface. 10-K/10-Q/4/3/5 and
# beneficial-ownership filings are deliberately excluded: they are routine
# or backward-looking, and every extra form is triage noise.
_OFFERING_FORMS = frozenset({
    "S-1", "S-3", "S-4", "F-1", "F-3",
    "424B1", "424B2", "424B3", "424B4", "424B5",
    "144",
})
_EVENT_ITEMS = frozenset({"1.01", "1.02", "2.02", "3.02", "7.01", "8.01"})

# Sentences carrying these stems are excerpted first: with a ~300-token model
# budget, the offering terms must survive, not page-1 boilerplate.
_OFFERING_HINTS = (
    "offer", "share", "dilut", "proceed", "underwrit", "warrant",
    "convertible", "at-the-market", "atm offering", "purchase agreement",
    "subscription", "private placement", "registered direct", "pricing",
)

# ~250 tokens, leaving room for the assessment wrapper inside the worker's
# 350-token state budget. Over-budget input is flagged truncated and becomes
# gate-ineligible, so the excerpt must fit with margin, not exactly.
EXCERPT_CHARS = 1000


class _TextStripper(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in ("script", "style", "noscript"):
            self._skip += 1
        elif tag in ("p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4"):
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style", "noscript") and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self._parts.append(data)

    def text(self) -> str:
        blob = "".join(self._parts)
        blob = re.sub(r"[ \t\xa0]+", " ", blob)
        blob = re.sub(r"\n{3,}", "\n\n", blob)
        return _html.unescape(blob).strip()


def strip_html(raw: bytes | str) -> str:
    if isinstance(raw, bytes):
        try:
            text = raw.decode("utf-8", errors="replace")
        except Exception:
            return ""
    else:
        text = raw
    if "<" not in text:
        return re.sub(r"\s+", " ", text).strip()
    stripper = _TextStripper()
    try:
        stripper.feed(text[:2_000_000])  # bound pathological filings
    except Exception:
        return ""
    return stripper.text()


def _sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9(])", text)
    return [p.strip() for p in parts if len(p.strip()) > 40]


def excerpt_offering(text: str, budget: int = EXCERPT_CHARS,
                     head_chars: int = 600) -> tuple[str, bool]:
    """Deterministic excerpt: document head first, then offering-hint sentences.

    Prospectuses state the offering (issuer, security, size, price) in the
    opening summary; the hint sentences that follow carry the terms. Head-first
    ordering matters: hint-greedy excerpts over-sample risk-factor boilerplate
    that mentions offerings without one happening, which flips classifications.
    Returns (excerpt, shortened). The full document hash is retained by the
    caller; a shortened excerpt is recorded, never silent.
    """
    clean = re.sub(r"\s+", " ", text).strip()
    if len(clean) <= budget:
        return clean, False
    head = clean[:head_chars].rstrip()
    picked: list[str] = [head]
    used = len(head)
    for sent in _sentences(clean[head_chars:]):
        lowered = sent.lower()
        if not any(h in lowered for h in _OFFERING_HINTS):
            continue
        if sent in head:
            continue
        if len(sent) > budget - used - 5:
            continue  # skip one monster sentence; shorter ones may still fit
        picked.append(sent)
        used += len(sent) + 5
        if used >= budget:
            break
    excerpt = "\n...\n".join(picked)[:budget]
    return excerpt, True


def _doc_url(cik: int, accession: str, doc: str) -> str:
    return _ARCHIVES_URL.format(cik=cik, acc=accession.replace("-", ""), doc=doc)


def relevant_filings(
    symbol: str,
    *,
    cutoff: datetime | None = None,
    lookback_days: int = 30,
    client: EdgarClient | None = None,
) -> list[dict]:
    """Offering/adverse-relevant filings accepted in (cutoff - lookback, cutoff].

    Only filings with a verifiable ``acceptanceDateTime`` no later than the
    cutoff are returned. Amendments (``/A`` forms) match their base form.
    """
    from data.edgar_client import skip_edgar_enabled

    if skip_edgar_enabled():
        return []
    sym = (symbol or "").upper().strip()
    if not sym:
        return []
    now = cutoff or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    client = client or EdgarClient()
    cik = client._cik_for_ticker(sym)
    if cik is None:
        return []
    try:
        payload = client._get_json(
            f"https://data.sec.gov/submissions/CIK{cik:010d}.json")
    except Exception as exc:  # noqa: BLE001 - no text is valid evidence: none
        log.warning(f"Laya filings | submissions fetch failed for {sym}: {exc}")
        return []
    recent = (payload.get("filings") or {}).get("recent") or {}
    forms = recent.get("form") or []
    filed = recent.get("filingDate") or []
    accepted = recent.get("acceptanceDateTime") or []
    accessions = recent.get("accessionNumber") or []
    docs = recent.get("primaryDocument") or []
    items = recent.get("items") or []
    descs = recent.get("primaryDocDescription") or []
    out: list[dict] = []
    for i, form in enumerate(forms):
        base = str(form or "").removesuffix("/A")
        if base in _OFFERING_FORMS:
            keep, why = True, "offering-form"
        elif base in ("8-K", "6-K"):
            if base == "6-K":
                keep, why = True, "fpi-material"
            else:
                row_items = (items[i] if i < len(items) else "") or ""
                codes = {c.strip() for c in row_items.split(",") if c.strip()}
                keep = bool(codes & _EVENT_ITEMS)
                why = "8k-event-item"
        else:
            continue
        if not keep:
            continue
        acc_ts = None
        try:
            raw_ts = accepted[i] if i < len(accepted) else None
            acc_ts = datetime.fromisoformat(str(raw_ts).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            continue
        if acc_ts > now:
            continue  # not yet available at the cutoff
        age_days = (now - acc_ts).days
        if age_days > lookback_days:
            continue
        accession = accessions[i] if i < len(accessions) else ""
        doc = docs[i] if i < len(docs) else ""
        if not accession or not doc:
            continue
        out.append({
            "symbol": sym, "form": str(form),
            "filed": filed[i] if i < len(filed) else "",
            "accepted_at": acc_ts.isoformat(),
            "accession": accession, "revision_id": accession,
            "description": descs[i] if i < len(descs) else "",
            "doc_url": _doc_url(cik, accession, doc),
            "match": why,
        })
    out.sort(key=lambda r: r["accepted_at"])
    return out


def fetch_filing_text(doc_url: str, *, user_agent: str | None = None,
                      timeout: int = FETCH_TIMEOUT) -> str:
    """Fetch one filing document and strip it to text. Empty on any failure."""
    from data.edgar_client import _resolve_user_agent

    req = urllib.request.Request(
        doc_url,
        headers={"User-Agent": _resolve_user_agent(user_agent),
                 "Accept": "text/html,application/xhtml+xml,text/plain"},
    )
    time.sleep(_FETCH_PACING_S)  # SEC throttle courtesy
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            raw = resp.read()
    except Exception as exc:  # noqa: BLE001 - fetch failure means no evidence
        log.warning(f"Laya filings | document fetch failed: {exc}")
        return ""
    return strip_html(raw)
