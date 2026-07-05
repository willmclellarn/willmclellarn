# Programmatic SOL Trading — Execution Stack & AI-Agent Architecture

*Companion to [README.md](README.md) (market analysis) and [backtest.py](backtest.py) (strategy backtest). July 2026.*

> Not financial advice. Automated trading can lose money faster than manual trading.
> Check the regulatory status of any derivatives venue for your jurisdiction before wiring funds.

## The architecture in one picture

```
┌────────────────────────── every 4h / on schedule ─────────────────────────┐
│  AI AGENT LAYER (Claude API)                                              │
│  regime classifier + macro/news veto — reads market data & news,          │
│  returns structured JSON: {regime, long_enabled, short_enabled, veto}     │
└──────────────────────────────┬────────────────────────────────────────────┘
                               │ boolean gates only — never orders, never sizes
┌──────────────────────────────▼────────────────────────────────────────────┐
│  DETERMINISTIC STRATEGY CORE (Python)                                     │
│  z-score entries/exits, stops, position sizing, funding monitor           │
│  (the V4 rules in backtest.py, with the agent supplying the regime gate)  │
└──────────────────────────────┬────────────────────────────────────────────┘
                               │ orders
┌──────────────────────────────▼────────────────────────────────────────────┐
│  EXECUTION ADAPTER (CCXT / venue SDK)   +   HARD RISK LAYER               │
│  spot: Coinbase/Kraken · perps: Hyperliquid/Drift                         │
│  kill switches the agent CANNOT override: daily loss limit, max position, │
│  circuit breaker on >8% hourly moves                                      │
└───────────────────────────────────────────────────────────────────────────┘
```

The division of labor: **math decides trades, the LLM decides context**. The agent's
output is consumed as booleans that gate the deterministic core — exactly the layer the
backtest showed was missing (12% win rate in the trending regime vs 88% in the range
regime with identical entry rules).

## 1. Venues

