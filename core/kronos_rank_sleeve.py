"""
core/kronos_rank_sleeve.py — Cross-sectional Kronos 1w ranked forecast sleeve.

Sits *beside* Toby chart patterns as an independent entry source (closer to
the official Kronos finetune top-K demo than `kronos_gate`'s hard veto).

Flow:
  1. Forecast +1w close % move for every eligible daily symbol.
  2. Rank by pred_1w.
  3. Emit top_k BUY and (unless long_only) bottom_k SELL as TradeSignals
     with pattern=`pattern_kronos_rank`.

Does NOT replace `kronos_gate` — that still filters Toby patterns. Sleeve
signals skip the gate (the forecast *is* the signal).
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from config import settings
from core.kronos_eval import LOOKBACK, MAX_CONTEXT, predict_1w_return
from core.kronos_gate import get_kronos_gate, kronos_infer_lock
from data.ohlcv_store import OHLCVStore
from patterns.base_pattern import TradeSignal
from utils.logger import log

PATTERN_NAME = "pattern_kronos_rank"


@dataclass(frozen=True)
class ForecastRow:
    symbol: str
    pred_1w: float
    last_close: float
    asof: pd.Timestamp


def _min_move() -> float:
    v = settings.kronos_rank_min_move_pct
    if v is None:
        return settings.kronos_min_move_pct
    return float(v)


def _sleeve_daily_df(store: OHLCVStore, symbol: str, lookback: int):
    from data.history import load_daily_ohlcv_df

    df = load_daily_ohlcv_df(symbol, tv_fallback=False, limit=MAX_CONTEXT)
    if df is not None and len(df) >= min(lookback, 60):
        return df
    return store.get_df(symbol, "1d", min_bars=min(lookback, 60))


def _confidence_from_pred(pred_1w: float) -> float:
    """Map |pred| into [min_confidence, 0.99] so engine floor can still apply."""
    floor = 0.55
    return float(min(0.99, floor + abs(pred_1w)))


def _signal_from_row(row: ForecastRow, action: str, rank: int, n_pool: int) -> TradeSignal:
    pred = row.pred_1w
    close = row.last_close
    stop_pct = abs(pred) * 0.5
    if action == "BUY":
        stop_loss = close * (1 - stop_pct)
        take_profit = close * (1 + pred)
    else:
        stop_loss = close * (1 + stop_pct)
        take_profit = close * (1 + pred)  # pred negative → TP below
    note = (
        f"KronosRank 1w {pred:+.2%} rank={rank}/{n_pool} "
        f"asof={pd.Timestamp(row.asof).date()}"
    )
    return TradeSignal(
        symbol=row.symbol,
        action=action,  # type: ignore[arg-type]
        pattern=PATTERN_NAME,
        timeframe="1d",
        confidence=_confidence_from_pred(pred),
        price=close,
        qty=1,
        stop_loss=round(stop_loss, 4),
        take_profit=round(take_profit, 4),
        trailing_stop_pct=stop_pct,
        trailing_stop_mode="lowest_close" if action == "BUY" else "highest_close",
        trailing_activation_pct=abs(pred) * 0.5,
        notes=note,
    )


def forecast_universe(
    store: OHLCVStore,
    symbols: list[str],
    *,
    sample_count: int | None = None,
    lookback: int | None = None,
) -> list[ForecastRow]:
    """Forecast pred_1w for each symbol that has enough daily history in store."""
    gate = get_kronos_gate()
    sc = settings.kronos_sample_count if sample_count is None else sample_count
    lb = LOOKBACK if lookback is None else lookback
    rows: list[ForecastRow] = []
    with kronos_infer_lock():
        if not gate._ensure_loaded():
            log.warning("KronosRank | predictor unavailable — sleeve emits nothing")
            return []
        for symbol in symbols:
            df = _sleeve_daily_df(store, symbol, lb)
            if df is None or len(df) < 60:
                continue
            out = predict_1w_return(gate._predictor, df, sample_count=sc, lookback=lb)
            if out is None:
                continue
            pred_1w, last_close = out
            rows.append(
                ForecastRow(
                    symbol=symbol,
                    pred_1w=pred_1w,
                    last_close=last_close,
                    asof=pd.Timestamp(df.index[-1]),
                )
            )
    return rows


def forecast_from_frames(
    frames: dict[str, pd.DataFrame],
    *,
    sample_count: int | None = None,
    lookback: int | None = None,
) -> list[ForecastRow]:
    """Same as forecast_universe but from pre-sliced DataFrames (backtest)."""
    gate = get_kronos_gate()
    sc = settings.kronos_sample_count if sample_count is None else sample_count
    lb = LOOKBACK if lookback is None else lookback
    rows: list[ForecastRow] = []
    with kronos_infer_lock():
        if not gate._ensure_loaded():
            log.warning("KronosRank | predictor unavailable — sleeve emits nothing")
            return []
        for symbol, df in frames.items():
            if df is None or len(df) < 60:
                continue
            out = predict_1w_return(gate._predictor, df, sample_count=sc, lookback=lb)
            if out is None:
                continue
            pred_1w, last_close = out
            rows.append(
                ForecastRow(
                    symbol=symbol,
                    pred_1w=pred_1w,
                    last_close=last_close,
                    asof=pd.Timestamp(df.index[-1]),
                )
            )
    return rows


def rank_and_emit(
    rows: list[ForecastRow],
    *,
    top_k: int | None = None,
    bottom_k: int | None = None,
    long_only: bool | None = None,
    min_move: float | None = None,
) -> list[TradeSignal]:
    """Select top_k longs / bottom_k shorts by pred_1w and build TradeSignals."""
    if not rows:
        return []

    top_k = settings.kronos_rank_top_k if top_k is None else top_k
    bottom_k = settings.kronos_rank_bottom_k if bottom_k is None else bottom_k
    long_only = settings.kronos_rank_long_only if long_only is None else long_only
    min_move = _min_move() if min_move is None else min_move

    ranked = sorted(rows, key=lambda r: r.pred_1w, reverse=True)
    n = len(ranked)
    signals: list[TradeSignal] = []

    # Longs: highest pred_1w, must clear +min_move
    long_candidates = [r for r in ranked if r.pred_1w >= min_move]
    for i, row in enumerate(long_candidates[: max(0, top_k)]):
        signals.append(_signal_from_row(row, "BUY", rank=i + 1, n_pool=n))

    if not long_only and bottom_k > 0:
        short_candidates = [r for r in reversed(ranked) if r.pred_1w <= -min_move]
        for i, row in enumerate(short_candidates[:bottom_k]):
            signals.append(_signal_from_row(row, "SELL", rank=n - i, n_pool=n))

    return signals


def run_sleeve(
    store: OHLCVStore,
    symbols: list[str],
    *,
    top_k: int | None = None,
    bottom_k: int | None = None,
    long_only: bool | None = None,
    min_move: float | None = None,
) -> list[TradeSignal]:
    """Forecast → rank → emit. Entry point for the scanner."""
    rows = forecast_universe(store, symbols)
    if not rows:
        return []
    signals = rank_and_emit(
        rows,
        top_k=top_k,
        bottom_k=bottom_k,
        long_only=long_only,
        min_move=min_move,
    )
    log.info(
        f"KronosRank | forecast={len(rows)} symbols → emitted {len(signals)} "
        f"(top_k={top_k or settings.kronos_rank_top_k}, "
        f"long_only={long_only if long_only is not None else settings.kronos_rank_long_only})"
    )
    return signals


def is_kronos_rank_signal(signal: TradeSignal) -> bool:
    return signal.pattern == PATTERN_NAME


def _candles_to_df(candles: list, session_tz: str = "America/New_York") -> pd.DataFrame:
    """OHLCVCandle list → DataFrame indexed by normalized session date."""
    rows = []
    for c in candles:
        rows.append(
            {
                "timestamp": c.timestamp,
                "open": c.open,
                "high": c.high,
                "low": c.low,
                "close": c.close,
                "volume": c.volume,
            }
        )
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    idx = pd.to_datetime(df["timestamp"])
    tz = session_tz or "America/New_York"
    if getattr(idx.dt, "tz", None) is not None:
        idx = idx.dt.tz_convert(tz)
    else:
        idx = idx.dt.tz_localize(tz)
    df.index = idx.dt.tz_localize(None).dt.normalize()
    return df[["open", "high", "low", "close", "volume"]]


def backtest_rank_sleeve(
    ohlcv_1d: dict[str, list],
    config: dict,
) -> tuple[list, int]:
    """Date-aligned cross-sectional Kronos rank backtest (1d only).

    Returns (trades, signals_count). Imports backtester helpers lazily to
    avoid circular imports at module load.
    """
    from core.backtester import (
        BacktestTrade,
        _apply_sizing,
        _check_exit,
        _close_trade,
        _open_trade,
        _update_trailing_reference,
        apply_risk_gates,
    )
    from core.engine_defaults import (
        ENGINE,
        passes_cooldown,
        passes_min_confidence,
        passes_regime_filter,
    )
    from data.ohlcv_store import OHLCVStore

    if not ohlcv_1d:
        return [], 0

    top_k = int(config.get("kronos_rank_top_k", settings.kronos_rank_top_k))
    bottom_k = int(config.get("kronos_rank_bottom_k", settings.kronos_rank_bottom_k))
    long_only = bool(config.get("kronos_rank_long_only", settings.kronos_rank_long_only))
    min_move = config.get("kronos_rank_min_move_pct")
    if min_move is None:
        min_move = _min_move()
    rebalance = int(
        config.get("kronos_rank_rebalance_bars", settings.kronos_rank_rebalance_bars)
    )
    rebalance = max(1, rebalance)

    frames = {
        sym: _candles_to_df(candles, config.get("session_tz") or "America/New_York")
        for sym, candles in ohlcv_1d.items()
    }
    frames = {s: df for s, df in frames.items() if df is not None and len(df) >= 60}
    if not frames:
        return [], 0

    # Union of trading dates, sorted.
    all_dates = sorted({d for df in frames.values() for d in df.index})
    if len(all_dates) < LOOKBACK + 5:
        log.warning("KronosRank BT | not enough shared history")
        return [], 0

    # Per-symbol candle list + date→index for fills/exits.
    candle_lists = {s: list(c) for s, c in ohlcv_1d.items() if s in frames}
    frame_pos = {s: {d: i for i, d in enumerate(df.index)} for s, df in frames.items()}

    store = OHLCVStore(window=max(LOOKBACK + 50, 512))
    open_pos: dict[str, BacktestTrade] = {}
    pending: dict[str, TradeSignal] = {}
    trades: list[BacktestTrade] = []
    cooldown_tracker: dict[tuple[str, str], tuple[int, bool]] = {}
    signals_count = 0

    start_i = LOOKBACK
    for di in range(start_i, len(all_dates)):
        d = all_dates[di]

        # ── Exits on today's bar ──────────────────────────────────────────
        for sym in list(open_pos.keys()):
            pos = open_pos[sym]
            idx = frame_pos.get(sym, {}).get(d)
            if idx is None:
                continue
            candle = candle_lists[sym][idx]
            exit_price, exit_reason = _check_exit(
                candle, pos, idx, min_hold_bars=config.get("min_hold_bars", 0),
            )
            if exit_price is not None:
                _close_trade(
                    pos, exit_price, exit_reason, candle, config.get("txn_cost_pct", 0.0),
                )
                trades.append(pos)
                cooldown_tracker[(sym, PATTERN_NAME)] = (di, pos.pnl < 0)
                del open_pos[sym]
            else:
                _update_trailing_reference(pos, candle)

        # ── Fill pendings on next bar ─────────────────────────────────────
        for sym in list(pending.keys()):
            if sym in open_pos:
                del pending[sym]
                continue
            idx = frame_pos.get(sym, {}).get(d)
            if idx is None:
                continue
            sig = pending.pop(sym)
            candle = candle_lists[sym][idx]
            pos = _open_trade(sig, candle, idx)
            pos.breakeven_trigger_pct = config.get("breakeven_trigger_pct")
            pos.breakeven_buffer_pct = config.get("breakeven_buffer_pct", 0.0)
            open_pos[sym] = pos

        # ── Rebalance / emit ──────────────────────────────────────────────
        if (di - start_i) % rebalance != 0:
            continue

        cap = int(config.get("max_open_positions") or 0)
        if cap > 0 and len(open_pos) + len(pending) >= cap:
            continue

        sliced: dict[str, pd.DataFrame] = {}
        for sym, df in frames.items():
            sub = df.loc[:d]
            if len(sub) >= 60:
                sliced[sym] = sub

        rows = forecast_from_frames(sliced)
        if not rows:
            continue
        for sym, sub in sliced.items():
            end_idx = frame_pos[sym][d]
            store.replace_all(sym, "1d", candle_lists[sym][: end_idx + 1])

        emitted = rank_and_emit(
            rows,
            top_k=top_k,
            bottom_k=bottom_k,
            long_only=long_only,
            min_move=float(min_move),
        )
        for signal in emitted:
            signals_count += 1
            if signal.symbol in open_pos or signal.symbol in pending:
                continue
            if not passes_min_confidence(signal, config.get("min_confidence")):
                continue
            if not passes_regime_filter(
                signal, store, enabled=config.get("regime_filter", True),
            ):
                continue
            if not passes_cooldown(
                signal, di, cooldown_tracker,
                cooldown_bars=config.get("cooldown_bars", 10),
            ):
                continue
            if not apply_risk_gates(
                signal, store, signal.symbol, "1d",
                min_atr_stop_multiple=config.get("min_atr_stop_multiple"),
                synthetic_stop_multiple=config.get("synthetic_stop_multiple", 1.5),
                atr_stop_floor_multiple=config.get("atr_stop_floor_multiple"),
                hard_stop_percentage=config.get("hard_stop_percentage"),
                min_reward_risk_ratio=config.get("min_reward_risk_ratio"),
                trailing_activation_default=config.get("trailing_activation_default"),
            ):
                continue
            _apply_sizing(
                signal, store, signal.symbol, "1d",
                config.get("account_value", 100_000.0),
                config.get("risk_per_trade_pct", ENGINE.risk_per_trade_pct),
                config.get("position_sizing", "risk"),
                max_position_pct=config.get("max_position_pct", ENGINE.max_position_pct),
            )
            pending[signal.symbol] = signal

    # Force-close remaining at last available bar.
    for sym, pos in list(open_pos.items()):
        candles = candle_lists.get(sym) or []
        if not candles:
            continue
        candle = candles[-1]
        _close_trade(
            pos, candle.close, "end_of_data", candle, config.get("txn_cost_pct", 0.0),
        )
        trades.append(pos)

    log.info(
        f"KronosRank BT | signals={signals_count} trades={len(trades)} "
        f"rebalance_bars={rebalance}"
    )
    return trades, signals_count
