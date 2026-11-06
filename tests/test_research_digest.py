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

    def test_the_digest_is_a_short_counts_message(self):
        # 2026-11-03, Peter: "These reports are too long" ... "Yes to all counts".
        text = rd.format_digest(self._data())
        self.assertEqual(text.split('\n'), [
            '📋 *Daily research digest — Tue 2026-09-08*',
            '3 batches · 2 new · 1 refreshed · 1 for review · 1 requeued · 0 excluded'
            ' · Fully researched: 3,296 / 100,000',
            '⚠ Needs you: 1 for manual review — 台新金控 (2887.TW)',
        ])

    def test_no_company_lists_for_new_refreshed_requeued_or_excluded(self):
        data = self._data()
        data['excluded'] = ['Excluded Co (EXC)']
        text = rd.format_digest(data)
        for name in ('株式会社トレードワークス', '國泰金控', '玉山金控', '삼성증권', 'Excluded Co'):
            self.assertNotIn(name, text)
        self.assertNotIn('http', text)

    def test_a_company_requeued_or_refreshed_in_many_batches_counts_once(self):
        # 2026-11-05: with no working provider the loop requeued the same companies on
        # every slot (Montage Gold 11 times), and the digest counted 274 "requeued".
        stuck = ['Montage Gold (MAU.TO)', 'Incyte (INCY)']
        for seq in (544, 545, 546):
            _write_batch(self.dir, seq, self.DAY, source='maintenance', requeued=stuck,
                         summary={'updated': ['玉山金控 (2884.TW)'], 'claimed': []})
        data = self._data()
        self.assertEqual(data['requeued'],
                         ['삼성증권 (016360.KS)', 'Montage Gold (MAU.TO)', 'Incyte (INCY)'])
        self.assertEqual(data['requeued_times'], 7)
        self.assertEqual([c['name'] for c in data['refreshed']], ['玉山金控 (2884.TW)'])
        counts = rd.format_digest(data).split('\n')[1]
        self.assertIn('1 refreshed', counts)
        self.assertIn('3 requeued (7 times)', counts)
        self.assertNotIn('Montage', rd.format_digest(data))

    def test_requeued_without_repeats_shows_no_times_note(self):
        self.assertNotIn('times', rd.format_digest(self._data()))

    def test_no_review_line_when_nothing_needs_peter(self):
        data = self._data()
        data['review'] = []
        text = rd.format_digest(data)
        self.assertNotIn('Needs you', text)
        self.assertEqual(len(text.split('\n')), 2)

    def test_repeated_stall_alerts_collapse_into_one_line(self):
        data = self._data()
        data['alerts'] = ['direction list mined out', 'discovery failed for France',
                          'direction list mined out', 'discovery failed for Singapore',
                          'discovery failed for France']
        lines = rd.format_digest(data).split('\n')
        self.assertEqual(len(lines), 4)
        self.assertEqual(lines[3], 'Alerts: direction list mined out; '
                                   'discovery failed for France, Singapore')

    def test_no_alert_line_without_alerts(self):
        self.assertNotIn('Alerts', rd.format_digest(self._data()))
        self.assertEqual(rd._alert_line([]), '')
        self.assertEqual(rd._alert_line(None), '')

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


# A slice of loop.log as the loop really prints it (lines copied from scripts/loop.log,
# 2026-11-02/03), with the previous and next day around it.
_LOG = """\
  ⚠️  discovery [Japan] produced nothing — every provider failed
📋 daily digest for 2026-09-07 sent (3 message(s))
  ⚠️  direction list mined out — every listed industry is resting; falling back to the config rotation until the list is retyped
Queue empty (research), auto-falling back to maintenance mode.
=== API Batch 537 (new, gemini): 1 companies ===
  ⚠️  discovery [France] produced nothing — every provider failed
  discovery [Singapore]: nominated 0, 0 new to research
  ⚠️  discovery unproductive [Singapore] — 2 nominations returned, none usable (2 already covered) — resting until 2026-11-10
Queue empty (research), auto-falling back to maintenance mode.
  ⚠️  discovery [France] produced nothing — every provider failed
  ⚠️  discovery [Sweden] produced nothing — every provider failed
  ⚠️  discovery unproductive [France] — 2 nominations returned, none usable (2 already covered) — resting until 2026-11-10
Queue empty (maintenance) — No matching candidates.
  ⚠️  direction list mined out — every listed industry is resting; falling back to the config rotation until the list is retyped
📋 daily digest for 2026-09-08 sent (1 message(s))
  ⚠️  discovery [Brazil] produced nothing — every provider failed
"""


