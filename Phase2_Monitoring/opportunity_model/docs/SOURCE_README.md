# TradeX Opportunity Scoring Model

**Phase 1 — deterministic, auditable, capital-aware decision layer.**
Status: implemented, tested (31/31 passing), runnable worked example.

---

## 1. Executive Architecture

```
MARKET DATA → FILTER2.0 → FEATURE ENGINE → RETURN MODEL → RISK MODEL
→ MARKET REGIME MODEL → COST MODEL
→ HARD ECONOMIC/RISK GATES (Level 1)
→ OPPORTUNITY SCORING MODEL (Level 2: quality, Level 3: ranking)
→ TOP VALID OPPORTUNITIES (or NO TRADE)
→ PORTFOLIO MANAGER → RISK ENGINE → EXECUTION ENGINE → ZERODHA KITE
```

The Opportunity Model is a **synthesis and ranking layer**, not a
predictor. It consumes the outputs of the Return, Risk, Regime, and Cost
Models — it never recomputes them. Its only questions are: *given
everything upstream already believes, how attractive is this opportunity,
and how does it compare with the alternatives?*

## 2. Responsibility Boundaries

| Model | Question it answers | Owns |
|---|---|---|
| FILTER2.0 | Is this security worth considering at all? | universe selection |
| Return Model | What return distribution is reasonable? | `expected_return`, quantiles, `probability_of_profit` |
| Risk Model | What could go wrong? | downside, drawdown, MAE, stop-loss probability, tail risk |
| Regime Model | What environment are we in, and does this strategy fit it? | `regime`, `regime_compatibility` |
| Cost Model | How much of the edge survives friction? | net return, cost ratio, break-even move, scenarios |
| **Opportunity Model** | **Given all of the above, how attractive is this vs. alternatives?** | gates, component scores, ranking |
| Portfolio Manager | Should we allocate scarce capital? | diversification, sizing |
| Risk Engine | Are we allowed to take this risk? | hard safety limits, kill switches |

The Opportunity Model never recalculates broker charges, regime
detection, volatility, or slippage — it only reads and synthesizes.

## 3. Mathematical Formulation

We rejected a single closed-form "Expected Net Value" equation (spec §7)
in favor of a **three-level architecture** (spec §19), because a single
formula conflates three genuinely different questions with different
failure semantics:

- **Level 1 (Hard Eligibility)** — boolean, gate-based. "Can this be
  considered at all?" Implemented in `gates.py`.
- **Level 2 (Opportunity Quality)** — a 0–100 weighted synthesis of
  interpretable components, each independently owned/tested/documented.
  Implemented in `components.py` + `scoring.py`.
- **Level 3 (Relative Ranking)** — sorts the *eligible* candidates and
  supports mandatory abstention. Implemented in `ranking.py`.

A score of 90 does **not** mean "trade immediately" — it means
"exceptionally attractive under the available evidence, subject to
Portfolio Manager capital/diversification/risk constraints."

### Why not a simple weighted score (Approach A alone)?

A naive `Σ wᵢ·xᵢ` cannot express hard constraints (an unaffordable trade
must never rank #1 regardless of score) or asymmetric penalties (a fat
tail should suppress a score multiplicatively, not just subtract a
fraction of one weighted term). We use **Approach F: hybrid score + hard
gates**, layering:

1. Gates (boolean pass/fail, independent, non-compensatory)
2. A weighted arithmetic mean of 8 normalized components (the
   interpretable "Approach A" core)
3. Two **multiplicative dampeners** applied after the weighted mean:
   tail-risk severity and scenario resilience

The multiplicative dampeners are the key deviation from a pure Approach
A, and directly answer self-critique Q1/Q9 below (a bad tail or a fragile
scenario profile can meaningfully suppress an otherwise-good weighted
average, rather than being drowned out by seven other decent numbers).

We evaluated and rejected, for **Phase 1 specifically**:

- **Approach C (expected utility)** — requires a risk-aversion parameter
  we have no principled way to calibrate yet with zero live trade
  history; deferred to Phase 2+.
- **Approach G/H (pairwise/learned ranking)** — needs realized-outcome
  training data that does not exist yet for a brand-new system; would
  also be far less auditable for a ₹1,000 live-money account. See §11
  "ML Transition Criteria" below.
