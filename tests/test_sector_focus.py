"""The sector steering control: dashboard wiring, and the focus reaching the scraper.

The scraper had no sector-selection logic at all — direction came from a counter modulo a
hand-typed rotation. These tests pin the pieces that replaced that: a standing focus, the panel
that sets it, and the path from the focus to the words the nominating model actually reads.
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

import botffet
import dashboard
import industry_focus as inf
import research_loop as rl
from research_state import DEEP_RESEARCHED, WorkflowState


def _closure_class(action_name, class_name):
    """Reach a class defined inside run_tui's closure.

    Same approach as tests/test_dashboard_chat.py: run_tui(return_app=True) returns a 2-tuple
    that other tests unpack positionally, so it must not be widened.
    """
    DashApp, _ = dashboard.run_tui(return_app=True)
    fn = getattr(DashApp, action_name)
    cells = dict(zip(fn.__code__.co_freevars,
                     (c.cell_contents for c in (fn.__closure__ or ()))))
    return DashApp, cells[class_name]


class BindingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.DashApp, _ = dashboard.run_tui(return_app=True)

    def test_i_opens_the_sector_panel(self):
        keys = [b.key for b in self.DashApp.BINDINGS]
        self.assertIn('i', keys)
        self.assertEqual(len(keys), len(set(keys)), 'a duplicate binding would shadow one')

    def test_no_existing_binding_was_displaced(self):
        keys = [b.key for b in self.DashApp.BINDINGS]
        for key in ('q', 'c', 's', 'g', 'space', 'n', 'r', 'l', 'p', 'v'):
            self.assertIn(key, keys, key)

    def test_action_exists(self):
        self.assertTrue(callable(getattr(self.DashApp, 'action_industries', None)))

    def test_return_app_contract_is_still_a_two_tuple(self):
        self.assertEqual(len(dashboard.run_tui(return_app=True)), 2)


class DropdownTests(unittest.TestCase):
    def test_option_values_stay_bare_so_they_match(self):
        """The visible label may carry a count; the stored value must not.

        industry_focus is matched as a substring against the companies table, so an option
        value of '半導體  (483 held)' would match nothing at all.
        """
        DashApp, _ = dashboard.run_tui(return_app=True)
        app = DashApp()
        themes = set(inf.load_themes(dashboard.read_loop_config()))
        values = {value for _, value in app.focus_industries}
        # Real corpus labels legitimately contain brackets ('Aerospace Materials (Carbon
        # Fiber)'), so the invariant is about the annotation this code adds, not brackets.
        for label, value in app.focus_industries:
            self.assertEqual(value, value.strip(), repr(value))
            self.assertNotIn('held)', value, f'{value!r} carries the count annotation')
            self.assertTrue(label.startswith(value), (label, value))
        for theme in themes:
            self.assertIn(theme, values, f'{theme} must be selectable as a bare value')


class StandingFocusReachesTheScraperTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = WorkflowState(os.path.join(self.tmp.name, 'workflow.sqlite3'))
        self._llm_call = rl.llm_call

    def tearDown(self):
        rl.llm_call = self._llm_call
        self.tmp.cleanup()

    def test_the_focus_becomes_a_hard_constraint_in_the_nomination_prompt(self):
        captured = {}

        def fake_llm_call(cfg, key, state, prompt, provider='gemini', use_search=True, **kw):
            captured['prompt'] = prompt
            return '[]'

        rl.llm_call = fake_llm_call
        rl.nominate_new({}, None, {}, 5, covered=[], provider='gemini',
                        focus_industry='低軌衛星', focus_country='Japan')
        self.assertIn('產業必須是 低軌衛星', captured['prompt'])
        self.assertIn('國家必須是 Japan', captured['prompt'])

    def test_an_unfocused_slot_inherits_the_standing_focus_end_to_end(self):
        self.state.set_standing_focus('低軌衛星', country='Japan')
        slot = {'country_focus': '', 'industry_focus': ''}
        country, industry, origin = rl.effective_focus(self.state, slot)
        self.assertEqual((country, industry, origin), ('Japan', '低軌衛星', 'standing'))

        captured = {}

        def fake_llm_call(cfg, key, state, prompt, provider='gemini', use_search=True, **kw):
            captured['prompt'] = prompt
            return '[]'

        rl.llm_call = fake_llm_call
        rl.nominate_new({}, None, {}, 5, covered=[], provider='gemini',
                        focus_industry=industry, focus_country=country)
        self.assertIn('產業必須是 低軌衛星', captured['prompt'])

    def test_a_focused_pull_from_the_pending_queue_uses_substring_matching(self):
        self.state.upsert_company({'Company': 'Sat (S1)', 'Country': 'Japan',
                                   'Industry': '低軌衛星與地面站設備'})
        self.state.set_standing_focus('低軌衛星')
        _, industry, _ = rl.effective_focus(self.state, {'country_focus': '',
                                                         'industry_focus': ''})
        queue = self.state.research_queue(10, industry=industry)
        self.assertEqual([r['ticker'] for r in queue], ['S1'])


class FocusEchoGuardTests(unittest.TestCase):
    """Setting a focus must report what it matches, so a typo is caught when typed."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = WorkflowState(os.path.join(self.tmp.name, 'workflow.sqlite3'))
        self.state.upsert_company({'Company': 'Sat (S1)', 'Country': 'Japan',
                                   'Industry': '低軌衛星'}, DEEP_RESEARCHED)

    def tearDown(self):
        self.tmp.cleanup()

    def test_known_sector_reports_its_holdings(self):
        self.assertEqual(inf.focus_match_count(self.state, '低軌衛星')['held'], 1)

    def test_unknown_sector_reports_zero(self):
        self.assertEqual(inf.focus_match_count(self.state, '低軌衞星')['held'], 0)


