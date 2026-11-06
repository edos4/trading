"""Immutable source identity for one scanner run; never reads operator secrets."""
from __future__ import annotations

import hashlib
from pathlib import Path
import platform
from uuid import uuid4


def build_run_manifest(*, patterns_v2: bool, market: str) -> dict:
    root=Path(__file__).resolve().parent.parent
    sources=[root/'main.py',root/'config.py']
    sources += sorted((root/'core').glob('*.py'))
    sources += [root/'data/stream_client.py',root/'data/stream_server.py',root/'utils/trade_export.py']
    if patterns_v2:
        from patterns_v2.loader import detector_root
        sources += sorted((detector_root()/'patterns').glob('*.py'))
        sources += [root/'patterns_v2/signals.py',root/'patterns_v2/loader.py']
    hashes={str(p.relative_to(root)) if p.is_relative_to(root) else str(p):
            hashlib.sha256(p.read_bytes()).hexdigest() for p in sources}
    return {'schema_version':1,'run_id':uuid4().hex,'market':market,
            'python':platform.python_version(),'source_sha256':hashes,
            'scope':'sources at scanner construction; historical trades retain their own evidence'}
