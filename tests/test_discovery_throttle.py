"""Discovery pacing: which slice gets asked, and when discovery gives up.

Before this, `daily_empty_discovery_limit` did two jobs at once and did neither well. It was a
GLOBAL counter, so two unproductive attempts anywhere stopped discovery for the whole day — and
because Japan and South Korea sit at positions 0 and 1 of the rotation and are both picked
clean, that is exactly what happened every day, after 2 of 6 attempts. Saturation is now handled
per slice by a cooldown ledger; the global counter is only a broken-provider circuit breaker.
"""

import datetime
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

import research_loop as rl

TODAY = '2026-08-28'
ROTATION = [['Japan', ''], ['South Korea', ''], ['Germany', ''], ['USA', '生技新藥']]
CFG = {'discovery_focus_rotation': ROTATION,
       'daily_discovery_attempt_budget': 24, 'daily_empty_discovery_limit': 6,
       'discovery_slice_strikes': 2, 'discovery_slice_cooldown_days': 7,
       'discovery_slice_cooldown_cap_days': 56}


def _state(cursor=0, slices=None):
    return {'calls': {}, 'discovery': {'attempts': 0, 'empty_results': 0},
            'discovery_cursor': cursor, 'discovery_slices': slices or {}}


class NoWritesMixin:
    """save_state writes loop_state.json; these tests must never touch the live file."""

    def setUp(self):
        self._save = rl.save_state
        rl.save_state = lambda state: None

    def tearDown(self):
        rl.save_state = self._save


class RotationTests(NoWritesMixin, unittest.TestCase):
    def test_an_absent_ledger_behaves_exactly_as_before(self):
        state = _state(cursor=1)
        self.assertEqual(rl.discovery_focus(CFG, state, today=TODAY), ('Japan', ''))

    def test_an_explicit_slot_focus_still_wins(self):
        state = _state(cursor=1)
        self.assertEqual(rl.discovery_focus(CFG, state, 'Taiwan', '航運', today=TODAY),
                         ('Taiwan', '航運'))

    def test_no_rotation_configured_is_a_global_attempt(self):
        self.assertEqual(rl.discovery_focus({}, _state(), today=TODAY), ('', ''))

    def test_a_resting_slice_is_skipped(self):
        state = _state(cursor=1, slices={'Japan|': {'cooldown_until': '2026-09-30'}})
        self.assertEqual(rl.discovery_focus(CFG, state, today=TODAY), ('South Korea', ''))

    def test_the_exact_stall_that_shut_discovery_down_every_day(self):
        # Japan and South Korea both picked clean, cursor sitting right before them.
        state = _state(cursor=1, slices={'Japan|': {'cooldown_until': '2026-09-30'},
                                         'South Korea|': {'cooldown_until': '2026-09-30'}})
        self.assertEqual(rl.discovery_focus(CFG, state, today=TODAY), ('Germany', ''))
        self.assertTrue(rl.discovery_budget_available(CFG, state),
                        'discovery must still have budget, not be shut down for the day')

    def test_an_expired_cooldown_puts_a_slice_back_in_the_rotation(self):
        state = _state(cursor=1, slices={'Japan|': {'cooldown_until': '2026-08-01'}})
        self.assertEqual(rl.discovery_focus(CFG, state, today=TODAY), ('Japan', ''))

    def test_every_slice_resting_takes_the_one_that_frees_up_soonest(self):
        state = _state(cursor=1, slices={
            'Japan|': {'cooldown_until': '2026-12-01'},
            'South Korea|': {'cooldown_until': '2026-09-01'},
            'Germany|': {'cooldown_until': '2026-11-01'},
            'USA|生技新藥': {'cooldown_until': '2026-10-01'}})
        # Stalling entirely would be worse than one stale attempt.
        self.assertEqual(rl.discovery_focus(CFG, state, today=TODAY), ('South Korea', ''))

    def test_the_chosen_slice_is_recorded_so_its_result_can_be_credited(self):
        state = _state(cursor=1)
        rl.discovery_focus(CFG, state, today=TODAY)
        self.assertEqual(state['discovery_last_slice'], 'Japan|')

    def test_the_cursor_advances_past_whatever_was_skipped(self):
        """Follows the real call order, which the cursor arithmetic depends on.

        build_queue calls record_discovery_attempt() (which increments the cursor) and THEN
        discovery_focus() (which reads cursor - 1 and advances past whatever it chose). Both
        halves are needed; calling discovery_focus twice in a row without an attempt between
        legitimately re-asks the same slice.
        """
        state = _state(cursor=0, slices={'Japan|': {'cooldown_until': '2026-09-30'}})
        picks = []
        for _ in range(3):
            rl.record_discovery_attempt(state)
            picks.append(rl.discovery_focus(CFG, state, today=TODAY))
        self.assertEqual(picks, [('South Korea', ''), ('Germany', ''), ('USA', '生技新藥')])

    def test_a_full_day_of_attempts_never_re_asks_a_resting_slice(self):
        state = _state(cursor=0, slices={'Japan|': {'cooldown_until': '2026-09-30'},
                                         'South Korea|': {'cooldown_until': '2026-09-30'}})
        picks = []
        for _ in range(6):
            rl.record_discovery_attempt(state)
            picks.append(rl.discovery_focus(CFG, state, today=TODAY)[0])
        self.assertNotIn('Japan', picks)
        self.assertNotIn('South Korea', picks)
        self.assertEqual(set(picks), {'Germany', 'USA'})