class FocusCommandParsingTests(unittest.TestCase):
    """/focus is parsed and executed in plain Python — the model never sees it."""

    def test_bare_command_shows_the_current_focus(self):
        self.assertEqual(botffet.parse_command('/focus'), ('focus', {'action': 'show'}))

    def test_a_sector_sets_it_until_cleared(self):
        kind, sel = botffet.parse_command('/focus 低軌衛星')
        self.assertEqual((kind, sel['action'], sel['sector'], sel['batches']),
                         ('focus', 'set', '低軌衛星', None))

    def test_a_trailing_number_is_a_batch_budget(self):
        _, sel = botffet.parse_command('/focus 低軌衛星 3')
        self.assertEqual((sel['sector'], sel['batches']), ('低軌衛星', 3))

    def test_a_multi_word_sector_survives(self):
        _, sel = botffet.parse_command('/focus Aerospace and Defence')
        self.assertEqual(sel['sector'], 'Aerospace and Defence')

    def test_clear_words_all_work(self):
        for word in ('clear', 'off', 'none', 'stop', 'CLEAR'):
            self.assertEqual(botffet.parse_command(f'/focus {word}')[1]['action'], 'clear')

    def test_telegram_bot_suffix_is_stripped(self):
        self.assertEqual(botffet.parse_command('/focus@Some_bot')[1]['action'], 'show')


class FocusCommandBehaviourTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = WorkflowState(os.path.join(self.tmp.name, 'workflow.sqlite3'))
        self.state.upsert_company({'Company': 'Sat (S1)', 'Country': 'Japan',
                                   'Industry': '低軌衛星與地面站'}, DEEP_RESEARCHED)

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, text):
        return botffet._focus_command(botffet.parse_command(text)[1], workflow=self.state)

    def test_show_with_nothing_set_explains_the_default_behaviour(self):
        self.assertIn('No standing focus', self._run('/focus')['text'])

    def test_setting_echoes_what_it_matches(self):
        reply = self._run('/focus 低軌衛星')
        self.assertIn('1 researched', reply['text'])
        self.assertEqual(self.state.standing_focus()['industry'], '低軌衛星')

    def test_a_sector_matching_nothing_is_set_but_flagged(self):
        # Not an error — opening a brand-new area is the point of steering.
        reply = self._run('/focus 不存在的產業')
        self.assertIn('Nothing already held matches it', reply['text'])
        self.assertIsNotNone(self.state.standing_focus())

    def test_batch_budget_is_stated_back(self):
        self._run('/focus 低軌衛星 3')
        self.assertEqual(self.state.standing_focus()['batches_remaining'], 3)

    def test_clearing_says_what_went_away(self):
        self._run('/focus 低軌衛星')
        self.assertIn('低軌衛星', self._run('/focus clear')['text'])
        self.assertIsNone(self.state.standing_focus())

    def test_clearing_nothing_is_not_an_error(self):
        self.assertIn('no standing focus', self._run('/focus clear')['text'].lower())


