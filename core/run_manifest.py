"""Immutable source identity for one scanner run; never reads operator secrets."""
from __future__ import annotations

import hashlib
import json
import math
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import platform
from uuid import uuid4


CONTROL_FIELDS = (
    'short_entries_enabled', 'stop_loss_pct', 'stop_loss_trail_r', 'stop_loss_arm_r',
    'stop_loss_min_risk_frac', 'target_max_r', 'target_risk_basis', 'paper_v2_min_stop_atr',
    'min_signal_price_usd', 'max_open_positions', 'max_open_per_pattern',
    'paper_max_open_per_symbol', 'paper_max_gross_exposure', 'paper_max_volume_participation',
    'paper_require_liquidity_history', 'disabled_patterns_v2', 'vision_confirmation_enabled',
    'vision_min_indicator_confidence', 'kronos_gate_model', 'kronos_gate_sideways_atr',
    'kronos_gate_sideways_min_pct', 'kronos_gate_sideways_max_pct', 'kronos_gate_min_agree',
    'kronos_exit_min_agree',
)


def resolved_controls() -> dict:
    """Explicit whitelist: no environment contents, credentials or DSNs."""
    from config import settings
    return {key: getattr(settings, key) for key in CONTROL_FIELDS}


def record_observation(manifest: dict, symbol: str, timeframe: str, candle,
                       initial_context=None) -> None:
    """Per-feed hash chains compare runs independent of concurrent fetch order."""
    key = f'{symbol}|{timeframe}'
    fingerprints = manifest.setdefault('observation_fingerprints', {})
    prior = fingerprints.get(key)
    def row(bar):
        values = []
        for field in ('open', 'high', 'low', 'close', 'volume'):
            try:
                value = float(getattr(bar, field))
                values.append(value if math.isfinite(value) else repr(value))
            except (TypeError, ValueError):
                values.append(repr(getattr(bar, field)))
        return [bar.timestamp.isoformat(), *values]
    context = [row(bar) for bar in (initial_context or [])
               if bar.timestamp is not None and bar.timestamp.tzinfo is not None
               and bar.timestamp <= candle.timestamp] if prior is None else []
    payload = json.dumps([context, row(candle)], separators=(',', ':'), allow_nan=False).encode()
    fingerprints[key] = {'sha256': hashlib.sha256((prior['sha256'].encode() if prior else b'')+payload).hexdigest(),
                         'bars': prior['bars']+1 if prior else 1,
                         'context_bars': prior['context_bars'] if prior else len(context),
                         'first': prior['first'] if prior else candle.timestamp.isoformat(),
                         'last': candle.timestamp.isoformat()}


def build_run_manifest(*, patterns_v2: bool, market: str, rsi_divergence: bool = False) -> dict:
    root=Path(__file__).resolve().parent.parent
    sources=[root/'main.py',root/'config.py']
    sources += sorted((root/'core').glob('*.py'))
    sources += [root/'data/stream_client.py',root/'data/stream_server.py',root/'utils/trade_export.py']
    strategy = {}
    if rsi_divergence:
        from core.rsi_divergence import POLICY
        strategy['rsi_divergence_policy'] = deepcopy(POLICY)
        sources += [root/'analysis/indicator_engine.py', root/'indicators/_signal.py',
                    root/'patterns/base_pattern.py', root/'patterns/_rules.py']
    if patterns_v2:
        from patterns_v2.loader import detector_root
        sources += sorted((detector_root()/'patterns').glob('*.py'))
        sources += [root/'patterns_v2/signals.py',root/'patterns_v2/loader.py']
    hashes={str(p.relative_to(root)) if p.is_relative_to(root) else str(p):
            hashlib.sha256(p.read_bytes()).hexdigest() for p in sources}
    return {'schema_version':1,'run_id':uuid4().hex,'market':market,
            'created_at': datetime.now(timezone.utc).isoformat(),
            'python':platform.python_version(),'source_sha256':hashes,
            'resolved_controls': resolved_controls(), 'control_changes': [], 'observation_fingerprints': {},
            'scope':'sources at scanner construction; historical trades retain their own evidence',
            **strategy}
