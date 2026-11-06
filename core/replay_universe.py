"""Load an explicitly dated research universe without querying today's ranks."""
from __future__ import annotations

from datetime import date
import hashlib
import json
from pathlib import Path
import re


def load_replay_universe(path: str, market: str, start: str | None, limit: int) -> tuple[list, dict]:
    raw=Path(path).read_bytes(); value=json.loads(raw)
    if value.get('schema_version')!=1 or value.get('market')!=market or not start:
        raise ValueError('Dated universe requires schema_version=1, matching market and an explicit stream start')
    as_of=date.fromisoformat(value['as_of'])
    if as_of>date.fromisoformat(start):
        raise ValueError('Universe as_of is later than the replay start')
    rows=[]; seen=set(); identities=set()
    for member in value.get('members',[]):
        symbol=str(member['symbol']).upper(); exchange=str(member['exchange']).upper()
        identity=str(member['security_id']).strip()
        if (not re.fullmatch(r'[A-Z0-9.^/_-]+',symbol) or not exchange or not identity
                or symbol in seen or identity in identities):
            raise ValueError('Universe requires unique symbols/security IDs and nonempty exchanges')
        seen.add(symbol); identities.add(identity); rows.append((symbol,exchange))
    if not rows:
        raise ValueError('Dated universe has no members')
    return rows[:limit], {'selection':'supplied_dated_snapshot','as_of':as_of.isoformat(),
        'sha256':hashlib.sha256(raw).hexdigest(),'members':value['members'][:limit],
        'requested':limit,'returned':len(rows[:limit]),'source':value.get('source'),
        'membership_independently_verified':False}
