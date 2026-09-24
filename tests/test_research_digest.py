"""One digest a day instead of two Telegram messages per batch (2026-09-08)."""

import datetime
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

import research_digest as rd
import research_loop as rl


def _write_batch(folder, seq, day, **fields):
    batch = {'label': f'API Batch {seq} (new, gemini)', 'source': 'new', 'provider': 'gemini',
             'items': [], 'records': [], 'failed': [], 'requeued': [], 'excluded': [],
             'summary': {'updated': [], 'claimed': []}}
    batch.update(fields)
    with open(os.path.join(folder, f'batch_api_{seq:03d}_{day}.json'), 'w',
              encoding='utf-8') as f:
        json.dump(batch, f, ensure_ascii=False)


class CollectAndFormatTests(unittest.TestCase):
    DAY = '2026-09-08'

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.progress = os.path.join(self.dir, 'progress.json')
        with open(self.progress, 'w') as f:
            json.dump({'deep_research_completed': 3296, 'total_capacity': 100000}, f)
        _write_batch(self.dir, 537, self.DAY,
                     records=[{'Company': '株式会社トレードワークス (3997.T)', 'Country': 'Japan',
                               'Sub-Sector': '金融科技'}],
                     summary={'updated': [], 'claimed': ['株式会社トレードワークス (3997.T)']})
        _write_batch(self.dir, 542, self.DAY, provider='claude',
                     records=[{'Company': '國泰金控 (2882.TW)', 'Country': 'Taiwan',
                               'Sub-Sector': '金融科技'}],
                     failed=['台新金控 (2887.TW)'], requeued=['삼성증권 (016360.KS)'],
                     summary={'updated': [], 'claimed': ['國泰金控 (2882.TW)']})
        _write_batch(self.dir, 543, self.DAY, source='maintenance',
                     label='Maintenance Batch 543 (maintenance, gemini)',
                     summary={'updated': ['玉山金控 (2884.TW)'], 'claimed': []})
        _write_batch(self.dir, 536, '2026-09-07',
                     summary={'updated': [], 'claimed': ['Yesterday Co (Y1)']})

    def tearDown(self):
        self.tmp.cleanup()

    def _data(self):
        return rd.collect_day(self.DAY, self.dir, self.progress)

    def test_only_the_days_files_are_folded_in(self):
        data = self._data()
        self.assertEqual(len(data['batches']), 3)
        self.assertEqual([c['name'] for c in data['new']],
                         ['株式会社トレードワークス (3997.T)', '國泰金控 (2882.TW)'])
        self.assertEqual([c['name'] for c in data['refreshed']], ['玉山金控 (2884.TW)'])
        self.assertEqual(data['review'], ['台新金控 (2887.TW)'])
        self.assertEqual(data['requeued'], ['삼성증권 (016360.KS)'])
        self.assertEqual(data['providers'], {'gemini': 2, 'claude': 1})
        self.assertEqual(data['progress'], (3296, 100000))

    def test_companies_are_described_by_country_and_sector(self):
        text = rd.format_digest(self._data())
        self.assertIn('📋 *Daily research digest — Tue 2026-09-08*', text)
        self.assertIn('3 batches (2 Gemini, 1 Claude, 1 maintenance) · 2 new · 1 refreshed '
                      '· 1 for review · 1 requeued · 0 excluded', text)
        self.assertIn('Fully researched: 3,296 / 100,000', text)
        self.assertIn('1. 株式会社トレードワークス (3997.T) · Japan · 金融科技', text)
        self.assertIn('*Refreshed (1)*\n玉山金控 (2884.TW)', text)
        self.assertIn('⚠ *Manual review (1)*: 台新金控 (2887.TW)', text)

    def test_an_empty_day_says_so_instead_of_staying_silent(self):
        text = rd.format_digest(rd.collect_day('2026-01-01', self.dir, self.progress))
        self.assertIn('No batches ran', text)

    def test_long_digests_are_split_on_lines_and_numbered(self):
        text = '\n'.join(f'{i}. Company number {i} (T{i}) · Country · Sector'
                         for i in range(1, 200))
        pieces = rd.chunk(text, limit=1000)
        self.assertGreater(len(pieces), 1)
        for i, piece in enumerate(pieces, 1):
            self.assertLessEqual(len(piece), 1000 + 12)
            self.assertTrue(piece.endswith(f'({i}/{len(pieces)})'))
        self.assertEqual(rd.chunk('short'), ['short'])

    def test_send_delivers_every_piece_through_the_given_sender(self):
        sent = []
        pieces = rd.send_digest(self.DAY, sent.append, self.dir, self.progress)
        self.assertEqual(sent, pieces)
        self.assertEqual(len(sent), 1)


