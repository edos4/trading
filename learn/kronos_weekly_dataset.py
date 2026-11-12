"""Read-only full-history Kronos snapshots with causal, market-balanced windows."""
from __future__ import annotations

import hashlib
import json
from datetime import date, timedelta
from pathlib import Path
import random
from typing import Iterable

import numpy as np
import pandas as pd

from learn import kronos_dataset as kd


def is_session(day: date, market: str) -> bool:
    from core.market import is_ph_holiday, is_us_holiday
    return day.weekday() < 5 and not (is_ph_holiday(day) if market == 'ph' else is_us_holiday(day))


def expected_session(end: date, market: str) -> date:
    day = end - timedelta(days=1)
    for _ in range(30):
        if is_session(day, market):
            return day
        day -= timedelta(days=1)
    raise ValueError(f'No completed session for {market}')


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def suspension_breaks(days: np.ndarray, market: str) -> tuple[list[int], int]:
    """Split indexes where more than five expected sessions are missing."""
    breaks = [0]
    gaps = 0
    for i in np.flatnonzero(np.diff(days) > 7) + 1:
        missing = sum(is_session(date.fromordinal(d), market)
                      for d in range(int(days[i - 1]) + 1, int(days[i])))
        if missing > 5:
            breaks.append(int(i))
            gaps += 1
    breaks.append(len(days))
    return breaks, gaps


def _compact(starts: list[int], offset: int) -> list[list[int]]:
    result = []
    run = count = None
    previous = None
    for start in starts:
        if previous is None or start != previous + 1:
            if count:
                result.append([offset + run, count])
            run, count = start, 1
        else:
            count += 1
        previous = start
    if count:
        result.append([offset + run, count])
    return result


def single_scale_mask(closes: np.ndarray, lookback: int) -> np.ndarray:
    """True when a lookback contains no close still more than ten times its anchor."""
    from core.kronos_input import PRICE_BREAK_RATIO
    if len(closes) < lookback:
        return np.zeros(0, dtype=bool)
    windows = np.lib.stride_tricks.sliding_window_view(np.asarray(closes, dtype=float), lookback)
    return windows[:, :-1].max(axis=1) <= PRICE_BREAK_RATIO * windows[:, -1]


def ranges_for_days(days: np.ndarray, offset: int, lower: int, upper: int,
                    lookback: int, suffix: int, market: str,
                    closes: np.ndarray | None = None) -> list[list[int]]:
    """Compact legal start intervals; targets lie in [lower, upper).

    With ``closes``, a start is kept only when its lookback is already on the
    anchor's price scale. Forecast labels are not inspected.
    """
    breaks, _gaps = suspension_breaks(days, market)
    scale = single_scale_mask(closes, lookback) if closes is not None else None
    starts = []
    for first, stop in zip(breaks, breaks[1:]):
        target_first = max(first + lookback, int(np.searchsorted(days, lower)))
        target_stop = min(stop, int(np.searchsorted(days, upper)))
        count = target_stop - target_first - suffix + 1
        if count <= 0:
            continue
        local = range(target_first - lookback, target_first - lookback + count)
        if scale is None:
            starts.extend(local)
        else:
            starts.extend(start for start in local if scale[start])
    return _compact(starts, offset)


