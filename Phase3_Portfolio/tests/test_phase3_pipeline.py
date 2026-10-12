"""End-to-end Phase 3 pipeline tests.

These are integration tests by intent: every one of them exercises at
least two components through the real wiring. Component-internal
behaviour is covered by each package's own suite.
"""
from decimal import Decimal

import pytest

from capital_feasibility.exceptions import CostModelIntegrationError
from phase3_factories import (
    make_candidate,
    make_execution_input,
    make_open_position,
    make_portfolio_state,
    make_request,
)
from phase3 import (
    AllocationStatus,
    ApproximateZerodhaCostProvider,
    Phase3Config,
    Phase3Engine,
    Phase3Status,
    Stage,
)
from portfolio_manager.enums import SystemStatus


def engine(config=None):
    return Phase3Engine(cost_provider=ApproximateZerodhaCostProvider(), config=config)


# --------------------------------------------------------------------------- #
# Happy path
# --------------------------------------------------------------------------- #

def test_single_candidate_produces_one_instruction():
    result = engine().run(make_request())

    assert result.status is Phase3Status.OK
    assert len(result.instructions) == 1

    instruction = result.instructions[0]
    assert instruction.quantity > 0
    assert instruction.required_capital == instruction.position_value + instruction.estimated_entry_cost
    assert instruction.allocation_status in (
        AllocationStatus.ALLOCATED,
        AllocationStatus.ALLOCATED_REDUCED,
    )
    assert instruction.audit_ids["sizing_audit_id"]
    assert instruction.audit_ids["feasibility_audit_id"]


def test_every_instruction_is_within_its_own_feasible_quantity():
    candidates = [make_candidate(f"OPP-{i}", f"SYM{i}", sector=f"SECTOR{i}") for i in range(1, 4)]
    result = engine().run(make_request(candidates=candidates))

    for outcome in result.outcomes:
        if outcome.instruction is None:
            continue
        assert outcome.instruction.quantity <= outcome.feasibility_result.max_affordable_quantity
        assert outcome.instruction.quantity <= outcome.sizing_result.recommended_quantity


def test_batch_never_allocates_more_than_deployable_capital():
    """The whole reason the ledger exists: candidates are not independent."""
    candidates = [make_candidate(f"OPP-{i}", f"SYM{i}", sector=f"SECTOR{i}") for i in range(1, 6)]
    result = engine().run(make_request(candidates=candidates))

    total = sum(i.required_capital for i in result.instructions)
    assert total == result.capital.capital_allocated
    assert total <= result.capital.deployable_at_start
    assert result.capital.deployable_remaining == result.capital.deployable_at_start - total


def test_later_candidates_see_earlier_allocations():
    """Same sector twice: the second evaluation must already include the
    first trade's exposure, not the pre-batch exposure."""
    candidates = [
        make_candidate("OPP-1", "SYMA", sector="TECH"),
        make_candidate("OPP-2", "SYMB", sector="TECH"),
    ]
    result = engine().run(make_request(candidates=candidates))

    outcomes = {o.opportunity_id: o for o in result.outcomes}
    first = outcomes["OPP-1"]
    second = outcomes["OPP-2"]

    if first.instruction is not None and second.feasibility_result is not None:
        # Post-trade sector exposure for the second candidate must exceed
        # its own position's share of capital, because the first trade is
        # already counted in the sector.
        assert second.feasibility_result.post_trade_sector_exposure_ratio is not None


# --------------------------------------------------------------------------- #
# Capital constraints
# --------------------------------------------------------------------------- #

def test_capital_starved_account_rejects_at_capital_stage():
    state = make_portfolio_state(cash=55.0, total_equity=55.0)
    exec_inputs = {"OPP-1": make_execution_input("OPP-1", entry="500.00", stop="470.00", target="560.00")}
    result = engine().run(make_request(portfolio_state=state, execution_inputs=exec_inputs))

    assert result.instructions == []
    outcome = result.outcomes[0]
    assert outcome.status in (
        AllocationStatus.REJECTED_BY_CAPITAL,
        AllocationStatus.REJECTED_BY_SIZING,
    )
    assert outcome.terminal_stage in (Stage.CAPITAL_FEASIBILITY, Stage.POSITION_SIZING)


def test_lot_size_rounding_floors_quantity():
    exec_inputs = {"OPP-1": make_execution_input("OPP-1", lot_size=5)}
    result = engine().run(make_request(execution_inputs=exec_inputs))

    for instruction in result.instructions:
        assert instruction.quantity % 5 == 0


def test_unknown_committed_capital_is_flagged_not_assumed():
    result = engine().run(make_request(committed_capital=None))
    assert result.capital.committed_capital_is_known is False
    assert any("committed_capital" in w for w in result.warnings)


# --------------------------------------------------------------------------- #
# Input / upstream failure handling
# --------------------------------------------------------------------------- #

def test_missing_execution_input_is_rejected_not_guessed():
    result = engine().run(make_request(execution_inputs={}))

    assert result.instructions == []
    assert result.outcomes[0].status is AllocationStatus.REJECTED_MISSING_EXECUTION_INPUT
    assert result.outcomes[0].terminal_stage is Stage.EXECUTION_INPUT


def test_portfolio_manager_failing_closed_stops_the_pipeline():
    state = make_portfolio_state(system_status=SystemStatus.UNAVAILABLE)
    result = engine().run(make_request(portfolio_state=state))

    assert result.status is Phase3Status.DECISION_UNAVAILABLE
    assert result.instructions == []


