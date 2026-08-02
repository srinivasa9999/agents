# Trading Plan: Fixing the Fade-and-Average Loss Cycle

> Personal rules document. Not financial advice, and no strategy guarantees a
> win rate — the goal here is positive *expectancy*, which is what actually
> compounds an account. Read the "Why win rate is the wrong target" section
> first.

## 1. The pattern that is losing money (name it honestly)

The current behaviour, written out as a system:

1. See a stock that has already moved ~±4% intraday.
2. Assume "it can't go further" and take a position *against* the move
   (fade / mean-reversion), with **no defined invalidation point**.
3. If it reverts → book a small profit quickly.
4. If it keeps going → **average into the loser**, increasing size exactly
   when the market is proving the idea wrong.
5. Occasionally the averaged position comes back → relief exit near breakeven.
6. Sometimes it never comes back → one loss wipes out many small wins.

This has three structural flaws, each individually enough to lose money
long-term:

- **The premise is statistically backwards.** A stock up/down 4% intraday is
  showing *momentum*, and intraday returns have fat tails: large moves are
  more likely to extend than the gut expects. "It won't go beyond X%" is a
  feeling, not an edge. Circuit-limit stocks routinely move 8–20%. Fading
  strength/weakness without a reversal signal is fighting the only
  information the market has given you.
- **No stop-loss = unlimited downside on a capped-upside trade.** Booking
  small profits but letting losses run is a negatively skewed payoff. Even a
  70% win rate loses money if the average loss is 5× the average win.
- **Averaging a loser is doubling the bet after the thesis failed.** It
  turns one wrong trade into a portfolio-threatening one, and it *feels*
  good (lower breakeven) which is why it's so dangerous. Averaging is a
  valid tool only when it was **planned before entry** (scaled entry at
  pre-decided levels, with total size and stop fixed in advance) — never as
  a reaction to being underwater.

## 2. Why "more than 50% win rate" is the wrong target

Profitability = `(win% × avg win) − (loss% × avg loss)`.

| Style | Win rate | Avg win : avg loss | Outcome |
|---|---|---|---|
| Current (fade + average) | often 60–70% | 1 : 4 or worse | **Loses** |
| Trend-following | 35–45% | 3 : 1 | Wins |
| Disciplined mean-reversion | 55–65% | 1 : 1 | Wins |

A 40%-win-rate system with 2.5:1 reward:risk makes money; a 70%-win-rate
system with 1:4 loses. The fix is not "find a signal that's right more
often" — it is **capping the loss per trade so the ratio flips**. Once
losses are capped at 1R, even a modest win rate is profitable.

## 3. Non-negotiable risk rules (these matter more than the entry signal)

1. **Risk per trade: max 1% of trading capital.** Position size is *derived*
   from the stop distance, never chosen first:
   `qty = (capital × 1%) ÷ (entry − stop)`.
2. **Every order has a stop-loss placed at entry time** — an actual SL order
   on Kite, not a mental level. If you can't say where the idea is wrong,
   you don't have a trade.
3. **Never add to a losing position.** Zero exceptions. Adding is allowed
   only to a *winning* position after a stop can be moved to breakeven.
4. **Daily loss limit: 2% of capital (≈2 losing trades).** Hit it → close
   the terminal for the day. Revenge trading after two stops is how the big
   losses happen.
5. **Minimum reward:risk of 1.5:1 at entry**, measured to a real level
   (pivot, prior day high/low, VWAP), not a hope. If the target is closer
   than 1.5× the stop distance, skip the trade.
6. **Weekly circuit-breaker:** down 5% on the week → no trades until Monday,
   review the journal instead.

## 4. Two rule-based setups (pick ONE and trade only it for 30 sessions)

### Setup A — Trade *with* the 4% move (momentum continuation)

Instead of fading the day's big mover, join it on a controlled pullback:

