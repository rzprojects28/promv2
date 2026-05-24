# Prometheus — Trading Strategy

A reference document covering the end-to-end strategy behind Prometheus: what
it trades, how it picks ideas, how it sizes them, when it exits, and the
guardrails that keep it honest.

This document is for readers who want to understand the system without reading
the code. It covers methodology and behavior, not implementation details.

---

## Overview

Prometheus is an AI-driven prop trading system that runs a fully automated
paper-trading pipeline for a single Interactive Brokers account. It generates
trade ideas, validates them against risk rules, executes them on IBKR, monitors
open positions for exit conditions, and journals every closed trade for
periodic review.

The strategy is built on **ITPM** (Institute of Trading and Portfolio
Management) principles: long the best individual stocks in the strongest
sectors, short the worst stocks in the weakest sectors, and use options to
maximise asymmetric risk/reward on dated catalysts.

The pipeline runs on a cron schedule and reports daily and weekly performance
via Telegram. All AI inference is performed by Claude (Opus 4.7) with
extensive anti-hallucination guardrails.

---

## Schedule

The full strategy runs once per US trading day, with reports following
afterwards.

| Time (SGT)        | Days   | What runs                                      |
| ----------------- | ------ | ---------------------------------------------- |
| 21:40             | Mon-Fri| Full trading pipeline (research → execution → monitor → journal) |
| 05:00 (next day)  | Tue-Sat| Daily Telegram report + SQLite snapshot        |
| 08:00             | Sat    | Weekly Telegram report (Mon-Sun SGT window)    |

The trading pipeline fires shortly after the US open. The daily report fires
the next morning local time, giving the day's positions time to settle.

---

## Pipeline Architecture

The strategy is divided into five sequential stages. Each stage's output
becomes the next stage's input, and each stage can be reasoned about in
isolation.

```
Research  →  Risk Manager  →  Execution  →  Monitor  →  Journal
(ideas)      (validation)     (orders)       (exits)     (review)
```

The trading code is strictly separated from the reporting code. Reports read
the trading system's on-disk state but never write back to it.

---

## Stage 1 — Research

**Goal:** produce 2–3 high-conviction trade theses for the day, grounded in
live market data.

Research is divided into four sub-stages that run sequentially.

### 1.1 Sector Ranking

The 11 SPDR sector ETFs (XLK, XLF, XLV, XLE, XLI, XLB, XLU, XLRE, XLY, XLP,
XLC) are ranked by composite momentum, computed as the average of 1-week,
1-month, and 3-month returns. The output is the top 3 and bottom 3 sectors.

The sector ETFs themselves are **screening tools only** — never tradable
instruments. The system explicitly blocks any thesis on a sector ETF or
broad-market ETF (QQQ, SPY, IWM, DIA, VTI).

### 1.2 Institutional Flow

The system fetches recent 13F holdings filings (45-day lookback) and Form 4
insider transactions (14-day lookback) from SEC EDGAR. This data is used as a
soft contextual signal in the Analysis Agent prompt.

### 1.3 Unusual Whales Flow

Two feeds from the Unusual Whales API:

- **Dark pool block prints** with notional value of $1M or higher
- **Unusual options flow** with premium of $500k or higher

The tickers appearing across these feeds are the primary candidate source for
the Analysis Agent.

### 1.4 Analysis Agent

This is the synthesis step where Claude generates the actual trade ideas. It
runs through a strict procedure:

1. **Extract candidate tickers** from the unusual-flow data plus the
   large-cap representatives of the top and bottom sectors (capped at 20).

2. **Fetch live data for every candidate** — and **hard fail** if any source
   is empty:
   - Live prices from IBKR (with Yahoo Finance fallback)
   - Upcoming earnings dates for the next 45 days
   - Implied volatility rank from IBKR option chains
   - Analyst EPS revision direction
   - Technical levels: 20-day high/low, 50-day MA, 200-day MA

3. **Build a Claude prompt** that injects today's date and every live data
   block verbatim at the top of the message.

4. **Call Claude (Opus 4.7)** with a cached system prompt that forces strict
   ITPM rules and explicitly forbids using training memory.

5. **Validate every returned thesis** before accepting it. Any thesis that
   fails validation is dropped with the reason logged.

### Validation rules for theses

A thesis is rejected if any of the following hold:

