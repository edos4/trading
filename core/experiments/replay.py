"""Causal per-symbol experiment replay; never touches a Paper account.

EMA comparison accounts are independent fixed-notional research. The SPY
example is a separate finite-cash account. Research books turn a directional
observation into one fixed-notional position and exit after the declared
hold. PH stays long-only.
"""
from __future__ import annotations

import math
from datetime import date
from typing import Callable

import pandas as pd

from core.experiments.adapters import Variant, variants
from core.experiments.contracts import BatchRequest
from core.market import get_market, round_qty_to_lot


class Cancelled(RuntimeError):
    pass


def replay(number: int, symbol: str, df: pd.DataFrame, request: BatchRequest,
           *, cancel: Callable[[], bool] = lambda: False) -> dict:
    df = df.loc[df.date <= request.end_date.isoformat()].reset_index(drop=True)
    output = {"signals": [], "trades": [], "positions": [], "outcomes": [], "logs": [],
              "equity": {}, "variants": {}, "coverage": {"bars": len(df)}}
    if df.empty:
        output["coverage"]["reason"] = "No history"
        output["logs"].append({"symbol":symbol,"date":request.start_date.isoformat(),
                               "status":"unavailable","reason":"No daily history"})
        return output
    first, end = request.start_date.isoformat(), request.end_date.isoformat()
    dates = list(df.date)
    in_range = [i for i, day in enumerate(dates) if first <= day <= end]
    output["coverage"]["in_range_bars"] = len(in_range)
    if not in_range:
        output["coverage"]["reason"] = "No bars inside the selected dates"
        output["logs"].append({"symbol":symbol,"date":first,"status":"outside-date-range",
                               "reason":"No completed bars inside the selected dates"})
        return output
    if number == 40 and symbol != "SPY":
        output["coverage"]["reason"] = "Source universe is SPY only"
        return output
    # The history adapter already trims to end_date; keep replay safe for
    # direct callers and future bar-loader implementations as well.
    df = df.iloc[:in_range[-1] + 1].copy()
    for variant in variants(number, df):
        if cancel():
            raise Cancelled()
        if len(df) <= variant.warmup or in_range[-1] < variant.warmup:
            output["variants"][variant.name] = {"status": "insufficient-history", "required_bars": variant.warmup + 1}
            output["logs"].append({"symbol":symbol,"variant":variant.name,"date":first,
                                   "status":"not-ready","reason":f"Requires {variant.warmup + 1} completed bars including warm-up; available {len(df)}"})
            continue
        _variant(symbol, df, request, variant, output, cancel)
    return output