- **Pure Monte Carlo (§29)** — the upstream models only expose point
  estimates plus (at most) return quantiles and three cost scenarios;
  simulating a distribution we don't have honest parameters for would
  manufacture false precision. `scenarios.py` uses the three discrete
  Cost-Model scenarios directly instead.

### Net edge dominance (spec §9)

Component A (`net_edge`) is built directly from the Cost Model's
`expected_net_return_pct` — never from gross return. A gate
(`min_net_return`) and a component-level normalization both operate on
the net figure, so a big gross number with thin net economics is
penalized twice in a *complementary* (not double-counted — see §6) way:
once as an eligibility check, once as a quality-magnitude signal.

## 4. Component Definitions (Level 2)

| # | Component | Formula (informal) | Weight |
|---|---|---|---|
| A | Net Edge | `net_return_pct` (blended with return_p25 if available) | 0.25 |
| C | Risk Efficiency | `net_return / avg(downside, MAE)` | 0.20 |
| D | Regime Compatibility | `regime_compatibility` blended toward neutral by `regime_probability` | 0.10 |
| E | Prediction Reliability | `0.55·calibration + 0.35·(1−uncertainty) + 0.10·confidence` | 0.10 |
| F | Liquidity/Execution | `f(relative_order_size, spread)` | 0.08 |
| G | Capital Efficiency | `net_return_pct` component × capital-utilization multiplier | 0.12 |
| H | Holding-Time Efficiency | `net_return_pct / holding_period_days` (never annualized) | 0.10 |
| J | Cost Resilience | inverse of `cost_ratio` | 0.05 |
| I | Tail-Risk | **multiplicative** dampener, not weighted | n/a |
| — | Scenario Resilience | **multiplicative** dampener from conservative/base ratio | n/a |

Weights sum to 1.0 and are Phase-1 heuristics (documented, not yet
walk-forward-calibrated — see §8).

## 5. Normalization Methodology

Phase 1 uses **reference-range normalization**: a configured floor → 0,
a configured target → 100, linear in between, clamped outside. This is
fully deterministic and auditable with zero historical data dependency.
It is *not* the final answer — see `normalization.py` docstring and §8
below for the Phase 2 upgrade to historical-percentile / rank
normalization once enough walk-forward trade history exists to build
stable, per-strategy percentile bands (needed for cross-strategy
comparability, spec §24, so one strategy's naturally larger return scale
doesn't structurally out-score another's).

## 6. Double-Counting Audit (spec §22)

Explicitly worked through, because this is the failure mode most likely
to silently corrupt the ranking:

| Potential overlap | How it's prevented |
|---|---|
| Probability of Profit vs. Expected Value | PoP is used **only** as a hard gate (`min_probability_of_profit`) and inside `prediction_reliability`'s confidence term (10% weight) — never as a second multiplier on net_return. Net edge (A) already reflects the Cost Model's point estimate, which implicitly bakes in the return distribution's central tendency. |
| Expected downside vs. MAE | Blended (averaged) into a single `risk_exposure` figure inside component C, rather than used as two independent weighted terms. |
| Tail risk vs. downside/MAE | Handled as a **separate multiplicative dampener** (component I), explicitly not part of the weighted average, so it can't be "diluted" by, nor double-add onto, component C. |
| Liquidity vs. slippage/cost | Component F only reads `relative_order_size` and `spread` (structural execution feasibility). It explicitly does **not** re-penalize `slippage_estimate_pct` — that's assumed already folded into the Cost Model's `expected_total_cost_pct`. |
| Cost ratio vs. break-even move | `cost_ratio` (component J) measures the fraction of gross return consumed by cost; `break_even_move_pct` is used **only** in a hard gate (ensures the required move is achievable relative to the expected move), never as a scored component — avoids scoring the same friction signal twice. |
| Confidence vs. calibration | `prediction_confidence` (self-reported) is deliberately **down-weighted** (10%) relative to `model_calibration_quality` (55%) inside component E, per spec §12 — confidence alone earns little credit. |
| Regime probability vs. regime compatibility | Multiplied together (probability *scales* the compatibility signal toward neutral, not summed) so an uncertain regime call can't simultaneously get full credit for both "compatible" and "confident." |
| Capital Efficiency (G) vs. Net Edge (A) | **Caught during self-critique**: `net_profit / capital_committed` is algebraically identical to `net_return_pct`, i.e. a literal implementation of spec §16's formula would silently duplicate component A. Fixed by blending the net-return normalization with a capital-*utilization* multiplier (`capital_required / available_capital`) so G rewards preserving optionality on a ₹1,000 account, distinct from A's magnitude signal. See `components.py::capital_efficiency_score` docstring. |
| Cost Resilience (J) vs. Net Edge (A) | Acknowledged **partial, intentional** overlap (both derive from cost economics) — controlled by giving J the smallest weight (0.05) in the sum, so it nudges rather than duplicates A's influence. |

