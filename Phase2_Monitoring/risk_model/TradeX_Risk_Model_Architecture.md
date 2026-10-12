# TradeX Risk Prediction Model — Architecture & Mathematical Specification

**Status:** Phase 1 (baseline layer) implemented and tested alongside this
document. Phases 2-4 (fitted ML, portfolio integration, DB/monitoring) are
specified here but not yet implemented — see the roadmap at the end.

---

## 1. Executive Architecture

The Risk Model is a pure **analytical component**. It never places, blocks,
or resizes a trade by itself. It answers one question — *"given this
candidate trade, what can go wrong, how likely, how bad, how fast?"* — and
hands a structured, versioned answer to the **Risk Engine**, which is the
actual safety authority.

```
Market Data → FILTER2.0 → Feature Engine → {Return Model, Risk Model}
            → Cost Model → Regime Model → Opportunity Engine
            → Portfolio Manager → Risk Engine → Execution Engine → Kite
```

Two independent pathways share infrastructure but not targets:

- **INTRADAY** (≈30min → EOD): dominated by intraday volatility, spread,
  and time-of-day effects. No overnight gap risk.
- **DELIVERY** (1-10 trading days): dominated by multi-day volatility,
  overnight gap risk, and event/regime risk over a longer window.

Both pathways produce the same `RiskPrediction` schema (Section 20 spec /
`schema.py`), so the Opportunity Engine doesn't need to special-case them.

---

## 2. Objective & Philosophy

`P(adverse outcome | information at entry)` — a full conditional
distribution, not a single label. Concretely, for every candidate trade we
want an estimate of:

- `P(loss)`, `P(stop hit)`, `P(target before stop)`
- The **magnitude** of downside if it happens: expected/median downside,
  MAE quantiles, tail quantiles
- **How fast** it could happen: expected time-to-stop
- **How confident** the model is in all of the above

