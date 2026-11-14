"""Multi-stock sizing and execution on synthetic Upstox paper quotes."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from execution.paper_trial.config import CURRENT_FEES, D, stamp
from execution.paper_trial.ledger import Ledger
from execution.paper_trial.trend import SETTINGS
from test_paper_trial import config, market, quotes, DATES


class PortfolioTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = config(initial_cash='10000', per_trade_limit=None, entry_policy='TREND_PORTFOLIO',
                          trend_settings=dict(SETTINGS), entry_times=(), exit_policy='NET_RECOVERY',
                          net_profit_inr='10', fee_model=CURRENT_FEES, recycle_entries=True)
        self.path = Path(self.tmp.name) / 'trial.sqlite'
        self.book = Ledger(self.path, self.cfg)
        self.at = stamp(DATES[0], '09:30')

    def tearDown(self):
        self.book.close()
        self.tmp.cleanup()

    def prices(self, at, i, multiple):
        prices = {'SBIN': D(100) + D(i) / 10}
        if multiple:
            prices.update(ITC=D(100) - D(i) / 10, HDFCBANK=D(200) + D(i) / 10)
        self.book.step(at, market(at), quotes(at, prices, quantity=10000))

    def warm(self, multiple):
        for i in range(61):
            self.prices(self.at - 300 + i * 5, i, multiple)

    def test_three_concurrent_symbols_and_restart_with_reserved_cash(self):
        self.warm(True)
        pending = self.book.pending()
        self.assertEqual(len({s['symbol'] for s in pending}), 3)
        self.assertEqual(sum(D(s['budget']) for s in pending), 10000)
        self.book.close()
        self.book = Ledger(self.path, self.cfg)
        self.prices(self.at + .4, 60, True)
        active = self.book.active()
        self.assertEqual(len(active), 3)
        self.assertEqual({t['direction'] for t in active}, {'LONG', 'SHORT'})
        used = sum(D(t['entry_price']) * t['quantity'] + D(t['entry_fees']) for t in active)
        self.assertTrue(9500 < used <= 10000)
        self.prices(self.at + 1, 60, True)
        self.assertFalse(self.book.pending())
        # Close one seat profitably. Other positions stay open and the closed
        # stock cannot re-enter from any seat during its cooldown.
        for offset in (2, 2.5, 3):
            at = self.at + offset
            self.book.step(at, market(at), quotes(at, {'SBIN': 109, 'ITC': 94, 'HDFCBANK': 206}))
        self.assertEqual({t['symbol'] for t in self.book.active()}, {'ITC', 'HDFCBANK'})
        self.assertFalse(self.book.pending())

    def test_single_signal_can_spend_full_available_cash_including_prior_gains(self):
        with self.book.atomic():
            self.book.s['realized_gross'] = '500'
        self.warm(False)
        self.assertEqual([D(s['budget']) for s in self.book.pending()], [10500])
        self.prices(self.at + .4, 60, False)
        trade, = self.book.active()
        self.assertGreater(D(trade['entry_price']) * trade['quantity'], 10000)
        with self.book.atomic():
            for net in ('5.01', '10.00', '10.01'):
                with patch.object(self.book, 'net_liquidation', return_value=D(net)):
                    self.book._recovery_exit(trade, self.at + 1)
                self.assertEqual(bool(trade['exit_intent']), net == '10.01')
