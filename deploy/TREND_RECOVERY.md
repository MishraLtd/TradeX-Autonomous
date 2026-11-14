# Trend entries and timed recovery: paper trial

On the `profitup` branch fresh trials use `TREND_PORTFOLIO`. The net profit
trigger is strictly above ₹10 per position, after both sides' estimated costs.
Up to three distinct positions share available cash, with no fixed rupee cap
per trade. A single eligible stock can use the full free balance. The recovery
trigger stays below −₹5 net. For an existing trial, clone this branch, pin the
reviewed commit and pass `--profitup` in the VM procedure below. The remaining
single-position examples describe the previous `TREND_ROTATION` policy.

This policy replaces the fixed direction formula and immediate price stop.
The operator selected a minimum one-hour recovery wait and a compulsory exit
at two hours. These are experimental rules, not evidence of profitable signals.

## Entry and stock selection

- No first entry before 09:30 IST. Collect live book prices after 09:15.
- Use a five-minute midpoint-price change of at least 0.10% in magnitude.
  Both of the latest two one-minute changes must agree with that direction.
  A rising signal enters LONG; a falling signal enters SHORT. This threshold
  is an implementation default, not a fitted parameter or probability forecast.
- Require continuous observations: sample at most once per five-second bucket,
  reject gaps over 20 seconds and anchors older than 15 seconds. Snapshots and
  stale/duplicate frames cannot establish a signal. Use no future observations.
- The first daily selection chooses the strongest absolute percentage move
  among eligible affordable stocks. Ties use alphabetical symbol order.
- Continue trading the focused stock, re-evaluating its direction before each
  entry. After every complete exit, wait four minutes before any new entry,
  including a switch to another stock. No signal means wait, not reuse the old
  direction. Recheck direction at the simulated entry fill.
- The existing rotation tests remain: three consecutive net losses or the last
  three completed trades total at most INR 10 net. On rotation, choose uniformly
  among confirmed trending eligible alternatives, excluding the stock just left.
- Hold one position at a time; budget is at most INR 2,000 including entry fees,
  limited by available virtual cash. No new entry at/after 14:45, allowing a
  15-minute hold before compulsory square-off.

## Exit precedence

1. Existing system/daily-risk exits and 15:00 IST square-off override every hold.
   The INR 100 accumulated-loss guard includes open losses and is still active.
2. Exit immediately when estimated whole-trade liquidation net is strictly
   above INR 5, including both sides' fees/taxes. Profit overrides all waits.
3. When net first falls strictly below INR -5, persist the recovery start time.
   This triggers a wait, not an immediate stop order. Recovering/worsening prices
   and restarts never reset the timer.
4. During the first recovery hour, only profit or higher-priority exits apply.
   After one hour, exit when net is INR -0.50 or better. INR -0.50 is the explicit
   near-zero tolerance. Positive net below INR 5 also qualifies after that hour.
5. At two hours from the first breach, request an exit at the available price
   even if the loss remains. A stale feed can delay the fill; never invent one.

Without a profit/recovery/risk/square-off exit, hold the trade. Fifteen minutes
is a minimum, not an automatic exit deadline. There is no fixed per-trade price
stop. Waiting can increase the loss, and the two-hour timeout is not a maximum
loss guarantee. All exit thresholds are triggers; later simulated fills and
final fees may produce different results. Slippage, spread and fees continue
to be included honestly; historical fees and trades are not rewritten.

## Existing VM upgrade

Run while flat, after 15:15 IST and before the 17:00 shutdown, or before 09:10
on the next planned session. Do not run the fresh-trial installer.

```bash
export TRADEX_RELEASE='FULL_REVIEWED_COMMIT_SHA'
git clone https://github.com/MishraLtd/TradeX-Autonomous.git "$HOME/tradex-trend-$TRADEX_RELEASE" &&
cd "$HOME/tradex-trend-$TRADEX_RELEASE" &&
git checkout --detach "$TRADEX_RELEASE" &&
sudo bash deploy/azure/upgrade-upstox-greedy.sh "$TRADEX_RELEASE" --trend-recovery
```

The upgrade backs up the existing trial, preserves cash, trades, dates and
daily risk pauses, and resolves all eight approved stocks. It resets selection
state for the new policy and records the change. Fresh market observations must
build a new trend window. It never clears today's losses to resume trading.
Health/backup checks and a fresh worker heartbeat are required by the installer.

```bash
cd /opt/tradex/current/Phase2A_Execution
sudo systemctl is-active tradex.service tradex-health.timer tradex-backup.timer
sudo -u tradex env TRADEX_MODE=upstox-paper /opt/tradex/current/venv/bin/python -m execution.paper_trial report
```

Verify `entry_policy=TREND_ROTATION`, `exit_policy=NET_RECOVERY`,
`net_profit_inr=5`, `trend_settings` and the new release. The report now includes
the actual `heartbeat`. Trades include `entry_trend` and, after a loss breach,
`recovery_at`; events record cooldowns and recovery deadlines. Exit reasons are
`NET_PROFIT_TAKE`, `RECOVERY_NEAR_ZERO`, `RECOVERY_TIMEOUT`, `RISK_HALT` or
`INTRADAY_SQUARE_OFF`. Reports distinguish earlier policy phases.

Upstox supplies data only. Every trade remains a local paper simulation.