def build_from_frames(frames: Iterable[tuple[str, str, pd.DataFrame]], out: Path, *,
                      end: date, start: date | None = None, lookback: int = 256,
                      predict_len: int = 10, min_bars: int = 500,
                      market_weights: tuple[float, float] = (.5, .5),
                      exclude_symbols: Iterable[str] = ()) -> dict:
    """Stream canonical symbol frames to a fresh corpus; never reuse stale data."""
    excluded = sorted({symbol.strip().upper() for symbol in exclude_symbols if symbol.strip()})
    excluded_set = set(excluded)
    if out.exists() and any(out.iterdir()):
        raise ValueError('Snapshot output must be empty')
    out.mkdir(parents=True, exist_ok=True)
    train_end = end - timedelta(weeks=16)
    val_end = end - timedelta(weeks=4)
    bounds = {'train': (start or date.min, train_end), 'val': (train_end, val_end), 'eval': (val_end, end)}
    report = {'rejected_rows': 0, 'dropped_symbols': 0, 'suspected_discontinuities': 0, 'long_gaps': 0,
              'mixed_scale_windows': 0,
              'markets': {m: {'symbols': 0, 'bars': 0, 'train': 0, 'val': 0, 'eval': 0, 'long_gaps': 0,
                              'training_only_symbols': 0, 'validation_symbols': 0, 'evaluation_symbols': 0}
                          for m in ('us', 'ph')}}
    symbols = []
    offset = 0
    first_day = None
    with (out / kd.FEATURES_FILE).open('wb') as features, (out / kd.DAYS_FILE).open('wb') as dates:
        for market, symbol, frame in frames:
            if market not in ('us', 'ph') or symbol in excluded_set:
                continue
            work = frame.copy().sort_index(kind='stable')
            work = work[~work.index.duplicated(keep='last')]
            work = work[work.index < pd.Timestamp(end)]
            if start is not None:
                work = work[work.index >= pd.Timestamp(start)]
            work = work[kd.FEATURE_COLS[:5]].apply(pd.to_numeric, errors='coerce')
            prices = work.iloc[:, :4]
            valid = (np.isfinite(work).all(axis=1) & (prices > 0).all(axis=1)
                     & (work.volume >= 0) & (work.high >= prices.max(axis=1))
                     & (work.low <= prices.min(axis=1)))
            report['rejected_rows'] += int((~valid).sum())
            work = work[valid]
            if len(work) < min_bars:
                report['dropped_symbols'] += 1
                continue
            days = np.array([d.date().toordinal() for d in work.index], dtype=np.int32)
            closes = work.close.to_numpy(dtype=float)
            _breaks, gaps = suspension_breaks(days, market)
            ranges = {}
            for split, (lo, hi) in bounds.items():
                raw = ranges_for_days(days, offset, lo.toordinal(), hi.toordinal(),
                                      lookback, predict_len + 1, market)
                kept = ranges_for_days(days, offset, lo.toordinal(), hi.toordinal(),
                                       lookback, predict_len + 1, market, closes)
                report['mixed_scale_windows'] += sum(count for _, count in raw) - sum(count for _, count in kept)
                ranges[split] = kept
            if not ranges['train']:
                report['dropped_symbols'] += 1
                continue
            report['long_gaps'] += gaps
            report['suspected_discontinuities'] += int((work.close.pct_change().abs() > .5).sum())
            work['amount'] = kd.amount_for_frame(work)
            array = work[kd.FEATURE_COLS].to_numpy(dtype=np.float32)
            if not np.isfinite(array).all():
                raise ValueError(f'Float32 overflow for {symbol}')
            features.write(array.tobytes())
            dates.write(days.tobytes())
            first_day = int(days[0]) if first_day is None else min(first_day, int(days[0]))
            symbols.append({'name': symbol, 'market': market, 'offset': offset,
                            'count': len(work), 'ranges': ranges})
            counts = report['markets'][market]
            counts['symbols'] += 1
            counts['bars'] += len(work)
            counts['long_gaps'] += gaps
            if ranges['val']:
                counts['validation_symbols'] += 1
            if ranges['eval']:
                counts['evaluation_symbols'] += 1
            if not ranges['val'] and not ranges['eval']:
                counts['training_only_symbols'] += 1
            for split in bounds:
                counts[split] += sum(count for _, count in ranges[split])
            offset += len(work)
    for market, counts in report['markets'].items():
        if any(counts[split] == 0 for split in bounds):
            raise ValueError(f'Insufficient {market} split coverage: {counts}')
    meta = {'schema_version': 3, 'history_scope': 'all_available' if start is None else 'bounded',
            'excluded_symbols': excluded,
            'window_start': (start or date.fromordinal(first_day)).isoformat(), 'window_end': end.isoformat(),
            'train_end': train_end.isoformat(), 'val_end': val_end.isoformat(),
            'lookback': lookback, 'predict_len': predict_len, 'market_weights': market_weights,
            'min_bars': min_bars, 'symbols': symbols, 'report': report}
    (out / kd.SYMBOLS_FILE).write_text(json.dumps(meta, sort_keys=True))
    hashes = {name: file_hash(out / name) for name in (kd.FEATURES_FILE, kd.DAYS_FILE, kd.SYMBOLS_FILE)}
    meta['corpus_sha256'] = hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()
    (out / kd.REPORT_FILE).write_text(json.dumps({**report, 'corpus_sha256': meta['corpus_sha256'], 'files': hashes}, indent=2))
    return meta


