# TradeX Market Regime Model — Architecture & Design Record

Status: **Phase 5 (rule-based baseline) implemented and tested.** Phases 6+
(statistical/ML detector, calibration, empirical strategy-performance
matrix, live backtest integration, shadow mode, drift monitoring) are
**scaffolded with real interfaces but require your historical NSE data and
a live/paper trading loop to actually run** — see "What's NOT done yet"
at the bottom. Nothing here pretends those later phases are complete.

---

## Phase 1 — Executive Architecture

```
Market Data Engine → Feature Engine → MARKET REGIME MODEL → Strategy Router
                                            │
                    (context only, never orders) 
                                            ▼
                        Return Model, Risk Model, Cost Model,
                        Portfolio Manager, Risk Engine, Execution Engine
```

The package `market_regime/` is a pure-stdlib Python library (no numpy /
pandas / pydantic) that exposes one class, `RegimeEngine`, as the
integration point (§38). It is read-only with respect to trading: every
method returns data, never places, sizes, or cancels an order (§54).

## Phase 2 — Regime Definitions (final taxonomy decision)

**7 states**: `TRENDING_UP, TRENDING_DOWN, SIDEWAYS, HIGH_VOLATILITY,
LOW_VOLATILITY, PANIC, UNKNOWN`.

Rationale for NOT adding the transitional classes the spec asked us to
investigate (`EARLY_TREND`, `TREND_EXHAUSTION`, `VOLATILITY_EXPANSION`,
`BREAKOUT_ENVIRONMENT`, ...): every extra class needs its own reliable
sample size to estimate (§27), and PANIC is already rare — splitting the
already-scarce "interesting" regimes further would make each one
statistically unreliable long before we could show it improves trading
(§9, §52). Instead, transitional *concepts* are represented as **derived
signals on top of the 7-state timeline**:
- "early trend" ≈ `regime_duration_bars` is small and `transition_probability`
  from a prior SIDEWAYS/PANIC is still non-trivial
- "trend exhaustion" ≈ long `regime_duration_bars` + rising `transition_probability`
  away from the current label
- "volatility expansion/contraction" ≈ `volatility_percentile` trend over the
  last N bars (not a separate class, a derivative of the existing one)
- "breakout environment" ≈ SIDEWAYS → TRENDING_* transition with rising
  `transition_probability`

This keeps the label set small (interpretable, low data requirement) while
still answering "are we in a transition" via `transition_probability` /
`transition_direction` / `regime_duration_bars` in the output schema. If a
walk-forward ablation later shows a finer taxonomy earns its keep (§40),
`RegimeLabel` can be extended — nothing else in the architecture assumes
exactly 7 members.

## Phase 3/4 — Feature Taxonomy & Mathematical Definitions

Implemented, all causal (no look-ahead — every function takes an
`end_index` and only reads `values[:end_index+1]`):

| Dimension | Module | Method |
|---|---|---|
| Trend | `trend.py` | MA structure (price/fast/slow SMA ordering) + MA slope (OLS on trailing SMA-50 series, mean-normalized) + Wilder ADX(14), combined as `0.5·structure + 0.5·direction`, gated (not directed) by ADX strength |
| Volatility | `volatility.py` | ATR(14) normalized by price, percentile-ranked against a trailing 252-bar **strictly historical** window (excludes the current bar) |
| Breadth | `breadth.py` | `% of universe above SMA50` (or advance/decline ratio as fallback); returns `UNKNOWN` rather than a fake neutral when the data source doesn't provide it (§20's "don't introduce unnecessarily expensive data" — for a ₹1,000 book, this is correctly optional) |
| Momentum | `momentum.py` | Average of 3 multi-horizon (¼, ½, 1× lookback) returns — deliberately not a single RSI/ROC reading (§3E says multi-horizon) |
| Stress | `stress.py` | Additive 0–8+ point score from: 1-day return, 20-bar drawdown, ATR percentile, breadth collapse, correlation z-score (optional input), volume z-score — bucketed into `StressLevel.{NORMAL,ELEVATED,SEVERE,PANIC}` per §16. This is the multi-factor score the spec explicitly asked for instead of `NIFTY < -2%`. |

