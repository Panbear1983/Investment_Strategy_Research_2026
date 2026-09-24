import datetime
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

import industry_focus as inf
from research_state import DEEP_RESEARCHED, RESEARCH_PENDING, WorkflowState


class ThemeCoverageTests(unittest.TestCase):
    THEMES = ['半導體', '國防', '低軌衛星', '航運']

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = WorkflowState(os.path.join(self.tmp.name, 'workflow.sqlite3'))
        self.ids = {}
        for name, industry, status in (
                ('Exact (E1)', '半導體', DEEP_RESEARCHED),
                ('Suffixed (E2)', '半導體業', DEEP_RESEARCHED),
                ('Both (B1)', '半導體設備與國防航太', DEEP_RESEARCHED),
                ('Defence (D1)', '航太與國防', DEEP_RESEARCHED),
                ('Waiting (W1)', '低軌衛星', RESEARCH_PENDING),
                ('Nothing (N1)', '寵物食品通路', DEEP_RESEARCHED)):
            self.ids[name[:name.index(' ')]] = self.state.upsert_company(
                {'Company': name, 'Country': 'Taiwan', 'Industry': industry}, status)

    def tearDown(self):
        self.tmp.cleanup()

    def _cov(self, **kw):
        kw.setdefault('themes', self.THEMES)
        return inf.theme_coverage(self.state, **kw)

    def _by_theme(self, coverage):
        return {r['theme']: r for r in coverage['themes']}

    def test_held_counts_use_substring_matching(self):
        self.assertEqual(self._by_theme(self._cov())['半導體']['held'], 3)

    def test_a_label_that_only_ever_appears_inside_a_phrase_is_still_counted(self):
        self.assertEqual(self._by_theme(self._cov())['國防']['held'], 2)

    def test_pending_is_counted_separately_from_held(self):
        row = self._by_theme(self._cov())['低軌衛星']
        self.assertEqual((row['held'], row['pending']), (0, 1))

    def test_unmapped_bucket_is_reported_not_hidden(self):
        coverage = self._cov()
        self.assertEqual(coverage['researched'], 5)
        self.assertEqual(coverage['unmapped'], 1)

    def test_multi_theme_companies_are_counted_and_declared(self):
        # 'Both' matches 半導體 and 國防, so the theme column deliberately over-sums.
        self.assertEqual(self._cov()['multi'], 1)

    def test_themes_sort_by_held_descending(self):
        self.assertEqual([r['theme'] for r in self._cov()['themes']][0], '半導體')

    def test_thin_floor_flags_starved_themes(self):
        coverage = self._cov(thin_floor=3)
        by_theme = self._by_theme(coverage)
        self.assertFalse(by_theme['半導體']['thin'])
        self.assertTrue(by_theme['國防']['thin'])

    def test_window_filters_recent_work(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        old = (now - datetime.timedelta(days=30)).isoformat()
        batch = self.state.create_batch(1, 'B1', 'research', 'new', 'gemini',
                                        [{'Company': 'Exact (E1)'}])
        with self.state.connect() as db:
            db.execute('UPDATE batch_items SET company_id=?, updated_at=? WHERE batch_id=?',
                       (self.ids['Exact'], old, batch))
        self.assertEqual(self._by_theme(self._cov(window_days=7))['半導體']['worked'], 0)
        self.assertEqual(self._by_theme(self._cov(window_days=60))['半導體']['worked'], 1)
        self.assertTrue(self._by_theme(self._cov())['半導體']['last_touched'])

    def test_empty_corpus_does_not_divide_by_zero(self):
        empty = WorkflowState(os.path.join(self.tmp.name, 'empty.sqlite3'))
        coverage = inf.theme_coverage(empty, themes=self.THEMES)
        self.assertEqual(coverage['researched'], 0)
        self.assertIn('unmapped', inf.summary_line(coverage))


class ThemeConfigTests(unittest.TestCase):
    def test_config_themes_win(self):
        self.assertEqual(inf.load_themes({'industry_themes': ['航運', ' 國防 ']}),
                         ['航運', '國防'])

    def test_missing_or_broken_config_falls_back_rather_than_emptying_the_panel(self):
        for bad in (None, {}, {'industry_themes': 'not a list'},
                    {'industry_themes': []}, {'industry_themes': ['', '  ']}):
            self.assertEqual(inf.load_themes(bad), inf.DEFAULT_THEMES)


class FocusEchoTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = WorkflowState(os.path.join(self.tmp.name, 'workflow.sqlite3'))
        self.state.upsert_company({'Company': 'Defence (D1)', 'Country': 'Taiwan',
                                   'Industry': '航太與國防'}, DEEP_RESEARCHED)

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_real_sector_reports_what_it_matches(self):
        self.assertEqual(inf.focus_match_count(self.state, '國防')['held'], 1)

    def test_a_typo_reports_zero_so_it_is_caught_when_typed(self):
        self.assertEqual(inf.focus_match_count(self.state, '國防xyz')['held'], 0)

    def test_country_narrows_the_count(self):
        self.assertEqual(inf.focus_match_count(self.state, '國防', 'Japan')['held'], 0)


class SlotFocusSummaryTests(unittest.TestCase):
    # update_daily_slot refuses past or already-processed slots, so this uses tomorrow. That
    # restriction is exactly why a standing focus exists: editing slots one by one can only
    # ever reach the future, and only for the day that has been seeded.
    DAY = (datetime.date.today() + datetime.timedelta(days=1)).isoformat()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = WorkflowState(os.path.join(self.tmp.name, 'workflow.sqlite3'))
        self.state.seed_daily_slots(self.DAY, [['08:00', 'gemini'], ['09:00', 'claude']])

    def tearDown(self):
        self.tmp.cleanup()

    def test_unfocused_day_reports_zero(self):
        self.assertEqual(inf.slot_focus_summary(self.state, self.DAY)['focused'], 0)

    def test_focused_slots_are_counted_and_named(self):
        slot = self.state.daily_slots(self.DAY)[0]
        self.state.update_daily_slot(slot['id'], industry_focus='低軌衛星')
        summary = inf.slot_focus_summary(self.state, self.DAY)
        self.assertEqual((summary['focused'], summary['total']), (1, 2))
        self.assertEqual(summary['values'], ['低軌衛星'])


class SummaryLineTests(unittest.TestCase):
    COVERAGE = {'themes': [{'theme': 'x'}] * 32, 'window_days': 7, 'researched': 100,
                'unmapped': 29, 'worked_themes': 29, 'thin_themes': 8}

    def test_standing_focus_is_shown_with_its_scope(self):
        line = inf.summary_line(self.COVERAGE,
                                {'industry': '低軌衛星', 'country': '', 'batches_remaining': None})
        self.assertIn('focus: 低軌衛星 (until cleared)', line)

    def test_a_batch_budget_is_spelled_out(self):
        line = inf.summary_line(self.COVERAGE,
                                {'industry': '低軌衛星', 'country': '', 'batches_remaining': 3})
        self.assertIn('3 batches', line)

    def test_without_a_focus_it_falls_back_to_slot_coverage(self):
        line = inf.summary_line(self.COVERAGE, None, {'focused': 0, 'total': 20})
        self.assertIn('slot focus 0/20', line)
        self.assertIn('29% unmapped', line)


class DirectionRowsTests(unittest.TestCase):
    """The direction list annotated for the dashboard: held, new, and which row is next."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = WorkflowState(os.path.join(self.tmp.name, 'workflow.sqlite3'))
        for name, industry, status in (
                ('Exact (E1)', '半導體', DEEP_RESEARCHED),
                ('Suffixed (E2)', '半導體業', DEEP_RESEARCHED),
                ('Defence (D1)', '航太與國防', DEEP_RESEARCHED),
                ('Waiting (W1)', '低軌衛星', RESEARCH_PENDING)):
            self.state.upsert_company(
                {'Company': name, 'Country': 'Taiwan', 'Industry': industry}, status)
        self.plan = [
            {'id': 1, 'industry': '半導體', 'country': '', 'enabled': 1},
            {'id': 2, 'industry': '', 'country': 'Taiwan', 'enabled': 1},
            {'id': 3, 'industry': '國防', 'country': '', 'enabled': 0},
            {'id': 4, 'industry': '保險', 'country': '', 'enabled': 1},
        ]

    def tearDown(self):
        self.tmp.cleanup()

    def test_rows_carry_held_new_and_state_with_the_thinnest_marked_next(self):
        rows = inf.direction_rows(self.state, self.plan,
                                  slices={'|半導體': {'cooldown_until': '2099-01-01',
                                                     'last_productive': '2026-09-01'}},
                                  today='2026-09-08')
        by_id = {r['id']: r for r in rows}
        self.assertEqual((by_id[1]['held'], by_id[1]['new']), (2, 2))
        self.assertEqual(by_id[1]['state'], 'resting until 2099-01-01')
        self.assertEqual(by_id[1]['last_productive'], '2026-09-01')
        self.assertEqual((by_id[2]['held'], by_id[2]['state']), (3, 'active'))
        self.assertEqual(by_id[3]['state'], 'paused')
        self.assertEqual((by_id[4]['held'], by_id[4]['state']), (0, 'next'))

    def test_a_cooldown_in_the_past_is_not_resting(self):
        rows = inf.direction_rows(self.state, self.plan[:1],
                                  slices={'|半導體': {'cooldown_until': '2026-09-01'}},
                                  today='2026-09-08')
        self.assertEqual(rows[0]['state'], 'next')

    def test_pick_prefers_fewest_held_then_typed_order(self):
        rows = [{'industry': 'A', 'held': 5, 'enabled': 1},
                {'industry': 'B', 'held': 2, 'enabled': 1},
                {'industry': 'C', 'held': 2, 'enabled': 1}]
        self.assertEqual(inf.pick_direction(rows, today='2026-09-08')['industry'], 'B')
        self.assertIsNone(inf.pick_direction([], today='2026-09-08'))

    def test_labels_line_and_typed_text(self):
        rows = inf.direction_rows(self.state, self.plan,
                                  slices={'|半導體': {'cooldown_until': '2099-01-01'}},
                                  today='2026-09-08')
        self.assertEqual([inf.direction_label(r) for r in rows],
                         ['半導體', 'free (Taiwan)', '國防', '保險'])
        self.assertEqual(inf.direction_line(rows),
                         'Direction: 半導體 2 (resting) · free (Taiwan) 3 · 保險 0 ▶')
        self.assertEqual(inf.direction_text(rows), '半導體, free (Taiwan), 國防, 保險')
        self.assertTrue(inf.direction_line([]).startswith('Direction: none set'))

    def test_parse_accepts_every_separator_and_form_and_round_trips(self):
        parsed = inf.parse_direction_text('保險, 銀行，Japan/醫療、free (Germany); free\n保險')
        self.assertEqual(parsed, [('保險', ''), ('銀行', ''), ('醫療', 'Japan'),
                                  ('', 'Germany'), ('', '')])
        self.assertEqual(inf.parse_direction_text(''), [])
        rows = [{'industry': i, 'country': c} for i, c in parsed]
        self.assertEqual(inf.parse_direction_text(inf.direction_text(rows)), parsed)
