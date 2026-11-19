"""Dual-exchange market replays, tariff checks and continuous-ledger migration."""
from dataclasses import asdict, replace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from execution.paper_trial.config import D, EXTRA_LISTINGS, TrialConfig, ZERODHA_FEES, implementation_fingerprint, stamp
from execution.paper_trial.costs import breakdown, fee, fee_delta
from execution.paper_trial.ledger import Ledger, read_state
from execution.paper_trial.report import report, write_private
from execution.paper_trial.spread import quote_pair
from execution.paper_trial.trend import SETTINGS
from execution.paper_trial.upgrade import upgrade
from execution.paper_trial.upstox import APPROVED_SYMBOLS, DUAL_SYMBOLS, DataError, Decoder, bse_listings, regular_session
from test_paper_trial import DATES, market


def portfolio():
    isins = dict(zip(APPROVED_SYMBOLS, ('INE062A01020', 'INE154A01025', 'INE040A01034', 'INE338F01015',
                 'INE423A01024', 'INE296A01032', 'INE982J01020', 'INE095A01012', 'INE205A01025')))
    isins.update(EXTRA_LISTINGS)
    nse = {s: dict(key='NSE_EQ|' + i, tick='5' if s in EXTRA_LISTINGS else '0.01', slot=n,
                   lot_size=1, mis_eligible=True) for n, (s, i) in enumerate(isins.items())}
    bse = {s: dict(key='BSE_EQ|' + i, tick='0.01', lot_size=1, group='A', mis_eligible=True)
           for s, i in isins.items()}
    return TrialConfig(nse, DATES, initial_cash='100000', per_trade_limit=None, daily_loss_limit=None,
                       trend_settings=dict(SETTINGS), exit_policy='NET_RECOVERY', entry_policy='TREND_PORTFOLIO',
                       recycle_entries=True, entry_times=(), net_profit_inr='0', fee_model=ZERODHA_FEES,
                       continuous=True, bse_instruments=bse)


def quote(at, price, qty=100, stream='a', live=True):
    return dict(at=at, received=at, traded=at, bid=str(price), ask=str(price), last=str(price),
                bid_qty=qty, ask_qty=qty, stream_id=stream, live=live)


def pair(at, nse=100, bse=102, symbol='SBIN', **kwargs):
    return {symbol: quote(at, nse, **kwargs), 'BSE:' + symbol: quote(at, bse, **kwargs)}