class LedgerTests(NoWritesMixin, unittest.TestCase):
    def _empty_attempt(self, state, times=1, today=TODAY):
        for _ in range(times):
            rl.discovery_focus(CFG, state, today=today)
            rl.record_discovery_result(state, [], created=0, cfg=CFG, today=today)

    def test_one_disappointment_does_not_retire_a_slice(self):
        state = _state(cursor=1)
        self._empty_attempt(state)
        self.assertEqual(rl.slice_cooldown_until(state, 'Japan', ''), '')

    def test_two_disappointments_send_it_to_rest(self):
        state = _state(cursor=1)
        self._empty_attempt(state)
        state['discovery_cursor'] = 1          # ask the same slice again
        self._empty_attempt(state)
        self.assertEqual(rl.slice_cooldown_until(state, 'Japan', ''), '2026-09-04')

    def test_the_rest_period_doubles_but_is_capped(self):
        state = _state(cursor=1, slices={'Japan|': {'empty_streak': 20}})
        state['discovery_last_slice'] = 'Japan|'
        rl.record_discovery_result(state, [], created=0, cfg=CFG, today=TODAY)
        expected = (datetime.date.fromisoformat(TODAY) + datetime.timedelta(days=56)).isoformat()
        self.assertEqual(rl.slice_cooldown_until(state, 'Japan', ''), expected)

    def test_finding_something_wakes_the_slice_up_again(self):
        state = _state(cursor=1, slices={'Japan|': {'empty_streak': 3,
                                                    'cooldown_until': '2026-12-01'}})
        state['discovery_last_slice'] = 'Japan|'
        rl.record_discovery_result(state, ['x'], created=1, cfg=CFG, today=TODAY)
        self.assertEqual(rl.slice_cooldown_until(state, 'Japan', ''), '')
        self.assertEqual(state['discovery_slices']['Japan|']['empty_streak'], 0)

    def test_names_returned_but_all_already_covered_still_counts_as_empty(self):
        # The commonest dead end: 20 nominations, every one already in the corpus.
        state = _state(cursor=1)
        rl.discovery_focus(CFG, state, today=TODAY)
        rl.record_discovery_result(state, ['a'] * 20, created=0, cfg=CFG, today=TODAY)
        self.assertEqual(state['discovery_slices']['Japan|']['empty_streak'], 1)

    def test_a_result_with_no_recorded_slice_does_not_crash(self):
        state = _state()
        state.pop('discovery_last_slice', None)
        self.assertEqual(rl.record_discovery_result(state, [], created=0, cfg=CFG), 0)


class BudgetTests(NoWritesMixin, unittest.TestCase):
    def test_the_daily_ceiling_is_the_configured_one(self):
        state = _state()
        state['discovery']['attempts'] = 23
        self.assertTrue(rl.discovery_budget_available(CFG, state))
        state['discovery']['attempts'] = 24
        self.assertFalse(rl.discovery_budget_available(CFG, state))

    def test_the_circuit_breaker_still_stops_a_broken_provider(self):
        state = _state()
        state['discovery']['empty_results'] = 6
        self.assertFalse(rl.discovery_budget_available(CFG, state))

    def test_two_empties_no_longer_shut_the_day_down(self):
        state = _state()
        state['discovery']['empty_results'] = 2
        self.assertTrue(rl.discovery_budget_available(CFG, state))

    def test_one_productive_attempt_resets_the_breaker(self):
        state = _state(cursor=1)
        state['discovery']['empty_results'] = 5
        rl.discovery_focus(CFG, state, today=TODAY)
        rl.record_discovery_result(state, ['x'], created=1, cfg=CFG, today=TODAY)
        self.assertEqual(state['discovery']['empty_results'], 0)


