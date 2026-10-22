"""Deterministic replay for a visual indicator rule.

Deliberately separate from ``core/backtester.py`` (which is coupled to pinned
PostgreSQL pattern versions). Entry is the next bar's open after a false→true
edge, matching paper fills; exits are rule-false (next open), intrabar stop or
target, max-hold, or end of data.
"""
from __future__ import annotations

from datetime import date, datetime

import pandas as pd

from analysis.rule_engine import evaluate_bar, evaluate_series, required_bars
from analysis.rule_detector import exit_levels
from core.rule_contracts import RulePayload


def load_candles(symbol: str, market: str = "us"):
    from core.backtester import _load_cached_ohlcv
    from data.history import fetch_ohlcv_candles

    candles = _load_cached_ohlcv(symbol, "1d", market)
    if candles is None:
        try:
            candles = fetch_ohlcv_candles(symbol, "1d", market=market)
        except Exception:
            candles = []
    return candles or []


def frame_from_candles(candles) -> tuple[pd.DataFrame, list[str]]:
    rows = []
    dates = []
    for candle in candles:
        stamp = candle.timestamp
        if isinstance(stamp, datetime):
            dates.append(stamp.date().isoformat())
        elif isinstance(stamp, date):
            dates.append(stamp.isoformat())
        else:
            dates.append(str(stamp or ""))
        rows.append({
            "open": float(candle.open),
            "high": float(candle.high),
            "low": float(candle.low),
            "close": float(candle.close),
            "volume": float(candle.volume or 0.0),
        })
    return pd.DataFrame(rows), dates


def _within(dates: list[str], start: str | None, end: str | None) -> int:
    """First index inside [start, end]; used after warmup."""
    first = 0
    for index, value in enumerate(dates):
        if start and value < start:
            first = index + 1
            continue
        break
    return first


def _bracket(rule: RulePayload, direction: str, price: float, df: pd.DataFrame):
    return exit_levels(rule.exit, direction, price, df)


def _trade(symbol, direction, entry, exit_price, qty, entry_date, exit_date,
           bars, reason, txn_cost) -> dict:
    gross = (exit_price - entry) if direction == "long" else (entry - exit_price)
    fees = (entry + exit_price) * qty * float(txn_cost)
    pnl = gross * qty - fees
    notional = entry * qty
    return {
        "symbol": symbol,
        "direction": direction,
        "entry_date": entry_date,
        "exit_date": exit_date,
        "entry": round(entry, 4),
        "exit": round(exit_price, 4),
        "qty": round(qty, 4),
        "bars": int(bars),
        "reason": reason,
        "pnl": round(pnl, 2),
        "pnl_pct": round((pnl / notional) * 100.0, 4) if notional else 0.0,
    }


def _replay(symbol: str, df: pd.DataFrame, dates: list[str], rule: RulePayload,
            *, notional: float, txn_cost: float, slippage: float,
            start_date: str | None, end_date: str | None) -> list[dict]:
    series = evaluate_series(rule, df)
    warm = required_bars(rule)
    n = len(df)
    direction = rule.direction
    trades: list[dict] = []

    def allowed(index: int) -> bool:
        if index >= n:
            return False
        value = dates[index]
        if end_date and value > end_date:
            return False
        return not start_date or value >= start_date

    position = None
    pending_entry = False
    pending_exit = False
    start_index = max(warm, _within(dates, start_date, end_date))

    for i in range(start_index, n):
        if end_date and dates[i] > end_date:
            if position is not None:
                last = i - 1
                trades.append(_trade(
                    symbol, direction, position["entry"],
                    float(df["close"].iloc[last]), position["qty"],
                    dates[position["entry_idx"]], dates[last],
                    last - position["entry_idx"], "end_date", txn_cost,
                ))
                position = None
            break
        if not allowed(i):
            continue
        o = float(df["open"].iloc[i])
        h = float(df["high"].iloc[i])
        l = float(df["low"].iloc[i])
        c = float(df["close"].iloc[i])

        if position is not None:
            entry_idx = position["entry_idx"]
            if pending_exit and i > entry_idx:
                trades.append(_trade(
                    symbol, direction, position["entry"], o, position["qty"],
                    dates[entry_idx], dates[i], i - entry_idx, "rule_false",
                    txn_cost,
                ))
                position = None
                pending_exit = False
            elif i > entry_idx:
                stop = position["stop"]
                target = position["target"]
                reason = None
                exit_price = None
                # Gap through the bracket fills at the open.
                if direction == "long":
                    if o <= stop:
                        reason, exit_price = "stop", o
                    elif target is not None and o >= target:
                        reason, exit_price = "target", o
                    elif l <= stop:
                        reason, exit_price = "stop", stop
                    elif target is not None and h >= target:
                        reason, exit_price = "target", target
                else:
                    if o >= stop:
                        reason, exit_price = "stop", o
                    elif target is not None and o <= target:
                        reason, exit_price = "target", o
                    elif h >= stop:
                        reason, exit_price = "stop", stop
                    elif target is not None and l <= target:
                        reason, exit_price = "target", target
                held = i - entry_idx
                if reason is None and rule.exit.max_hold_bars is not None \
                        and held >= int(rule.exit.max_hold_bars):
                    reason, exit_price = "max_hold", c
                if reason is not None:
                    trades.append(_trade(
                        symbol, direction, position["entry"], exit_price,
                        position["qty"], dates[entry_idx], dates[i], held, reason,
                        txn_cost,
                    ))
                    position = None
                elif rule.exit.on_false and not bool(series.iloc[i]) \
                        and not pd.isna(series.iloc[i]):
                    pending_exit = True
            continue

        if pending_entry:
            pending_entry = False
            entry = o * (1.0 + slippage) if direction == "long" else o * (1.0 - slippage)
            qty = notional / entry if entry > 0 else 0.0
            stop, target = _bracket(rule, direction, entry, df)
            if qty <= 0 or stop is None:
                continue
            if (direction == "long" and entry <= stop) or \
                    (direction == "short" and entry >= stop):
                continue
            position = {"entry": entry, "qty": qty, "stop": stop,
                        "target": target, "entry_idx": i}
            continue

        if allowed(i + 1) and bool(series.iloc[i]) and not bool(series.iloc[i - 1]):
            pending_entry = True

    if position is not None:
        last = n - 1
        trades.append(_trade(
            symbol, direction, position["entry"], float(df["close"].iloc[last]),
            position["qty"], dates[position["entry_idx"]], dates[last],
            last - position["entry_idx"], "data_end", txn_cost,
        ))
    return trades


