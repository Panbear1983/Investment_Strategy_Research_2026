"""Maintenance slots per day: recommended from the corpus, set from the dashboard (2026-09-08)."""

import datetime
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

import maintenance_capacity as mc
from research_state import DEEP_RESEARCHED, WorkflowState

SCHEDULE = [['00:30', 'gemini'], ['01:42', 'gemini'], ['02:54', 'gemini'], ['04:06', 'claude'],
            ['05:18', 'gemini'], ['06:30', 'gemini'], ['07:42', 'gemini'], ['08:54', 'claude'],
            ['10:06', 'gemini', 'maintenance'], ['11:18', 'gemini'], ['12:30', 'gemini'],
            ['13:42', 'claude', 'maintenance'], ['14:54', 'gemini'], ['16:06', 'gemini'],
            ['17:18', 'gemini'], ['18:30', 'claude'], ['19:42', 'gemini'], ['20:54', 'gemini'],
            ['22:06', 'gemini'], ['23:18', 'claude']]


def _maint(schedule):
    return [e[0] for e in schedule if len(e) > 2 and e[2] == 'maintenance']


class RecommendationTests(unittest.TestCase):
    def test_daily_need_follows_the_cadence(self):
        need = mc.daily_screen_need({'leader': 1400, 'potential': 980, 'hidden_champion': 700,
                                     'supporting': 140, 'minor_supplier': 56})
        self.assertAlmostEqual(need, 1400 / 7 + 980 / 14 + 700 / 14 + 140 / 28 + 56 / 28)

    def test_slots_are_bounded_by_whichever_capacity_runs_out_first(self):
        tiers = {'leader': 1400, 'potential': 980, 'hidden_champion': 700}
        # 320 checks/day: 4 slots by screening (80 each); at a 50% flag rate 8 by updates.
        self.assertEqual(mc.recommend_slots(tiers, flag_rate=0.0), 4)
        self.assertEqual(mc.recommend_slots(tiers, flag_rate=0.5), 8)
        self.assertEqual(mc.recommend_slots({}, flag_rate=0.2), 1)
        self.assertEqual(mc.recommend_slots(tiers, flag_rate=5.0, max_slots=6), 6)

    def test_recommendation_reads_the_live_corpus(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = WorkflowState(os.path.join(tmp, 'wf.sqlite3'))
            for i in range(70):
                state.upsert_company({'Company': f'L{i} (L{i})', 'Country': 'Taiwan',
                                      'Industry': 'x', 'Tier': '龍頭股'}, DEEP_RESEARCHED)
            for i in range(28):
                state.upsert_company({'Company': f'S{i} (S{i})', 'Country': 'Taiwan',
                                      'Industry': 'x', 'Tier': '輔助公司'}, DEEP_RESEARCHED)
            rec = mc.recommend_from_workflow(state, {'batch_size': 20,
                                                     'maintenance_screen_group_size': 20,
                                                     'maintenance_screen_groups_per_slot': 4,
                                                     'schedule': SCHEDULE})
        self.assertEqual(rec['tier_counts'], {'leader': 70, 'supporting': 28})
        self.assertEqual(rec['daily_need'], 11)          # 70/7 + 28/28
        self.assertEqual(rec['flag_rate'], mc.DEFAULT_FLAG_RATE)   # too few screenings yet
        self.assertEqual(rec['recommended'], 1)


class SpreadTests(unittest.TestCase):
    def _providers(self, schedule):
        return [e[1] for e in schedule if len(e) > 2 and e[2] == 'maintenance']

    def test_adding_keeps_the_existing_slots_and_spreads_around_the_clock(self):
        five = mc.with_maintenance_count(SCHEDULE, 5)
        times = _maint(five)
        self.assertEqual(len(times), 5)
        self.assertTrue({'10:06', '13:42'} <= set(times))
        self.assertEqual([e[:2] for e in five], [e[:2] for e in SCHEDULE])  # times/providers intact
        idx = sorted(i for i, e in enumerate(five) if len(e) > 2)
        gaps = [b - a for a, b in zip(idx, idx[1:])] + [len(five) - idx[-1] + idx[0]]
        self.assertGreaterEqual(min(gaps), 2)          # never two maintenance slots back to back

    def test_added_slots_balance_the_providers_with_gemini_on_ties(self):
        five = mc.with_maintenance_count(SCHEDULE, 5)
        providers = self._providers(five)
        self.assertLessEqual(abs(providers.count('gemini') - providers.count('claude')), 1)
        self.assertGreaterEqual(providers.count('gemini'), providers.count('claude'))

    def test_removing_drops_the_most_crowded_slot(self):
        one = mc.with_maintenance_count(SCHEDULE, 1)
        self.assertEqual(len(_maint(one)), 1)
        self.assertEqual(_maint(mc.with_maintenance_count(SCHEDULE, 0)), [])

    def test_bounds_and_identity(self):
        self.assertEqual(_maint(mc.with_maintenance_count(SCHEDULE, 2)), ['10:06', '13:42'])
        self.assertEqual(len(_maint(mc.with_maintenance_count(SCHEDULE, 99))), 20)
        self.assertEqual(mc.with_maintenance_count([], 3), [])
        self.assertEqual(mc.maintenance_count(SCHEDULE), 2)


class DashboardWritersTests(unittest.TestCase):
    def setUp(self):
        import dashboard
        self.dashboard = dashboard
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_schedule_is_written_to_every_config_and_nothing_else_changes(self):
        paths = []
        for name in ('live.json', 'mirror.json'):
            path = os.path.join(self.tmp.name, name)
            with open(path, 'w', encoding='utf-8') as f:
                json.dump({'model': 'Gemini 3.8 Flash (High)', 'schedule': SCHEDULE,
                           'industry_themes': ['生技']}, f, ensure_ascii=False)
            paths.append(path)
        new = mc.with_maintenance_count(SCHEDULE, 4)
        self.assertEqual(self.dashboard.write_schedule(new, paths), 2)
        for path in paths:
            with open(path, encoding='utf-8') as f:
                cfg = json.load(f)
            self.assertEqual(_maint(cfg['schedule']), _maint(new))
            self.assertEqual(len(_maint(cfg['schedule'])), 4)
            self.assertTrue({'10:06', '13:42'} <= set(_maint(cfg['schedule'])))
            self.assertEqual(cfg['industry_themes'], ['生技'])
            self.assertEqual(cfg['model'], 'Gemini 3.8 Flash (High)')

    def test_todays_future_slots_flip_and_past_ones_stay_locked(self):
        state = WorkflowState(os.path.join(self.tmp.name, 'wf.sqlite3'))
        now = datetime.datetime(2026, 9, 8, 12, 0)
        state.seed_daily_slots('2026-09-08', SCHEDULE)
        target = [['10:06', 'gemini', 'maintenance'], ['13:42', 'claude', 'maintenance'],
                  ['04:06', 'claude', 'maintenance'], ['23:18', 'claude', 'maintenance']]
        target += [e for e in SCHEDULE if e[0] not in {t[0] for t in target}]
        flipped, locked = self.dashboard.apply_schedule_to_today(state, target, now=now)
        self.assertEqual((flipped, locked), (1, 1))      # 23:18 flips; 04:06 is in the past
        modes = {s['slot_time']: s['mode'] for s in state.daily_slots('2026-09-08')}
        self.assertEqual(modes['23:18'], 'maintenance')
        self.assertEqual(modes['04:06'], 'research')
        self.assertEqual(self.dashboard.apply_schedule_to_today(None, SCHEDULE), (0, 0))