## 7. Hard Gate Logic (Level 1)

Implemented in `gates.py`, all gates are independently evaluated (not
short-circuited) so a rejection can report every reason, not just the
first (`opportunity_score` docstrings, spec §39/§63). Defaults live in
`config.py::HardGateThresholds`, all overridable per environment/strategy.
Gates cover: staleness, economic viability, minimum net return, cost
ratio ceiling, break-even feasibility, minimum PoP, downside/stop-loss
ceilings, liquidity headroom & relative order size & spread, model
calibration floor, prediction-uncertainty ceiling, prohibited regimes
(PANIC by default), max holding period, and capital feasibility.

**Capital feasibility is tracked as a separate boolean field, not folded
into `opportunity_score`** (spec §17) — an unaffordable ₹5,000 trade that
would otherwise score 94 is reported as `capital_feasibility=False`, not
silently re-scored to look mediocre.

## 8. Weight Methodology & ML Transition Criteria (spec §30)

- **Phase 1 (current)**: fixed, documented, config-versioned weights
  (this repo). No historical calibration yet — TradeX has no trade
  history to calibrate against.
- **Phase 2**: once TradeX has accumulated a walk-forward-validated
  dataset of realized outcomes (suggested minimum: **≥300 closed trades
  spanning ≥3 distinct market regimes**, to avoid regime-overfit
  weights), re-derive weights via constrained regression / logistic
  calibration against realized net return or realized-return-percentile,
  evaluated strictly out-of-sample (see `validation.py`).
- **Phase 3**: only after Phase 2's calibrated linear model has been
  shown, out-of-sample, to *not* fully explain ranking quality (e.g.
  persistent residual structure in `score_bucket_analysis` / Spearman
  correlation), consider a learning-to-rank model (LightGBM/XGBoost
  pairwise objective) trained on the labels below — never before.

## 9. Label Design for a Future Learned Model (spec §31)

Rejected `"did price go up"` as too simplistic. Candidate target:
**realized net return relative to realized risk incurred**
(`actual_net_return_pct / max(actual_mae_pct, epsilon)`), captured in
`opportunity_outcomes` (see `persistence.py`). This avoids leakage
because every feature used to predict it is fixed at `decision_time`,
and it captures the same risk-adjusted philosophy as the Phase-1 score
rather than a naive up/down label.

## 10. Leakage Prevention

- Every field on `OpportunityCandidate` must be a value known at
  `data_generated_at` ≤ `timestamp` (decision time). No component
  function is allowed a "peek" at future bars.
- `validation.py` operates strictly on **already-realized** outcomes,
  joined back by `opportunity_id` — it is never called during live
  scoring, so it cannot leak into a live decision.
- Chronological (not random) train/val/test splits with purging/embargo
  for overlapping multi-day holding periods are mandated for any future
  Phase-2/3 calibration work (not yet needed in Phase 1, which has no
  learned parameters).

## 11. Score Stability & Sensitivity (spec §26)

`validation.py::sensitivity_analysis` perturbs `expected_return_pct` by
±5% and asserts the resulting score delta stays bounded (test:
`test_score_sensitivity_is_bounded`, threshold <15 points for a 5% input
perturbation — deliberately loose in Phase 1, tightenable once we have
empirical elasticity data). `ranking.py` also implements **hysteresis**:
scores within `stability.minimum_material_score_change` (3 points) of
each other are flagged as "statistically indistinguishable" in the
ranking explanation rather than implying a meaningful ordering.

## 12. Self-Critique (spec §67) — Answered

