"""Continue an expired trial with its original ledger and new verified days."""
from dataclasses import asdict
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from execution.paper_trial.config import TrialConfig, stamp, implementation_fingerprint
from execution.paper_trial.ledger import Ledger, read_state
from execution.paper_trial.report import write_private
from execution.paper_trial.trend import LEGACY_SETTINGS
from execution.paper_trial.upgrade import upgrade
from execution.paper_trial.upstox import APPROVED_SYMBOLS
from test_paper_trial import config, market, quotes, DATES


class ContinuousTest(unittest.TestCase):
    def test_expired_portfolio_migrates_and_trades_on_verified_days(self):
        old = config(entry_policy='TREND_PORTFOLIO', initial_cash='10000', per_trade_limit=None,
                     trend_settings=LEGACY_SETTINGS, entry_times=(), exit_policy='NET_RECOVERY',
                     net_profit_inr='10')
        rows = dict(old.instruments)
        for i, symbol in enumerate(APPROVED_SYMBOLS[3:], 3):
            rows[symbol] = dict(rows['SBIN'], slot=i, key=f'NSE_EQ|INE{i:09}')
        with tempfile.TemporaryDirectory() as directory:
            db, cfg, backup = (Path(directory) / name for name in ('trial.sqlite', 'config.json', 'backup'))
            previous = Ledger(db, old)
            with previous.atomic():
                previous.s.update(completed=True, realized_gross='-417.11',
                                  sessions=dict.fromkeys(DATES, 'FINISHED'))
            previous.close()
            write_private(cfg, asdict(old))
            with patch('execution.paper_trial.upstox.expanded_instruments', return_value=rows):
                receipt = upgrade(cfg, db, backup, implementation_fingerprint(), 'a'*40,
                                  now=stamp('2026-11-18', '07:30'), continuous=True)
            self.assertEqual(receipt['cash_after'], '99582.89')
            self.assertTrue(read_state(backup / 'trial.sqlite')['completed'])
            worker = Ledger(db, TrialConfig.load(cfg))
            try:
                day = '2026-11-19'
                start = stamp(day, '09:15')
                worker.step(start, dict(market(start), calendar_valid=False))
                self.assertNotIn(day, worker.s['sessions'])
                for i in range(182):
                    at = start + 5*i
                    worker.step(at, dict(market(at), entries_paused=True), quotes(at, {'SBIN': 100+i/10}))
                self.assertFalse(worker.active() or worker.pending())
                for i in (182, 183):
                    at = start + 5*i
                    worker.step(at, market(at), quotes(at, {'SBIN': 100+i/10}))
                self.assertTrue(worker.active())
                self.assertFalse(worker.s['completed'])
                for at in (stamp(day, '15:00'), stamp(day, '15:00')+1):
                    worker.step(at, dict(market(at), entries_paused=True), quotes(at, quantity=100000))
                worker.step(stamp(day, '15:10'), {})
                self.assertEqual(worker.s['sessions'][day], 'FINISHED')
            finally:
                worker.close()
            worker = Ledger(db, TrialConfig.load(cfg))
            try:
                at = stamp('2026-11-20', '09:15')
                worker.step(at, market(at))
                self.assertEqual(worker.s['sessions']['2026-11-20'], 'RUNNING')
                self.assertTrue(worker.s['trades'])
            finally:
                worker.close()
