import sys
import os
import unittest
from decimal import Decimal
from datetime import datetime

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from cost_model.models import (
    TradeInput, Exchange, Segment, ProductType, TradeType, Side, OrderType,
    Scenario, EconomicViability, LiquiditySnapshot,
)
from cost_model.engine import CostEngine
from cost_model.exceptions import CostModelValidationError, CostModelDataIncomplete
from cost_model.scenarios import run_scenarios
from cost_model.reconciliation import reconcile
from cost_model.config import GateConfig


def make_trade(**overrides):
    defaults = dict(
        symbol="TESTSTOCK",
        exchange=Exchange.NSE,
        segment=Segment.EQUITY,
        product_type=ProductType.CNC,
        trade_type=TradeType.DELIVERY,
        side=Side.BUY,
        quantity=5,
        entry_price=Decimal("100"),
        exit_price=Decimal("105"),
        entry_timestamp=datetime(2026, 9, 10, 9, 30),
        shares_from_demat_holdings=True,
    )
    defaults.update(overrides)
    return TradeInput(**defaults)


class TestBrokerageAndStatutoryCharges(unittest.TestCase):
    def setUp(self):
        self.engine = CostEngine()

    def test_delivery_trade_1000_capital(self):
        # 1. ₹1,000 delivery trade: 10 shares @ ₹100, exit ₹101
        trade = make_trade(quantity=10, entry_price=Decimal("100"), exit_price=Decimal("101"))
        bd = self.engine.price_trade(trade)
        self.assertEqual(bd.brokerage, Decimal("0.00"))  # zero-brokerage delivery
        self.assertGreater(bd.stt, 0)
        self.assertGreater(bd.dp_charges, 0)  # sell leg from demat holdings
        self.assertEqual(bd.trade.turnover, Decimal("1000.00"))

    def test_intraday_trade_1000_capital(self):
        # 2. ₹1,000 intraday trade
        trade = make_trade(trade_type=TradeType.INTRADAY, product_type=ProductType.MIS,
                            quantity=10, entry_price=Decimal("100"), exit_price=Decimal("101"))
        bd = self.engine.price_trade(trade)
        self.assertGreater(bd.brokerage, 0)  # intraday brokerage applies
        self.assertEqual(bd.dp_charges, Decimal("0.00"))  # never charged on intraday

    def test_500_trade(self):
        # 3. ₹500 trade
        trade = make_trade(quantity=5, entry_price=Decimal("100"), exit_price=Decimal("103"))
        bd = self.engine.price_trade(trade)
        self.assertEqual(bd.trade.turnover, Decimal("500.00"))

    def test_2000_trade(self):
        # 4. ₹2,000 trade
        trade = make_trade(quantity=20, entry_price=Decimal("100"), exit_price=Decimal("102"))
        bd = self.engine.price_trade(trade)
        self.assertEqual(bd.trade.turnover, Decimal("2000.00"))

    def test_10000_trade(self):
        # 5. ₹10,000 trade
        trade = make_trade(quantity=100, entry_price=Decimal("100"), exit_price=Decimal("101"))
        bd = self.engine.price_trade(trade)
        self.assertEqual(bd.trade.turnover, Decimal("10000.00"))

    def test_one_share_trade(self):
        # 6. one-share trade
        trade = make_trade(quantity=1, entry_price=Decimal("450"), exit_price=Decimal("460"))
        bd = self.engine.price_trade(trade)
        self.assertEqual(bd.trade.quantity, 1)
        self.assertGreater(bd.total_cost, 0)

    def test_multi_share_trade(self):
        # 7. multi-share trade
        trade = make_trade(quantity=137, entry_price=Decimal("37.50"), exit_price=Decimal("38.00"))
        bd = self.engine.price_trade(trade)
        self.assertEqual(bd.trade.quantity, 137)

    def test_nse_trade(self):
        # 8. NSE trade
        trade = make_trade(exchange=Exchange.NSE)
        bd = self.engine.price_trade(trade)
        self.assertIn("NSE", bd.fee_schedule_version)

    def test_bse_trade(self):
        # 9. BSE trade
        trade = make_trade(exchange=Exchange.BSE)
        bd = self.engine.price_trade(trade)
        self.assertIn("BSE", bd.fee_schedule_version)

    def test_buy_only(self):
        # 10. buy only (no exit price -> entry-leg-only costing)
        trade = make_trade(exit_price=None)
        bd = self.engine.price_trade(trade)
        self.assertEqual(bd.dp_charges, Decimal("0.00"))  # no sell leg yet
        self.assertGreater(bd.stt, 0)  # delivery STT applies on buy too

    def test_sell_only_intraday_no_buy_stt(self):
        # 11. sell only — model this as an intraday trade priced with
        # entry==reference & exit as the actual sell, isolating sell-side STT
        trade = make_trade(trade_type=TradeType.INTRADAY, product_type=ProductType.MIS,
                            entry_price=Decimal("100"), exit_price=Decimal("99"), quantity=10)
        bd = self.engine.price_trade(trade)
        # intraday STT is sell-side only, so total stt = sell leg stt alone
        expected_sell_stt = (Decimal("990.00") * Decimal("0.025") / 100).quantize(Decimal("0.01"))
        self.assertEqual(bd.stt, expected_sell_stt)

    def test_round_trip(self):
        # 12. round trip
        trade = make_trade(entry_price=Decimal("200"), exit_price=Decimal("210"), quantity=5)
        bd = self.engine.price_trade(trade)
        self.assertGreater(bd.total_statutory_broker_cost, bd.brokerage)  # more than just brokerage

    def test_delivery_sale_with_dp_cost(self):
        # 13. delivery sale with DP cost
        trade = make_trade(shares_from_demat_holdings=True)
        bd = self.engine.price_trade(trade)
        self.assertEqual(bd.dp_charges, Decimal("15.34"))

    def test_intraday_square_off_no_dp(self):
        # 14. intraday square-off — no DP charge regardless of the flag
        trade = make_trade(trade_type=TradeType.INTRADAY, product_type=ProductType.MIS,
                            same_day_cnc_square_off=False, shares_from_demat_holdings=False)
        bd = self.engine.price_trade(trade)
        self.assertEqual(bd.dp_charges, Decimal("0.00"))

    def test_limit_order_execution_lower_slippage_than_market(self):
        # 16/17. limit vs market order execution
        trade_market = make_trade(order_type=OrderType.MARKET)
        trade_limit = make_trade(order_type=OrderType.LIMIT)
        bd_market = self.engine.price_trade(trade_market)
        bd_limit = self.engine.price_trade(trade_limit)
        self.assertLessEqual(bd_limit.slippage_cost, bd_market.slippage_cost)

    def test_high_spread_stock(self):
        # 18. high-spread stock
        liq = LiquiditySnapshot(best_bid=Decimal("99.00"), best_ask=Decimal("101.00"))
        trade = make_trade(entry_price=Decimal("100"), exit_price=None)
        trade.liquidity = liq
        bd = self.engine.price_trade(trade)
        self.assertGreater(bd.spread_cost, 0)

    def test_low_liquidity_stock_gets_impact_penalty(self):
        # 19. low-liquidity stock
        liq = LiquiditySnapshot(avg_daily_traded_value=Decimal("50000"))  # tiny ADV
        trade = make_trade(quantity=50, entry_price=Decimal("100"), exit_price=None)  # ₹5,000 order
        trade.liquidity = liq
        bd = self.engine.price_trade(trade)
        self.assertGreater(bd.market_impact_cost, 0)

    def test_highly_liquid_stock_gets_small_impact(self):
        # 20. highly liquid stock
        liq = LiquiditySnapshot(avg_daily_traded_value=Decimal("500000000000"))  # huge ADV
        trade = make_trade(quantity=10, entry_price=Decimal("100"), exit_price=None)
        trade.liquidity = liq
        bd = self.engine.price_trade(trade)
        self.assertGreaterEqual(bd.market_impact_cost, 0)
        self.assertLess(bd.market_impact_cost, Decimal("1.00"))

    def test_high_volatility_increases_impact(self):
        # 21. high-volatility environment
        base_liq = LiquiditySnapshot(avg_daily_traded_value=Decimal("1000000"),
                                      recent_volatility_pct=Decimal("1.5"))
        high_vol_liq = LiquiditySnapshot(avg_daily_traded_value=Decimal("1000000"),
                                          recent_volatility_pct=Decimal("6.0"))
        t1 = make_trade(quantity=10, entry_price=Decimal("100"), exit_price=None)
        t1.liquidity = base_liq
        t2 = make_trade(quantity=10, entry_price=Decimal("100"), exit_price=None)
        t2.liquidity = high_vol_liq
        bd1 = self.engine.price_trade(t1)
        bd2 = self.engine.price_trade(t2)
        self.assertGreater(bd2.market_impact_cost, bd1.market_impact_cost)

    def test_missing_fee_schedule_segment_raises(self):
        # 23. missing fee schedule — monkeypatch an unregistered combo
        from cost_model.fee_schedule import get_schedule
        with self.assertRaises(CostModelDataIncomplete):
            get_schedule("XSE", TradeType.DELIVERY, datetime(2026, 9, 1).date())

    def test_outdated_fee_schedule_raises(self):
        # 24. outdated fee schedule — a date far in the past should have
        # no covering schedule (our registry starts 2026-01-01)
        trade = make_trade(entry_timestamp=datetime(2020, 1, 1, 9, 30))
        with self.assertRaises(CostModelDataIncomplete):
            self.engine.price_trade(trade)

    def test_invalid_inputs_negative_quantity(self):
        # 25a. invalid inputs — negative quantity
        with self.assertRaises(CostModelValidationError):
            CostEngine().price_trade(make_trade(quantity=-5))

    def test_invalid_inputs_negative_price(self):
        # 25b. invalid inputs — negative price
        with self.assertRaises(CostModelValidationError):
            trade = make_trade()
            trade.entry_price = Decimal("-10")
            CostEngine().price_trade(trade)

    def test_invalid_inputs_zero_quantity(self):
        with self.assertRaises(CostModelValidationError):
            CostEngine().price_trade(make_trade(quantity=0))


