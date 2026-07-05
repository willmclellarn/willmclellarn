"""Backtest: z-score mean reversion on SOL daily closes, Oct 2025 - Jul 2026.

Strategy (per README §6 / EXECUTION.md):
  signal   z = (close - SMA20) / std20
  long     z < -Z_ENTRY, then wait for a confirming up-close (knife filter)
  short    z > +Z_ENTRY, then wait for a confirming down-close
  exit     z crosses back through Z_EXIT toward the mean, hard stop, or time stop

Variants reported:
  BH   buy & hold
  V1   raw z-score reversion, no filters (enter on signal day close)
  V2   V1 + confirmation-day entry (the "don't catch knives" veto)
  V3   V2 long-only (spot-only implementation, no perp needed)

Costs are charged per side (fee + slippage). Daily closes only — intraday
stops/fills are approximated at next close; treat results as a regime-fit
sanity check on ~9 months of data, not a validated edge.

Usage: python3 backtest.py [--cost-bps 15]
"""
import argparse

import numpy as np
import pandas as pd

Z_ENTRY = 1.5
Z_EXIT = 0.0
STOP_PCT = 0.08
TIME_STOP = 10  # trading days in position


SLOPE_GATE = 0.03  # 10-day SMA50 slope beyond which counter-trend entries are blocked


def load() -> pd.DataFrame:
    df = pd.read_csv("data/sol_daily.csv", parse_dates=["date"]).set_index("date")
    df["ret"] = df["close"].pct_change()
    df["sma20"] = df["close"].rolling(20).mean()
    df["z"] = (df["close"] - df["sma20"]) / df["close"].rolling(20).std()
    df["slope50"] = df["close"].rolling(50).mean().pct_change(10)
    return df.dropna(subset=["z"])


def run(df: pd.DataFrame, cost: float, confirm: bool, allow_short: bool,
        z_entry: float = Z_ENTRY, z_exit: float = Z_EXIT,
        regime_gate: bool = False) -> pd.DataFrame:
    """Simulate; returns trade list. Position entered/exited at daily close."""
    trades = []
    pos = 0          # +1 long, -1 short
    armed = 0        # signal fired, awaiting confirmation close
    entry_px = entry_i = None

    for i in range(1, len(df)):
        px, z, ret = df["close"].iloc[i], df["z"].iloc[i], df["ret"].iloc[i]

        if pos != 0:
            stop = (px / entry_px - 1) * pos <= -STOP_PCT
            done = (z >= z_exit) if pos > 0 else (z <= -z_exit)
            if stop or done or (i - entry_i) >= TIME_STOP:
                gross = (px / entry_px - 1) * pos
                trades.append({
                    "entry": df.index[entry_i].date(), "exit": df.index[i].date(),
                    "side": "long" if pos > 0 else "short",
                    "gross": gross, "net": gross - 2 * cost,
                    "days": i - entry_i,
                    "why": "stop" if stop else ("mean" if done else "time"),
                })
                pos = 0
            continue

        if armed != 0:
            confirmed = (ret > 0) if armed > 0 else (ret < 0)
            if confirmed:
                pos, entry_px, entry_i = armed, px, i
                armed = 0
                continue
            # signal invalidated if z mean-reverts before confirmation
            if (armed > 0 and z >= z_exit) or (armed < 0 and z <= -z_exit):
                armed = 0
            continue

        slope = df["slope50"].iloc[i]
        long_ok = not (regime_gate and not np.isnan(slope) and slope < -SLOPE_GATE)
        short_ok = not (regime_gate and not np.isnan(slope) and slope > SLOPE_GATE)

        if z < -z_entry and long_ok:
            if confirm:
                armed = 1
            else:
                pos, entry_px, entry_i = 1, px, i
        elif z > z_entry and allow_short and short_ok:
            if confirm:
                armed = -1
            else:
                pos, entry_px, entry_i = -1, px, i

    return pd.DataFrame(trades)


def equity(df: pd.DataFrame, trades: pd.DataFrame) -> pd.Series:
    """Daily equity curve from the trade list (position held between entry/exit)."""
    daily = pd.Series(0.0, index=df.index)
    for _, t in trades.iterrows():
        sign = 1 if t["side"] == "long" else -1
        window = df.loc[str(t["entry"]):str(t["exit"])].index[1:]
        daily.loc[window] += sign * df["ret"].loc[window]
        daily.loc[str(t["exit"])] -= 2 * (t["gross"] - t["net"]) / 2  # costs on exit day
    return (1 + daily).cumprod()


def stats(name: str, curve: pd.Series, trades: pd.DataFrame | None) -> dict:
    r = curve.pct_change().dropna()
    dd = (curve / curve.cummax() - 1).min()
    out = {
        "variant": name,
        "total": f"{(curve.iloc[-1] - 1) * 100:+.1f}%",
        "maxDD": f"{dd * 100:.1f}%",
        "sharpe": f"{r.mean() / r.std() * np.sqrt(365):.2f}" if r.std() > 0 else "-",
    }
    if trades is not None and len(trades):
        out |= {
            "trades": len(trades),
            "win%": f"{(trades['net'] > 0).mean() * 100:.0f}%",
            "avg net": f"{trades['net'].mean() * 100:+.2f}%",
            "exposure": f"{trades['days'].sum() / len(curve) * 100:.0f}%",
        }
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cost-bps", type=float, default=15.0,
                    help="per-side cost in bps (fee + slippage)")
    args = ap.parse_args()
    cost = args.cost_bps / 1e4

    df = load()
    print(f"data: {df.index[0].date()} -> {df.index[-1].date()}  ({len(df)} days)  "
          f"cost {args.cost_bps:.0f}bps/side\n")

    bh = (1 + df["ret"]).cumprod()
    rows = [stats("buy & hold", bh, None)]

    variants = {
        "V1 raw z-score L/S": dict(confirm=False, allow_short=True),
        "V2 +confirm day L/S": dict(confirm=True, allow_short=True),
        "V3 confirm, long-only": dict(confirm=True, allow_short=False),
        "V4 V2 +regime gate": dict(confirm=True, allow_short=True, regime_gate=True),
    }
    curves = {"buy & hold": bh}
    all_trades = {}
    for name, kw in variants.items():
        tr = run(df, cost, **kw)
        curves[name] = equity(df, tr)
        all_trades[name] = tr
        rows.append(stats(name, curves[name], tr))

    print(pd.DataFrame(rows).to_string(index=False))

    print("\nV4 trade log:")
    print(all_trades["V4 V2 +regime gate"].to_string(index=False))

    print("\nsensitivity (V2 total return, net of costs):")
    grid = []
    for ze in (1.25, 1.5, 2.0):
        row = {"z_entry": ze}
        for cb in (15, 30, 60):
            tr = run(df, cb / 1e4, confirm=True, allow_short=True, z_entry=ze)
            row[f"{cb}bps"] = f"{(equity(df, tr).iloc[-1] - 1) * 100:+.1f}%"
        grid.append(row)
    print(pd.DataFrame(grid).to_string(index=False))

    pd.DataFrame({k: v for k, v in curves.items()}).to_csv("data/backtest_curves.csv")
    print("\ncurves -> data/backtest_curves.csv")


if __name__ == "__main__":
    main()