- The ticker is a sector or broad-market ETF
- The entry price drifts more than 1% from the live price the system just fetched
- The invalidation conditions text contains no parseable numeric stop level
- The parsed stop falls outside ±30% of the entry price
- For options: the structure type is in the blocked set, the strikes/expiries
  don't match the structure's shape rules, the DTE falls outside the allowed
  window, or the stated reward-to-risk is below 2.0

Output: a JSON file containing validated theses plus the rejection log.

### Options strategy selection

The Analysis Agent picks an options structure based on the current IV regime:

| IV Regime          | Bullish                          | Bearish                          |
| ------------------ | -------------------------------- | -------------------------------- |
| Low (<25th %ile)   | Long Call                        | Long Put                         |
| Normal (25–65th)   | Bull Call Spread (debit)         | Bear Put Spread (debit)          |
| High (>65th %ile)  | Bull Put Spread (credit)         | Bear Call Spread (credit)        |
| High + earnings ≤30d | Calendar Ratio Call Spread     | Calendar Ratio Put Spread        |

DTE windows enforced by the validator:

- Verticals and single options: 30–60 DTE at entry
- Calendars and diagonals: short leg 7–45 DTE, long leg 45–120 DTE

### Allowed and blocked structures

**Allowed (defined-risk):** long call, long put, bull call spread, bear put
spread, bull put spread, bear call spread, calendar call/put spread, diagonal
call/put spread, calendar ratio call/put spread, covered call.

**Blocked:** naked short calls and puts (undefined risk), iron condors and
iron butterflies, straddles and strangles, butterflies, strip/strap
variants. These are either undefined-risk or are not workhorse structures in
ITPM methodology.

---

## Stage 2 — Risk Manager

**Goal:** validate each thesis against hard portfolio risk rules and compute
the position size.

The Risk Manager fetches the live NetLiquidation value from IBKR (falling
back to $100k if unreachable), then runs every thesis through a series of
checks.

### Hard limits

| Limit                          | Value      | Rationale                                |
| ------------------------------ | ---------- | ---------------------------------------- |
| Max position size              | 5% of NAV  | Single-name exposure cap                 |
| Max sector concentration       | 20% of NAV | Sector exposure cap                      |
| Max open positions             | 5          | Manageable position count                |
| Max net directional delta      | 30% of NAV | Net market exposure cap                  |
| **Max risk per trade**         | **0.5% of NAV** | The central risk budget             |
| Correlation block threshold    | 60%        | Block if a sector would dominate the book |

### ITPM position sizing (stocks)

The size of a stock position is derived from the risk budget, not from
conviction:

> Max loss = NAV × 0.5%
> Risk per share = Entry price − Stop price (extracted from invalidation text)
> Position size (shares) = Max loss / Risk per share
> Capped at 5% of NAV in absolute size

If the stop cannot be extracted from the thesis (no numeric price in the
invalidation conditions), the system falls back to conviction-based sizing
(High = 4%, Medium = 3%, Low = 1.5%).

### Position sizing (options)

For options trades, contract count is determined at execution time based on
live leg prices. The Risk Manager sets the premium budget (NAV × 0.5%) and
only runs the portfolio-level checks here.

### Validation checks (run per thesis)

1. **No duplicate** — ticker not already in the open book
2. **Position size** within the 5% cap
3. **Sector concentration** — adding this position keeps sector exposure under 20%
4. **Open position count** — under 5 active names
5. **Net delta** — signed sum of position sizes stays under 30%
6. **Correlation** — uses pre-defined correlation groups (mega-cap tech,
   semiconductors, financials, energy majors, healthcare, consumer
   discretionary, utilities, REITs); rejects if the new thesis would create
   two or more correlated positions in the same group

Outputs: an approved-trades list and a rejected-trades list, each with the
per-check pass/fail reasons attached.

---

## Stage 3 — Execution

**Goal:** submit orders to IBKR for every approved trade.

### Market hours gate

Before submitting any order, the system checks whether the US equity/options
market is currently open. The check uses a hard-coded NYSE/CBOE holiday
calendar and accounts for early-close days. If the market is closed,
execution exits cleanly and sends a Telegram notification — no orders are
queued.

This gate exists in both Execution and Monitor. Research, Risk, and Journal
can run any time.

### Stock execution

For stock theses:

1. Fetch the live bid/ask from IBKR
2. Use the mid-price as the limit price (or last-trade if bid/ask is unavailable)
3. Compute share quantity from the dollar size
4. Submit a DAY limit order
5. Record the position with a 45-day hard deadline
6. Send a Telegram alert with the thesis, catalyst, invalidation, and deadline

### Options execution

For options theses, the process is more involved:

1. **Parse** the options structure and validate its shape (correct number of
   legs, correct call/put mix, correct strike ordering for the declared type)
2. **Qualify** each option contract through IBKR to confirm it exists and
   retrieve its conId
3. **Fetch the mid price for every leg**. If any leg has a bid/ask spread
   wider than 25% of mid, **skip the trade** — wide markets create
   unacceptable execution risk
4. **Compute the max loss per contract**:
   - Long singles: premium paid × 100
   - Debit verticals: debit × 100
   - Credit verticals: (spread width − net credit) × 100
   - Calendars and diagonals: debit paid × 100
5. **Size the position** so the total max loss is at or under 0.5% of NAV. If
   even a single contract exceeds the budget, **skip the trade** — there is
   no silent fallback to the underlying stock
6. **Submit the order**:
   - Single-leg: a standard option order
   - Multi-leg: a single combo (Bag) order at the net debit or net credit

The full leg prices at entry are stored with the position record for later
analysis.

---

## Stage 4 — Monitor

**Goal:** evaluate every open position for exit conditions and close any that
meet them.

The Monitor also respects the market hours gate. Outside RTH it exits
without action — closing orders that can't fill would corrupt the position
state.

For each open position, the Monitor evaluates five exit triggers **in
priority order**. The first match wins; no further checks run for that
position.

### Trigger 1 — Hard deadline

Every position has a 45-day deadline set at entry. If today's date is past
the deadline, the position is closed regardless of P&L.

### Trigger 2 — 21 DTE management (options only)

Once an options position has been held for 24+ days, theta decay accelerates
sharply. The position is closed to avoid the worst of the decay curve.

### Trigger 3 — Rule-based stop

The Risk Manager's calculated stop price is compared against the current
price. For longs, a price at or below the stop triggers an exit. For shorts,
a price at or above the stop triggers an exit. This is a deterministic
numeric check with no AI involvement.

### Trigger 4 — Profit-take threshold

If the position is up 20% or more, take profit. This is rule-based, not AI-
driven. The system does NOT ask the AI to judge whether the catalyst has
"fired" because the AI has no post-trade market data to make that judgment
honestly.

### Trigger 5 — AI invalidation check (last resort)

Only reached if none of the above triggered. The AI is asked to evaluate
whether a price-level invalidation in the thesis has been breached. The
prompt is heavily constrained:

- Numeric price-level conditions only
- Narrative or ambiguous conditions return "false"
- The bias is explicitly to HOLD
- Training memory and external knowledge are explicitly forbidden

### Closing a position

When an exit triggers:

- **Stocks** close via a limit order priced at current × 0.998 (sell) or
  × 1.002 (buy) for near-instant fill
- **Single-leg options** close by flipping the original action and submitting
  a very passive limit order
- **Multi-leg options** close by rebuilding the combo with every leg's action
  reversed, submitted as a single net-priced order

The position is moved from open to closed with exit date, exit price, exit
reason, exit category, and P&L percentage. A Telegram alert is sent.

---

## Stage 5 — Journal

**Goal:** review every closed trade and aggregate performance patterns.

Journal runs on a weekly cadence (every 7 days, tracked via a last-run
timestamp). It reviews any trades that have been closed since the last run.

### Per-trade review

For each closed trade, the AI produces a structured JSON review:

- **Thesis accuracy** (Yes / No / Partial) — derived from P&L sign + direction
- **Invalidation quality** (Clear / Vague / N/A) — judging the text quality
  of the original invalidation conditions
- **Verdict** bucketed by P&L magnitude (Strong Win / Weak Win / Neutral /
  Weak Loss / Strong Loss)
- **Key lesson** — one sentence drawn from the exit reason and P&L
- **Pattern tags** — chosen from a fixed 16-tag vocabulary (e.g.
  high_conviction_win, thesis_too_vague, stop_worked)
- **Repeat-this-setup** flag

The AI is given **only the trade record** and explicitly forbidden from using
training memory to invent a market narrative.