class TestGSTAndStampDuty(unittest.TestCase):
    def setUp(self):
        self.engine = CostEngine()

    def test_stamp_duty_buy_side_only(self):
        trade = make_trade(entry_price=Decimal("100"), exit_price=Decimal("100"), quantity=10)
        bd = self.engine.price_trade(trade)
        # stamp duty should equal buy-leg-only amount: 0.015% of ₹1000
        expected = (Decimal("1000.00") * Decimal("0.015") / 100).quantize(Decimal("0.01"))
        self.assertEqual(bd.stamp_duty, expected)

    def test_gst_matches_zerodha_worked_example(self):
        # Cross-check against Zerodha's own worked intraday example:
        # ₹1,00,000 intraday buy -> brokerage ₹20 (capped), exch ₹2.97,
        # SEBI ₹0.10 -> GST ≈ 18% of (20+2.97+0.10) plus small IPFT delta.
        trade = make_trade(trade_type=TradeType.INTRADAY, product_type=ProductType.MIS,
                            entry_price=Decimal("1000"), quantity=100, exit_price=None)
        bd = self.engine.price_trade(trade)
        self.assertEqual(bd.brokerage, Decimal("20.00"))  # capped at flat ₹20
        self.assertGreater(bd.gst, Decimal("4.00"))
        self.assertLess(bd.gst, Decimal("5.00"))