1. **Can a high-return, extremely risky trade score too highly?**
   Mitigated by risk-efficiency normalization (return/risk ratio, not
   return alone) plus the tail-risk multiplicative dampener. See
   `test_high_return_high_risk_does_not_auto_win`.
2. **Can a high-probability, tiny-return trade score too highly?**
   PoP is a gate, not a scored magnitude driver; net edge and capital
   efficiency both scale with the (tiny) return, so the score stays low.
   See `test_high_probability_low_return_does_not_auto_win`.
3. **Can transaction costs be double-counted?** Audited in §6 above.
4. **Can liquidity be double-counted?** Audited in §6 above.
5. **Can regime compatibility be double-counted?** Audited in §6 above.
6. **Can confidence be gamed?** Self-reported confidence is capped at
   10% weight inside one component; calibration (harder to game, owned
   upstream) dominates at 55%, and a hard gate independently rejects
   candidates below a calibration floor regardless of score.
7. **Can the model use future information?** No — see §10.
8. **Can ranking overfit historical data?** N/A in Phase 1 (no learned
   parameters); Phase 2/3 transition criteria (§8) mandate walk-forward,
   never-optimize-against-test-period discipline.
9. **Can the score become unstable?** See §11.
10. **Can a ₹1,000 account realistically benefit from the top-ranked
    trade?** Capital feasibility is gated explicitly and separately
    (§7); capital-efficiency component rewards smaller-footprint trades.
11. **Can the model encourage excessive turnover?** It never forces a
    trade (abstention, §13) and holding-time efficiency rewards quicker
    capital turnover only insofar as *return per day* is genuinely
    higher, not merely because a trade is short.
12. **Can the model force a trade when none is good?** No — see §13.
13. **Can one strategy dominate rankings unfairly?** Acknowledged
    open risk in Phase 1's fixed reference-range normalization; flagged
    for the Phase-2 percentile-normalization upgrade (§5, §24).
14. **Can score 90 mean different things in different periods?** Yes,
    honestly — Phase 1 has no score-calibration study yet (spec §51);
    `score_confidence`/`score_uncertainty` fields exist precisely to
    avoid overclaiming precision, and §52/§53 validation utilities exist
    to eventually test this empirically.
15–25. Regime PANIC, liquidity collapse, model changes, single/zero
    candidates, ties, capital infeasibility despite high score, long
    holding periods, near-break-even trades: all have explicit tests in
    `tests/test_opportunity_model.py` (see test names matching each
    scenario) or explicit gate/handling logic referenced above.

## 13. Abstention & Top-K (spec §35, §36)

`rank_opportunities` returns `no_trade=True` with an explicit reason
when zero candidates pass the hard gates, or when none of the eligible
candidates clears `MINIMUM_SCORE_TO_RANK` (55, separate from and
typically stricter than any individual gate — a candidate can be
*acceptable* without being *attractive*). Top-K is a **ceiling**
(`config.top_k_max`, default 5), never a target — see
`test_fifty_candidates_do_not_force_five_results`.

## 14. Low-Capital Specialization (spec §58)

At ₹500–₹2,000, `min_relative_liquidity_headroom` and
`max_relative_order_size` gates dominate (nearly every mid/large-cap
stock is trivially liquid relative to such small orders; the binding
constraint is minimum viable lot economics and per-trade cost ratio,
since brokerage/STT can be a large fraction of a tiny position's gross
return). At ₹25,000+, cost ratio stops binding and diversification
(outside this model's scope — Portfolio Manager's job) becomes the
limiting factor. We use one common scoring framework across capital
levels with capital-aware *inputs* (gates + capital-efficiency
component), rather than separate models per capital tier, since the
underlying economics are continuous, not regime-shifted, across this
range.

## 15. Python Architecture

```
opportunity_model/
  __init__.py            public API surface
  config.py               all thresholds/weights, versioned
  schemas.py               Pydantic input/output contracts
  normalization.py        reference-range normalization (+ Phase-2 stub)
  gates.py                 Level 1 hard eligibility
  components.py             Level 2 component scores (A,C,D,E,F,G,H,I,J)
  scenarios.py              optimistic/base/conservative resilience
  scoring.py                 orchestration: evaluate_opportunity()
  ranking.py                  Level 3: rank_opportunities(), abstention
  explainability.py           explain_opportunity() human-readable report
  validation.py                ranking metrics, sensitivity, bucket analysis
  persistence.py                 SQL schema + row (de)serialization
  exceptions.py                   fail-closed error types
  testing_helpers.py               candidate builder (tests/example only)
  example.py                        worked example (spec §59/§60)
  tests/test_opportunity_model.py    31 unit tests (spec §44 scenarios)
```