Explicitly rejected as insufficient: `risk = 1 - P(profit)`. Two trades at
70% profit probability with -0.5% vs -4.0% expected downside are not
equivalent, and the schema keeps these as separate fields precisely so the
Opportunity Engine can distinguish "Trade A" from "Trade B" in the spec's
own worked example (Section 50 / this doc's §12).

Priority order (Section 61): **capital preservation > accurate downside
estimation > calibration > position sizing > opportunity selection >
returns.** The model is never tuned to maximize trade count, raw
classification accuracy, or gross return.

---

## 3. Mathematical Target Definitions

Let entry occur at time `t₀` at price `P₀`. Let the horizon be `H` bars
(intraday: minutes/bars to EOD; delivery: 1-10 trading days). Let
`{Hᵢ, Lᵢ, Cᵢ}` for `i = 1..H` be the forward OHLC path strictly after `t₀`.

| Target | Definition |
|---|---|
| **Final return** | `(C_H - P₀) / P₀` |
| **MAE** (Section 6) | `min(0, min_i(Lᵢ) - P₀) / P₀` — worst excursion, not final return |
| **MFE** (Section 7) | `max(0, max_i(Hᵢ) - P₀) / P₀` |
| **Stop-hit (Target B)** | First `i` such that `Lᵢ ≤ stop_price`, compared against first `i` such that `Hᵢ ≥ target_price`; label ∈ {STOP, TARGET, NEITHER} |
| **Downside quantiles (Target E)** | Empirical `Q10..Q90` of the MAE (or final-return, when negative) distribution within a conditioning bucket |
| **Large-loss probability (Target F)** | `P(loss > θ)` for configurable `θ ∈ {0.25, 0.5, 1, 2, 3}%` |
| **Gap risk (Target G, delivery only)** | `(Open₁ - Close₀) / Close₀`, clipped to `≤ 0`, where `Close₀` is the entry-day close and `Open₁` the next session's open |
| **Time-to-risk-event (Target I)** | Bar index at which the stop (or a configurable adverse threshold) is first breached; modeled as a discrete time-to-event, right-censored at `H` when neither barrier is hit |

**Right-censoring is handled explicitly** (`labels.py:compute_trade_label`
returns `None`, forcing the row to be dropped) — not zero-filled — when the
full forward horizon isn't observable. This matters most near the end of
any historical dataset.

---

## 4. MAE/MFE Methodology

MAE and MFE are computed once per (entry, horizon, stop, target) tuple by
walking the forward OHLC path (`labels.compute_trade_label`). We report:

- `expected_MAE` (mean), `MAE_p25/p50/p75/p90` (empirical quantiles),
  `P(MAE ≤ threshold)`.
- MFE is reported alongside MAE and final return but **never substituted**
  for risk — it exists to distinguish "genuinely dangerous" trades from
  ones that merely fluctuated before working out (Section 7).

**Phase 1** estimates the MAE distribution analytically via GBM Monte
Carlo (`baselines.simulate_stop_target_race`) — the running minimum of
simulated log-price paths, converted back to percentage terms. **Phase 2**
replaces/supplements this with empirical quantile regression
(`baselines.build_quantile_mae_model`, gradient-boosted quantile loss)
once enough real historical trade-like observations exist per
`min_training_samples_*`.

---

## 5. Stop-vs-Target Race Methodology

We model this as a two-barrier first-passage problem. Two implementations,
by design, in order of increasing data requirements:

1. **Analytical/simulation baseline** (Phase 1, no historical samples
   needed): treat log-price as GBM with the forecast volatility (see §7)
   and zero or small drift; Monte Carlo simulate `n_paths` forward paths
   over `H` bars; record which barrier (if either) is crossed first. This
   is deterministic given a fixed seed, cheap (~20k paths in <50ms), and
   fully auditable — no black box.
2. **Calibrated classifier** (Phase 2): logistic regression (with Platt
   calibration, chosen over isotonic because isotonic overfits at the
   few-hundred-sample scale this project starts at) trained on the causal
   feature set to predict `stop_hit ∈ {0,1}` directly from realized
   history, compared against baseline 1 out-of-sample before promotion.

Both report `P(stop before target)`, `P(target before stop)`, and
`P(neither)` so they sum to 1 — this is asserted implicitly by construction
in the simulation and should be asserted explicitly as a unit invariant
once Phase 2's classifier reports all three.

---

## 6. Downside-Distribution & Tail-Risk Methodology

`P(loss > θ)` for each configured threshold is estimated by re-running the
same barrier simulation with the loss threshold as a one-sided barrier
(target effectively disabled) — see `inference.py`'s `large_loss_probs`
loop. This keeps the large-loss estimate consistent with the same
volatility model used for the stop/target race, rather than a separately
calibrated tail model that could disagree with it.

**Tail reliability flag**: per Section 9, we never report a precise 99th
percentile from inadequate samples. `RiskPrediction.tail_estimate_reliable`
is set `False` (and `tail_risk_p95/p99` left `None`) whenever the OOD
detector flags the current state, or whenever `similar_sample_count` (once
Phase 2's conditional-bucket approach lands) is below
`config.min_similar_state_samples`.

---

## 7. Volatility Forecasting Methodology

Estimated **only** from information available strictly before entry
(`features.py`):

- `ATR(14)` and `ATR(14)/close` (`atr_14_pct`)
- Rolling realized volatility of log returns at 5/10/20-day windows,
  annualized (`vol_5d/10d/20d`)
- EWMA volatility (span 20) as a more recency-weighted alternative
  (`ewma_vol_20`)

All are computed via pandas rolling/EWM windows that are right-closed at
the current bar — never centered, never using negative shifts — which is
what makes them causal by construction (verified by the leakage test, §11
below). The default estimator fed into the Monte Carlo baseline is
`vol_20d`, but the interface accepts an explicit override so a future
regime-aware or intraday-bar-resolution estimator can be swapped in
without changing `predict_risk()`'s signature.

**Explicit non-goal for Phase 1**: true intraday (sub-daily) volatility
estimation. The current feature set is daily-bar-resolution, which is an
acceptable proxy for delivery trades but a known limitation for intraday
risk (see §16, Known Limitations). Phase 2 should add minute/5-min-bar
volatility features once that data is available from FILTER2.0 upstream.

---

## 8. Gap-Risk Methodology (Delivery only)

A stop-loss order does **not** protect against an overnight gap that opens
below the stop. `labels.compute_trade_label` computes
`gap_down_pct = min(0, (next_open - entry_price)/entry_price)` on the
first forward bar for delivery trades specifically, so this is tracked
separately from intraday stop-hit risk (Section 10's core distinction).

**Phase 1 limitation, stated plainly**: `inference.py` currently uses a
placeholder constant (`0.15`) for `probability_of_gap_down` in the risk
score, because fitting a real gap model requires a per-symbol historical
distribution of overnight gaps, which the Monte Carlo baseline (a
continuous-path GBM) does not produce — GBM has no jumps. Phase 2 must fit
an empirical or jump-diffusion gap model from real overnight-return
history (`overnight_return_history`, `historical gap frequency`, per the
spec's §10 feature list) before this number can be trusted. Until then,
`RiskPrediction.probability_of_gap_down` should be treated as indicative,
not decision-grade, for delivery trades — this is exactly the kind of gap
the abstention mechanism (§10 below) should eventually flag.

---

## 9. Feature Architecture & Leakage Prevention

`features.py` builds one row of causal features per historical bar:
returns, ATR, rolling/EWMA volatility, SMA-distance (20/50/200), RSI,
relative volume, gap%, range%, drawdown-from-60-day-high. Trade-specific
features (stop distance, target distance, holding horizon, position size)
are appended at the trade level, not baked into the series-level feature
frame, since they vary per candidate trade rather than per bar.

**Leakage prevention is enforced, not just documented:**

1. Every feature is a backward-looking rolling/EWM window (`min_periods`
   equal to the window, so partial windows produce `NaN` rather than a
   biased early estimate) — verified by `tests/test_leakage.py`'s
   `test_features_are_causal`, which mutates all OHLCV data after a cut
   point by 5-10x and asserts every feature value at or before the cut is
   bit-for-bit unchanged.
2. Labels (`labels.py`) carry explicit `entry_timestamp` and
   `target_window_end_timestamp` fields. `validation.assert_no_lookahead`
   and `validation.assert_no_train_test_overlap` turn the "features must
   precede entry" and "no purge failure" invariants into automated
   assertions (`LeakageError`) rather than manual review items.
3. `labels.compute_trade_label` **requires the full forward horizon** to
   be observable; if not, it returns `None` and the row is dropped —
   never zero-filled, which would silently bias the label toward "no
   event."

---

## 10. Calibration, Uncertainty & Abstention Methodology

**Calibration** (Section 16) is treated as more important than raw
accuracy for a risk system. `calibration.py` implements Brier score, log
loss, expected calibration error (ECE), and per-bin reliability curves
(predicted-mean vs observed-frequency in 10 buckets). `config.py` defines
`calibration_degradation_ece_threshold = 0.08`; a live monitoring job
(Phase 3) should compare rolling realized outcomes against predictions and
raise `RISK_MODEL_CALIBRATION_DEGRADED` when ECE exceeds this.

**Uncertainty** (Section 17-18) is not `confidence = probability × 100`.
Phase 1's uncertainty is `min(1, baseline_only_floor + 0.5 · min(ood_score,
1))` — i.e. it has a floor because the Monte Carlo baseline has no
historical validation yet, plus a term scaling with distributional
novelty. Phase 2 should replace the floor term with actual measured
out-of-sample calibration quality and ensemble disagreement once a fitted
model exists, per the components listed in Section 18 (training sample
support, feature completeness, distance from training distribution,
calibration quality, ensemble agreement).

**OOD detection** (Section 19): `ood.py`'s `OODDetector` fits a Gaussian
reference (mean + regularized covariance) on the training feature
distribution and flags a new observation via squared Mahalanobis distance
against the chi-square quantile for the feature dimensionality
(`chi2_percentile = 0.995` by default). This is intentionally the
simplest statistically-grounded method available — cheap, deterministic,
interpretable — matching the "avoid unnecessary complexity" priority.
Ensemble-disagreement or density-based OOD can replace it later without
changing the downstream contract (`ood_score: float`,
`is_out_of_distribution: bool`).

**Abstention** (Section 48): when `is_ood` is true, `risk_model_status`
is set to `OUT_OF_DISTRIBUTION`; when `uncertainty` exceeds
`config.default_max_model_uncertainty`, it's set to `UNCERTAIN`. The Risk
Engine (not this model) decides what to do with that status — likely
`REJECT` or a severe size reduction, per Section 49's gate logic.

---

## 11. Risk Score Formula (Section 21)

Fully specified in `scoring.py`. It is a weighted sum of **normalized**
components (each rescaled to `[0,1]` "danger" units against a documented
reference magnitude before combining):

```
score01 = 0.20·P(loss) + 0.15·P(stop hit)
        + 0.15·clip(|E[downside]| / 3%, 0, 1)
        + 0.15·clip(|MAE_p90| / 5%, 0, 1)
        + 0.10·clip(volatility / 60%, 0, 1)
        + 0.10·P(gap down)             [delivery only]
        + 0.15·uncertainty
risk_score = round(100 · clip(score01, 0, 1), 2)
```

Reference magnitudes (3% downside, 5% MAE p90, 60% annualized vol) are
chosen as "large move" benchmarks for short-horizon NSE mid/small-cap
trades — they are configuration, not law, and are versioned
(`SCORING_VERSION`) so historical scores stay interpretable if the weights
change. Weights sum to 1.0 by construction (asserted at import time).
Crucially, this is a **summary for sorting/thresholding only** — the
Portfolio Manager and Risk Engine always have access to every underlying
component, per Section 22's instruction not to collapse everything into
one metric too early.

---

## 12. Worked Example (Section 50, reproduced against this design)

**Trade A** — Entry ₹100, Target ₹102, Stop ₹99:
Return Model: `E[return]=+2.0%, P(profit)=72%`.
Risk Model: `P(loss)=28%, P(stop hit)=22%, E[downside]=-0.65%,
E[MAE]=-0.75%, P(loss>1%)=4%` → **low tail risk, small controlled losses.**

**Trade B** — same `E[return]` and `P(profit)`, but
`E[downside]=-1.7%, E[MAE]=-2.2%, P(loss>1%)=19%, P(loss>2%)=8%` → **same
apparent quality by the Return Model alone, materially worse by every risk
field.**

This is exactly why `risk = 1 - P(profit)` is rejected (§2): a Return-Model-only
view would treat A and B identically. The full `RiskPrediction` schema
keeps every one of these fields distinguishable so the Opportunity Engine
computes net economics on the actual joint distribution, not a collapsed
proxy.

---

## 13. Validation Methodology (Sections 33-34, 56-58)

**Never random-split trade rows.** `validation.PurgedWalkForwardSplit`
implements chronological expanding-window folds where:

- **Purge**: any training row whose label window
  (`target_window_end_timestamp`) extends into the test period is dropped.
  This matters most for multi-day delivery horizons, where two trades
  entered a day apart can have overlapping label windows — naively
  training on one and testing on the other leaks future price information.
- **Embargo**: an additional `config.embargo_bars` (default 5) rows
  immediately preceding the test period are also dropped, to absorb
  residual serial correlation beyond what the label window itself
  captures.

`validation.assert_no_train_test_overlap` turns this into an automated
check rather than a hoped-for property; `tests/test_leakage.py`'s
`test_purged_split_has_no_overlap` exercises it against synthetic data.

**Model comparison** (Sections 58-60): a candidate is only promoted if it
beats the *previous* tier out-of-sample on calibration (Brier/ECE) **and**
the trading-utility metrics (Section 35/47) — not on backtest return
alone, and not via repeated testing on the same fixed out-of-sample
period (Section 71).

---

## 14. Database Schema (Section 53)

Proposed tables (not yet created — Phase 4). Matches the project's
existing Supabase usage (the `ohlcv_daily` table already backs the
FILTER2.0 pipeline per prior work on this project).

```sql
risk_model_versions (
    model_version text primary key,
    feature_version text not null,
    config_version text not null,
    trade_type text not null,               -- INTRADAY | DELIVERY
    training_cutoff timestamptz not null,
    created_at timestamptz not null default now(),
    notes text
);

risk_predictions (
    id bigserial primary key,
    symbol text not null,
    exchange text not null default 'NSE',
    trade_type text not null,
    prediction_timestamp timestamptz not null,
    entry_price numeric not null,
    stop_price numeric not null,
    target_price numeric not null,
    holding_horizon text not null,
    -- full RiskPrediction payload, stored both structured (for querying)
    -- and as jsonb (for forward-compatible schema evolution):
    probability_of_loss numeric,
    probability_of_stop_hit numeric,
    expected_downside_pct numeric,
    expected_mae_pct numeric,
    mae_p90_pct numeric,
    risk_score numeric,
    prediction_confidence numeric,
    prediction_uncertainty numeric,
    risk_model_status text,
    payload jsonb not null,
    model_version text references risk_model_versions(model_version),
    created_at timestamptz not null default now()
);
-- never UPDATE a row in risk_predictions: every prediction is immutable
-- once written (Section 54 — "never overwrite historical predictions").

risk_realized_outcomes (
    prediction_id bigint references risk_predictions(id),
    realized_loss boolean,
    realized_return_pct numeric,
    realized_mae_pct numeric,
    realized_stop_hit boolean,
    realized_gap_pct numeric,
    exit_timestamp timestamptz,
    created_at timestamptz not null default now()
);

risk_calibration_runs (
    id bigserial primary key,
    model_version text references risk_model_versions(model_version),
    evaluation_window_start date,
    evaluation_window_end date,
    brier_score numeric,
    log_loss numeric,
    expected_calibration_error numeric,
    n_samples int,
    status text,  -- OK | RISK_MODEL_CALIBRATION_DEGRADED
    created_at timestamptz not null default now()
);
```

Indexes: `risk_predictions(symbol, prediction_timestamp)`,
`risk_predictions(model_version)`,
`risk_realized_outcomes(prediction_id)`.

---

## 15. Python Architecture (as implemented)

```
risk_model/
    __init__.py
    config.py          # RiskConfig dataclass — every threshold, documented
    exceptions.py       # RiskModelError, InsufficientDataError, LeakageError, DataQualityError
    schema.py           # Pydantic RiskPrediction + status enums
    features.py          # causal feature engineering (FEATURE_VERSION)
    labels.py            # causal label construction (training-time only)
    validation.py        # purged walk-forward split + leakage assertions
    calibration.py        # Brier/log-loss/ECE/reliability curves
    ood.py                # Mahalanobis OOD detector
    baselines.py           # naive / Monte Carlo / calibrated logistic / quantile GBM
    scoring.py              # documented risk_score formula
    inference.py             # predict_risk() orchestration
    tests/
        conftest.py           # synthetic OHLCV fixture
        test_leakage.py         # causality + purge/embargo tests
        test_baselines.py        # baseline sanity tests
        test_calibration.py       # calibration metric correctness
        test_inference.py          # end-to-end smoke test
    requirements.txt
    README.md
    ARCHITECTURE.md (this file)
```

Deliberately **not** microservices — a single importable package that can
run in the same process as the rest of TradeX's Python pipeline, or be
wrapped in a thin script for a scheduled job. Matches Section 52's
instruction to keep the first implementation easy to run locally and
deploy to a VPS/Azure box (the project already runs an n8n VM for
ingestion — this package doesn't need its own service).

---

## 16. Testing Strategy & Current Coverage

12 tests currently pass (`pytest risk_model/tests -v`):

- **Leakage** (5 tests): causal-feature invariance under future mutation;
  right-censored labels are dropped, not zero-filled; label window always
  ends after entry; purged splits have zero train/test overlap;
  `assert_no_lookahead` correctly raises on a synthetic violation.
- **Baselines** (3 tests): naive frequency baseline matches hand-computed
  frequencies; Monte Carlo race gives symmetric probabilities for
  symmetric stop/target distances and correctly orders MAE tail
  percentiles; calibrated logistic pipeline fits and produces a valid
  calibration report on synthetic data.
- **Calibration** (3 tests): near-zero ECE on well-calibrated synthetic
  probabilities; large ECE correctly flags a badly-calibrated model;
  Brier score hits its exact 0/1 bounds on perfect/inverted predictions.
- **Inference** (1 test): end-to-end `predict_risk()` smoke test on
  synthetic OHLCV, asserting schema invariants (probabilities in `[0,1]`,
  tail ordering, score bounds).

**Not yet covered** (needs real data, Phase 2+): gap-down scenarios against
real overnight-return history, regime-conditional behavior, portfolio
concentration/correlated positions, stress scenarios (Section 39), model
version-mismatch handling, and the full Section 56 test matrix (which
assumes a live database and trade journal to test against).

---

## 17. Known Limitations & Self-Critique (Section 74)

Answered against the spec's own 20-question self-critique, honestly:

1. **Can tail risk be underestimated?** Yes — Phase 1's tail estimate
   comes from a continuous-path GBM, which structurally cannot produce
   jump risk (gaps, halts, circuit-limit moves). `tail_estimate_reliable`
   exists precisely to flag this, but is not yet wired to detect "this
   instrument has fat-tailed jump behavior" — that requires empirical gap
   data (Phase 2).
2. **Is calibration actually good?** Unknown — Phase 1 has no fitted model
   trained on real outcomes yet, so there's nothing to calibrate. The
   Monte Carlo baseline is *internally consistent* (its own probabilities
   sum correctly) but not *empirically validated* against realized TradeX
   outcomes. This is honestly reflected in the fixed uncertainty floor.
3. **Does it beat a simple baseline?** N/A yet — there's no ML model in
   the loop to compare. Baseline 1 vs Baseline 2 comparison is possible
   today with synthetic data; a real comparison needs real trade history.
4. **Look-ahead bias?** Actively tested against (§9/§16), but only for
   the feature functions implemented so far — any new feature added later
   must extend `test_features_are_causal` or the guarantee silently stops
   holding for that feature.
5. **Survivorship bias?** Not addressed at all yet — `ohlcv_daily` (the
   project's actual data source) would need to be checked for whether it
   retains delisted symbols; if not, this is a real, currently
   undocumented gap that should be logged before Phase 2 training begins.
6. **Overnight gap risk understood?** Structurally present in the schema
   and label construction, but the actual number (§8 above) is a
   placeholder — explicitly called out, not hidden.
7. **OOD/drift detection?** OOD: yes (Mahalanobis). Drift monitoring
   (Section 43): not implemented — needs a live feedback loop from
   realized outcomes, which requires the trade journal to exist first.
8. **Can the Risk Engine override the model?** Yes by architecture — this
   package never executes anything; it only returns a typed prediction.

---

## 18. Roadmap

- **Phase 2** (needs real `ohlcv_daily` history + a fixed trade-config
  convention): fit Baseline 3 (calibrated logistic) and the quantile-MAE
  model per trade_type; run the A/B comparison (Section 47) against
  Baseline 2; fit `OODDetector` on real feature distributions.
- **Phase 3**: drift/calibration monitoring against a live trade journal;
  gap-risk model fit from real overnight-return history; regime-
  conditional risk (Section 29).
- **Phase 4**: Supabase persistence per §14's schema; Portfolio Risk
  Engine interface (incremental risk, correlation, Section 27-28); risk-of-
  ruin simulation for the ₹1,000 capital constraint (Section 38).

This document and the accompanying package should be read together with
the project's own 74-section specification; nothing here contradicts it —
each section above cites the spec section it implements.