class TestBreakEvenEngine(unittest.TestCase):
    def setUp(self):
        self.engine = CostEngine()

    def test_break_even_price_produces_zero_net_pnl(self):
        trade = make_trade(entry_price=Decimal("100"), quantity=5, exit_price=None)
        from cost_model.fee_schedule import get_schedule
        from cost_model import calculators as calc
        schedule = get_schedule(trade.exchange, trade.trade_type, trade.entry_timestamp.date())
        buy_total, _ = self.engine._leg_cost(schedule, trade.turnover, Side.BUY, trade)
        from cost_model.breakeven import solve_break_even_price, _net_pnl_at_exit
        be_price = solve_break_even_price(trade, buy_total, Decimal("0"))
        net_at_be = _net_pnl_at_exit(trade, be_price, buy_total)
        self.assertLess(abs(net_at_be), Decimal("0.05"))  # near-zero within rounding

    def test_break_even_move_positive_for_delivery(self):
        trade = make_trade(entry_price=Decimal("100"), quantity=5, exit_price=Decimal("105"))
        bd = self.engine.price_trade(trade)
        self.assertGreater(bd.break_even_move_pct, 0)


class TestScenarioAnalysis(unittest.TestCase):
    def test_conservative_never_cheaper_than_optimistic(self):
        engine = CostEngine()
        trade = make_trade(entry_price=Decimal("100"), quantity=10, exit_price=Decimal("102"))
        result = run_scenarios(engine, trade)
        opt = result.breakdowns[Scenario.OPTIMISTIC]
        cons = result.breakdowns[Scenario.CONSERVATIVE]
        self.assertLessEqual(opt.total_execution_cost, cons.total_execution_cost)
        self.assertGreaterEqual(opt.net_pnl, cons.net_pnl)

    def test_robustly_viable_requires_conservative_pass(self):
        engine = CostEngine()
        # tiny edge — likely optimistic-only pass, should NOT be robustly viable
        trade = make_trade(entry_price=Decimal("100"), quantity=10, exit_price=Decimal("100.30"))
        result = run_scenarios(engine, trade)
        if result.breakdowns[Scenario.OPTIMISTIC].economic_viability == EconomicViability.PASS:
            self.assertFalse(
                result.robustly_viable and
                result.breakdowns[Scenario.CONSERVATIVE].economic_viability == EconomicViability.REJECT
            )