class ThroughputTests(unittest.TestCase):
    def test_the_ceiling_now_exceeds_what_the_loop_can_research(self):
        """The point of raising it: discovery must not cap growth below research capacity."""
        import json
        cfg = json.load(open(os.path.join(os.path.dirname(__file__), '..', 'scripts',
                                          'config.json'), encoding='utf-8'))
        research_capacity = len(cfg['schedule']) * cfg['batch_size']
        discovery_ceiling = cfg['daily_discovery_attempt_budget'] * cfg['batch_size']
        self.assertGreaterEqual(discovery_ceiling, research_capacity,
                                'discovery would still be the bottleneck')


class SchemaEnforcedNominationTests(unittest.TestCase):
    """Stop asking politely for JSON and make the CLI enforce it.

    The Antigravity CLI runs with `--mode plan`; in that mode a nomination call sometimes wrote
    itself an implementation_plan.md and answered with prose about it, or tried to run a script
    and died on a denied permission prompt. Each wasted a gemini call before falling through to
    Claude. `agy --json-schema` removes the failure class — but the schema root must be an
    OBJECT: a top-level array schema is rejected outright (status ERROR, 0 tokens).
    """

    CFG = {'daily_call_budgets': {'gemini': 10, 'claude': 10}, 'backend': 'cli'}

    def setUp(self):
        self._llm = rl.llm_call
        self.seen = {}

        def fake(cfg, key, state, prompt, provider='gemini', use_search=True, json_schema=None):
            self.seen['prompt'] = prompt
            self.seen['schema'] = json_schema
            return ('{"nominations": [{"Country": "Japan", "Company": "Acme (AAA)", '
                    '"Industry": "x"}]}')
        rl.llm_call = fake

    def tearDown(self):
        rl.llm_call = self._llm

    def test_the_schema_root_is_an_object_because_an_array_root_is_rejected(self):
        self.assertEqual(rl.NOMINATION_SCHEMA['type'], 'object')
        self.assertEqual(rl.NOMINATION_SCHEMA['properties']['nominations']['type'], 'array')

    def test_gemini_gets_the_schema_and_is_told_to_wrap(self):
        rl.nominate_new(self.CFG, None, {}, 5, covered=[], provider='gemini')
        self.assertIs(self.seen['schema'], rl.NOMINATION_SCHEMA)
        self.assertIn('nominations 陣列', self.seen['prompt'])

    def test_other_providers_are_still_asked_for_a_bare_array_in_words(self):
        rl.nominate_new(self.CFG, None, {}, 5, covered=[], provider='claude')
        self.assertIsNone(self.seen['schema'])
        self.assertIn('僅輸出 JSON 陣列', self.seen['prompt'])

    def test_a_wrapped_response_is_unwrapped_into_nominations(self):
        items = rl.nominate_new(self.CFG, None, {}, 5, covered=[], provider='gemini')
        self.assertEqual([i['Company'] for i in items], ['Acme (AAA)'])

    def test_a_bare_array_still_works_for_the_other_providers(self):
        rl.llm_call = lambda *a, **k: '[{"Country":"Japan","Company":"B (BBB)","Industry":"x"}]'
        items = rl.nominate_new(self.CFG, None, {}, 5, covered=[], provider='claude')
        self.assertEqual([i['Company'] for i in items], ['B (BBB)'])

    def test_an_object_without_nominations_is_a_failure_not_a_silent_empty(self):
        rl.llm_call = lambda *a, **k: '{"something_else": []}'
        with self.assertRaises(rl.NominationFailed):
            rl.nominate_new(self.CFG, None, {}, 5, covered=[], provider='gemini')