def _variant(symbol: str, df: pd.DataFrame, request: BatchRequest, variant: Variant,
             output: dict, cancel: Callable[[], bool]) -> None:
    cost = request.txn_cost_pct
    if cost is None:
        cost = get_market(request.market).txn_cost_pct
    slip, notional = request.slippage_pct, request.position_notional
    finite = bool(variant.policy.get("finite_account"))
    position, pending = None, None
    realized = 0.0
    days = {}
    stats = {"status": "completed", "kind": variant.kind, "signal_count": 0,
             "trade_count": 0, "wins": 0, "gross_profit": 0.0, "gross_loss": 0.0,
             "fees": 0.0, "turnover": 0.0, "realized_pnl": 0.0, "unrealized_pnl": 0.0,
             "r_total": 0.0, "r_count": 0,
             "account_model": "finite-cash" if finite else "independent-fixed-notional"}

    # Daily proxy. Forecast capacity from previously completed sessions only.
    volume = df.volume.to_numpy(dtype=float)
    trailing_shares = df.volume.rolling(20, min_periods=1).mean().shift(1).fillna(0).to_numpy()
    trailing_value = (df.volume * df.close).rolling(20, min_periods=1).mean().shift(1).fillna(0).to_numpy()
    executed = {}

    def capacity(index: int) -> int:
        if index == 0:
            return 0
        prior_close = float(df.close.iloc[index - 1])
        available = min(trailing_shares[index], trailing_value[index] / prior_close)
        return max(0, math.floor(available * request.participation) - executed.get(index, 0))

    def supported(index: int, qty: int) -> bool:
        # Ex-post impossibility check only. Never tune the quantity to today's volume.
        return qty > 0 and qty + executed.get(index, 0) <= volume[index]

    def event(index: int, status: str, direction: int = 1, **extra) -> dict:
        return {"symbol": symbol, "variant": variant.name, "date": str(df.date.iloc[index]),
                "action": "BUY" if direction > 0 else "SELL", "status": status, **extra}

    def close(index: int, raw_price: float, reason: str) -> bool:
        nonlocal position, realized
        short = position["action"] == "SELL"
        direction = 1 if short else -1
        qty = position["qty"]
        if qty > capacity(index) or not supported(index, qty):
            output["logs"].append(event(index, "unfilled", direction, reason="Insufficient exit liquidity",
                                        requested_qty=qty, filled_qty=0, reference_price=raw_price))
            return False
        executed[index] = executed.get(index, 0) + qty
        price = raw_price * (1 + slip) if short else raw_price * (1 - slip)
        fees = price * position["qty"] * cost
        change = (position["entry_price"] - price) if short else (price - position["entry_price"])
        net = change * position["qty"] - position["entry_fee"] - fees
        realized += net
        stats["trade_count"] += 1
        stats["wins"] += int(net > 0)
        stats["gross_profit"] += max(net, 0)
        stats["gross_loss"] += max(-net, 0)
        stats["fees"] += fees
        stats["turnover"] += price * position["qty"]
        if position.get("planned_risk",0) > 0:
            stats["r_total"] += net / position["planned_risk"]
            stats["r_count"] += 1
        output["trades"].append({
            **event(index, "closed"), **position, "exit_date": str(df.date.iloc[index]),
            "exit_price": price, "exit_reason": reason, "exit_reference_price": raw_price,
            "exit_requested_qty": qty, "exit_filled_qty": qty, "exit_shortfall": abs(price-raw_price)*qty, "fees": position["entry_fee"] + fees,
            "pnl": net, "pnl_pct": 100 * net / (position["entry_price"] * position["qty"]),
            "bars": index - position["entry_index"],
            "r": net / position["planned_risk"] if position.get("planned_risk", 0) > 0 else None,
        })
        position = None
        return True

    def open_pnl(mark_price: float) -> float:
        if position is None:
            return 0.0
        change = (position["entry_price"] - mark_price) if position["action"] == "SELL" else (mark_price - position["entry_price"])
        return change * position["qty"] - position["entry_fee"]

    long_only = get_market(request.market).long_only
    start, end = request.start_date.isoformat(), request.end_date.isoformat()
    for i in range(variant.warmup, len(df)):
        if i % 128 == 0 and cancel():
            raise Cancelled()
        day = str(df.date.iloc[i])
        if day < start or day > end:
            continue
        o, c = float(df.open.iloc[i]), float(df.close.iloc[i])
        if pending is not None:
            action, signal_index, detail = pending
            pending = None
            if action == "exit" and position is not None:
                if not close(i, o, detail):
                    pending = ("exit", signal_index, detail)
            elif action == "entry" and position is None:
                direction = int(detail)
                short = direction < 0
                price = o * (1 - slip) if short else o * (1 + slip)
                qty = math.floor(notional / (price * (1 + cost)))
                planned_risk, stop = 0.0, None
                if short and finite:
                    output["logs"].append(event(i, "rejected", direction, reason="Short risk model is not defined"))
                    continue
                if finite:
                    atr = float(variant.policy["atr"][signal_index])
                    distance = variant.policy["trail_atr"] * atr
                    capital = request.initial_capital + realized
                    if not math.isfinite(distance) or distance <= 0 or capital <= 0:
                        output["logs"].append(event(i, "rejected", reason="No valid risk budget/ATR"))
                        continue
                    qty = min(math.floor(capital * variant.policy["risk_fraction"] / distance),
                              math.floor(capital / (price * (1 + cost))))
                    stop, planned_risk = price - distance, qty * distance
                    if stop <= 0:
                        output["logs"].append(event(i, "rejected", reason="ATR invalidation is not a positive market price"))
                        continue
                requested_qty = qty
                qty = min(qty, capacity(i))
                if request.market == "ph":
                    qty = round_qty_to_lot(qty, price, as_of=date.fromisoformat(day))
                if not supported(i, qty):
                    qty = 0
                if qty < 1:
                    output["logs"].append(event(i, "rejected", direction, reason="Insufficient liquidity or permitted lot", requested_qty=requested_qty, filled_qty=0, reference_price=o))
                else:
                    executed[i] = executed.get(i, 0) + qty
                    if finite:
                        planned_risk = qty * distance
                    fee = price * qty * cost
                    position = {"symbol": symbol, "variant": variant.name,
                                "action": "SELL" if short else "BUY",
                                "signal_date": str(df.date.iloc[signal_index]), "entry_date": day,
                                "entry_index": i, "entry_price": price, "qty": qty,
                                "entry_requested_qty": requested_qty, "entry_filled_qty": qty,
                                "entry_reference_price": o, "entry_shortfall": abs(price-o)*qty,
                                "entry_fee": fee, "stop_loss": stop, "take_profit": None,
                                "planned_risk": planned_risk, "high_close": price}
                    stats["fees"] += fee
                    stats["turnover"] += price * qty
                    output["logs"].append(event(i, "filled", direction, entry_price=price, qty=qty,
                                                 signal_date=str(df.date.iloc[signal_index])))
        if variant.kind == "trades":
            exit_signal = bool(variant.exit[i])
            exit_reason = "signal-next-open"
            hold = int(variant.policy.get("hold_bars") or 0)
            if position is not None and hold and i >= position["entry_index"] + hold - 1:
                exit_signal = True
                if not bool(variant.exit[i]):
                    exit_reason = f"hold-{hold}"
            if position is not None and finite and position["action"] == "BUY":
                stop = position["stop_loss"]
                exit_signal = exit_signal or (stop is not None and c <= stop)
                position["high_close"] = max(position["high_close"], c)
                atr = float(variant.policy["atr"][i])
                if math.isfinite(atr):
                    position["stop_loss"] = max(stop, position["high_close"] - variant.policy["trail_atr"] * atr)
            if position is not None and exit_signal and pending is None:
                pending = ("exit", i, exit_reason)
                closing = -1 if position["action"] == "BUY" else 1
                output["signals"].append(event(i, "exit-queued", closing,
                                               evidence=variant.evidence(i)))
            elif position is None and int(variant.entry[i]) != 0:
                direction = int(variant.entry[i])
                stats["signal_count"] += 1
                output["signals"].append(event(i, "entry-queued", direction, evidence=variant.evidence(i)))
                if direction < 0 and long_only:
                    output["logs"].append(event(i, "rejected", direction, reason="PH book is long-only"))
                else:
                    pending = ("entry", i, direction)
        elif variant.entry[i]:
            direction = int(variant.entry[i])
            stats["signal_count"] += 1
            signal = event(i, "observed", direction, evidence=variant.evidence(i))
            if variant.policy.get("neutral"):
                signal["action"] = "OBSERVE"
            output["signals"].append(signal)
            if variant.policy.get("neutral"):
                continue
            for horizon in (5, 20):
                # Outcomes are separate evidence and never fed back into the
                # indicator. A next-open entry and complete horizon are needed.
                endpoint = i + horizon
                measured = endpoint < len(df) and str(df.date.iloc[endpoint]) <= end
                outcome = {**signal, "horizon": horizon, "complete": measured,
                           "return_pct": None, "mae_pct": None, "mfe_pct": None}
                if measured:
                    entry = float(df.open.iloc[i + 1])
                    highs = df.high.iloc[i + 1:endpoint + 1]
                    lows = df.low.iloc[i + 1:endpoint + 1]
                    changes = [(float(highs.max()) / entry - 1) * 100,
                               (float(lows.min()) / entry - 1) * 100]
                    signed = [direction * value for value in changes]
                    outcome.update(return_pct=direction * (float(df.close.iloc[endpoint]) / entry - 1) * 100,
                                   mae_pct=min(signed), mfe_pct=max(signed),
                                   entry_date=str(df.date.iloc[i + 1]), outcome_date=str(df.date.iloc[endpoint]))
                output["outcomes"].append(outcome)
        if variant.kind == "trades":
            days[day] = realized + open_pnl(c)
    if variant.kind == "trades":
        last = len(df) - 1
        if position is not None and request.end_policy == "force-close":
            if close(last, float(df.close.iloc[last]), "end-date-liquidation"):
                days[str(df.date.iloc[last])] = realized
        if position is not None:
            current = float(df.close.iloc[last])
            unrealized = open_pnl(current)
            output["positions"].append({**position, "date": str(df.date.iloc[last]), "current": current,
                                         "unrealized_pnl": unrealized,
                                         "unrealized_pct": 100 * unrealized / (position["entry_price"] * position["qty"]),
                                         "bars": last - position["entry_index"],
                                         "value": current * position["qty"]})
            stats["unrealized_pnl"] = unrealized
        if pending:
            output["logs"].append(event(last, "unfilled", reason="No later eligible bar before end date",
                                         pending=pending[0]))
        stats["realized_pnl"] = realized
        output["equity"][variant.name] = days
    else:
        for key in ("realized_pnl", "unrealized_pnl", "gross_profit", "gross_loss", "fees", "turnover"):
            stats[key] = None
    output["variants"][variant.name] = stats