- **Filter:** stock moved ≥3–4% from previous close on above-average volume,
  no pending news/results lottery (check the news alerts this repo already
  sends). The ≥3–4% test is on the **session's extreme so far**, not
  necessarily the live price at signal time - a stock that spiked 4%+ inside
  the rule-7 window and has since settled to a smaller live move is still
  "today's mover." Don't disqualify a name just because the printed extreme
  has scrolled off-screen by 09:45.
- **Entry:** wait for the first pullback, then a **VWAP cross back** in the
  trade's direction (price closing back above VWAP for a long / below VWAP
  for a short), and enter on the break of the pullback's high/low. The cross
  is the trigger - a continuous "price is above/below VWAP" state is not.
  On lower-volatility names VWAP can sit far from price for hours without
  ever being tested, which makes "holds above VWAP" trivially true and
  useless as a filter. No pullback-then-cross = no trade; chasing the
  vertical move is forbidden.
- **Trend filter (9/20 EMA, 3-min chart):** at the moment of the
  breakout entry, the 9 EMA must be above the 20 EMA for a long / below it
  for a short. VWAP crossed but EMAs misaligned = skip, don't override one
  indicator with the other.
- **Momentum filter (14-period RSI, 3-min chart):** skip the entry if RSI is
  already ≥75 (long) / ≤25 (short) at the breakout - that's chasing an
  exhausted move, not joining a fresh pullback. Wait for RSI to cool back
  under those levels or let the trade go.
- **Stop:** below the pullback low (long) / above the pullback high (short).
- **Target:** 2R, or trail below higher lows once past 1.5R. Treat 2R as the
  minimum bar that justifies taking the trade at entry (rule 5), not a
  guaranteed outcome - real fills often land closer to 1.5-2R.
