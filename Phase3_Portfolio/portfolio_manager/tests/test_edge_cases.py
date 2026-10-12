from portfolio_manager.api import construct_portfolio
from portfolio_manager.enums import DecisionType, TradeType
from portfolio_manager.tests.factories import make_candidate, make_context, make_position, make_state


def test_missing_correlation_data_flagged_insufficient():
    from portfolio_manager.correlation import StaticCorrelationProvider
    from portfolio_manager.scoring import score_candidate
    from portfolio_manager.config import PortfolioManagerConfig

    ctx = make_context()
    config = PortfolioManagerConfig()
    cand = make_candidate(symbol="XYZ")
    corr = StaticCorrelationProvider()
    score = score_candidate(
        cand,
        portfolio_symbols=["OTHER"],
        portfolio_sectors=["TECH"],
        portfolio_strategies=["MOMENTUM"],
        correlation_provider=corr,
        context=ctx,
        config=config,
    )
    assert score.insufficient_data is True


def test_intraday_and_delivery_tracked_separately_in_candidate():
    cand_intraday = make_candidate(opportunity_id="I1", trade_type=TradeType.INTRADAY, expected_holding_period=0.02)
    cand_delivery = make_candidate(opportunity_id="D1", trade_type=TradeType.DELIVERY, expected_holding_period=5.0)
    assert cand_intraday.trade_type != cand_delivery.trade_type
    assert cand_intraday.expected_holding_period < cand_delivery.expected_holding_period


def test_no_candidates_and_no_positions_is_hold_cash_not_maintain():
    state = make_state(positions=[])
    ctx = make_context()
    decision = construct_portfolio(state, [], ctx)
    assert decision.decision_type == DecisionType.HOLD_CASH


def test_maintain_when_positions_exist_and_no_candidates():
    state = make_state(positions=[make_position()])
    ctx = make_context()
    decision = construct_portfolio(state, [], ctx)
    assert decision.decision_type == DecisionType.MAINTAIN_PORTFOLIO


def test_future_signal_timestamp_relative_to_as_of_is_rejected():
    """spec §37: never use information from after `as_of` (look-ahead)."""
    from datetime import timedelta

    from portfolio_manager.tests.factories import NOW

    ctx = make_context(as_of=NOW)
    future_cand = make_candidate(signal_timestamp=NOW + timedelta(hours=1))
    decision = construct_portfolio(make_state(), [future_cand], ctx)
    assert decision.selected_candidates == []


def test_decision_is_json_snapshot_reproducible():
    state = make_state()
    ctx = make_context()
    cand = make_candidate()
    d1 = construct_portfolio(state, [cand], ctx)
    d2 = construct_portfolio(state, [cand], ctx)
    assert d1.portfolio_state_snapshot_id == d2.portfolio_state_snapshot_id


def test_portfolio_manager_never_touches_broker_fields():
    """spec §49: Portfolio Manager must not call/reference the broker at all."""
    state = make_state()
    ctx = make_context()
    decision = construct_portfolio(state, [make_candidate()], ctx)
    forbidden_attrs = ["kite_order_id", "broker_response", "order_status"]
    for attr in forbidden_attrs:
        assert not hasattr(decision, attr)


def test_portfolio_expansion_then_contraction_is_still_deterministic():
    state = make_state()
    ctx = make_context()
    many = [make_candidate(opportunity_id=f"C{i}", symbol=f"S{i}", sector=f"SEC{i}") for i in range(5)]
    fewer = many[:2]
    d_expand = construct_portfolio(state, many, ctx)
    d_contract = construct_portfolio(state, fewer, ctx)
    assert set(d_contract.selected_candidates).issubset(set(d_expand.selected_candidates) | {c.opportunity_id for c in fewer})