All thresholds live in one place (`config.py`), none are scattered as
magic numbers in logic files, and every one is flagged
`CONFIG_VERSION = "v0-uncalibrated"` — see "Honesty flag" below.

## Phase 5 — Baseline Rule-Based Detector (`rule_based.py`)

`classify_regime(dims) -> RegimeProbabilities` combines the 5 dimension
reads into a transparent, additive "support score" per regime label (not
a black box — every point added is traceable to a specific dimension
read). PANIC has a hard override: if `stress_level == PANIC`, the PANIC
score is forced to beat every other label, because a violent crash
computed over the same window a trend detector reads as "strong down"
must not be silently reported as merely `TRENDING_DOWN` (§4/§16).

`heuristic_confidence(probs)` = normalized margin between the top and
runner-up support score, squashed into `[0, 0.97]`. **This is explicitly
NOT a calibrated posterior probability** (§7, §28) — see `calibration.py`.

## Phase 6 — Alternative Statistical/ML Approaches (scaffolded, not built)

Per §9/§52 ("do not assume ML is automatically superior... if simple rules
win, use simple rules"), the correct next step is NOT to hand-write a GMM
or HMM speculatively — it's to run the rule-based baseline (already built)
against your actual historical NIFTY/stock data via walk-forward
backtesting (Phase 15) and see whether it's already good enough. The
architecture is ready for a statistical/ML challenger to slot in without
touching `inference.py`:
- `calibration.py::BaseCalibrator` — implement `PlattCalibrator` /
  `IsotonicCalibrator` once you have (raw_score, was_correct) pairs.
- A future `statistical.py` / `ml_model.py` would implement the same
  `classify_regime(dims) -> RegimeProbabilities` signature as
  `rule_based.py`, and `inference.RegimeEngine` would ensemble/compare them
  — the ensemble slot is `RegimeEngine.__init__`, not spread through the
  codebase.
- Do NOT build this until Phase 15's walk-forward harness shows the
  rule-based baseline's downside on real data (§10: classification
  accuracy alone is not the goal — trading economics are).

## Phase 7 — Model Selection Methodology

Once a challenger model exists (Phase 6), compare rule-based vs
statistical vs ML vs ensemble using the SAME walk-forward splits (Phase 15)
and the SAME downstream trading-economics metrics from §10 (net return,
Sharpe, max drawdown, turnover, tail losses) — not classification accuracy
in isolation (§10). The winner must beat the rule-based baseline by a
margin that survives the sample-size/robustness checks in §50/§51 before
it replaces it in production.

## Phase 8 — Confidence & Uncertainty Methodology