| Need | Venue | Access | Notes |
|---|---|---|---|
| Spot SOL (long-only leg) | Coinbase Advanced Trade, Kraken Pro | REST/WS, unified via [CCXT](https://github.com/ccxt/ccxt) | US-friendly; taker ~10–60bps depending on tier — model your real tier in the backtest cost param |
| Perps: shorts + funding capture | [Hyperliquid](https://github.com/hyperliquid-dex/hyperliquid-python-sdk) | Official Python SDK; funding rate in market metadata; sub-second, no gas | The default choice for API perp trading in 2026; **geo-restrictions apply — verify eligibility for your jurisdiction** |
| Perps, Solana-native | [Drift Protocol](https://www.drift.trade/) | Python/TS SDK, on-chain | SOL-denominated funding markets; you custody keys |
| US-regulated derivatives | Coinbase/Kraken CFTC futures | Same APIs | Smaller contracts, compliant path for US persons |
| Funding-rate data | [CoinGlass](https://www.coinglass.com/FundingRate/SOL), [Coinalyze](https://coinalyze.net/solana/funding-rate/), venue APIs | REST | Poll per venue for the carry strategy's rotation logic |

Wallet/key hygiene: API keys with **trade-only permissions** (no withdrawal), scoped IPs,
secrets in a vault or env — never in code or the agent's prompt (prompts are logged).

## 2. Bot framework

Recommendation for this strategy, per the 2026 landscape ([comparison](https://trendrider.net/blog/freqtrade-vs-hummingbot-vs-ccxt-2026)):

- **[Freqtrade](https://www.freqtrade.io/)** for the mean-reversion core — it is built
  exactly for this shape (directional, candle-based, mean reversion), has the best
  backtesting/walk-forward/hyperopt tooling of the open frameworks, a **dry-run paper
  mode** (your mandatory first month), and Telegram control. Port the V4 rules from
  `backtest.py` into a Freqtrade strategy class; the agent layer writes its regime gates
  to a small state file/Redis key the strategy reads on each candle.
- **Custom asyncio service + venue SDK** for the funding-carry leg — carry isn't a
  candle strategy; it's a monitor that compares funding across venues, opens/unwinds a
  delta-neutral pair, and rebalances. ~300 lines against the Hyperliquid SDK.
- **Skip for now**: Hummingbot (market-making focus, weak backtesting), latency-sensitive
  approaches (you won't win that race).

## 3. The AI-agent layer (Claude API)

Three agents, all scheduled jobs (cron or a Freqtrade callback), all returning
**structured JSON validated against a schema** — no free-text parsing in the loop.

| Agent | Cadence | Input | Output |
|---|---|---|---|
| Regime classifier | 4h | last 90d of indicators (z-score, SMAs, realized vol, SOL/BTC) + current funding | `regime: trend_down \| range \| trend_up`, `long_enabled`, `short_enabled`, confidence |
| Macro/news veto | 1h (and on entry signal) | web search: BoJ, BTC ETF flows, SOL-specific news | `veto: bool`, `reason` — blocks NEW entries only; never forces exits (that's the stop's job) |
| Weekly analyst | weekly | full dataset + trade log | narrative report for you, level map refresh; not wired to execution |

Working pattern (Python SDK; the server-side `web_search` tool lets the veto agent read
news without you building a news pipeline):

```python
import anthropic
from pydantic import BaseModel
from typing import Literal

client = anthropic.Anthropic()

class RegimeCall(BaseModel):
    regime: Literal["trend_down", "range", "trend_up"]
    confidence: float          # 0-1
    long_enabled: bool
    short_enabled: bool
    rationale: str             # for your logs, not for the machine

SYSTEM = """You are the regime-classification layer of a SOL trading system.
The downstream strategy is counter-trend mean reversion: it buys z-score lows and sells
z-score highs, and it loses money in strong trends. Your ONLY job is to classify the
current regime from the data provided. Be conservative: when trend vs range is unclear,
classify as the trend and disable the counter-trend side. You do not place orders."""

def classify_regime(market_snapshot: str) -> RegimeCall:
    resp = client.messages.parse(
        model="claude-opus-4-8",
        max_tokens=2048,
        thinking={"type": "adaptive"},
        system=[{"type": "text", "text": SYSTEM,
                 "cache_control": {"type": "ephemeral"}}],   # stable prefix -> cached
        messages=[{"role": "user", "content": market_snapshot}],
        output_format=RegimeCall,
    )
    return resp.parsed_output

# The consumer treats the output as gates, nothing more:
call = classify_regime(build_snapshot())      # your indicator dump, ~2-3K tokens
strategy_state.update(
    long_ok=call.long_enabled and not veto.veto,
    short_ok=call.short_enabled and not veto.veto,
)
```

For the news-veto agent, add the server-side search tool so Claude fetches its own
headlines:

```python
resp = client.messages.create(
    model="claude-opus-4-8",
    max_tokens=4096,
    thinking={"type": "adaptive"},
    tools=[{"type": "web_search_20260209", "name": "web_search", "max_uses": 5}],
    system=VETO_SYSTEM,   # "search for BoJ policy, BTC ETF flows, SOL-specific news ..."
    messages=[{"role": "user", "content": "Assess the next 24h for macro shock risk."}],
    output_config={"format": {"type": "json_schema", "schema": VETO_SCHEMA}},
)
```

**Design rules that keep this safe:**

1. **The agent gates; code trades.** Agent output is booleans + confidence consumed by
   the deterministic core. No tool in any agent can place, modify, or cancel an order.
2. **Fail closed.** API error, timeout, or schema-validation failure → treat as
   `veto=true` / most-conservative regime. A dead agent must never mean "trade freely."
3. **Hard risk layer below everything**: daily loss limit, max position, per-order size
   cap, and a volatility circuit breaker, enforced in the execution adapter. The agent
   (and you at 2am) cannot override them without a code deploy.
4. **Log every agent call** (prompt hash, output, latency, token usage) next to every
   trade — you'll want to audit which regime calls made/lost money.
5. **Cost is a non-issue at this cadence**: regime calls every 4h + hourly veto checks
   at ~3K input / 500 output tokens ≈ ~250 calls/month ≈ **a few dollars/month** on
   Opus 4.8 ($5/$25 per MTok), less with the cached system prompt. Don't cheap out on
   the model here — one bad regime call costs more than a year of API fees.

**Upgrade path**: if the weekly-analyst job grows into real research (reading filings,
backtesting variants, writing reports), move that one agent to the Claude Agent SDK or
Managed Agents with a scheduled deployment — but keep the in-loop classifier/veto as
plain, fast, stateless API calls.

## 4. Path to live

1. **Wire data + agents first, trade nothing.** Run the regime classifier + veto on
   schedule for 2 weeks; eyeball whether its calls match what you'd say.
2. **Freqtrade dry-run** with the V4 strategy + agent gates for ≥1 month. Compare
   dry-run fills against the backtest's assumptions (slippage will be worse).
3. **Live with 5–10% of intended size** until you have ≥20 live trades. Expect live
   results below backtest — if they're wildly below, your cost model is wrong.
4. **Scale only what's measured.** The backtest is 9 months of daily closes — treat
   every live month as new evidence, not confirmation.

## Sources

- [Hyperliquid Python SDK](https://github.com/hyperliquid-dex/hyperliquid-python-sdk) · [Hyperliquid API docs](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api) · [algorithmic trading on Hyperliquid](https://robottraders.io/blog/algorithmic-trading-hyperliquid-dex-python)
- [Freqtrade vs Hummingbot vs CCXT (2026)](https://trendrider.net/blog/freqtrade-vs-hummingbot-vs-ccxt-2026) · [Hyperliquid bot frameworks](https://coincodecap.com/best-hyperliquid-bot-frameworks-sdks-hummingbot-ccxt) · [open-source bots on GitHub](https://coincodecap.com/open-source-trading-bots-on-GitHub)
- [SOL funding rate — CoinGlass](https://www.coinglass.com/FundingRate/SOL) · [Coinalyze](https://coinalyze.net/solana/funding-rate/)
- Claude API: [platform.claude.com docs](https://platform.claude.com/docs) (structured outputs, web search tool, prompt caching)