class ProposalBoundaryTests(unittest.TestCase):
    """爸菲特 may suggest a direction. It may never set one."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = WorkflowState(os.path.join(self.tmp.name, 'workflow.sqlite3'))

    def tearDown(self):
        self.tmp.cleanup()

    def test_marker_is_parsed_and_removed_from_the_visible_answer(self):
        reply = ('低軌衛星 looks thin.\n'
                 'FOCUS-PROPOSAL: 低軌衛星 | only 3 held')
        text, proposal = botffet.extract_focus_proposal(reply)
        self.assertNotIn('FOCUS-PROPOSAL', text)
        self.assertEqual(proposal, {'industry': '低軌衛星', 'reason': 'only 3 held'})

    def test_an_answer_without_a_marker_is_untouched(self):
        self.assertEqual(botffet.extract_focus_proposal('just an answer'),
                         ('just an answer', None))

    def test_a_marker_with_no_sector_yields_no_proposal(self):
        text, proposal = botffet.extract_focus_proposal('hi\nFOCUS-PROPOSAL:   |  nothing')
        self.assertIsNone(proposal)
        self.assertNotIn('FOCUS-PROPOSAL', text)

    def test_a_marker_without_a_reason_still_parses(self):
        _, proposal = botffet.extract_focus_proposal('FOCUS-PROPOSAL: 航運')
        self.assertEqual(proposal['industry'], '航運')

    def test_a_proposal_changes_nothing_on_its_own(self):
        botffet.record_focus_proposal({'industry': '低軌衛星', 'reason': 'x'},
                                      workflow=self.state)
        self.assertIsNone(self.state.standing_focus())
        self.assertEqual(len(self.state.focus_proposals()), 1)

    def test_repeating_a_suggestion_refreshes_rather_than_piling_up(self):
        for reason in ('first', 'second', 'third'):
            botffet.record_focus_proposal({'industry': '低軌衛星', 'reason': reason},
                                          workflow=self.state)
        pending = self.state.focus_proposals()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]['reason'], 'third')

    def test_only_accepting_turns_a_proposal_into_an_instruction(self):
        pid = botffet.record_focus_proposal({'industry': '低軌衛星', 'reason': 'x'},
                                            workflow=self.state)
        self.assertIsNone(self.state.standing_focus())
        focus = self.state.accept_focus_proposal(pid)
        self.assertEqual(focus['industry'], '低軌衛星')
        self.assertEqual(self.state.standing_focus()['industry'], '低軌衛星')
        self.assertEqual(self.state.focus_proposals(), [])

    def test_dismissing_leaves_the_scraper_alone(self):
        pid = botffet.record_focus_proposal({'industry': '低軌衛星'}, workflow=self.state)
        self.state.resolve_focus_proposal(pid, 'dismissed')
        self.assertIsNone(self.state.standing_focus())
        self.assertEqual(self.state.focus_proposals(), [])

    def test_a_broken_workflow_never_breaks_the_answer(self):
        self.assertIsNone(botffet.record_focus_proposal(
            {'industry': '低軌衛星'}, workflow=object()))


class ModelCapabilityUnchangedTests(unittest.TestCase):
    """The suggestion path must grant the model nothing it did not already have."""

    def test_safety_flags_are_untouched(self):
        self.assertEqual(botffet.SAFETY_FLAGS, ['--tools', '', '--setting-sources', ''])

    def test_forbidden_flags_still_listed(self):
        for flag in ('--allowedTools', '--dangerously-skip-permissions'):
            self.assertIn(flag, botffet.FORBIDDEN_FLAGS)

    def test_the_screener_server_exposes_no_write_tool(self):
        import screener_mcp
        for name, tool in screener_mcp.TOOLS.items():
            self.assertNotIn('propose', name)
            self.assertNotIn('path', tool['schema'].get('properties', {}),
                             f'{name} must not take a path')

    def test_the_suggestion_instruction_reaches_the_agentic_prompt_only(self):
        prompt = botffet.build_agentic_prompt('q', [], 'persona',
                                              {'rows': 1, 'source_mtime': 'x'})
        self.assertIn(botffet.FOCUS_PROPOSAL_MARKER, prompt)
        self.assertIn('changes nothing on its own', prompt)


class FocusIsRememberedTests(unittest.TestCase):
    """Setting a focus is part of the conversation, so a follow-up can refer to it."""

    def test_focus_replies_enter_the_chat_history(self):
        import inspect
        source = inspect.getsource(botffet.answer)
        self.assertIn("'synthesis', 'focus'", source)


class FooterVisibilityTests(unittest.TestCase):
    """The footer is one line and truncates from the right.

    With twelve bindings and verbose labels it ran ~190 characters, so 'Sectors' and 'Videos'
    fell off the end of any normal-width terminal and the features looked absent.
    """

    NARROW = 80

    @classmethod
    def setUpClass(cls):
        cls.DashApp, _ = dashboard.run_tui(return_app=True)

    def _footer(self):
        return '  '.join(f'{b.key} {b.description}' for b in self.DashApp.BINDINGS)

    def test_the_screen_keys_survive_an_eighty_column_terminal(self):
        head = self._footer()[:self.NARROW]
        for label in ('Videos', 'Sectors'):
            self.assertIn(label, head, f'{label} falls off an {self.NARROW}-column footer')

    def test_chat_and_quit_are_still_first(self):
        self.assertEqual([b.key for b in self.DashApp.BINDINGS][:2], ['q', 'c'])

    def test_no_label_is_verbose_enough_to_push_the_others_off(self):
        for binding in self.DashApp.BINDINGS:
            self.assertLessEqual(len(binding.description), 12,
                                 f'{binding.key}: {binding.description!r} is too long')

    def test_both_languages_stay_short(self):
        for lang in ('en', 'zh'):
            for key in ('k_pause', 'k_new', 'k_lang', 'k_providers', 'k_review'):
                self.assertLessEqual(len(dashboard.tr(lang, key)), 12, (lang, key))