class PairTest(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'trial.sqlite'
        self.cfg = portfolio()
        self.ledger = Ledger(self.path, self.cfg)
        self.at = stamp('2026-11-19', '10:15')

    def tearDown(self):
        self.ledger.close()
        self.tmp.cleanup()

    def step(self, offset, quotes=None, **state):
        at = self.at + offset
        m = {**market(at), 'stream_id': 'a', **state}
        self.ledger.step(at, m, pair(at) if quotes is None else quotes)

    def test_both_routes_and_fee_reconciliation_with_no_duplicate_fills(self):
        for i, (symbol, nse, bse, side) in enumerate((('SBIN', 100, 102, 'BUY'), ('ITC', 104, 102, 'SELL'))):
            for j in (0, .30):
                offset = 2 * i + j
                self.step(offset, pair(self.at + offset, nse, bse, symbol))
            data = report(self.path, self.at + offset)
            fills = [f for f in data['fills'] if data['trades'][i]['id'] == f['trade']]
            self.assertEqual([(f['side'], f['venue']) for f in fills],
                             [(side, 'NSE'), ('SELL' if side == 'BUY' else 'BUY', 'BSE')])
            amounts = []
            for f in fills:
                amount = fee(f['side'], D(f['price']) * f['quantity'], ZERODHA_FEES, f['venue'])
                self.assertEqual(amount, sum(map(D, f['fee_components'].values())))
                self.assertEqual(amount, D(f['fees']))
                amounts.append(amount)
            trade = data['trades'][i]
            self.assertEqual(trade['quantity'], 10)
            self.assertEqual(D(trade['net_pnl_estimate']), D(trade['gross_pnl']) - sum(amounts))
            self.assertEqual(D(trade['spread_match']['net']), D(trade['net_pnl_estimate']))
            self.assertEqual(trade['remaining'], 0)
        self.step(3, pair(self.at + 3, 104, 102, 'ITC'))
        self.assertFalse(self.ledger.pending())
        self.assertEqual(data['open_positions'], 0)
        self.assertEqual(D(data['available_cash']), D('100000') + D(data['realized_net_closed']))
        self.assertEqual(data['entry_policy'], 'CROSS_EXCHANGE')
        self.assertEqual(data['exit_policy'], 'MATCHED_PAIR')

    def test_positive_net_one_paise_passes_and_zero_after_costs_does_not(self):
        cfg = replace(self.cfg, slippage_bps='0')
        for sell, expected in (('100.10', None), ('100.11', None), ('100.12', D('.01'))):
            books = {'NSE': quote(self.at, 100, 10), 'BSE': quote(self.at, sell, 10)}
            p = quote_pair(cfg, 'SBIN', books, 'NSE', D(1000))
            self.assertEqual(p and p['net'], expected)
        books = {'NSE': quote(self.at, 20000), 'BSE': quote(self.at, '20018.30')}
        self.assertIsNone(quote_pair(self.cfg, 'BOSCHLTD', books, 'NSE', D(100000)))
        books['BSE'] = quote(self.at, 20100)
        trade = quote_pair(self.cfg, 'BOSCHLTD', books, 'NSE', D(100000))
        self.assertEqual(trade['buy_price'], D(20010))
        self.assertEqual(trade['sell_price'], D('20089.95'))
        self.assertLessEqual(trade['budget'], 100000)

    def test_only_executable_book_prices_count(self):
        q = pair(self.at, 100, 110)
        q['SBIN'].update(ask='110', last='99')
        q['BSE:SBIN'].update(bid='100', last='111')
        self.step(0, q)
        self.assertFalse(self.ledger.pending())

    def test_interleaved_protobuf_books_do_not_match_a_snapshot_or_one_updated_leg(self):
        try:
            from execution.paper_trial.MarketDataFeed_pb2 import FeedResponse
        except ImportError:
            self.skipTest('Install the paper feed requirements for the binary replay')
        decoder = Decoder(self.cfg.books('2026-11-19'))
        def frame(offset, venues):
            at = self.at + offset
            message = FeedResponse(type=1, currentTs=round(at * 1000))
            message.marketInfo.segmentStatus.update({'NSE_EQ': 2, 'BSE_EQ': 2})
            for venue in venues:
                key = venue + '_EQ|INE062A01020'
                full = message.feeds[key].fullFeed.marketFF
                price = 100 if venue == 'NSE' else 102
                full.ltpc.ltp, full.ltpc.ltt = price, message.currentTs
                full.marketLevel.bidAskQuote.add(bidP=price, bidQ=100, askP=price, askQ=100)
            quotes = decoder.decode(message.SerializeToString(), at)
            for q in quotes.values():
                q['stream_id'] = 'a'
            self.step(offset, quotes, normal=decoder.normal)
        frame(0, ('NSE', 'BSE'))
        self.assertFalse(self.ledger.pending())
        frame(.1, ('NSE',))
        frame(.2, ('BSE',))
        self.assertEqual(len(self.ledger.pending()), 1)
        frame(.5, ('BSE',))
        self.assertFalse(self.ledger.s['trades'])
        frame(.6, ('NSE',))
        self.assertEqual(len(report(self.path, self.at + .6)['fills']), 2)

    def test_depth_cash_and_seats(self):
        q = {}
        for s in DUAL_SYMBOLS[:4]:
            q.update(pair(self.at, symbol=s))
        q.update(pair(self.at, 150000, 150500, 'MRF'))
        self.step(0, q)
        self.assertEqual(len(self.ledger.pending()), 3)
        self.assertEqual(len({s['symbol'] for s in self.ledger.pending()}), 3)
        self.assertNotIn('MRF', [s['symbol'] for s in self.ledger.pending()])
        self.assertGreaterEqual(self.ledger.available(), 0)
        for book in q.values():
            book.update(at=self.at + .3, received=self.at + .3, traded=self.at + .3, ask_qty=28)
        self.step(.3, q)
        self.assertEqual([t['quantity'] for t in self.ledger.s['trades'].values()], [2, 2, 2])
        self.assertFalse(any(t['remaining'] for t in self.ledger.s['trades'].values()))

    def test_slippage_signals_cannot_fill_after_the_gap_closes(self):
        self.step(0)
        self.step(.1)
        self.assertFalse(self.ledger.s['trades'])
        self.step(.3, {'SBIN': quote(self.at + .3, 102)})
        self.assertFalse(self.ledger.s['trades'])
        self.step(.4, {'BSE:SBIN': quote(self.at + .4, 102)})
        slot, = self.ledger.s['slots'].values()
        self.assertEqual(slot['reason'], 'PAIR_NO_LONGER_EXECUTABLE')
        self.assertEqual(self.ledger.available(), 100000)
        self.assertFalse(report(self.path, self.at + .4)['fills'])

    def test_stale_skew_snapshot_disconnect_and_missing_venue(self):
        self.step(0, {'SBIN': quote(self.at, 100)})
        self.step(.8, {'BSE:SBIN': quote(self.at + .8, 102)})
        self.step(2, {'SBIN': quote(self.at + 2, 100)})
        self.step(2.1, {'BSE:SBIN': quote(self.at + 2.1, 102, live=False)})
        self.step(2.2, pair(self.at + 2.2, stream='old'))
        self.step(2.3, pair(self.at + 2.3), normal=False)
        self.step(2.4, pair(self.at + 2.4), calendar_valid=False)
        self.step(2.5, pair(self.at + 2.5), entries_paused=True)
        self.assertFalse(self.ledger.pending())
        self.step(3)
        self.assertTrue(self.ledger.pending())
        self.step(3.1, {}, connected=False)
        self.assertFalse(self.ledger.pending())
        self.assertFalse(self.ledger.s['trades'])

    def test_expiry_bad_book_and_stale_last_trade(self):
        self.step(0)
        self.step(2.01, {})
        self.assertEqual(next(iter(self.ledger.s['slots'].values()))['reason'], 'PAIR_EXPIRED')
        self.step(3, {'SBIN': quote(self.at + 3, 100)})
        self.step(3.1, {'SBIN': {'at': self.at + 3.1}})
        self.step(3.2, {'BSE:SBIN': quote(self.at + 3.2, 102)})
        self.assertFalse(self.ledger.pending())
        q = pair(self.at + 3.3)
        q['SBIN']['traded'] -= 16
        self.step(3.3, q)
        self.assertFalse(self.ledger.pending())

    def test_restart_releases_pre_disconnect_intent_and_transaction_rolls_back_both_legs(self):
        self.step(0)
        self.ledger.close()
        self.ledger = Ledger(self.path, self.cfg)
        self.step(.3, pair(self.at + .3, stream='b'), stream_id='b')
        self.assertFalse(self.ledger.s['trades'])
        original = self.ledger.event
        def reject_second(now, kind, **data):
            if data.get('leg') == 'EXIT':
                raise RuntimeError('write interrupted')
            original(now, kind, **data)
        with patch.object(self.ledger, 'event', side_effect=reject_second), self.assertRaises(RuntimeError):
            self.step(.6, pair(self.at + .6, stream='b'), stream_id='b')
        self.assertFalse(report(self.path, self.at + .6)['fills'])
        self.assertEqual(D(self.ledger.s['fees']), 0)
        self.step(.6, pair(self.at + .6, stream='b'), stream_id='b')
        self.assertEqual(len(self.ledger.s['trades']), 1)

    def test_window_is_calendar_dated_and_warmup_cutoff_are_respected(self):
        self.at = stamp('2026-11-20', '09:29')
        self.step(0)
        self.assertFalse(self.ledger.pending())
        self.at = stamp('2026-11-20', '15:00')
        self.step(-.1)
        self.assertTrue(self.ledger.pending())
        self.step(0)
        self.assertFalse(self.ledger.pending())
        self.at = stamp('2026-11-23', '09:30')
        self.step(0, {'SBIN': quote(self.at, 100)})
        self.assertEqual(self.cfg.books('2026-11-23'), self.cfg.instruments)
        self.assertEqual(self.ledger.summary(self.at)['entry_policy'], 'TREND_PORTFOLIO')
        self.assertFalse(self.ledger.s['trades'])

    def test_next_session_trend_takes_profit_below_old_ten_rupee_threshold(self):
        self.at = stamp('2026-11-23', '10:15')
        with patch('execution.paper_trial.trend.signal', return_value={'direction': 'LONG', 'strength': '1'}):
            for offset, price in ((0, 100), (.3, 100), (1, 101), (1.3, 101)):
                self.step(offset, {'SBIN': quote(self.at + offset, price)})
        trade, = report(self.path, self.at + 1.3)['trades']
        self.assertEqual(trade['entry_policy'], 'TREND_PORTFOLIO')
        self.assertEqual(trade['reason'], 'NET_PROFIT_TAKE')
        self.assertEqual(trade['fee_model'], ZERODHA_FEES)
        self.assertTrue(0 < D(trade['net_pnl_estimate']) < 10)


class InputsTest(unittest.TestCase):
    def test_default_preparation_keeps_profile_continuous_and_skips_early_bse_close(self):
        from execution.paper_trial.upstox import prepare_config
        dual = portfolio()
        days = {'2026-11-19', '2026-11-20', '2026-11-23', '2026-11-24', '2026-11-25', '2026-11-26'}
        def regular(client, day, venue='NSE'):
            return day in days and (day, venue) != ('2026-11-19', 'BSE')
        with patch('execution.paper_trial.upstox.load_instruments', return_value=[]), \
                patch('execution.paper_trial.upstox.resolve_instruments', return_value=dual.instruments) as resolve, \
                patch('execution.paper_trial.upstox.bse_listings', return_value=dual.bse_instruments), \
                patch('execution.paper_trial.upstox.regular_session', side_effect=regular):
            cfg = prepare_config(Mock(), now=stamp('2026-11-19', '08:00'))
        self.assertEqual(resolve.call_args.args[0], DUAL_SYMBOLS)
        self.assertEqual(cfg.sessions[0], '2026-11-20')
        self.assertTrue(cfg.continuous)
        self.assertEqual(cfg.fee_model, ZERODHA_FEES)
        self.assertEqual(len(cfg.books('2026-11-20')), 26)

    def test_zerodha_round_trip_tariffs(self):
        small = breakdown('BUY', '10000', ZERODHA_FEES, 'NSE')
        big = breakdown('SELL', '100000', ZERODHA_FEES, 'BSE')
        self.assertEqual(small['brokerage'], 3)
        self.assertEqual(big['brokerage'], 20)
        self.assertEqual(small['exchange'] + small['ipft'], D('.307'))
        self.assertEqual(big['exchange'], D('3.75'))
        self.assertEqual(big['sebi'], D('.1'))
        self.assertEqual(big['gst'], D('4.293'))
        self.assertEqual((small['stamp_duty'], small['stt'], big['stamp_duty'], big['stt']), (D('.3'), 0, 0, 25))
        partial = fee_delta('SELL', 0, 40000, ZERODHA_FEES, 'BSE')
        remainder = fee_delta('SELL', 40000, 100000, ZERODHA_FEES, 'BSE')
        self.assertEqual(sum(partial.values()) + sum(remainder.values()), D('53.14'))

    def test_bse_must_match_nse_isin_mis_and_group(self):
        nse = {'POWERINDIA': portfolio().instruments['POWERINDIA']}
        isin = EXTRA_LISTINGS['POWERINDIA']
        row = dict(instrument_key='BSE_EQ|' + isin, isin=isin, segment='BSE_EQ', trading_symbol='OTHER',
                   instrument_type='A', lot_size=1, tick_size=5)
        for bod, eligible in (([row, row], [row]), ([row], []), ([dict(row, instrument_type='T')], [row])):
            with patch('execution.paper_trial.upstox.load_instruments', side_effect=[bod, eligible]), self.assertRaises(DataError):
                bse_listings(nse)
        with patch('execution.paper_trial.upstox.load_instruments', side_effect=[[row], [row]]):
            self.assertEqual(bse_listings(nse)['POWERINDIA']['tick'], '0.05')

    def test_both_normal_segments_and_bse_trading_calendar(self):
        cfg = portfolio()
        day = '2026-11-20'
        at = stamp(day, '09:30')
        client = Mock()
        client.timings.side_effect = lambda day, venue: (stamp(day, '09:15'), stamp(day, '15:30')) if venue == 'NSE' else None
        self.assertTrue(regular_session(client, day))
        self.assertFalse(regular_session(client, day, 'BSE'))
        decoder = Decoder(cfg.books(day))
        def status(segments):
            decoder.normalize(dict(currentTs=str(int(at * 1000)), marketInfo=dict(segmentStatus=segments)), at)
            return decoder.normal
        self.assertFalse(status({'NSE_EQ': 'NORMAL_OPEN'}))
        self.assertTrue(status({'NSE_EQ': 'NORMAL_OPEN', 'BSE_EQ': 'NORMAL_OPEN'}))
        self.assertFalse(status({'NSE_EQ': 'NORMAL_OPEN', 'BSE_EQ': 'NORMAL_CLOSE'}))

    def test_continuous_upgrade_preserves_history_historical_fee_model_and_cash(self):
        from execution.paper_trial.config import CURRENT_FEES
        dual = portfolio()
        old = replace(dual, instruments={s: dual.instruments[s] for s in APPROVED_SYMBOLS}, bse_instruments=None,
                      fee_model=CURRENT_FEES, net_profit_inr='10')
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            cfg, db = base / 'config.json', base / 'trial.sqlite'
            write_private(cfg, asdict(old))
            book = Ledger(db, old)
            with book.atomic():
                book.s['realized_gross'], book.s['fees'] = '45', '6.25'
            book.close()
            args = cfg, db, base / 'backup', implementation_fingerprint(), 'a' * 40
            with patch('execution.paper_trial.upstox.load_instruments', return_value=[]), \
                    patch('execution.paper_trial.upstox.resolve_instruments', return_value=dual.instruments), \
                    patch('execution.paper_trial.upstox.bse_listings', return_value=dual.bse_instruments):
                with self.assertRaisesRegex(ValueError, 'before 09:10'):
                    upgrade(*args, cross_exchange_week=True, now=stamp('2026-11-19', '11:00'))
                receipt = upgrade(*args, cross_exchange_week=True, now=stamp('2026-11-19', '16:00'))
            self.assertEqual(receipt['capital_adjustment'], '0')
            self.assertEqual(receipt['cash_after'], '100038.75')
            self.assertEqual(read_state(db)['policy_history'][0]['from_config']['fee_model'], CURRENT_FEES)
            self.assertEqual(TrialConfig.load(cfg).fingerprint, dual.fingerprint)
            self.assertTrue((base / 'backup/trial.sqlite').is_file())