class AlertsFromLoopLogTests(unittest.TestCase):
    """2026-11-03, Peter: "Yes 2" — the Alerts line must appear in real digests, fed by the
    stall markers the loop already prints to loop.log."""

    DAY = CollectAndFormatTests.DAY

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.log = os.path.join(self.dir, 'loop.log')
        with open(self.log, 'w', encoding='utf-8') as f:
            f.write(_LOG)
        _write_batch(self.dir, 537, self.DAY,
                     summary={'updated': [], 'claimed': ['Co (C1)']})

    def tearDown(self):
        self.tmp.cleanup()

    def _data(self, log=None):
        return rd.collect_day(self.DAY, self.dir, os.path.join(self.dir, 'none.json'),
                              log or self.log)

    def test_the_days_stall_markers_become_one_merged_alerts_line(self):
        lines = rd.format_digest(self._data()).split('\n')
        self.assertEqual(lines[-1],
                         'Alerts: direction list mined out; discovery failed for France, '
                         'Sweden; discovery unproductive for Singapore, France; '
                         'deep research queue empty; research loop idle')
        self.assertEqual(sum(line.startswith('Alerts') for line in lines), 1)

    def test_neighbouring_days_do_not_leak_in(self):
        text = rd.format_digest(self._data())
        self.assertNotIn('Japan', text)
        self.assertNotIn('Brazil', text)

    def test_before_the_days_digest_is_sent_the_slice_runs_to_the_end(self):
        # The loop calls send_digest just before it prints this day's "digest sent" marker.
        with open(self.log, 'w', encoding='utf-8') as f:
            f.write(_LOG.split('📋 daily digest for 2026-09-08')[0])
        self.assertIn('discovery failed for France, Sweden',
                      rd.format_digest(self._data()))

    def test_without_the_previous_marker_the_slice_starts_at_the_first_batch(self):
        # The loop was down at midnight: no "digest for 09-07 sent" line. Start at batch 537.
        with open(self.log, 'w', encoding='utf-8') as f:
            f.write(_LOG.replace('📋 daily digest for 2026-09-07 sent', 'restarted'))
        alerts = self._data()['alerts']
        self.assertIn('discovery failed for France', alerts)
        self.assertNotIn('discovery failed for Japan', alerts)

    def test_a_calm_day_or_missing_log_has_no_alerts_line(self):
        with open(self.log, 'w', encoding='utf-8') as f:
            f.write('📋 daily digest for 2026-09-07 sent (1 message(s))\n'
                    '=== API Batch 537 (new, gemini): 1 companies ===\n'
                    '  discovery [Sweden]: nominated 2, 2 new to research\n')
        self.assertNotIn('Alerts', rd.format_digest(self._data()))
        missing = self._data(os.path.join(self.dir, 'no-such.log'))
        self.assertEqual(missing['alerts'], [])
        self.assertNotIn('Alerts', rd.format_digest(missing))


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
    def test_hand_picked_companies_are_still_recorded(self):
        # The digest no longer lists companies (2026-11-03), but the collected data keeps
        # the requested marker for anything that reads it.
        with tempfile.TemporaryDirectory() as tmp:
            _write_batch(tmp, 600, '2026-09-09',
                         records=[{'Company': '台積電 (2330.TW)', 'Country': 'Taiwan',
                                   'Sub-Sector': '半導體'}],
                         requested=['台積電 (2330.TW)'],
                         summary={'updated': [], 'claimed': ['台積電 (2330.TW)', 'Other (OTH)']})
            data = rd.collect_day('2026-09-09', tmp, os.path.join(tmp, 'none.json'))
            text = rd.format_digest(data)
        self.assertEqual(data['requested'], ['台積電 (2330.TW)'])
        self.assertEqual([c['requested'] for c in data['new']], [True, False])
        self.assertIn('1 batches · 2 new', text)
        self.assertNotIn('Fully researched', text)
