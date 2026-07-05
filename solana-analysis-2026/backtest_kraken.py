"""Backtest the mean-reversion strategy on Kraken's own SOL/USD candles.

Runs the same variants as backtest.py (see that file for the rules), but:
  - pulls OHLCV directly from Kraken's public API via ccxt (no API keys needed;
    run this on your own machine — Kraken's API is not reachable from every network)
  - prices costs off the Kraken Pro fee schedule (--tier / --maker)
  - supports intraday candles (--timeframe 4h/1h); Kraken's public OHLC endpoint
    returns at most 720 candles per timeframe, so 1d covers ~2 years, 4h ~4 months,
    1h ~30 days

Usage:
  pip install ccxt pandas numpy
  python3 backtest_kraken.py                       # daily candles, base-tier taker
  python3 backtest_kraken.py --tier 10k --maker    # your fee tier, limit entries
  python3 backtest_kraken.py --timeframe 4h        # recent-regime intraday check
  python3 backtest_kraken.py --csv data/sol_daily.csv   # offline fallback

Fills are approximated at candle close, stops at next close — same limitation as
backtest.py. With --maker, no slippage is charged but real limit orders have fill
risk the backtest can't see: assume results are optimistic.
"""
import argparse

import numpy as np
import pandas as pd

from backtest import Z_ENTRY, equity, run, stats

# Kraken Pro spot fee schedule (per side, bps) — check your live tier in-app:
# https://www.kraken.com/features/fee-schedule
FEE_TIERS_BPS = {
    "base": {"maker": 25, "taker": 40},   # < $10k 30-day volume
    "10k":  {"maker": 20, "taker": 35},
    "250k": {"maker": 10, "taker": 20},
    "10m":  {"maker": 0,  "taker": 5},
}
TAKER_SLIPPAGE_BPS = 10  # market-order slippage estimate on SOL/USD depth


def fetch_kraken(timeframe: str) -> pd.DataFrame:
    import ccxt

    kraken = ccxt.kraken()
    ohlcv = kraken.fetch_ohlcv("SOL/USD", timeframe=timeframe, limit=720)
    df = pd.DataFrame(ohlcv, columns=["ts", "open", "high", "low", "close", "volume"])
    df["date"] = pd.to_datetime(df["ts"], unit="ms")
    return df.set_index("date")[["open", "high", "low", "close", "volume"]]


def load_csv(path: str) -> pd.DataFrame:
    df = pd.read_csv(path, parse_dates=["date"]).set_index("date")
    return df[["close"]]


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df["ret"] = df["close"].pct_change()
    df["sma20"] = df["close"].rolling(20).mean()
    df["z"] = (df["close"] - df["sma20"]) / df["close"].rolling(20).std()
    df["slope50"] = df["close"].rolling(50).mean().pct_change(10)
    return df.dropna(subset=["z"])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--timeframe", default="1d", choices=["1d", "4h", "1h"])
    ap.add_argument("--tier", default="base", choices=sorted(FEE_TIERS_BPS))
    ap.add_argument("--maker", action="store_true",
                    help="price entries/exits as limit fills (no slippage charged; "
                         "real fill risk not modeled)")
    ap.add_argument("--csv", help="offline fallback: CSV with date,close columns "
                                  "instead of fetching from Kraken")
    args = ap.parse_args()

    fees = FEE_TIERS_BPS[args.tier]
    side_bps = fees["maker"] if args.maker else fees["taker"] + TAKER_SLIPPAGE_BPS
    cost = side_bps / 1e4

    raw = load_csv(args.csv) if args.csv else fetch_kraken(args.timeframe)
    df = add_indicators(raw)
    src = args.csv or f"Kraken SOL/USD {args.timeframe}"
    print(f"data: {src}  {df.index[0].date()} -> {df.index[-1].date()}  ({len(df)} candles)")
    print(f"costs: {args.tier} tier, {'maker' if args.maker else 'taker'} = "
          f"{side_bps:.0f}bps/side ({2 * side_bps / 100:.2f}% round trip)\n")

    bh = (1 + df["ret"]).cumprod()
    rows = [stats("buy & hold", bh, None)]
    for name, kw in {
        "V2 z-score + confirm L/S": dict(confirm=True, allow_short=True),
        "V4 + regime gate": dict(confirm=True, allow_short=True, regime_gate=True),
    }.items():
        tr = run(df, cost, **kw)
        rows.append(stats(name, equity(df, tr), tr))
        if name.startswith("V4"):
            v4_trades = tr
    print(pd.DataFrame(rows).to_string(index=False))

    print("\nV4 trade log:")
    print(v4_trades.to_string(index=False) if len(v4_trades) else "  (no trades)")

    print("\nfee-tier sensitivity (V4 total return):")
    grid = []
    for tier, f in FEE_TIERS_BPS.items():
        row = {"tier": tier}
        for mode, bps in [("taker", f["taker"] + TAKER_SLIPPAGE_BPS), ("maker", f["maker"])]:
            tr = run(df, bps / 1e4, confirm=True, allow_short=True, regime_gate=True)
            row[mode] = f"{(equity(df, tr).iloc[-1] - 1) * 100:+.1f}%"
        grid.append(row)
    print(pd.DataFrame(grid).to_string(index=False))

    if not args.maker:
        print(f"\nnote: at {args.tier}-tier taker fees a round trip costs "
              f"{2 * side_bps / 100:.2f}%. The strategy's average winning trade on "
              f"daily candles grossed ~+4.6% in the range regime — prefer maker "
              f"(limit) entries or a higher volume tier.")


if __name__ == "__main__":
    main()