- **Tighten-to-breakeven trigger:** if price closes back through the 20 EMA
  against the position, or RSI diverges against it (price makes a fresh
  high/low that RSI doesn't confirm), move the stop to breakeven immediately
  - independent of the 1.5R trail rule above.
- **Exit by 15:10 IST regardless** — no overnight conversion of an intraday
  trade ("it will recover tomorrow" is averaging in disguise).

### Setup B — Mean-reversion done properly (if fading is the preference)

Fading extremes can work, but only with a *location* and an *invalidation*:

- **Filter:** the move is ≥4% **into a pre-identified level** — pivot
  R2/R3 or S2/S3 (this repo already computes these daily), prior day
  high/low, or a top-OI strike acting as resistance/support. A 4% move in
  the middle of nowhere is not a fade candidate.
- **Trigger:** do not catch the falling/rising knife. Wait for a reversal
  bar on the 15-minute chart (close back inside the level, or a lower-high
  after the extreme). The trigger is what separates this from the current
  losing behaviour.
- **Momentum-exhaustion confirmation (14-period RSI, 15-min chart):** the
  reversal bar must coincide with RSI ≥70 (fading strength) or ≤30 (fading
  weakness). Level + reversal bar without an RSI extreme = no trade; a
  location alone isn't proof the move is exhausted.
- **Stop:** just beyond the extreme of the move (the high/low of the spike).
  If price takes out that extreme, the reversion idea is *dead* — exit, do
  not average.
- **Target:** first scale at 1R (e.g. VWAP), rest at 2R. This keeps the win
  rate high without the fatal left tail.
- **Hard rule:** one attempt per stock per day. Stopped out = done with that
  name today.

### Indicator reference (read manually off the chart, not yet automated)

Every indicator below is a filter or a veto - none of them replace the core
trigger (VWAP cross for A, reversal bar at a level for B). They add
conditions, they don't substitute for one. This repo doesn't compute
EMA/RSI for individual stocks yet (only VWAP, via the volume-weighted
typical-price formula used in the backtests) - read these off your
charting platform (e.g. the "Trend-Day Detector" indicator already on your
TradingView charts) until/unless that's automated.

| Indicator | Params | Timeframe | Used in |
|---|---|---|---|
| VWAP | volume-weighted, session-to-date | 3-min | Setup A (entry trigger) |
| EMA | 9-period & 20-period | 3-min | Setup A (trend filter, breakeven trigger) |
| RSI | 14-period | 3-min (Setup A) / 15-min (Setup B) | Both (exhaustion filter) |

### Backtest notes (real Kite data, 31 Jul 2026, 4 sessions)

Findings from actually replaying the rules against real 3-minute candles
(HDFC Bank, Hyundai Motor India, Bajaj Finance, Swiggy) - update this list as
more sessions get tested:

- **Setup B triggered a valid location+reversal in 3 of 4 sessions, and all
  three fell entirely inside the rule-7 window.** Don't be surprised if this
  setup sits idle for stretches once rule 7 is respected - that's the rule
  working, not evidence the setup is broken. The extreme print that makes a
  level worth fading is disproportionately likely to be the opening spike.
- **The old-habit (fade-and-average) side lost money in all 4 sessions**,
  but by two different mechanisms worth recognising in real time: a clean
  loss (HDFC, Bajaj Finance, Swiggy) where the average never came back, and
  a near-breakeven "save" (Hyundai) where it did - despite carrying an
  unrealised drawdown over 1.5x the 1% risk budget at the worst point in
  both Hyundai and Bajaj Finance. A trade that "worked" on P&L can still
  have been a rule violation the whole time it was open; judge the process,
  not just the close.
- **Setup A's disciplined side won all 4 sessions** (+0.63R to +2.00R), which
  is the reason for the two filter/entry clarifications above.
- **EMA/RSI filters above are not yet backtested against these 4 sessions**
  - they were added afterward. Before trusting them live, re-check each of
  the four real entries against the EMA-alignment and RSI-exhaustion rules
  to see whether they would have confirmed, vetoed, or made no difference.
  Update this note once that's done.

## 5. Use the alert system in this repo as the discipline layer

The monitor already watches open positions (`positions:` in
`config/config.yaml`). Tune it to nag *before* a loss becomes a disaster,
not after:

- `ltp_move_pct_threshold` is set to **2.0%** and `pnl_percentage_threshold`
  to **1.0%** - both tuned against the real backtested stop distances above
  (0.18%-0.88% of entry price), so a Telegram ping means "your stop should
  already have executed - check it," not "you're already badly hurt."
  `pnl_absolute_threshold_rupees` stays at ₹5,000 = rule 1's 1% risk on
  ₹5,00,000 capital; re-tune it first if trading capital changes.
- Treat any position alert on a *loser* as a hard instruction: verify the SL
  order exists and has not been cancelled/moved. Moving a stop further away
  is averaging by another name.
- The pivot levels and top-OI strikes the system already publishes each
  morning are the pre-identified levels Setup B requires — write down the
  day's fade zones *before* 09:15, and only fade into those.

## 6. Journal and review (how the win rate actually improves)

For every trade, record: date, symbol, setup (A or B — anything else is an
error), entry, stop, size, planned R:R, exit, realised R, and one line on
whether the rules were followed. After 30 sessions:

- Rule-compliant trades vs. violations, and the P&L of each bucket. Almost
  always the violations account for the losses — that is the evidence that
  keeps discipline honest.
- Expectancy = average realised R per trade. Positive expectancy with 40
  trades of data is the green light to size up gradually (1% → 1.25% risk),
  and not before.

## 7. Hard prohibitions (the list that protects the account)

- No trade without a pre-placed stop-loss order.
- No averaging losers, no "one more lot to lower my average."
- No removing or widening a stop after entry.
- No trading in the first 15 minutes (09:15–09:30) — opening spikes are
  where the fade instinct gets punished worst.
- No overnight carry of a failed intraday position.
- No trading after the daily loss limit; no new setup ideas mid-week —
  changes to this plan are made on weekends only, in writing, here.