def build_snapshot(out: Path, **kwargs) -> dict:
    """One repeatable-read PostgreSQL scan, with no default history lower bound."""
    import psycopg
    from data.db import build_dsn
    with psycopg.connect(build_dsn()) as conn:
        conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        def frames():
            with conn.cursor(name='weekly_kronos_snapshot') as cursor:
                cursor.itersize = 50000
                start = kwargs.get('start')
                lower_bound = 'AND b.bar_date >= %s ' if start is not None else ''
                params = (start, kwargs['end']) if start is not None else (kwargs['end'],)
                cursor.execute(
                    "SELECT s.market,b.symbol,b.bar_date,b.ts,b.open,b.high,b.low,b.close,b.volume "
                    "FROM daily_bars b JOIN symbols s ON s.symbol=b.symbol "
                    "WHERE s.market IN ('us','ph') " + lower_bound + "AND b.bar_date < %s "
                    "ORDER BY s.market,b.symbol,b.bar_date,b.ts", params)
                key = None
                rows = []
                for market, symbol, day, ts, *values in cursor:
                    if key is not None and key != (market, symbol):
                        yield (*key, _frame(rows))
                        rows = []
                    key = (market, symbol)
                    rows.append((day, *values))
                if key is not None:
                    yield (*key, _frame(rows))
        return build_from_frames(frames(), out, **kwargs)


def _frame(rows: list) -> pd.DataFrame:
    frame = pd.DataFrame(rows, columns=['bar_date', *kd.FEATURE_COLS[:5]])
    frame.index = pd.to_datetime(frame.pop('bar_date'))
    return frame


class WeeklyWindowDataset:
    """Deterministic balanced draws; held-out suffixes may borrow past context."""
    def __init__(self, dataset_dir, lookback_window, predict_window, clip, seed, n_iter, *, split='train'):
        self.feats, self.days, self.meta = kd.load_corpus(dataset_dir)
        if (lookback_window, predict_window) != (self.meta['lookback'], self.meta['predict_len']):
            raise ValueError('Snapshot window geometry differs')
        self.lookback = lookback_window
        self.window = lookback_window + predict_window + 1
        self.clip, self.seed, self.split = clip, seed, split
        self.rng = random.Random(seed)
        self.market_weights = self.meta['market_weights']
        self.market_override = None
        self.pools = {}
        for market in ('us', 'ph'):
            ranges = [r for s in self.meta['symbols'] if s['market'] == market for r in s['ranges'][split]]
            cumulative = np.cumsum([r[1] for r in ranges], dtype=np.int64)
            if len(cumulative) == 0:
                raise ValueError(f'No {market} {split} windows')
            self.pools[market] = (ranges, cumulative)
        self.n_samples = n_iter
        self.sample_counts = {'us': 0, 'ph': 0}

    def __len__(self):
        return self.n_samples

    def set_epoch_seed(self, epoch):
        self.rng.seed(self.seed + epoch)
        self.sample_counts = {'us': 0, 'ph': 0}

    def _draw(self):
        market = self.market_override or self.rng.choices(['us', 'ph'], weights=self.market_weights)[0]
        ranges, cumulative = self.pools[market]
        value = self.rng.randrange(int(cumulative[-1]))
        index = int(np.searchsorted(cumulative, value, side='right'))
        local = value - (int(cumulative[index - 1]) if index else 0)
        start = ranges[index][0] + local
        self.sample_counts[market] += 1
        x = kd.normalize_window(np.array(self.feats[start:start + self.window]), self.lookback, self.clip)
        return x, kd.stamp_for_ordinals(self.days[start:start + self.window])

    def __getitem__(self, index):
        import torch
        x, stamp = self._draw()
        return torch.from_numpy(x), torch.from_numpy(stamp)