### Aggregate statistics

The Journal also recomputes performance statistics across all reviewed
trades, broken down by conviction level, sector, direction, and pattern.
A Telegram summary is sent at the end of the run.

---

## Reporting

Reports are decoupled from the trading code. They read the trading system's
on-disk state but never write back to it.

### Daily report

Fired the morning after the trading run. Contains:

- Number of open trades and account value
- Unrealized P&L (in dollars, as a percentage of account, and as a size-
  weighted average per position)
- Risk exposure: both *budgeted* (sum of risk-per-share × quantity, or full
  position size for unstopped positions) and *live* (mark-to-market distance
  from current price to stop)
- Today's decisions: theses approved and rejected by the Risk Manager
- Today's activity: trades actually opened and closed

A snapshot of these metrics is also written to the SQLite history database
for later analysis.

### Weekly report

Fired Saturday morning SGT, covering the most recently completed Monday–
Sunday SGT window. Contains:

- Trades opened and closed this week (with win/loss split)
- Realized P&L (USD-weighted)
- Best and worst trade with exit reason
- End-of-week open positions snapshot

### Live dashboard

A FastAPI server exposes the same data through HTTP endpoints. It reads the
SQLite history for closed trades and daily snapshots, and queries IBKR
directly for live floating P&L on open positions.

---

## Anti-Hallucination Philosophy

The system is designed around a single observation: large language models
will confidently invent facts when given vague prompts. The strategy treats
every AI call as untrusted until proven otherwise.

Three patterns are used consistently:

### Pattern 1 — Inject live data into every prompt

The Analysis Agent prompt contains today's date, live prices, live IV, live
earnings dates, and live technical levels — all fetched moments before the
call. The model is told to use only the data in the prompt and to omit any
thesis that can't be grounded in it. Returning zero theses is acceptable.

### Pattern 2 — System prompts forbid training-memory inference

Every AI call has a system prompt that explicitly forbids using training
memory, news intuition, or any data not in the prompt. For ambiguous cases,
the model is instructed to return UNKNOWN or HOLD. The bias is always
toward inaction.

### Pattern 3 — Code validates AI output before accepting it

The model is treated as a recommender, not an authority. Deterministic code
runs after every AI call:

- Theses with prices drifting from live data are dropped
- Theses without parseable numeric stops are dropped
- Options structures that don't match their declared shape are dropped
- Invalidation triggers require explicit numeric-threshold confirmation

If the AI produces something the validator can't verify, it's rejected.

---

## Risk Budget Summary

The single most important number in the system is the per-trade risk budget:

> **No single trade may lose more than 0.5% of NAV.**

Every other limit composes around this. Stock sizing is derived from it.
Options contract counts are sized to it. Position caps (5% per name, 20% per
sector, 5 names maximum, 30% net delta) prevent any combination of trades
from compounding past it.

The trade is also defined-risk by construction: undefined-risk option
structures are blocked at the validator level, and unstopped stock positions
fall back to conviction-based sizing that respects the same caps.

---

## Operational Constraints

A few constraints are encoded in the system's documentation and enforced
through code review:

- **Single account.** The system is designed for one IBKR account. Past
  multi-account experiments have been removed.
- **Trading strategy is sacred.** Business logic in research, risk,
  execution, monitor, and journal modules is treated as the production
  strategy. Path and import refactors are routine; behavior changes require
  explicit authorization.
- **Trading and reporting are isolated.** The two packages communicate only
  through on-disk JSON state and a SQLite history database. Neither imports
  from the other.
- **Market hours gate everywhere IBKR touches.** Execution and Monitor both
  refuse to act outside Regular Trading Hours. Research, Risk, and Journal
  run regardless.

---

## Glossary

- **DTE** — Days To Expiry. The number of calendar days from today to an
  option's expiration date.
- **IV** — Implied Volatility. The market's forward-looking volatility
  estimate priced into an option.
- **ITPM** — Institute of Trading and Portfolio Management. The methodology
  the strategy is built on.
- **NAV** — Net Asset Value. Total account equity, fetched from IBKR as
  NetLiquidation.
- **RTH** — Regular Trading Hours. The standard US equity session
  (09:30–16:00 ET, with early closes on some days).
- **SGT** — Singapore Time (UTC+8). The trading desk timezone; all schedules
  are expressed in SGT.
