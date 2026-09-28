import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from variational_grid.comparison import Experiment
from variational_grid.cl_bz_scalper import ScalperCohort, ScalperFrame, read_snapshot, read_history, encoded
from variational_grid.models import D, GridError
from variational_grid.reset import process_reset, read_state, request_reset


class CLScalperTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        project = Path(__file__).resolve().parents[1]
        base = json.loads((project / 'config.example.json').read_text())
        base.update(session_file=str(self.root / 'session.json'), state_file=str(self.root / 'old.sqlite3'))
        (self.root / 'config.json').write_text(json.dumps(base))
        data = json.loads((project / 'cl-bz-scalper.example.json').read_text())
        data.update(base_config='config.json', output_dir='new-account')
        data['strategy']['slippage_bps'] = '0'
        self.path = self.root / 'experiment.json'
        self.path.write_text(json.dumps(data))
        self.experiment = Experiment.load(self.path)
        self.config = self.experiment.settings
        self.cohort = ScalperCohort(self.experiment).__enter__()
        self.addCleanup(self.cohort.__exit__, None)
        self.store = next(iter(self.cohort.stores.values()))
        self.ts = 1700000000.

    def frame(self, bid='99', ask='101', bz_bid='110', bz_ask='110.02', *, quotes=True, closed=None, close=None, source=None):
        now = self.ts
        markets = {s: {'state': 'closed' if closed == s else 'open', 'source_ts': now, 'closes_at': close, 'schedule_known': close is not None} for s in ('CL', 'BZ')}
        rows = {}
        for symbol, a, b in (('CL', bid, ask), ('BZ', bz_bid, bz_ask)):
            rows[symbol] = {'bid': a, 'ask': b, 'mark': str((D(a) + D(b)) / 2), 'qty': self.config.quantity_barrels, 'ts': source or now}
        return ScalperFrame(now, markets, rows if quotes else {}, data_kind='synthetic')

    def tick(self, seconds=10, **kwargs):
        self.ts += seconds
        summary = self.cohort.ingest(self.frame(**kwargs))
        return summary['scenarios'][0]

    def enter(self):
        self.tick()
        return self.tick(bid='99.98', ask='100')

    def account(self):
        return self.store.account()

    def test_first_quote_only_places_entry_then_real_fill_creates_hedge_and_tp(self):
        row = self.tick()
        self.assertEqual(row['fill_count'], 0)
        self.assertEqual(row['scalper']['active_entries'], 1)
        self.assertEqual(row['bz_barrels'], '0')
        row = self.tick(bid='99.98', ask='100')
        self.assertEqual(row['fill_count'], 2)
        self.assertEqual(row['cl_barrels'], '1')
        self.assertEqual(row['bz_barrels'], '-1')
        self.assertEqual(row['scalper']['active_take_profits'], 1)
        self.assertEqual(D(self.account()['slots'][0]['tp_price']), D('100.1'))

    def test_mark_touch_does_not_fill_buy_above_limit(self):
        self.tick()
        row = self.tick(bid='98', ask='100.01')
        self.assertEqual(row['fill_count'], 0)

    def test_cl_tp_ignores_negative_bz_and_combined_pnl(self):
        self.enter()
        row = self.tick(bid='100.1', ask='100.12', bz_bid='115', bz_ask='115.02')
        self.assertEqual(row['closed_pairs'], 1)
        self.assertEqual(row['cl_barrels'], '0')
        self.assertEqual(row['bz_barrels'], '0')
        trade = self.account()['closed_batches'][0]
        self.assertEqual(D(trade['cl_pnl']), D('.1'))
        self.assertLess(D(trade['net_pnl']), 0)
        self.assertEqual(row['fill_count'], 4)

    def test_tp_requires_bid_not_mid_or_ask(self):
        self.enter()
        row = self.tick(bid='100.099', ask='100.2')
        self.assertEqual(row['closed_pairs'], 0)

    def test_slippage_in_entry_and_tp_quote_crossing(self):
        self.config.slippage_bps = '1'
        self.tick()
        self.assertEqual(self.tick(bid='99.98', ask='100')['fill_count'], 0)
        row = self.tick(bid='99.97', ask='99.98')
        self.assertEqual(row['open_pairs'], 1)
        target = D(self.account()['slots'][0]['tp_price'])
        self.assertEqual(self.tick(bid=str(target), ask=str(target + 1))['closed_pairs'], 0)
        self.assertEqual(self.tick(bid=str(target / D('.9999')), ask=str(target + 1))['closed_pairs'], 1)

    def test_spread_does_not_gate_entry_or_require_candles(self):
        self.tick(bz_bid='90', bz_ask='90.02')
        row = self.tick(bid='99.98', ask='100', bz_bid='150', bz_ask='150.02')
        self.assertEqual(row['open_pairs'], 1)
        self.assertIsNone(self.cohort.latest()['center'])

    def test_reprice_changes_only_entry_intent_not_hedge(self):
        self.tick()
        original = self.account()['orders'][0]
        self.tick(20, bid='100', ask='102')
        order = self.account()['orders'][0]
        self.assertGreater(order['id'], original['id'])
        self.assertEqual(D(order['price']), D(101))
        self.assertEqual(self.account()['bz']['qty'], '0')

    def test_same_rfq_cannot_be_reused_even_with_changed_prices(self):
        self.tick()
        source = self.ts
        self.assertEqual(self.tick(bid='99.98', ask='100', source=source)['fill_count'], 0)

    def test_cooldown_does_not_cancel_held_tp_and_drop_waives_next_entry(self):
        row = self.enter()
        self.assertEqual(row['scalper']['cooldown_seconds'], 112.5)
        self.assertEqual(row['scalper']['active_entries'], 0)
        self.assertEqual(row['scalper']['active_take_profits'], 1)
        row = self.tick(bid='100.1', ask='100.12')
        self.assertEqual(row['scalper']['active_entries'], 1)

    def test_preclose_cancels_both_intents_before_tp_fill_and_resume_skips_first_quote(self):
        self.enter()
        self.tick(120, bid='99.99', ask='100.01')
        self.assertEqual(len(self.account()['orders']), 2)
        close = self.ts + 310
        row = self.tick(bid='101', ask='101.02', close=close)
        self.assertEqual(row['open_pairs'], 1)
        self.assertEqual(self.account()['orders'], [])
        self.assertTrue(self.account()['pair_pause']['active'])
        target = self.account()['slots'][0]['tp_price']
        self.tick(301, bid='101', ask='101.02')
        self.assertEqual(self.account()['slots'][0]['tp_price'], target)
        self.assertEqual(self.account()['closed_count'], 0)
        self.assertTrue(any(o['side'] == 'sell' for o in self.account()['orders']))
        self.assertEqual(self.tick(bid='101', ask='101.02')['closed_pairs'], 1)

    def test_closed_unknown_stale_pause_and_held_valuation_time_is_preserved(self):
        self.enter()
        source = self.ts
        self.tick(closed='BZ', quotes=False)
        row = self.tick(quotes=False)
        self.assertEqual(row['open_pairs'], 1)
        self.assertEqual(self.account()['orders'], [])
        self.assertEqual(self.cohort.latest()['market']['cl_source_ts'], source)
        self.assertTrue(self.account()['pair_pause']['active'])

    def test_clock_regression_in_quote_cannot_refresh_valuation(self):
        self.enter()
        source = self.ts
        self.tick(source=source - 1)
        self.assertTrue(self.account()['pair_pause']['active'])
        self.assertEqual(self.account()['last_quotes']['CL']['ts'], source)

    def test_insufficient_margin_never_places_unhedgeable_entry(self):
        self.config.initial_balance_usdc = '10'
        row = self.tick()
        self.assertEqual(row['scalper']['phase'], 'margin')
        self.assertEqual(self.account()['orders'], [])

    def test_max_capacity_counts_entry_and_never_places_31st_batch(self):
        self.config.wait_seconds = 1
        self.config.initial_balance_usdc = '5000'
        for index in range(30):
            self.tick(bid='99', ask='101')
            row = self.tick(bid='99.98', ask='100')
            self.assertEqual(row['open_pairs'], index + 1)
        row = self.tick(bid='99', ask='101')
        self.assertEqual(row['scalper']['occupied_batches'], 30)
        self.assertEqual(row['scalper']['active_entries'], 0)
        self.assertEqual(row['scalper']['active_take_profits'], 30)

    def test_one_quote_fills_only_one_tp_and_tps_precede_entry(self):
        self.config.wait_seconds = 1
        self.enter()
        self.tick(bid='99.98', ask='100')
        self.tick(bid='99.97', ask='99.98')
        self.assertEqual(len(self.account()['slots']), 2)
        row = self.tick(bid='101', ask='101.02')
        self.assertEqual(row['closed_pairs'], 1)
        self.assertEqual(row['open_pairs'], 1)
        self.assertEqual(row['fill_count'], 6)

    def test_crash_after_ledger_commit_replays_no_duplicate_pair(self):
        self.tick()
        self.ts += 10
        frame = self.frame(bid='99.98', ask='100')
        with patch.object(self.cohort, 'set_runtime', side_effect=RuntimeError('crash')):
            with self.assertRaises(RuntimeError):
                self.cohort.ingest(frame)
        self.cohort.recover()
        self.assertEqual(self.account()['next_fill'], 3)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM fills').fetchone()[0], 2)

    def test_journal_recovery_before_apply_produces_identical_result(self):
        self.tick()
        self.ts += 10
        frame = self.frame(bid='99.98', ask='100')
        with patch.object(self.cohort, 'apply', side_effect=RuntimeError('crash')):
            with self.assertRaises(RuntimeError):
                self.cohort.ingest(frame)
        self.cohort.recover()
        self.assertEqual(self.account()['next_fill'], 3)
        self.assertEqual(self.cohort.latest()['ts'], self.ts)

    def test_failed_second_fill_rolls_back_both_legs(self):
        from variational_grid.cl_bz_scalper import book_fill
        self.tick()
        def fail(account, symbol, *args, **kwargs):
            if symbol == 'BZ':
                raise RuntimeError('simulated failure')
            return book_fill(account, symbol, *args, **kwargs)
        with patch('variational_grid.cl_bz_scalper.book_fill', side_effect=fail):
            with self.assertRaises(RuntimeError):
                self.tick(bid='99.98', ask='100')
        self.assertEqual(self.account()['cl']['qty'], '0')
        self.assertEqual(self.account()['bz']['qty'], '0')
        self.cohort.recover()
        self.assertEqual(self.account()['cl']['qty'], '1')

    def test_pause_and_original_tp_survive_recovery(self):
        self.enter()
        self.tick(closed='CL', quotes=False)
        self.cohort.recover()
        account = self.account()
        self.assertTrue(account['pair_pause']['active'])
        self.assertEqual(account['orders'], [])
        self.assertEqual(D(account['slots'][0]['tp_price']), D('100.1'))

    def test_reset_archives_positions_and_clears_only_own_account(self):
        self.enter()
        old = read_state(self.experiment)['generation']
        request_reset(self.experiment, old)
        self.assertTrue(process_reset(self.cohort))
        self.assertEqual(self.account()['slots'], [])
        reset = read_state(self.experiment)
        self.assertNotEqual(reset['generation'], old)
        self.assertTrue((self.experiment.output / 'archives' / reset['archive_id'] / 'complete.json').exists())
        self.assertIsNone(read_snapshot(self.experiment)['summary'])

    def test_snapshot_matches_published_not_ahead_account_and_history_gaps(self):
        self.enter()
        published = self.ts
        self.tick(closed='CL', quotes=False)
        self.tick(bid='100', ask='100.02')
        history = read_history(self.experiment, '24h', self.ts)['history']['points']
        self.assertIsNone(history[-2]['pnl'][0])
        self.assertNotEqual(history[-1]['segment'], history[0]['segment'])
        account = self.account()
        account['slots'] = []
        with self.store.transaction():
            self.store.set('account', encoded(account))
        self.assertEqual(len(read_snapshot(self.experiment)['positions']), 1)
        self.assertEqual(read_history(self.experiment, '1h', published)['summary_ts'], published)

    def test_invalid_frames_never_enter_journal(self):
        for change in ('qty', 'ask', 'timestamp'):
            frame = self.frame()
            if change == 'qty':
                frame.quotes['BZ']['qty'] = '2'
            elif change == 'ask':
                frame.quotes['CL']['ask'] = '-1'
            else:
                frame.ts = float('nan')
            with self.subTest(change=change), self.assertRaises(GridError):
                self.cohort.ingest(frame)
        self.assertEqual(self.cohort.db.execute('SELECT COUNT(*) FROM frames').fetchone()[0], 0)

    def test_config_rejects_unknown_fields_output_overlap_and_invalid_tp(self):
        original = json.loads(self.path.read_text())
        for alteration in ({'take_profit_percent': '0'}, {'take_profit_percent': 'nan'}, {'max_batches': 31}, {'state_file': 'bad'}, {'typo': 1}):
            data = copy.deepcopy(original)
            data['strategy'].update(alteration)
            self.path.write_text(json.dumps(data))
            with self.subTest(alteration=alteration), self.assertRaises(GridError):
                Experiment.load(self.path)
        original['output_dir'] = '.'
        self.path.write_text(json.dumps(original))
        with self.assertRaises(GridError):
            Experiment.load(self.path)


if __name__ == '__main__':
    unittest.main()
