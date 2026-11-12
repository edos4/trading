"""Causal direction examples, fixed quality rules, and recency-weighted draws."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
import hashlib
import json
from pathlib import Path

import numpy as np

from core.kronos_confirm import classify, sideways_band_pct
from core.market import PH_MIN_ADV_PHP, US_MIN_ADV_USD
from indicators._math import wilder_atr
from learn import kronos_dataset as kd
from learn.kronos_weekly_dataset import file_hash, single_scale_mask, suspension_breaks

LABELS = ('up', 'down', 'flat')
FEATURES = tuple(f'return_{n}' for n in (1, 3, 5, 10, 20, 60)) + (
    'volatility_20', 'volatility_60', 'atr_pct', 'body', 'range',
    'range_position_20', 'sma_gap_20', 'sma_gap_60', 'log_volume',
    'log_turnover', 'relative_volume', 'zero_volume_fraction',
    'unchanged_fraction', 'ph_market',
)
FEATURE_VERSION = 'causal-ohlcv-direction-v1'


@dataclass(frozen=True)
class QualityPolicy:
    """Rules use only bars available at the forecast anchor, in native currency."""
    window: int = 60
    max_zero_volume_fraction: float = .5
    max_unchanged_fraction: float = .8
    max_day_price_ratio: float = 4.
    us_min_turnover: float = US_MIN_ADV_USD
    ph_min_turnover: float = PH_MIN_ADV_PHP

    def __post_init__(self) -> None:
        if self.window < 15:
            raise ValueError('Quality window must contain at least 15 bars')
        if not all(np.isfinite(v) and 0 <= v <= 1 for v in
                   (self.max_zero_volume_fraction, self.max_unchanged_fraction)):
            raise ValueError('Quality fractions must be between zero and one')
        if not np.isfinite(self.max_day_price_ratio) or self.max_day_price_ratio <= 1:
            raise ValueError('Price ratio must exceed one')
        if not all(np.isfinite(v) and v >= 0 for v in (self.us_min_turnover, self.ph_min_turnover)):
            raise ValueError('Turnover floors must be finite and nonnegative')


def _rolling_mean(values: np.ndarray, window: int) -> np.ndarray:
    sums = np.concatenate(([0.], np.cumsum(values, dtype=np.float64)))
    return (sums[window:] - sums[:-window]) / window


def quality_masks(values: np.ndarray, lookback: int, market: str, policy: QualityPolicy) -> dict[str, np.ndarray]:
    """One mask per reason, indexed by context start; no forecast labels read."""
    if market not in ('us', 'ph') or lookback < max(61, policy.window):
        raise ValueError('Unsupported market or insufficient context')
    n = len(values) - lookback + 1
    if n <= 0:
        return {}
    close = np.asarray(values[:, 3], dtype=float)
    volume = np.asarray(values[:, 4], dtype=float)
    window = policy.window
    align = slice(lookback - window, None)
    turnover = close * volume
    unchanged = np.concatenate(([0.], np.isclose(close[1:], close[:-1], rtol=1e-6, atol=0).astype(float)))
    ratio = close[1:] / close[:-1]
    jumps = np.concatenate(([0.], ((ratio > policy.max_day_price_ratio) |
                                   (ratio < 1 / policy.max_day_price_ratio)).astype(float)))
    # Close-to-close transitions start inside the quality window, not before it.
    transition_align = slice(lookback - window + 1, None)
    return {
        'zero_volume': _rolling_mean((volume == 0).astype(float), window)[align] > policy.max_zero_volume_fraction,
        'stale_prices': _rolling_mean(unchanged, window - 1)[transition_align] > policy.max_unchanged_fraction,
        'price_discontinuity': _rolling_mean(jumps, window - 1)[transition_align] > 0,
        'low_turnover': _rolling_mean(turnover, window)[align] <
                        (policy.us_min_turnover if market == 'us' else policy.ph_min_turnover),
    }


def context_features(context: np.ndarray, market: str) -> tuple[np.ndarray, float]:
    """Features and ATR flat band fitted solely to this completed context."""
    work = np.asarray(context, dtype=float)
    if market not in ('us', 'ph') or work.ndim != 2 or work.shape[1] < 5 or len(work) < 61:
        raise ValueError('Require at least 61 OHLCV bars and a supported market')
    prices, volume = work[:, :4], work[:, 4]
    if (not np.isfinite(work).all() or (prices <= 0).any() or (volume < 0).any()
            or (work[:, 1] < prices.max(axis=1)).any() or (work[:, 2] > prices.min(axis=1)).any()):
        raise ValueError('Invalid OHLCV context')
    close = work[:, 3]
    returns = close[1:] / close[:-1] - 1
    atr_pct = float(wilder_atr(work[:, 1], work[:, 2], close, 14)[-1]) / close[-1] * 100
    high, low = work[-20:, 1].max(), work[-20:, 2].min()
    features = [close[-1] / close[-n - 1] - 1 for n in (1, 3, 5, 10, 20, 60)] + [
        returns[-20:].std(), returns[-60:].std(), atr_pct / 100,
        (close[-1] - work[-1, 0]) / close[-1], (work[-1, 1] - work[-1, 2]) / close[-1],
        (close[-1] - low) / max(high - low, close[-1] * 1e-8),
        close[-1] / close[-20:].mean() - 1, close[-1] / close[-60:].mean() - 1,
        np.log1p(volume[-60:].mean()), np.log1p((volume[-60:] * close[-60:]).mean()),
        volume[-1] / max(volume[-20:].mean(), 1.), (volume[-60:] == 0).mean(),
        np.isclose(close[-59:], close[-60:-1], rtol=1e-6, atol=0).mean(), float(market == 'ph'),
    ]
    vector = np.asarray(features, dtype=np.float64)
    if not np.isfinite(vector).all():
        raise ValueError('Nonfinite causal features')
    return vector, sideways_band_pct(atr_pct) / 100


class DirectionCorpus:
    """Read-only corpus; all legal contexts indexed once, independent of splits."""
    def __init__(self, directory: Path, *, lookback: int = 400, quality: QualityPolicy = QualityPolicy(),
                 excluded_symbols: tuple[str, ...] = ('AXRX', 'GASE', 'GBDMF')):
        self.directory = Path(directory)
        report = json.loads((self.directory / kd.REPORT_FILE).read_text())
        hashes = {name: file_hash(self.directory / name)
                  for name in (kd.FEATURES_FILE, kd.DAYS_FILE, kd.SYMBOLS_FILE)}
        digest = hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()
        if hashes != report['files'] or digest != report['corpus_sha256']:
            raise ValueError('Corpus hash verification failed')
        if lookback < max(61, quality.window):
            raise ValueError('Lookback is shorter than feature or quality history')
        self.features, self.days, self.meta = kd.load_corpus(self.directory)
        self.sha256, self.lookback, self.quality = digest, lookback, quality
        self.excluded_symbols = tuple(sorted(set(excluded_symbols)))
        self.symbols = [s for s in self.meta['symbols'] if s['name'] not in self.excluded_symbols]
        self.ranges: list[tuple[int, int, int]] = []
        self.report = {'candidate_windows': 0, 'eligible_windows': 0,
                       'quality_rejections': {}, 'symbols': len(self.symbols)}
        for index, symbol in enumerate(self.symbols):
            offset, count = symbol['offset'], symbol['count']
            values, days = self.features[offset:offset + count], self.days[offset:offset + count]
            legal = np.zeros(count, dtype=bool)
            breaks, _ = suspension_breaks(days, symbol['market'])
            for first, stop in zip(breaks, breaks[1:]):
                if stop - first >= lookback + 3:
                    legal[first + lookback:stop - 2] = True
            scale = single_scale_mask(values[:, 3], lookback)
            legal[lookback:] &= scale[:count - lookback]
            masks = quality_masks(values, lookback, symbol['market'], quality)
            candidate = legal.copy()
            self.report['candidate_windows'] += int(candidate.sum())
            for reason, mask in masks.items():
                rejected = np.zeros(count, dtype=bool)
                rejected[lookback:] = mask[:count - lookback]
                self.report['quality_rejections'][reason] = self.report['quality_rejections'].get(reason, 0) + int((candidate & rejected).sum())
                legal &= ~rejected
            targets = np.flatnonzero(legal)
            self.report['eligible_windows'] += len(targets)
            if len(targets):
                cuts = np.flatnonzero(np.diff(targets) != 1) + 1
                for chunk in np.split(targets, cuts):
                    self.ranges.append((offset + int(chunk[0]), len(chunk), index))
            if (index + 1) % 2000 == 0:
                from utils.logger import log
                log.info(f'Direction | indexed {index + 1}/{len(self.symbols)} symbols; '
                         f'{self.report["eligible_windows"]:,} eligible windows')

    def history_hash(self, cutoff: date, names: list[tuple[str, str]] | None = None) -> tuple[str, list[tuple[str, str]]]:
        """Hash historical prefixes without global offsets, permitting future appends."""
        wanted = set(names) if names is not None else {(s['market'], s['name']) for s in self.symbols}
        digest = hashlib.sha256()
        found = []
        for symbol in sorted(self.symbols, key=lambda s: (s['market'], s['name'])):
            key = symbol['market'], symbol['name']
            if key not in wanted:
                continue
            offset, count = symbol['offset'], symbol['count']
            count = int(np.searchsorted(self.days[offset:offset + count], cutoff.toordinal()))
            digest.update(json.dumps([*key, count]).encode())
            digest.update(self.days[offset:offset + count].tobytes())
            digest.update(self.features[offset:offset + count].tobytes())
            found.append(key)
        if set(found) != wanted:
            raise ValueError('Final corpus is missing original training symbols')
        return digest.hexdigest(), found

    def pool(self, lower: date, upper: date, *, gap_sessions: int = 0,
             half_life_days: float = 0, uniform_share: float = .1) -> 'WindowPool':
        return WindowPool(self, lower, upper, gap_sessions, half_life_days, uniform_share)

    def examples(self, targets: list[tuple[int, int]]) -> dict:
        x, y, bands, returns, markets, dates = [], [], [], [], [], []
        for target, index in targets:
            market = self.symbols[index]['market']
            context = self.features[target - self.lookback:target]
            vector, band = context_features(context, market)
            anchor = float(context[-1, 3])
            actual = [float(self.features[target + h - 1, 3]) / anchor - 1 for h in (1, 3)]
            x.append(vector)
            y.append([LABELS.index(classify(r, band * 100)) for r in actual])
            bands.append(band)
            returns.append(actual)
            markets.append(market)
            dates.append([int(self.days[target]), int(self.days[target + 2])])
        if not x:
            raise ValueError('No eligible direction examples')
        return dict(x=np.asarray(x), y=np.asarray(y, dtype=np.int64), bands=np.asarray(bands),
                    returns=np.asarray(returns), markets=np.asarray(markets), dates=np.asarray(dates),
                    targets=np.asarray(targets, dtype=np.int64), lookback=self.lookback,
                    quality=asdict(self.quality))


class WindowPool:
    """Market-balanced draws with a uniform component retaining older history."""
    def __init__(self, corpus: DirectionCorpus, lower: date, upper: date, gap_sessions: int,
                 half_life_days: float, uniform_share: float):
        if lower >= upper or gap_sessions < 0 or not np.isfinite(half_life_days) or half_life_days < 0:
            raise ValueError('Invalid split bounds, gap, or recency half life')
        if not np.isfinite(uniform_share) or not 0 < uniform_share <= 1:
            raise ValueError('Uniform share must be positive and at most one')
        self.corpus, self.half_life, self.uniform_share = corpus, half_life_days, uniform_share
        self.entries = {m: [] for m in ('us', 'ph')}
        for start, count, index in corpus.ranges:
            symbol = corpus.symbols[index]
            dates = corpus.days[start:start + count]
            lo = int(np.searchsorted(dates, lower.toordinal()))
            # The full three-day target fits before the upper boundary; embargo
            # is counted in this symbol's stored sessions, not calendar days.
            symbol_days = corpus.days[symbol['offset']:symbol['offset'] + symbol['count']]
            stop = int(np.searchsorted(symbol_days, upper.toordinal())) - gap_sessions - 2 + symbol['offset']
            hi = min(count, stop - start)
            if hi > lo:
                self.entries[symbol['market']].append((start + lo, hi - lo, index))
        self.cumulative = {m: np.cumsum([n for _, n, _ in rows], dtype=np.int64) for m, rows in self.entries.items()}
        self.weighted, self.weight_cumulative = {}, {}
        for market, rows in self.entries.items():
            bins, weights = [], []
            for start, count, index in rows:
                dates = corpus.days[start:start + count]
                first = 0
                while first < count:
                    boundary = (int(dates[first]) // 90 + 1) * 90
                    stop = int(np.searchsorted(dates, boundary))
                    midpoint = (int(dates[first]) + int(dates[stop - 1])) / 2
                    weight = np.exp2(-max(0., upper.toordinal() - midpoint) / half_life_days) if half_life_days else 1.
                    bins.append((start + first, stop - first, index))
                    weights.append((stop - first) * weight)
                    first = stop
            self.weighted[market] = bins
            self.weight_cumulative[market] = np.cumsum(weights)

    def _available(self, market: str) -> int:
        values = self.cumulative[market]
        return int(values[-1]) if len(values) else 0

    def sample(self, n: int, seed: int, *, training: bool) -> list[tuple[int, int]]:
        if n < 2:
            raise ValueError('Sample budget must allow both markets')
        rng = np.random.default_rng(seed)
        result = []
        for market in ('us', 'ph'):
            total = self._available(market)
            if not total:
                raise ValueError(f'No eligible {market} windows in split')
            count = n // 2 + int(market == 'us' and n % 2)
            if not training:
                count = min(count, total)
                values = rng.choice(total, size=count, replace=False)
                for value in values:
                    i = int(np.searchsorted(self.cumulative[market], value, side='right'))
                    start, _, symbol = self.entries[market][i]
                    before = int(self.cumulative[market][i - 1]) if i else 0
                    result.append((start + int(value) - before, symbol))
            else:
                for _ in range(count):
                    recent = self.half_life > 0 and rng.random() >= self.uniform_share
                    rows = self.weighted[market] if recent else self.entries[market]
                    cumulative = self.weight_cumulative[market] if recent else self.cumulative[market]
                    value = rng.random() * float(cumulative[-1]) if recent else rng.integers(total)
                    i = int(np.searchsorted(cumulative, value, side='right'))
                    start, length, symbol = rows[i]
                    result.append((start + int(rng.integers(length)), symbol))
        rng.shuffle(result)
        return result
