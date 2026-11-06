"""Causal, content-addressed daily model input from the scanner's own tape."""
from __future__ import annotations

import hashlib
import json
import math
import pandas as pd


def context_hash(rows: list) -> str:
    return hashlib.sha256(json.dumps(rows,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def frame_context(df, anchor: str, timeframe: str) -> dict | None:
    columns=('open','high','low','close','volume')
    if timeframe.lower()!='1d' or df is None or not all(k in df for k in columns):
        return None
    rows=[]
    for stamp,row in df.iterrows():
        day=pd.Timestamp(stamp).date().isoformat()
        if day>anchor:
            continue
        record={'date':day}
        for key in columns:
            number=float(row[key])
            record[key]=number if math.isfinite(number) else None
        rows.append(record)
    rows=rows[-400:]
    return {'rows':rows,'sha256':context_hash(rows)}


def context_frame(context: dict, as_of: str) -> pd.DataFrame:
    rows=context['rows']
    if not rows or context_hash(rows)!=context['sha256']:
        raise ValueError('Replay model context identity differs or is empty')
    if any(row['date']>as_of for row in rows):
        raise ValueError('Replay model context contains future bars')
    frame=pd.DataFrame(rows)
    frame.index=pd.to_datetime(frame.pop('date'))
    return frame