class CliEnvelopeSalvageTests(unittest.TestCase):
    """structured_output sometimes comes back absent while the answer is still in `response`.

    Measured at roughly 4 failures in 81 discovery calls. Treating those as a failed provider
    wasted a gemini call each and fell through to Claude, so the response is salvaged.
    """

    def test_the_response_is_used_when_structured_output_is_missing(self):
        line = ('{"status":"SUCCESS","response":"{\\"nominations\\":[{\\"Company\\":\\"A\\"}],'
                '\\"toolAction\\":\\"x\\"}","structured_output":null}')
        self.assertEqual(rl._cli_envelope(line),
                         {'nominations': [{'Company': 'A'}], 'toolAction': 'x'})

    def test_structured_output_still_wins_when_present(self):
        line = ('{"status":"SUCCESS","response":"{\\"nominations\\":[]}",'
                '"structured_output":{"nominations":[{"Company":"B"}]}}')
        self.assertEqual(rl._cli_envelope(line), {'nominations': [{'Company': 'B'}]})

    def test_an_error_status_is_never_salvaged(self):
        line = '{"status":"ERROR","response":"{\\"nominations\\":[{\\"Company\\":\\"A\\"}]}"}'
        self.assertIsNone(rl._cli_envelope(line))

    def test_prose_in_the_response_yields_nothing(self):
        line = '{"status":"SUCCESS","response":"我已為您建立了一份計畫","structured_output":null}'
        self.assertIsNone(rl._cli_envelope(line))

    def test_the_diagnostic_names_what_was_actually_wrong(self):
        line = '{"status":"SUCCESS","response":"prose here","structured_output":null}'
        message = rl._cli_schema_error(line)
        # The old message printed the tail of the envelope, which was the echoed schema every
        # time — identical for every failure and no help at all.
        self.assertIn('status=SUCCESS', message)
        self.assertIn('prose here', message)
        self.assertNotIn('"type": "object"', message)

    def test_the_diagnostic_survives_output_that_is_not_json(self):
        self.assertIn('not a JSON envelope', rl._cli_schema_error('total nonsense'))


class CliEnvelopeTests(unittest.TestCase):
    def test_structured_output_is_extracted(self):
        line = ('{"status":"SUCCESS","response":"...","structured_output":'
                '{"nominations":[{"Company":"A"}]}}')
        self.assertEqual(rl._cli_envelope(line), {'nominations': [{'Company': 'A'}]})

    def test_an_error_envelope_yields_nothing(self):
        self.assertIsNone(rl._cli_envelope('{"status":"ERROR","response":""}'))

    def test_prose_instead_of_an_envelope_yields_nothing(self):
        # This is the exact failure the schema exists to remove; it must not be mistaken
        # for an answer.
        self.assertIsNone(rl._cli_envelope('我已為您建立了一份詳細的 Nomination Plan'))

    def test_empty_output_yields_nothing(self):
        self.assertIsNone(rl._cli_envelope(''))

    def test_only_the_last_line_matters(self):
        self.assertEqual(rl._cli_envelope('warning: something\n{"structured_output":{"a":1}}'),
                         {'a': 1})


class NoLiveWritesTests(unittest.TestCase):
    """Choosing a slice must not touch the disk.

    Making discovery_focus() persist state turned it into a disk write, and an existing test
    that exercised it then overwrote the live loop_state.json — resetting the day's call
    counters and restarting the batch numbering. Persistence belongs to the two bookkeeping
    calls that bracket the attempt.
    """

    def test_discovery_focus_does_not_save(self):
        calls = []
        original = rl.save_state
        rl.save_state = lambda state: calls.append(state)
        try:
            state = _state(cursor=1)
            rl.discovery_focus(CFG, state, today=TODAY)
        finally:
            rl.save_state = original
        self.assertEqual(calls, [], 'discovery_focus must not persist state')

    def test_the_ledger_survives_a_reload(self):
        import json
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'loop_state.json')
            original = rl.STATE_PATH
            rl.STATE_PATH = path
            try:
                rl.save_state({'date': datetime.date.today().isoformat(),
                               'calls': {'gemini': 5}, 'batch_seq': 42,
                               'discovery_cursor': 7,
                               'discovery_slices': {'Japan|': {'cooldown_until': '2026-12-01'}},
                               'discovery_last_slice': 'Japan|'})
                loaded = rl.load_state()
            finally:
                rl.STATE_PATH = original
        self.assertEqual(loaded['batch_seq'], 42)
        self.assertEqual(loaded['discovery_cursor'], 7)
        self.assertEqual(rl.slice_cooldown_until(loaded, 'Japan', ''), '2026-12-01')
        self.assertEqual(loaded['discovery_last_slice'], 'Japan|')

    def test_a_state_file_without_a_ledger_still_loads(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'loop_state.json')
            original = rl.STATE_PATH
            rl.STATE_PATH = path
            try:
                rl.save_state({'date': '2020-01-01', 'calls': {'gemini': 1}, 'batch_seq': 9})
                loaded = rl.load_state()
            finally:
                rl.STATE_PATH = original
        self.assertEqual(loaded['batch_seq'], 9)
        self.assertEqual(loaded['discovery_slices'], {})


