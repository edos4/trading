"""Cross-process ownership of a paper ledger and its replay cursor."""
from __future__ import annotations

from contextlib import ExitStack
import fcntl
from functools import wraps
import hashlib
import inspect
from pathlib import Path
import tempfile


class PaperBusy(RuntimeError):
    """Another controller owns the requested paper resource."""


class PaperLease:
    def __init__(self, resource: str):
        self.resource=resource
        key=hashlib.sha256(resource.encode()).hexdigest()
        self.path=Path(tempfile.gettempdir())/f'trading-paper-{key}.lock'
        self.handle=None

    def __enter__(self):
        self.handle=self.path.open('a+')
        try:
            fcntl.flock(self.handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError as exc:
            self.handle.close(); self.handle=None
            raise PaperBusy(f'Paper resource already owned: {self.resource}') from exc
        return self

    def __exit__(self,*args):
        if self.handle is not None:
            self.handle.close(); self.handle=None


def _leases(market: str | None, stream: bool) -> ExitStack:
    from core.market import get_market
    from config import settings
    stack=ExitStack()
    try:
        stack.enter_context(PaperLease('ledger:'+str(get_market(market).paper_account_path.resolve())))
        if stream:
            stack.enter_context(PaperLease(f'stream:{settings.papertrade_stream_host}:{settings.papertrade_stream_port}'))
    except BaseException:
        stack.close()
        raise
    return stack


def exclusive_paper_run(fn):
    """Acquire before reset/load, and release only after final save/export."""
    @wraps(fn)
    async def wrapped(*args,**kwargs):
        options=inspect.signature(fn).bind_partial(*args,**kwargs).arguments
        with _leases(options.get('market'),options.get('use_stream',False)):
            return await fn(*args,**kwargs)
    return wrapped


def exclusive_paper_book(fn):
    """Use the same ownership protocol for UI threads and reset actions."""
    @wraps(fn)
    def wrapped(self,*args,**kwargs):
        options=inspect.signature(fn).bind_partial(self,*args,**kwargs).arguments
        try:
            with _leases(self.market,options.get('use_stream',False)):
                return fn(self,*args,**kwargs)
        except PaperBusy as exc:
            with self.lock:
                self.error=self.status=str(exc)
            return str(exc)
    return wrapped