class DailyTriggerTests(unittest.TestCase):
    """The loop sends yesterday's digest once local time passes daily_digest_time."""

    def setUp(self):
        self._save = rl.save_state
        rl.save_state = lambda state: None
        # Hermetic: the trigger must never read the live batches directory in a test.
        self.tmp = tempfile.TemporaryDirectory()
        self._batches = rd.BATCHES_DIR
        rd.BATCHES_DIR = self.tmp.name
        self.sent = []
        self.cfg = {'daily_digest_time': '00:05'}

    def tearDown(self):
        rl.save_state = self._save
        rd.BATCHES_DIR = self._batches
        self.tmp.cleanup()

    def _sender(self, text):
        self.sent.append(text)

    def test_first_run_marks_the_due_day_without_sending(self):
        state = {}
        now = datetime.datetime(2026, 9, 8, 20, 0)
        self.assertIsNone(rl.maybe_send_daily_digest(self.cfg, state, now, self._sender))
        self.assertEqual(state['daily_digest_sent'], '2026-09-07')
        self.assertEqual(self.sent, [])

    def test_sends_once_after_the_time_and_never_twice(self):
        state = {'daily_digest_sent': '2026-09-07'}
        before = datetime.datetime(2026, 9, 9, 0, 2)
        self.assertIsNone(rl.maybe_send_daily_digest(self.cfg, state, before, self._sender))
        after = datetime.datetime(2026, 9, 9, 0, 6)
        self.assertEqual(rl.maybe_send_daily_digest(self.cfg, state, after, self._sender),
                         '2026-09-08')
        self.assertEqual(len(self.sent), 1)
        self.assertIn('2026-09-08', self.sent[0])
        self.assertIsNone(rl.maybe_send_daily_digest(self.cfg, state, after, self._sender))
        self.assertEqual(len(self.sent), 1)

    def test_a_missed_day_is_caught_up_on_the_next_idle_tick(self):
        state = {'daily_digest_sent': '2026-09-05'}
        now = datetime.datetime(2026, 9, 8, 12, 0)
        self.assertEqual(rl.maybe_send_daily_digest(self.cfg, state, now, self._sender),
                         '2026-09-07')

    def test_an_empty_time_disables_the_digest(self):
        state = {'daily_digest_sent': '2026-09-01'}
        self.assertIsNone(rl.maybe_send_daily_digest({'daily_digest_time': ''}, state,
                                                     datetime.datetime(2026, 9, 8, 12, 0),
                                                     self._sender))
        self.assertEqual(self.sent, [])

    def test_a_batch_no_longer_messages_on_its_own(self):
        source = open(os.path.join(os.path.dirname(__file__), '..', 'scripts',
                                   'research_loop.py'), encoding='utf-8').read()
        self.assertNotIn('started* —', source)
        self.assertNotIn('Complete!*', source)


class RequestedMarkerTests(unittest.TestCase):
    def test_hand_picked_companies_are_marked_in_the_digest(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_batch(tmp, 600, '2026-09-09',
                         records=[{'Company': '台積電 (2330.TW)', 'Country': 'Taiwan',
                                   'Sub-Sector': '半導體'}],
                         requested=['台積電 (2330.TW)'],
                         summary={'updated': [], 'claimed': ['台積電 (2330.TW)', 'Other (OTH)']})
            data = rd.collect_day('2026-09-09', tmp, os.path.join(tmp, 'none.json'))
            text = rd.format_digest(data)
        self.assertEqual(data['requested'], ['台積電 (2330.TW)'])
        self.assertIn('1. 台積電 (2330.TW) · Taiwan · 半導體 ★ requested by you', text)
        self.assertIn('2. Other (OTH)\n', text + '\n')
