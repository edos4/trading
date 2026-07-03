"""Debug why each pattern's analyze() returns None on manufactured data."""
from __future__ import annotations
import numpy as np
import pandas as pd

from data.ohlcv_store import OHLCVStore
from data.tv_client import OHLCVCandle, MarketSnapshot
from analysis.indicator_engine import IndicatorEngine
from generate_pattern_demos import (build_candles, make_snapshot, up_down_vol,
    gen_double_top, gen_double_bottom, gen_rounding_bottom, gen_rounding_top,
    gen_upward_channel, gen_descending_channel, gen_head_and_shoulders,
    gen_ema_crossover)
from datetime import datetime, timezone

TF = "1d"

def store_df(closes, vols=None):
    if vols is None:
        vols = np.full(len(closes), 1_000_000, dtype=float)
    candles = build_candles(closes, vols)
    store = OHLCVStore(window=500)
    store.replace_all("DEMO", TF, candles)
    df = store.get_df("DEMO", TF)
    return df, candles

def swing_highs(high, lb=2):
    out = []
    for i in range(lb, len(high)-lb):
        left = high.iloc[i-lb:i]; right = high.iloc[i+1:i+lb+1]
        if high.iloc[i] >= left.max() and high.iloc[i] >= right.max():
            out.append(i)
    return out

def swing_lows(low, lb=2):
    out = []
    for i in range(lb, len(low)-lb):
        left = low.iloc[i-lb:i]; right = low.iloc[i+1:i+lb+1]
        if low.iloc[i] <= left.min() and low.iloc[i] <= right.min():
            out.append(i)
    return out

def show_rsi(df, idxs):
    ind = IndicatorEngine(df); rsi = ind.rsi(14)
    for i in idxs:
        print(f"    bar {i}: close={df['close'].iloc[i]:.2f} high={df['high'].iloc[i]:.2f} low={df['low'].iloc[i]:.2f} RSI={rsi.iloc[i]:.2f}")

print("=== 002 double_top ===")
c = gen_double_top()
vols = up_down_vol(c, 89, 120, 400_000, 1_500_000, 1_000_000)
df, candles = store_df(c, vols)
ind = IndicatorEngine(df); rsi = ind.rsi(14)
sh = swing_highs(df['high'])
print("  swing highs:", sh)
show_rsi(df, sh)
print("  total bars:", len(df), "last close:", df['close'].iloc[-1])

print("=== 003 double_bottom ===")
c = gen_double_bottom()
vols = up_down_vol(c, 89, 120, 1_500_000, 400_000, 1_000_000)
df, candles = store_df(c, vols)
ind = IndicatorEngine(df); rsi = ind.rsi(14)
sl = swing_lows(df['low'])
print("  swing lows:", sl)
show_rsi(df, sl)
print("  total bars:", len(df), "last close:", df['close'].iloc[-1])

print("=== 004 rounding_bottom ===")
c = gen_rounding_bottom()
df, candles = store_df(c)
ind = IndicatorEngine(df); rsi = ind.rsi(14)
sl = swing_lows(df['low'])
print("  swing lows:", sl[-10:])
show_rsi(df, sl[-5:])
print("  total bars:", len(df), "last close:", df['close'].iloc[-1])
print("  bottom area 195-199:")
show_rsi(df, [195,196,197,198,199])

print("=== 005 rounding_top ===")
c = gen_rounding_top()
df, candles = store_df(c)
ind = IndicatorEngine(df); rsi = ind.rsi(14)
sh = swing_highs(df['high'])
print("  swing highs:", sh[-10:])
show_rsi(df, sh[-5:])
print("  total bars:", len(df), "last close:", df['close'].iloc[-1])

print("=== 006 upward_channel ===")
c = gen_upward_channel()
df, candles = store_df(c)
ind = IndicatorEngine(df); rsi = ind.rsi(14)
sh = swing_highs(df['high'])
print("  swing highs:", sh)
show_rsi(df, sh)
print("  total bars:", len(df), "last close:", df['close'].iloc[-1])

print("=== 007 descending_channel ===")
c = gen_descending_channel()
df, candles = store_df(c)
ind = IndicatorEngine(df); rsi = ind.rsi(14)
sl = swing_lows(df['low'])
print("  swing lows:", sl)
show_rsi(df, sl)
print("  total bars:", len(df), "last close:", df['close'].iloc[-1])

print("=== 008 head_and_shoulders ===")
c = gen_head_and_shoulders()
df, candles = store_df(c)
ind = IndicatorEngine(df); rsi = ind.rsi_wilder(14)
# close-swing-highs with lb=4 (pattern uses HEAD_LOOKBACK=4)
def close_swing_highs(close, lb=4):
    out=[]
    for i in range(lb, len(close)-lb):
        cl=close.iloc[i-lb:i].to_numpy(); cr=close.iloc[i+1:i+lb+1].to_numpy()
        if close.iloc[i] > cl.max() and close.iloc[i] > cr.max():
            out.append(i)
    return out
csh = close_swing_highs(df['close'])
print("  close-swing-highs (lb=4):", csh)
show_rsi(df, csh)
print("  total bars:", len(df), "last close:", df['close'].iloc[-1])