class TestEconomicGate(unittest.TestCase):
    def test_tiny_edge_rejected_despite_ml_predicting_rise(self):
        # PART 2 requirement: model must be able to say NOT VIABLE even
        # when price is predicted to rise.
        engine = CostEngine()
        trade = make_trade(entry_price=Decimal("100"), quantity=10, exit_price=Decimal("100.05"))
        bd = engine.price_trade(trade)
        self.assertEqual(bd.economic_viability, EconomicViability.REJECT)
        self.assertTrue(len(bd.rejection_reasons) > 0)

    def test_strong_edge_passes(self):
        engine = CostEngine()
        trade = make_trade(entry_price=Decimal("100"), quantity=50, exit_price=Decimal("104"))
        bd = engine.price_trade(trade)
        self.assertEqual(bd.economic_viability, EconomicViability.PASS)

    def test_small_position_flagged_by_min_viable_position(self):
        engine = CostEngine()
        trade = make_trade(entry_price=Decimal("50"), quantity=2, exit_price=Decimal("55"))  # ₹100 position
        bd = engine.price_trade(trade)
        self.assertTrue(any("min_viable_position_value" in r for r in bd.rejection_reasons))


class TestReconciliation(unittest.TestCase):
    def test_reconciliation_report_flags_unknown_charge(self):
        engine = CostEngine()
        trade = make_trade()
        bd = engine.price_trade(trade)
        actual = {"brokerage": Decimal("0.00"), "stt": bd.stt, "some_new_fee": Decimal("3.00")}
        report = reconcile(bd, actual)
        self.assertTrue(any("some_new_fee" in n for n in report.notes))


class TestMLAndPortfolioInterfaces(unittest.TestCase):
    def test_ml_interface_never_ranks_on_gross_alone(self):
        from cost_model.ml_interface import evaluate_trade_economics, ModelPrediction
        engine = CostEngine()
        trade = make_trade(entry_price=Decimal("100"), quantity=10, exit_price=Decimal("100.05"))
        pred = ModelPrediction(predicted_return_pct=Decimal("5.0"), probability_of_profit=Decimal("0.9"))
        result = evaluate_trade_economics(engine, trade, pred)
        # gross predicted return is 5% but net-cost-adjusted score must reflect reality
        self.assertNotEqual(result.expected_net_return_pct, pred.predicted_return_pct)

    def test_portfolio_capital_efficiency(self):
        from cost_model.portfolio_interface import capital_efficiency
        engine = CostEngine()
        trade = make_trade(entry_price=Decimal("100"), quantity=10, exit_price=Decimal("104"))
        bd = engine.price_trade(trade)
        eff = capital_efficiency(bd, expected_risk_rupees=Decimal("50"))
        self.assertEqual(eff.capital_committed, Decimal("1000.00"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