class DirectionListTests(NoWritesMixin, unittest.TestCase):
    """The typed direction list outranks the config rotation: thinnest row first."""

    PLAN = [{'industry': '醫療', 'country': '', 'held': 138, 'enabled': 1},
            {'industry': '化工', 'country': '', 'held': 6, 'enabled': 1},
            {'industry': '保險', 'country': '', 'held': 0, 'enabled': 1}]

    def test_the_never_researched_name_goes_first(self):
        self.assertEqual(rl.discovery_focus(CFG, _state(cursor=1), today=TODAY, plan=self.PLAN),
                         ('', '保險'))

    def test_then_the_thinnest_remaining_row(self):
        plan = [dict(r) for r in self.PLAN if r['industry'] != '保險']
        self.assertEqual(rl.discovery_focus(CFG, _state(1), today=TODAY, plan=plan), ('', '化工'))

    def test_ties_go_to_the_row_typed_earlier(self):
        plan = [{'industry': 'A', 'country': '', 'held': 3, 'enabled': 1},
                {'industry': 'B', 'country': '', 'held': 3, 'enabled': 1}]
        self.assertEqual(rl.discovery_focus(CFG, _state(1), today=TODAY, plan=plan), ('', 'A'))

    def test_a_resting_row_is_skipped_and_a_paused_row_ignored(self):
        plan = [dict(r) for r in self.PLAN]
        plan[1]['enabled'] = 0                       # 化工 paused
        state = _state(1, slices={'|保險': {'cooldown_until': '2099-01-01'}})
        self.assertEqual(rl.discovery_focus(CFG, state, today=TODAY, plan=plan), ('', '醫療'))

    def test_a_fully_resting_list_falls_back_to_the_config_rotation(self):
        state = _state(1, slices={f"|{r['industry']}": {'cooldown_until': '2099-01-01'}
                                  for r in self.PLAN})
        self.assertEqual(rl.discovery_focus(CFG, state, today=TODAY, plan=self.PLAN),
                         ('Japan', ''))

    def test_an_empty_list_falls_back_to_the_config_rotation(self):
        self.assertEqual(rl.discovery_focus(CFG, _state(1), today=TODAY, plan=[]), ('Japan', ''))

    def test_the_pick_is_credited_to_its_own_slice(self):
        state = _state(1)
        rl.discovery_focus(CFG, state, today=TODAY, plan=self.PLAN)
        self.assertEqual(state['discovery_last_slice'], '|保險')

    def test_an_explicit_slot_focus_still_beats_the_list(self):
        self.assertEqual(rl.discovery_focus(CFG, _state(1), 'Taiwan', '航運', today=TODAY,
                                            plan=self.PLAN), ('Taiwan', '航運'))


class NominationThemeTests(NoWritesMixin, unittest.TestCase):
    """The nomination prompt's theme sentence follows the plan, not a fixed tech list."""

    def setUp(self):
        super().setUp()
        self._llm = rl.llm_call
        self.prompts = []

        def fake(cfg, key, state, prompt, provider='gemini', use_search=True, **kw):
            self.prompts.append(prompt)
            return '[]'
        rl.llm_call = fake

    def tearDown(self):
        rl.llm_call = self._llm
        super().tearDown()

    def test_an_open_attempt_lists_the_curated_vocabulary(self):
        cfg = {'industry_themes': ['醫療', '能源', '化工']}
        rl.nominate_new(cfg, None, _state(), 5, [], 'gemini')
        self.assertIn('醫療、能源、化工', self.prompts[0])
        self.assertNotIn('AI伺服器、半導體、矽光子', self.prompts[0])

    def test_a_focused_attempt_names_its_industry_alone(self):
        rl.nominate_new({}, None, _state(), 5, [], 'gemini', '', '醫療')
        self.assertIn('本批次主題：醫療', self.prompts[0])
        self.assertIn('產業必須是 醫療', self.prompts[0])
        self.assertNotIn('AI伺服器、半導體', self.prompts[0])
