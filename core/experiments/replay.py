"""Causal per-symbol experiment replay; never touches a Paper account.

EMA comparison accounts are independent fixed-notional research. The SPY
example is a separate finite-cash account. Research books turn a directional
observation into one fixed-notional position and exit after the declared
hold. PH stays long-only.
"""
from __future__ import annotations

import math
from datetime import date, timedelta
from typing import Callable

import pandas as pd

from core.experiments.adapters import Variant, variants
from core.experiments.contracts import BatchRequest
from core.experiments.history import is_session, shift_session
from core.market import get_market, pse_execution_price, round_qty_to_lot


def fee_rate(request: BatchRequest, day: date, *, buy: bool) -> float:
    if request.txn_cost_pct is not None:
        return request.txn_cost_pct
    if request.market != "ph":
        return get_market(request.market).txn_cost_pct
    # PSE illustrative broker + VAT, SRC, exchange + VAT, and clearing.
    base = 0.003006
    if buy:
        return base
    tax = 0.001 if day >= date(2025, 7, 1) else 0.006 if day >= date(2018, 1, 1) else 0.005
    return base + tax


class Cancelled(RuntimeError):
    pass


def replay(number: int, symbol: str, df: pd.DataFrame, request: BatchRequest,
           *, cancel: Callable[[], bool] = lambda: False) -> dict:
    df = df.loc[df.date <= request.end_date.isoformat()].reset_index(drop=True)
    output = {"signals": [], "trades": [], "positions": [], "outcomes": [], "logs": [],
              "equity": {}, "exposure": {}, "variants": {}, "coverage": {"bars": len(df)}}
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
    slip, notional = request.slippage_pct, request.position_notional
    finite = bool(variant.policy.get("finite_account"))
    position, pending = None, None
    pending_qty = 0
    realized = 0.0
    days = {}
    exposure = {}
    stats = {"status": "completed", "kind": variant.kind, "signal_count": 0,
             "trade_count": 0, "wins": 0, "gross_profit": 0.0, "gross_loss": 0.0,
             "fees": 0.0, "turnover": 0.0, "realized_pnl": 0.0, "unrealized_pnl": 0.0,
             "long_pnl": 0.0, "short_pnl": 0.0, "borrow_fees": 0.0, "r_total": 0.0, "r_count": 0,
             "loss_r_0_to_1": 0, "loss_r_1_to_2": 0, "loss_r_over_2": 0,
             "account_model": "finite-cash" if finite else "independent-fixed-notional"}

    # Daily proxy. Forecast capacity from previously completed sessions only.
    volume = df.volume.to_numpy(dtype=float)
    trailing_shares = df.volume.rolling(20, min_periods=1).mean().shift(1).fillna(0).to_numpy()
    trailing_value = (df.volume * df.close).rolling(20, min_periods=1).mean().shift(1).fillna(0).to_numpy()
    executed = {}
    market = request.market
    dates = [date.fromisoformat(str(day)) for day in df.date]
    calendar = {}
    serial = 0
    if dates:
        day = dates[0]
        while day <= dates[-1]:
            serial += is_session(market, day)
            calendar[day] = serial
            day += timedelta(days=1)
    hold = int(variant.policy.get("hold_bars") or 0)

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

    def decision_quantity(i: int, direction: int) -> int:
        estimate = float(df.close.iloc[i]) * (1 + slip * direction)
        budget = request.initial_capital + realized if finite else notional
        buy = direction > 0
        if market == "ph":
            estimate = pse_execution_price(estimate, buy=buy)
            if estimate is None:
                return 0
        cost = fee_rate(request, shift_session(market, dates[i], 1), buy=buy)
        shares = max(0, math.floor(budget / (estimate * (1 + cost))))
        if finite:
            distance = variant.policy["trail_atr"] * float(variant.policy["atr"][i])
            if not math.isfinite(distance) or distance <= 0:
                return 0
            shares = min(shares, math.floor(budget * variant.policy["risk_fraction"] / distance))
        return shares

    def borrow_charge(index: int) -> float:
        if position["action"] != "SELL":
            return 0.0
        elapsed = (dates[index] - dates[position["entry_index"]]).days
        return position["qty"] * position["entry_price"] * request.borrow_rate * elapsed / 365

    def close(index: int, raw_price: float, reason: str) -> bool:
        nonlocal position, realized
        short = position["action"] == "SELL"
        direction = 1 if short else -1
        qty = position["qty"]
        price = raw_price * (1 + slip) if short else raw_price * (1 - slip)
        if market == "ph":
            price = pse_execution_price(price, buy=short)
            if price is None or round_qty_to_lot(qty, price, as_of=dates[index]) != qty:
                output["logs"].append(event(index, "unfilled", direction, reason="No regular-lot exit at this tick",
                                            requested_qty=qty, filled_qty=0, reference_price=raw_price))
                return False
        if qty > capacity(index) or not supported(index, qty):
            output["logs"].append(event(index, "unfilled", direction, reason="Insufficient exit liquidity",
                                        requested_qty=qty, filled_qty=0, reference_price=raw_price))
            return False
        executed[index] = executed.get(index, 0) + qty
        cost = fee_rate(request, dates[index], buy=short)
        fees = price * position["qty"] * cost
        change = (position["entry_price"] - price) if short else (price - position["entry_price"])
        borrow = borrow_charge(index)
        net = change * position["qty"] - position["entry_fee"] - fees - borrow
        stats["borrow_fees"] += borrow
        stats["short_pnl" if short else "long_pnl"] += net
        realized += net
        stats["trade_count"] += 1
        stats["wins"] += int(net > 0)
        stats["gross_profit"] += max(net, 0)
        stats["gross_loss"] += max(-net, 0)
        stats["fees"] += fees
        stats["turnover"] += price * position["qty"]
        if position.get("planned_risk",0) > 0:
            realized_r = net / position["planned_risk"]
            if realized_r < 0:
                bucket = "loss_r_over_2" if realized_r < -2 else "loss_r_1_to_2" if realized_r < -1 else "loss_r_0_to_1"
                stats[bucket] += 1
            stats["r_total"] += realized_r
            stats["r_count"] += 1
        output["trades"].append({
            **event(index, "closed"), **position, "exit_date": str(df.date.iloc[index]),
            "exit_price": price, "exit_fee_rate": cost, "exit_reason": reason, "exit_reference_price": raw_price,
            "exit_requested_qty": qty, "exit_filled_qty": qty, "exit_shortfall": abs(price-raw_price)*qty, "fees": position["entry_fee"] + fees,
            "borrow_fee": borrow, "pnl": net, "pnl_pct": 100 * net / (position["entry_price"] * position["qty"]),
            "bars": index - position["entry_index"],
            "elapsed_sessions": calendar[dates[index]] - calendar[dates[position["entry_index"]]],
            "r": net / position["planned_risk"] if position.get("planned_risk", 0) > 0 else None,
        })
        position = None
        return True

    def open_pnl(mark_price: float, index: int) -> float:
        if position is None:
            return 0.0
        change = (position["entry_price"] - mark_price) if position["action"] == "SELL" else (mark_price - position["entry_price"])
        return change * position["qty"] - position["entry_fee"] - borrow_charge(index)

    long_only = get_market(request.market).long_only
    start, end = request.start_date.isoformat(), request.end_date.isoformat()
    for i in range(variant.warmup, len(df)):
        if i % 128 == 0 and cancel():
            raise Cancelled()
        day = str(df.date.iloc[i])
        if day < start or day > end:
            continue
        o, c = float(df.open.iloc[i]), float(df.close.iloc[i])
        if position and hold and pending is None and dates[i] >= date.fromisoformat(position["scheduled_exit"]):
            pending = ("exit", position["entry_index"], f"hold-{hold}")
        if pending is not None:
            action, signal_index, detail = pending
            pending = None
            if action == "entry" and dates[i] != shift_session(market, dates[signal_index], 1):
                output["logs"].append(event(i, "expired", int(detail), reason="Missing next exchange session",
                                            signal_date=str(dates[signal_index])))
            elif action == "exit" and position is not None:
                if not close(i, o, detail):
                    pending = ("exit", signal_index, detail)
            elif action == "entry" and position is None:
                direction = int(detail)
                short = direction < 0
                price = o * (1 - slip) if short else o * (1 + slip)
                if market == "ph":
                    price = pse_execution_price(price, buy=not short)
                cost = fee_rate(request, dates[i], buy=not short)
                budget = request.initial_capital + realized if finite else notional
                qty = min(pending_qty, math.floor(budget / (price * (1 + cost))))
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
                    stop, planned_risk = price - distance, qty * distance
                    if stop <= 0:
                        output["logs"].append(event(i, "rejected", reason="ATR invalidation is not a positive market price"))
                        continue
                requested_qty = pending_qty
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
                                "scheduled_exit": shift_session(market, dates[i], hold).isoformat() if hold else None,
                                "entry_index": i, "entry_price": price, "qty": qty,
                                "entry_requested_qty": requested_qty, "entry_filled_qty": qty,
                                "entry_reference_price": o, "entry_shortfall": abs(price-o)*qty,
                                "entry_fee": fee, "entry_fee_rate": cost, "stop_loss": stop, "take_profit": None,
                                "planned_risk": planned_risk, "high_close": price}
                    if finite:
                        position.update(initial_stop=stop, initial_risk=planned_risk, trail_history=[])
                    stats["fees"] += fee
                    stats["turnover"] += price * qty
                    output["logs"].append(event(i, "filled", direction, entry_price=price, qty=qty,
                                                 signal_date=str(df.date.iloc[signal_index])))
        if variant.kind == "trades":
            exit_signal = bool(variant.exit[i])
            exit_reason = "signal-next-open"
            if position is not None and hold and shift_session(market, dates[i], 1) >= date.fromisoformat(position["scheduled_exit"]):
                exit_signal = True
                if not bool(variant.exit[i]):
                    exit_reason = f"hold-{hold}"
            if position is not None and finite and position["action"] == "BUY":
                stop = position["stop_loss"]
                trail_breach = c <= stop
                if exit_signal or trail_breach:
                    exit_reason = "prior-low-and-trail" if exit_signal and trail_breach else "prior-low" if exit_signal else "atr-trail"
                exit_signal = exit_signal or trail_breach
                position["trail_history"].append({"date": day, "active_stop": stop})
                position["high_close"] = max(position["high_close"], c)
                atr = float(variant.policy["atr"][i])
                if math.isfinite(atr):
                    position["stop_loss"] = max(stop, position["high_close"] - variant.policy["trail_atr"] * atr)
            if position is not None and int(variant.entry[i]):
                output["signals"].append(event(i, "suppressed", int(variant.entry[i]), reason="Position held",
                                               evidence=variant.evidence(i)))
            if position is not None and exit_signal and pending is None:
                if finite:
                    position["exit_trigger"] = {"date": day, "close": c, "active_stop": stop,
                                                "reason": exit_reason, **variant.evidence(i)}
                pending = ("exit", i, exit_reason)
                closing = -1 if position["action"] == "BUY" else 1
                output["signals"].append(event(i, "exit-queued", closing,
                                               reason=exit_reason, evidence=position.get("exit_trigger") or variant.evidence(i)))
            elif position is None and int(variant.entry[i]) != 0:
                direction = int(variant.entry[i])
                stats["signal_count"] += 1
                evidence = variant.evidence(i)
                if direction < 0 and (long_only or request.short_policy == "exclude"):
                    reason = "PH book is long-only" if long_only else "Borrow inventory unavailable"
                    output["signals"].append(event(i, "ineligible", direction, reason=reason, evidence=evidence))
                    output["logs"].append(event(i, "rejected", direction, reason=reason))
                else:
                    pending_qty = decision_quantity(i, direction)
                    output["signals"].append(event(i, "entry-queued", direction, evidence=evidence,
                                                   requested_qty=pending_qty, decision_price=c))
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
            days[day] = realized + open_pnl(c, i)
            gross = position["qty"] * c if position else 0.0
            net = -gross if position and position["action"] == "SELL" else gross
            basis = position["qty"] * position["entry_price"] + position["entry_fee"] if position else 0.0
            exposure[day] = (gross, net, basis)
    if hold:
        # Separate diagnostic. Forward prices are read only after order replay.
        indices = {day: index for index, day in enumerate(dates)}
        for i in range(variant.warmup, len(df)):
            side = int(variant.entry[i])
            if not side or str(dates[i]) < start or (side < 0 and (long_only or request.short_policy == "exclude")):
                continue
            if i % 128 == 0 and cancel():
                raise Cancelled()
            entry_day = shift_session(market, dates[i], 1)
            exit_day = shift_session(market, entry_day, hold)
            entry_i, exit_i = indices.get(entry_day), indices.get(exit_day)
            complete = entry_i is not None and exit_i is not None and volume[entry_i] > 0 and volume[exit_i] > 0
            outcome = event(i, "all-trigger-study", side, horizon=hold, complete=bool(complete),
                            entry_date=str(entry_day), outcome_date=str(exit_day), return_pct=None,
                            mae_pct=None, mfe_pct=None)
            if complete:
                entry = float(df.open.iloc[entry_i])
                exit_open = float(df.open.iloc[exit_i])
                extremes = [entry, exit_open, df.low.iloc[entry_i:exit_i].min(), df.high.iloc[entry_i:exit_i].max()]
                excursions = [100 * side * (px/entry - 1) for px in extremes]
                outcome.update(return_pct=100 * side * (exit_open/entry-1),
                               mae_pct=min(excursions), mfe_pct=max(excursions))
            output["outcomes"].append(outcome)
    if variant.kind == "trades":
        last = len(df) - 1
        if position is not None and request.end_policy == "force-close":
            if close(last, float(df.close.iloc[last]), "end-date-liquidation"):
                days[str(df.date.iloc[last])] = realized
                exposure[str(df.date.iloc[last])] = (0.0, 0.0, 0.0)
        if position is not None:
            current = float(df.close.iloc[last])
            unrealized = open_pnl(current, last)
            output["positions"].append({**position, "date": str(df.date.iloc[last]), "current": current,
                                         "unrealized_pnl": unrealized,
                                         "unrealized_pct": 100 * unrealized / (position["entry_price"] * position["qty"]),
                                         "bars": last - position["entry_index"],
                                         "elapsed_sessions": calendar[dates[last]] - calendar[dates[position["entry_index"]]],
                                         "value": current * position["qty"]})
            stats["unrealized_pnl"] = unrealized
            stats["borrow_fees"] += borrow_charge(last)
            stats["short_pnl" if position["action"] == "SELL" else "long_pnl"] += unrealized
        if pending:
            output["logs"].append(event(last, "unfilled", reason="No later eligible bar before end date",
                                         pending=pending[0]))
        stats["realized_pnl"] = realized
        output["equity"][variant.name] = days
        output["exposure"][variant.name] = exposure
    else:
        for key in ("realized_pnl", "unrealized_pnl", "gross_profit", "gross_loss", "fees", "turnover"):
            stats[key] = None
    output["variants"][variant.name] = stats