def _summary(trades: list[dict]) -> dict:
    closed = len(trades)
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] < 0]
    gross_win = sum(t["pnl"] for t in wins)
    gross_loss = abs(sum(t["pnl"] for t in losses))
    equity = 0.0
    peak = 0.0
    drawdown = 0.0
    for trade in trades:
        equity += trade["pnl"]
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return {
        "trades": closed,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round((len(wins) / closed) * 100.0, 2) if closed else 0.0,
        "avg_pnl_pct": round(sum(t["pnl_pct"] for t in trades) / closed, 4) if closed else 0.0,
        "total_pnl": round(equity, 2),
        "expectancy": round(equity / closed, 2) if closed else 0.0,
        "profit_factor": round(gross_win / gross_loss, 3) if gross_loss else None,
        "max_drawdown": round(drawdown, 2),
        "max_dd": round(drawdown, 2),
    }


def run_rule_backtest(rule: RulePayload, symbols: list[str], *,
                      market: str = "us", start_date: str | None = None,
                      end_date: str | None = None,
                      position_notional: float = 10_000.0,
                      txn_cost_pct: float | None = None,
                      slippage_pct: float = 0.0005,
                      max_matches: int = 2000) -> dict:
    from core.market import get_market

    profile = get_market(market)
    cost = profile.txn_cost_pct if txn_cost_pct is None else float(txn_cost_pct)
    trades: list[dict] = []
    matches: list[dict] = []
    errors: list[dict] = []
    for symbol in symbols:
        candles = load_candles(symbol, market)
        if not candles:
            errors.append({"symbol": symbol, "error": "no daily history"})
            continue
        df, dates = frame_from_candles(candles)
        if len(df) < required_bars(rule):
            errors.append({"symbol": symbol, "error": "insufficient history"})
            continue
        series = evaluate_series(rule, df)
        warm = required_bars(rule)
        for i in range(warm, len(df)):
            if len(matches) >= max_matches:
                break
            matches.append({
                "symbol": symbol,
                "date": dates[i],
                "close": round(float(df["close"].iloc[i]), 4),
                "matched": bool(series.iloc[i]),
            })
        trades.extend(_replay(
            symbol, df, dates, rule, notional=float(position_notional),
            txn_cost=cost, slippage=float(slippage_pct),
            start_date=start_date, end_date=end_date,
        ))
    trades.sort(key=lambda t: (t["exit_date"], t["symbol"]))
    return {
        "symbols": list(symbols),
        "market": market,
        "trades": trades,
        "summary": _summary(trades),
        "matches": matches,
        "errors": errors,
        "params": {
            "position_notional": float(position_notional),
            "txn_cost_pct": cost,
            "slippage_pct": float(slippage_pct),
        },
    }


def preview_rule(rule: RulePayload, symbol: str, *, market: str = "us",
                 bars: int = 120, max_bars: int = 500) -> dict:
    bars = max(1, min(int(bars or 120), max_bars))
    candles = load_candles(symbol, market)
    if not candles:
        raise ValueError(f"No daily history for {symbol}.")
    df, dates = frame_from_candles(candles)
    need = required_bars(rule)
    if len(df) < need:
        raise ValueError(
            f"{symbol} has {len(df)} bars; this rule needs {need}.")
    series = evaluate_series(rule, df)
    start = max(need, len(df) - bars)
    rows = []
    for i in range(start, len(df)):
        evidence = evaluate_bar(rule, df, i, series=series)
        rows.append({
            "date": dates[i],
            "close": round(float(df["close"].iloc[i]), 4),
            "matched": bool(evidence["matched"]),
            "warm": bool(evidence["warm"]),
            "conditions": evidence["conditions"],
        })
    return {
        "symbol": symbol,
        "market": market,
        "required_bars": need,
        "available_bars": len(df),
        "matched_count": sum(1 for row in rows if row["matched"]),
        "bars": rows,
    }
