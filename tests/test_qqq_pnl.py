"""Batch attribution preserves the original average-cost account and history."""
from contextlib import closing
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from variational_grid.models import D
from variational_grid.qqq_comparison import QQQExperiment, read_qqq_snapshot, summary_record
from variational_grid.qqq_hedge import QQQStore, book_fill, digest, encoded, initial_account, maker_step
from variational_grid.qqq_pnl import batch_pnl
from variational_grid.qqq_scalper import CURRENT_MODEL, ScalperSettings
from test_qqq import trade
from test_qqq_execution import book
from test_qqq_scalper import config


def fill(account, slot_id, quantity, price, fee_bps='0'):
    slot = next((s for s in account['slots'] if s['slot'] == slot_id), None)
    if slot is None:
        slot = {'slot': slot_id, 'qty': '0', 'entry_price': price}
        account['slots'].append(slot)
    book_fill(account, 'qqq', quantity, price, fee_bps, account['next_fill'],
              'maker_entry' if D(quantity) > 0 else 'maker_take_profit', slot_id)
    slot['qty'] = str(D(slot['qty']) + D(quantity))


def example(fee_bps='0'):
    account = initial_account()
    fill(account, 1, '1', '740', fee_bps)
    fill(account, 2, '1', '730', fee_bps)
    fill(account, 2, '-1', '730.4', fee_bps)
    return account


class BatchPnlTests(unittest.TestCase):
    def check_total(self, account, result, mark='737'):
        leg = account['qqq']
        old_total = D(leg['realized_gross']) - D(leg['fees_usdc']) + D(leg['qty']) * (D(mark) - D(leg['average_entry']))
        self.assertEqual(result['status'], 'ready')
        self.assertLessEqual(abs(D(result['net_pnl_usdc']) + D(result['remaining_pnl_usdc']) - old_total), D('1e-18'))

    def test_profitable_low_batch_keeps_negative_average_cost_realized_and_original_total(self):
        account = example()
        before = digest(account)
        result = batch_pnl(account, '737', '0')
        self.assertEqual(D(account['qqq']['realized_gross']), D('-4.6'))
        self.assertEqual(D(result['net_pnl_usdc']), D('.4'))
        self.assertEqual(D(result['remaining_pnl_usdc']), D('-3'))
        self.check_total(account, result)
        self.assertEqual(digest(account), before)

    def test_closed_fees_include_both_sides_and_open_fees_stay_with_inventory(self):
        account = example('1')
        result = batch_pnl(account, '737', '1')
        self.assertEqual(D(result['closed_fees_usdc']), D('.14604'))
        self.assertEqual(D(result['net_pnl_usdc']), D('.25396'))
        self.assertEqual(D(result['remaining_pnl_usdc']), D('-3.074'))
        self.check_total(account, result)

    def test_partial_entries_exits_reused_slot_and_flat_reconcile_to_independent_fill_sums(self):
        account = initial_account()
        gross, fees = D(0), D(0)
        actions = [(1,'1','740'), (2,'.4','730'), (2,'-.2','730.4'),
                   (2,'.6','730'), (2,'-.8','730.5'), (2,'1','730'),
                   (1,'-1','740.5'), (2,'-1','730.6')]
        for slot, qty, price in actions:
            fill(account, slot, qty, price, '2')
            if D(qty) < 0:
                entry = D('740' if slot == 1 else '730')
                gross += -D(qty) * (D(price) - entry)
                fees += -D(qty) * (D(price) + entry) * D('.0002')
            result = batch_pnl(account, '737', '2')
            self.assertLessEqual(abs(D(result['gross_pnl_usdc']) - gross), D('1e-18'))
            self.assertEqual(D(result['closed_fees_usdc']), fees)
            self.check_total(account, result)
        self.assertEqual(D(result['remaining_pnl_usdc']), 0)
        self.assertLessEqual(abs(D(result['net_pnl_usdc']) - (gross - fees)), D('1e-18'))

    def test_pending_zero_is_valid_but_missing_cost_quantity_or_fee_is_not_zero(self):
        empty = initial_account()
        empty['slots'].append({'slot': 1, 'qty': '0'})
        self.assertEqual(D(batch_pnl(empty, '737', '0')['net_pnl_usdc']), 0)
        for change in ('slots', 'entry_price', 'qty', 'fees', 'duplicate', 'nan', 'negative', 'fee_mismatch'):
            account = example()
            if change == 'slots': del account['slots']
            elif change == 'entry_price': del account['slots'][0]['entry_price']
            elif change == 'qty': account['slots'][0]['qty'] = '2'
            elif change == 'fees': del account['qqq']['fees_usdc']
            elif change == 'duplicate': account['slots'].append(dict(account['slots'][0]))
            elif change == 'nan': account['slots'][0]['entry_price'] = 'NaN'
            elif change == 'negative': account['slots'][0]['qty'] = '-1'
            with self.subTest(change=change):
                result = batch_pnl(account, '737', '1' if change == 'fee_mismatch' else '0')
                self.assertEqual(result['status'], 'unavailable')
                self.assertNotIn('net_pnl_usdc', result)
        self.assertEqual(batch_pnl(example(), '737', None)['status'], 'unavailable')

    def test_negative_net_profit_is_never_clamped_to_zero(self):
        result = batch_pnl(example('10'), '737', '10')
        self.assertEqual(D(result['net_pnl_usdc']), D('-1.0604'))

    def test_actual_gtt_multilevel_and_dust_ioc_use_executed_prices(self):
        cfg = replace(config(), scalper=ScalperSettings(model=CURRENT_MODEL))
        for quantity in ('10', '.0008'):
            with self.subTest(quantity=quantity):
                account, _ = maker_step(initial_account(), book(100, '99.99', '100.01'), 100, cfg, True)
                account, _ = maker_step(account, book(102, trades=[trade(1, 101, '100', quantity)]), 102, cfg, True)
                if quantity == '.0008':
                    account, _ = maker_step(account, book(120), 120, cfg, False)
                    account, _ = maker_step(account, book(122), 122, cfg, False)
                    self.assertEqual(account['orders'][0]['time_in_force'], 'IOC')
                sample = book(124, bids=[['100.09','3'],['100.07','4'],['100.05','3']])
                account, fills = maker_step(account, sample, 124, cfg, False)
                expected = sum((D(f['qty']) * (D(f['price']) - 100) for f in fills), D(0))
                self.assertGreater(expected, 0)
                result = batch_pnl(account, '100.1', '0')
                self.assertEqual(D(result['gross_pnl_usdc']), expected)
                self.check_total(account, result, '100.1')


class BatchSnapshotTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        self.config = replace(config(), state_file=str(root / 'account.sqlite3'))
        self.experiment = QQQExperiment(SimpleNamespace(poll_seconds=2), root, {'scalp': self.config}, self.config.settings)
        (root / 'experiment.json').write_text(encoded(self.experiment.identity()))
        self.account = example()
        self.row = {'name':'scalp', 'total_pnl_usdc':'-2.6', 'signed_exposure_percent':'100', 'net_exposure_usdc':'737', 'qqq':{**self.account['qqq'], 'mark':'737',
                    'realized_pnl_usdc':'-4.6', 'unrealized_pnl_usdc':'2', 'total_pnl_usdc':'-2.6'}}
        self.summary = {'kind':'qqq_hedge', 'ts':100, 'market':{}, 'scenarios':[self.row]}
        self.raw_summary = summary_record(self.summary)
        with closing(sqlite3.connect(root / 'comparison.sqlite3')) as db, db:
            db.executescript('CREATE TABLE runtime(id,payload); CREATE TABLE summaries(ts PRIMARY KEY,payload);')
            db.execute('INSERT INTO summaries VALUES (100,?)', (self.raw_summary,))
        with closing(QQQStore(self.config.state_file, self.config)) as store:
            with store.transaction():
                store.db.execute('INSERT INTO ticks(ts,snapshot,account) VALUES (100,?,?)', (encoded(self.row), encoded(self.account)))
                # A writer can be ahead of the published cohort; never use this.
                newer = example()
                fill(newer, 3, '10', '700')
                store.set('account', encoded(newer))
                store.db.execute('INSERT INTO ticks(ts,snapshot,account) VALUES (102,?,?)', ('{}', encoded(newer)))

    def test_old_snapshot_uses_same_timestamp_without_writing_or_replaying_history(self):
        from variational_grid.dashboard import read_db
        queries = []
        def traced(path):
            db = read_db(path)
            db.set_trace_callback(queries.append)
            return db
        with patch('variational_grid.dashboard.read_db', side_effect=traced), \
                patch('variational_grid.qqq_history.read_history', side_effect=AssertionError('no history scan')):
            result = read_qqq_snapshot(self.experiment)['summary']['scenarios'][0]
        self.assertEqual(D(result['qqq_batch_pnl']['net_pnl_usdc']), D('.4'))
        self.assertEqual({k:v for k,v in result.items() if k != 'qqq_batch_pnl'}, self.row)
        self.assertTrue(all(not q.upper().startswith(('INSERT','UPDATE','DELETE','REPLACE')) for q in queries))
        self.assertTrue(all('LIMIT 100' in q for q in queries if 'FROM fills' in q))
        with closing(sqlite3.connect(self.experiment.output / 'comparison.sqlite3')) as db:
            self.assertEqual(db.execute('SELECT payload FROM summaries').fetchone()[0], self.raw_summary)
        with closing(QQQStore(self.config.state_file, self.config)) as store:
            saved = store.db.execute('SELECT account FROM ticks WHERE ts=100').fetchone()[0]
            self.assertEqual(saved, encoded(self.account))

    def test_pruned_checkpoint_or_missing_old_cost_preserves_original_pnl_without_guessing(self):
        with closing(sqlite3.connect(self.config.state_file)) as db, db:
            damaged = deepcopy(self.account)
            del damaged['slots'][0]['entry_price']
            db.execute('UPDATE ticks SET account=? WHERE ts=100', (encoded(damaged),))
        result = read_qqq_snapshot(self.experiment)['summary']['scenarios'][0]
        self.assertEqual(result['qqq_batch_pnl']['status'], 'unavailable')
        self.assertEqual(result['qqq'], self.row['qqq'])
        with closing(sqlite3.connect(self.config.state_file)) as db, db:
            db.execute('DELETE FROM ticks WHERE ts=100')
        result = read_qqq_snapshot(self.experiment)
        self.assertFalse(result['details_available'])
        self.assertEqual(result['summary']['scenarios'][0]['qqq_batch_pnl']['reason'], 'checkpoint_unavailable')
        self.assertEqual(result['summary']['scenarios'][0]['qqq'], self.row['qqq'])


if __name__ == '__main__':
    unittest.main()