Note on scope vs. spec §42's suggested file list: `expected_value.py`,
`risk_adjustment.py`, `regime_adjustment.py`, `confidence.py`,
`liquidity.py`, `capital_efficiency.py`, and `holding_period.py` were
**consolidated into `components.py`** — each remains a small, separately
testable, separately documented pure function; splitting into one file
per function added import overhead without adding clarity at this
codebase's current size. Re-split is trivial if the file grows unwieldy.

## 16. API Contracts

```python
evaluate_opportunity(candidate, config=DEFAULT_CONFIG, now=None) -> OpportunityAssessment
rank_opportunities(candidates, config=DEFAULT_CONFIG, now=None) -> RankedOpportunitySet
explain_opportunity(assessment, total_candidates=None) -> str
sensitivity_analysis(candidate, config) -> dict          # validation.py
top_k_realized_return(assessments, outcomes, k) -> dict   # validation.py
score_bucket_analysis(assessments, outcomes) -> dict       # validation.py
spearman_rank_correlation(scores, realized_returns) -> Decimal  # validation.py
```

## 17. Database Schema

See `persistence.py::SCHEMA_SQL` for full DDL:
`opportunity_evaluations`, `opportunity_score_components`,
`opportunity_scenarios`, `opportunity_gate_results`,
`opportunity_model_versions`, `opportunity_outcomes`. Every evaluation
row stores `model_version`, `config_version`, `input_hash`, and the
upstream model versions used, so any historical score is exactly
reproducible (spec §41).

## 18. Testing

`pytest -q` → **31 passed**. Covers: strong/weak trades, high-return/
high-risk, high-PoP/low-return, gross-vs-net return, regime rejection,
liquidity rejection, capital infeasibility, holding-period efficiency
and ceiling, capital-efficiency ordering, calibration gating, multi-
candidate ranking (ties, identical inputs, zero/one/fifty candidates,
top-K ceiling), stale data, missing/invalid/NaN inputs, extreme spread,
scenario disagreement, sensitivity bounds, rank stability, custom config
thresholds, and a pinned regression reference score band.

## 19. Failure Modes & Observability

`DataIncompleteError` / `InvalidInputError` are raised for structurally
broken inputs (NaN/Inf, out-of-range probabilities) and are meant to be
caught **per-candidate** by the ranking loop (`ranking.py` already does
this), converting them into a `DATA_INCOMPLETE` assessment rather than
crashing the whole cycle — the live system fails closed per-candidate,
not globally. Every gate/rejection is a named, loggable enum
(`RejectionReason`), not a free-text string, so downstream log queries
and alerting are straightforward to build (spec §63 — full structured
event names are enumerated in this docstring set but not yet wired to
an actual logger, since TradeX's logging destination isn't finalized).

## 20. What This Model Deliberately Does NOT Do

Per spec §57: it does not predict price, does not guarantee anything,
does not bypass the Cost Model/Risk Engine/Portfolio Manager, does not
use future data, does not introduce ML/GPU infrastructure in Phase 1,
and is willing to output **NO TRADE**.

## 21. Deployment Instructions

1. `pip install pydantic` (only runtime dependency).
2. Wire `OpportunityCandidate` construction to the real Return/Risk/
   Regime/Cost model outputs (replace `testing_helpers.make_candidate`,
   which is test/example-only).
3. Apply `persistence.SCHEMA_SQL` to the TradeX Supabase project.
4. Call `rank_opportunities(candidates)` once per decision cycle (after
   FILTER2.0 + feature/prediction pipeline has produced 20–50
   candidates); pass `result.top_opportunities` to the Portfolio Manager.
5. On trade close, populate `opportunity_outcomes` and periodically run
   `validation.py`'s bucket/top-K/Spearman functions against a **locked,
   walk-forward test window** before ever touching the weights in
   `config.py`.

---

*Run the worked example:* `python -m opportunity_model.example`
*Run tests:* `pytest -q` (from the `opportunity_model/` repo root)