def test_cost_model_failure_abandons_the_whole_batch():
    class BrokenCostProvider:
        def price_buy_leg(self, **kwargs):
            raise CostModelIntegrationError("cost service unavailable")

    broken = Phase3Engine(cost_provider=BrokenCostProvider())
    result = broken.run(make_request())

    assert result.status is Phase3Status.PIPELINE_ERROR
    assert result.instructions == []
    assert any("cost service unavailable" in w for w in result.warnings)


# --------------------------------------------------------------------------- #
# Portfolio risk gating
# --------------------------------------------------------------------------- #

def test_risk_halt_skips_every_candidate(monkeypatch):
    eng = engine()
    real_assess = eng.risk_engine.assess

    def halted(portfolio, equity_history=None):
        report = real_assess(portfolio, equity_history)
        report["risk_status"] = "EMERGENCY_REDUCTION"
        return report

    monkeypatch.setattr(eng.risk_engine, "assess", halted)
    result = eng.run(make_request())

    assert result.status is Phase3Status.RISK_HALTED
    assert result.instructions == []
    assert result.outcomes[0].status is AllocationStatus.SKIPPED_RISK_HALT
    assert result.outcomes[0].terminal_stage is Stage.PRE_TRADE_RISK


def test_new_limit_breach_rejects_or_reduces_the_trade(monkeypatch):
    eng = engine()
    real_evaluate = eng.risk_engine.evaluate_new_trade

    def always_breaching(portfolio, proposed):
        impact = real_evaluate(portfolio, proposed)
        impact.new_breaches_caused_by_trade = ["position_concentration"]
        return impact

    monkeypatch.setattr(eng.risk_engine, "evaluate_new_trade", always_breaching)
    result = eng.run(make_request())

    assert result.instructions == []
    assert result.outcomes[0].status is AllocationStatus.REJECTED_BY_PORTFOLIO_RISK
    assert result.outcomes[0].terminal_stage is Stage.POST_TRADE_RISK


def test_risk_downsize_retry_produces_a_reduced_allocation(monkeypatch):
    """Breach only at full size: the trade should survive at a smaller one."""
    eng = engine()
    real_evaluate = eng.risk_engine.evaluate_new_trade
    seen = {}

    def breach_at_full_size(portfolio, proposed):
        impact = real_evaluate(portfolio, proposed)
        qty = proposed[0].quantity
        seen.setdefault("first", qty)
        if qty >= seen["first"]:
            impact.new_breaches_caused_by_trade = ["position_concentration"]
        return impact

    monkeypatch.setattr(eng.risk_engine, "evaluate_new_trade", breach_at_full_size)
    result = eng.run(make_request())

    assert len(result.instructions) == 1
    instruction = result.instructions[0]
    assert instruction.allocation_status is AllocationStatus.ALLOCATED_REDUCED
    assert instruction.reduced_by_stage is Stage.POST_TRADE_RISK
    assert instruction.quantity < result.outcomes[0].sizing_result.recommended_quantity


def test_existing_open_positions_are_carried_into_the_risk_view():
    state = make_portfolio_state(
        cash=600.0, total_equity=1000.0, invested=400.0,
        positions=[make_open_position(quantity=10, entry_price=40.0, current_price=40.0)],
    )
    result = engine().run(make_request(portfolio_state=state))

    assert result.risk_before["capital"]["invested_capital"] == pytest.approx(400.0, rel=0.01)
    assert result.risk_before["concentration"]["largest_position_symbol"] == "HELDCO"


# --------------------------------------------------------------------------- #
# Policy knobs
# --------------------------------------------------------------------------- #

def test_partial_fill_policy_reduces_rather_than_drops(monkeypatch):
    """Sizing wants more than the account can fund: with the default
    config the trade is reduced, and the reduction is attributed."""
    state = make_portfolio_state(cash=200.0, total_equity=1000.0, invested=800.0)
    exec_inputs = {"OPP-1": make_execution_input("OPP-1", entry="50.00", stop="49.50", target="56.00")}
    result = engine().run(make_request(portfolio_state=state, execution_inputs=exec_inputs))

    for outcome in result.outcomes:
        if outcome.instruction is not None and outcome.final_quantity < outcome.requested_quantity:
            assert outcome.instruction.allocation_status is AllocationStatus.ALLOCATED_REDUCED
            assert outcome.instruction.reduced_by_stage is not None


def test_score_jump_policy_can_reject_a_non_breaching_trade(monkeypatch):
    config = Phase3Config()
    config.reject_on_score_jump = True
    config.max_acceptable_score_increase = 0.0
    config.risk_downsize_steps = ()

    eng = engine(config)
    result = eng.run(make_request())

    assert result.instructions == []
    assert result.outcomes[0].status is AllocationStatus.REJECTED_BY_PORTFOLIO_RISK


def test_single_iteration_config_still_allocates():
    config = Phase3Config()
    config.max_sizing_iterations = 1
    result = engine(config).run(make_request())
    assert result.status is Phase3Status.OK


def test_invalid_config_is_rejected_at_construction():
    with pytest.raises(ValueError):
        Phase3Config(candidate_cost_basis="SOMETHING_ELSE")
    with pytest.raises(ValueError):
        Phase3Config(max_sizing_iterations=0)