Implemented as `heuristic_confidence()` today (Phase 5's honest,
uncalibrated margin measure) with `RegimeProbabilities.entropy()` available
once/if a properly calibrated probabilistic model is plugged in (§7 says
don't force-normalize a heuristic score into fake probabilities — we
don't). `ConfidenceConfig` buckets (`low=0.50, cautious=0.70, normal=0.85`)
gate trade permission in `position_adjustment.py` — flagged as priors
pending empirical calibration (§29).

## Phase 9 — Transition Detection (`transitions.py`)

`RegimeHistory` is a small stateful tracker (one per `(symbol_scope,
timeframe)`) that:
1. Confirms a new label only after N consecutive raw votes agree
   (`confirmation_bars`) AND the current regime has held for at least
   `min_regime_duration_bars` — this is the hysteresis mechanism that
   directly answers §18 ("regime flapping").
2. Overrides both locks immediately for `PANIC` — capital preservation
   (§45) beats label stability when the market is actually panicking.
3. Derives `regime_stability_score` (fraction of the last 20 bars matching
   the current confirmed label) and a simple `transition_probability` /
   `transition_direction` from recent raw-vote disagreement — intentionally
   NOT a fitted Markov transition matrix, because that needs labeled
   historical regime sequences to estimate reliably (§6 flags this as
   worth testing further once that data exists).

## Phase 10 — Strategy Compatibility Methodology (`strategy_compatibility.py`)

`STRATEGY_REGIME_PRIOR` is a small, explicitly-labeled **prior** matrix
(momentum likes trends, mean-reversion likes sideways, etc.) — per §11
("DO NOT simply hard-code these mappings... only if backtesting
demonstrates the relationship"), the function is built to blend this prior
with an empirical `strategy_regime_performance` table (schema in
`db/schema.sql`) once ≥30 trades exist per (strategy, regime) cell (§27),
shrinking toward the empirical estimate as sample size grows. Until that
table has data, it runs on the prior alone — and every score is separately
discounted by regime confidence × stability, so an uncertain/unstable read
mutes ALL strategy scores rather than just changing which one wins (§30).

## Phase 11 — Position-Sizing / Gating Methodology (`position_adjustment.py`)

Same honesty pattern: `BASE_POSITION_MULTIPLIER_PRIOR` /
`BASE_GATING_PRIOR` are flagged priors (§31 says these must be
calibrated). Final multiplier = `base_prior × confidence_factor ×
stability_factor`, continuous (not a step function) so small confidence
changes don't cause discontinuous position jumps. Gating precedence, most
conservative always wins (§44):
1. Stale/insufficient data → `BLOCK`, multiplier `0`, no exceptions.
2. `PANIC` stress → `BLOCK`; `SEVERE` stress → at least `RESTRICT`.
3. Confidence below `low`/`cautious` thresholds escalates gating by one
   severity level regardless of what the regime label says.

## Phase 12 — Database Schema

See `db/schema.sql`. 6 tables, matching §36's list minus nothing
unnecessary: `market_regime_states` (the timeline), `market_regime_features`
(raw feature audit trail), `market_regime_transitions`,
`market_regime_model_versions`, `strategy_regime_performance` (the
empirical table Phase 10 needs), `market_regime_backtests` (Phase 39's
baseline-vs-regime-aware comparison log). `persistence.py` builds
parameterized `(sql, params)` tuples against this schema without opening a
DB connection itself — wire it into whatever Postgres/Supabase client
TradeX already uses.

## Phase 13 — Python Architecture

```
market_regime/
  __init__.py            package version
  enums.py                RegimeLabel, TrendState, VolatilityState, ...
  config.py                ALL thresholds, one place, versioned
  exceptions.py            InsufficientDataError, StaleDataError, ...
  schemas.py                Bar, InstrumentSeries, DimensionScores,
                            RegimeProbabilities, RegimeState, ...
  _math_utils.py            dependency-free sma/atr/adx/percentile/slope/zscore
  trend.py / volatility.py / breadth.py / momentum.py / stress.py
                            one module per dimension (§3, ablation-testable)
  rule_based.py            Phase 5 baseline classifier
  calibration.py            Phase 8/28 hook (identity today)
  transitions.py            Phase 9/18 hysteresis + duration + transition signal
  strategy_compatibility.py Phase 10/12
  position_adjustment.py    Phase 11/31/32
  validator.py              Phase 20/44 - sufficiency, freshness, OHLC sanity
  persistence.py            Phase 12/36 SQL builders
  inference.py              RegimeEngine - the ONE integration point (§38)
tests/
  factory.py                synthetic OHLCV generators
  test_features.py, test_regime_classifier.py, test_transitions.py,
  test_failure_modes.py     34 tests, all passing (§48/§49)
db/schema.sql
```

Zero third-party dependencies by design (§19/§46 — "low computational
cost", "no unnecessary infra"). Add pydantic/numpy at the API boundary
later if TradeX's other components need JSON-schema validation there;
nothing internal requires it.

## Phase 14 — Backtest Integration (design, not run — no data here)

Planned harness (not built, since it needs your historical bar data):
```
for each walk-forward window (Phase 15):
    run BASELINE: FILTER2.0 → Return Model → Risk → Cost → Trade
    run REGIME_AWARE: same + RegimeEngine.get_current_regime() gating/sizing
    log both to market_regime_backtests with system_variant = 'baseline_no_regime' | 'regime_aware'
compare net CAGR, Sharpe, Sortino, max_drawdown, turnover, tail losses (§10, §39)
```
Success criterion is explicit and matches §39: the regime layer is only
"working" if it improves out-of-sample net risk-adjusted performance, not
if its classification looks clean.

## Phase 15 — Validation Framework (design)

- Chronological train → validation → test → walk forward → retrain,
  never shuffled (§25).
- Minimum data per timeframe (`config.DataFreshnessConfig.min_bars_required`):
  260 daily bars (~1yr) for percentile-based volatility features, 60 bars
  for intraday timeframes.
- Cross-era robustness (§26): evaluate separately across at least one bull
  run, one bear run, one prolonged sideways stretch, and one known
  volatility shock/crash in NSE history before trusting any threshold.

## Phase 16 — Monitoring & Drift (design)

Recommended metrics to compute from `market_regime_states` once it's
populated live: rising frequency of `UNKNOWN`/`BLOCK`, feature-value drift
vs. the training window's distribution, and stability-score degradation.
Retraining should be POLICY-triggered (e.g. quarterly review + drift
threshold breach), never automatic-on-drift (§42 explicitly warns against
auto-retraining just because drift exists).

## Phase 17 — Failure Modes

Implemented in `validator.py` + `inference.RegimeEngine._unknown_state`:
missing/insufficient bars, stale bars, corrupted OHLC (high<low,
non-positive price, negative volume — flagged in `reason_codes`, not
silently dropped) all degrade to `UNKNOWN` + `BLOCK` + multiplier `0`. The
short-timeframe read is authoritative for fail-safety: if it's unusable,
the WHOLE regime state is `UNKNOWN`, even if medium/long timeframes are
fine (§44 "never silently fall back to normal trading").

## Phase 18 — Testing Strategy

34 unit/integration tests across `test_features.py` (math + per-dimension
correctness, no-lookahead), `test_regime_classifier.py` (all 6 real regimes
+ UNKNOWN + multi-timeframe conflict + market/sector/stock alignment +
confidence contrast), `test_transitions.py` (hysteresis, panic override,
stability scoring), `test_failure_modes.py` (corrupted OHLC, gap crash,
zero volume, never-trade-on-UNKNOWN). All passing (`python3 -m unittest
discover -t . -s tests`).

## Phase 19 — Implementation

Done for Phase 5 (this document + the code in `market_regime/`).

---

## Honesty flag (read this before using real capital)

`config.CONFIG_VERSION = "v0-uncalibrated"`. Every numeric threshold in
`config.py` — ADX 25, ATR 80th percentile, breadth 55%/70%, confidence
buckets 0.50/0.70/0.85, position multipliers 1.00/0.70/0.50/0.00 — is a
reasonable textbook starting point, **not** a value fit to your NIFTY/NSE
history. §29/§31/§46 all explicitly say these must be empirically
calibrated, not assumed. Before this touches real capital: run it in
**shadow mode** (§53) against live or recent historical data, log its
calls, compare against what actually happened, THEN paper trade, THEN
consider live — in that order, and only after Phase 14/15's backtest shows
positive incremental value over the no-regime baseline.

## §56 Final self-challenge — answers

1. **Rediscovering the Return Model?** No by construction: the Return
   Model looks at instrument-specific expected return; the Regime Model
   never touches expected return, only trend/vol/breadth/momentum/stress
   context. Overlap risk exists at the trend-feature level (both may use
   moving averages) — worth an explicit correlation check between
   `dims.trend_score` and the Return Model's own trend features once both
   run on real data, so as not to double-count the same signal.
2. **Look-ahead leaking?** All rolling/percentile functions in
   `_math_utils.py` take an `end_index` and never read past it;
   `test_no_lookahead_sma` pins this down. Volatility percentile history
   explicitly excludes the current bar (`range(n - lookback, n - 1)`).
   Not proven for every possible future feature — re-check this
   invariant for any new feature added later.
3. **Are labels objectively meaningful?** Currently rule-derived, not
   ground-truth. Phase 23's proper answer (statistical segmentation /
   clustering-based latent states) needs real data and hasn't been done.
4. **Stable enough to trade?** `transitions.py`'s hysteresis exists
   specifically because raw per-bar votes are NOT stable enough on their
   own — the confirmed-regime layer is what's meant to be tradable, tested
   in `test_transitions.py`.
5–8. **Net returns / drawdowns / risk-adjusted / false positives?**
   Unknown until Phase 14's backtest harness runs on real data — not
   claimed here.
9. **Useful at ₹1,000 capital?** The design goal throughout (low
   turnover via hysteresis, `BLOCK` bias on uncertainty, zero paid
   dependencies) — but only Phase 14's actual backtest, including real
   transaction costs, can confirm it nets positive at that size.
10. **Unlike training data?** `UNKNOWN` is the explicit fallback (§8);
    right now it only triggers on missing/stale/insufficient data, not on
    genuine distribution shift in otherwise-present data — a real
    out-of-distribution detector (e.g. feature bounds learned from
    training data) is a Phase 16 monitoring task, not yet built.
11. **Timeframes disagree?** Reported, not hidden: `regime_alignment_score`
    < 1.0, tested in `test_multi_timeframe_conflict_reduces_alignment_score`.
    The short (decision) timeframe still drives the final label — whether
    that's the right choice vs. requiring alignment before trading is a
    Phase 14 backtest question.
12. **Sudden crash?** `test_gap_crash_pushes_toward_panic_or_high_vol` and
    the PANIC hard-override in `rule_based.py` cover this in synthetic
    data; real gap/circuit-breaker behavior on NSE should be specifically
    tested once real crash-day data is available.
13. **Excessive strategy switching?** Mitigated by hysteresis +
    stability-discounted strategy compatibility scores, not eliminated —
    `test_stability_score_lower_when_flapping` shows the metric correctly
    detects flapping, but nothing yet measures real turnover cost from it
    (Phase 39's job).
14. **Overfit to historical crises?** Can't be assessed without running
    Phase 26's cross-era test on real data; the additive/transparent
    stress score was deliberately kept simple to reduce this risk vs. a
    fitted model, but "kept simple" isn't proof.
15. **Confidence poorly calibrated?** Confirmed and disclosed:
    `heuristic_confidence()` is explicitly uncalibrated (Phase 8/28) — not
    a hidden risk, a stated limitation with a documented upgrade path.
16. **Sector info causing look-ahead?** Sector series go through the same
    causal feature functions as market series — no separate code path
    that could special-case future information.
17. **Stale index data → wrong regime?** Directly handled:
    `StaleDataError` → `UNKNOWN` + `BLOCK` at the engine level, tested in
    `test_stale_data_blocks_even_with_good_trend`.
18. **Fails closed?** Yes — every failure path in `validator.py` and
    `inference.RegimeEngine` resolves to `UNKNOWN`/`BLOCK`/multiplier `0`,
    never a silent default to "normal" (§44), verified by
    `test_unknown_state_never_allows_trading`.
19. **Computationally cheap?** Yes — pure stdlib, O(n) or O(n·window) per
    feature, no external calls, runs in milliseconds on 300-bar series
    (see the test run: 34 tests in ~0.02s).
20. **Complexity justified?** The Phase-5 scope deliberately stops at
    "rule-based + transparent scoring + hysteresis" — no ML has been
    added, precisely because §52 says not to add it until it's proven to
    help on real data.

## What's NOT done yet (explicitly, so nothing is overstated)

- No real NSE/NIFTY historical data has been used anywhere in this
  session — all tests use synthetic series. Every regime-detection claim
  above is validated only against synthetic scenarios designed to exercise
  the logic, not against real market behavior.
- No statistical/ML detector, no fitted calibration (Platt/isotonic), no
  empirical strategy-performance table, no walk-forward backtest, no
  shadow-mode run, no drift-monitoring job — all scaffolded with real
  interfaces, none executed, because they all require your data/data feed
  and a live or historical bar source this environment doesn't have.
- The DB schema (`db/schema.sql`) has not been applied anywhere; I did not
  touch your connected Supabase project. Say the word if you'd like me to
  create these tables there via the Supabase MCP tools — that's a real,
  reviewable action, not something to do silently.
