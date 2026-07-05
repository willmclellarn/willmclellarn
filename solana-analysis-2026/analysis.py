"""Recompute the SOL 2026 indicator set from data/sol_daily.csv.

Data assembly (see README §1):
  - closes: Coin Metrics community data + fawazahmed0/exchange-api daily snapshots
  - volume: Coin Metrics volume_reported_spot_usd_1d (through 2026-05-23)

Usage: python3 analysis.py   (requires pandas, numpy)
"""
import numpy as np
import pandas as pd

df = pd.read_csv("data/sol_daily.csv", parse_dates=["date"]).set_index("date")

df["ret"] = df["close"].pct_change()
for n in (20, 50, 100, 200):
    df[f"sma{n}"] = df["close"].rolling(n).mean()
df["macd"] = df["close"].ewm(span=12).mean() - df["close"].ewm(span=26).mean()
df["macd_sig"] = df["macd"].ewm(span=9).mean()

delta = df["close"].diff()
gain = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
loss = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
df["rsi14"] = 100 - 100 / (1 + gain / loss)

df["vol30_ann"] = df["ret"].rolling(30).std() * np.sqrt(365) * 100
sd = df["close"].rolling(20).std()
df["bb_up"], df["bb_dn"] = df["sma20"] + 2 * sd, df["sma20"] - 2 * sd

y = df.loc["2026-01-01":]
dd = y["close"] / y["close"].cummax() - 1
last = df.iloc[-1]

print(f"as of {df.index[-1].date()}  close ${last['close']:.2f}")
print(f"YTD {(last['close'] / y['close'].iloc[0] - 1) * 100:+.1f}%   "
      f"max drawdown {dd.min() * 100:.1f}%   current {dd.iloc[-1] * 100:.1f}%")
print(f"RSI14 {last['rsi14']:.1f}   MACD {last['macd']:.2f}/{last['macd_sig']:.2f}   "
      f"30d vol {last['vol30_ann']:.0f}%")
for n in (20, 50, 100, 200):
    print(f"  vs SMA{n} ${last[f'sma{n}']:.2f}: {(last['close'] / last[f'sma{n}'] - 1) * 100:+.1f}%")

m = y.resample("ME").agg(open=("close", "first"), close=("close", "last"),
                         hi=("close", "max"), lo=("close", "min"),
                         avg_vol=("vol_usd", "mean"))
m["ret%"] = (m["close"] / m["open"] - 1) * 100
print("\nmonthly 2026:\n", m.round(2))
