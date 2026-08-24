"""On-demand Kronos OHLC forecast for the --ui / --web chart viewers."""

from __future__ import annotations

import re
from typing import Any

import pandas as pd

from analysis.chart_renderer import build_trade_viewer_payload, _viewer_bar_time, _viewer_finite
from config import settings
from core.kronos_eval import LOOKBACK, MAX_CONTEXT, with_amount
from core.kronos_gate import get_kronos_gate, kronos_infer_lock
from core.market import get_market
from utils.logger import log

MIN_PRED_DAYS = 1
MAX_PRED_DAYS = 120
DEFAULT_PRED_DAYS = 3
MIN_BARS = 60
_SYMBOL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9.\-]{0,15}$")
KRONOS_LINE = "#e040fb"


def normalize_symbol(raw: str) -> str:
    symbol = (raw or "").strip().upper()
    if not _SYMBOL_RE.fullmatch(symbol):
        raise ValueError("Enter a ticker like AAPL (letters, digits, . or -).")
    return symbol


def clamp_pred_days(days: int) -> int:
    try:
        n = int(days)
    except (TypeError, ValueError) as exc:
        raise ValueError("Prediction days must be an integer.") from exc
    if n < MIN_PRED_DAYS or n > MAX_PRED_DAYS:
        raise ValueError(f"Prediction days must be {MIN_PRED_DAYS}–{MAX_PRED_DAYS}.")
    return n


def predict_ohlc(
    predictor,
    df: pd.DataFrame,
    pred_len: int,
    *,
    sample_count: int = 1,
    lookback: int = LOOKBACK,
) -> pd.DataFrame:
    """Forecast the next ``pred_len`` trading days of OHLC from ``df``."""
    if df is None or len(df) < MIN_BARS:
        raise ValueError(f"Need at least {MIN_BARS} daily bars for Kronos.")
    pred_len = clamp_pred_days(pred_len)
    use = min(lookback, len(df), MAX_CONTEXT)
    if use < MIN_BARS:
        raise ValueError(f"Need at least {MIN_BARS} daily bars for Kronos.")
    x_df = with_amount(df.iloc[-use:])
    last = pd.Timestamp(x_df.index[-1])
    if last.tzinfo is not None:
        last = last.tz_convert("UTC").tz_localize(None)
    y_timestamp = pd.Series(pd.bdate_range(start=last, periods=pred_len + 1, freq="B")[1:])
    pred_df = predictor.predict(
        df=x_df.reset_index(drop=True),
        x_timestamp=pd.Series(x_df.index),
        y_timestamp=y_timestamp,
        pred_len=pred_len,
        T=1.0,
        top_p=0.9,
        sample_count=sample_count,
        verbose=False,
    )
    if pred_df is None or len(pred_df) < pred_len:
        raise ValueError("Kronos returned an empty forecast.")
    out = pred_df.iloc[:pred_len].copy()
    out.index = pd.DatetimeIndex(y_timestamp.iloc[: len(out)].to_list())
    return out


def build_kronos_viewer_payload(
    actual_df: pd.DataFrame,
    pred_df: pd.DataFrame,
    *,
    symbol: str,
    session_tz: str = "America/New_York",
) -> dict[str, Any]:
    """Trade-viewer payload plus predicted candles and a Kronos close line."""
    payload = build_trade_viewer_payload(
        actual_df,
        symbol=symbol,
        timeframe="1d",
        session_tz=session_tz,
    )
    origin_time = payload["candles"][-1]["time"]
    last_close = float(payload["candles"][-1]["close"])
    pred_candles: list[dict[str, Any]] = []
    forecast = [{"time": origin_time, "value": last_close}]
    for idx, row in pred_df.iterrows():
        t = _viewer_bar_time(idx)
        o = _viewer_finite(row.get("open"))
        h = _viewer_finite(row.get("high"))
        low = _viewer_finite(row.get("low"))
        c = _viewer_finite(row.get("close"))
        if None in (o, h, low, c):
            continue
        if h < max(o, c):
            h = max(o, c)
        if low > min(o, c):
            low = min(o, c)
        pred_candles.append({
            "time": t,
            "open": o,
            "high": h,
            "low": low,
            "close": c,
            "predicted": True,
        })
        forecast.append({"time": t, "value": c})

    if not pred_candles:
        raise ValueError("Kronos forecast had no valid OHLC bars.")

    pred_close = float(pred_candles[-1]["close"])
    pred_return = pred_close / last_close - 1.0 if last_close else 0.0
    payload["title"] = f"{symbol} 1D · Kronos {len(pred_candles)}d"
    payload["pred_candles"] = pred_candles
    payload["forecast"] = forecast
    payload["forecast_color"] = KRONOS_LINE
    payload["markers"] = list(payload.get("markers") or []) + [{
        "time": origin_time,
        "position": "aboveBar",
        "color": KRONOS_LINE,
        "shape": "circle",
        "text": "Kronos",
    }]
    payload["pred"] = {
        "days": len(pred_candles),
        "origin": origin_time,
        "last_close": last_close,
        "pred_close": pred_close,
        "pred_return_pct": pred_return * 100.0,
    }
    return payload


def forecast_symbol(
    symbol: str,
    days: int,
    *,
    market: str | None = None,
) -> dict[str, Any]:
    """Fetch daily history, run Kronos, return a trade-viewer chart payload."""
    symbol = normalize_symbol(symbol)
    days = clamp_pred_days(days)
    profile = get_market(market)
    from data.history import load_daily_ohlcv_df

    df = load_daily_ohlcv_df(symbol, tv_fallback=True, limit=MAX_CONTEXT)
    if df is None or len(df) < MIN_BARS:
        raise ValueError(
            f"Not enough daily history for {symbol} (need ≥{MIN_BARS} bars)."
        )

    gate = get_kronos_gate()
    with kronos_infer_lock():
        if not gate._ensure_loaded():
            raise ValueError(
                "Kronos weights are missing or failed to load. "
                "See README Kronos setup (~/Kronos/weights)."
            )
        try:
            pred_df = predict_ohlc(
                gate._predictor,
                df,
                days,
                sample_count=settings.kronos_sample_count,
                lookback=LOOKBACK,
            )
        except ValueError:
            raise
        except Exception as exc:
            log.exception(f"Kronos | forecast failed for {symbol}")
            raise ValueError(f"Kronos prediction failed: {exc}") from exc

    payload = build_kronos_viewer_payload(
        df, pred_df, symbol=symbol, session_tz=profile.session_tz,
    )
    payload["market"] = profile.id
    return payload
