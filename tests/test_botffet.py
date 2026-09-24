import json
import os
import sys
import sqlite3
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

import botffet
import screen
from botffet import (
    DEFAULT_DAILY_CAP,
    ProviderError,
    answer,
    build_prompt,
    classify_token,
    detect_country,
    parse_command,
    plan_query,
    rank,
)
from screen import COLS


def _row(**over):
    base = dict.fromkeys(COLS, 'x')
    base.update({
        'Country': 'Taiwan', 'Timeframe': '中期', 'Sub-Sector': '散熱',
        'Industry': '電子零組件', 'Tier': '龍頭股', 'Company': 'Alpha (1234.TW)',
    })
    base.update(over)
    return [base[c] for c in COLS]


CORPUS = [
    _row(Company='Alpha (1234.TW)', Country='Taiwan', Tier='隱形冠軍',
         **{'Technical Moat': '液冷 散熱 模組專利'}),
    _row(Company='Beta (6324.T)', Country='Japan', Tier='隱形冠軍',
         **{'Clients & Orders': 'ASML 供應鏈核心'}),
    _row(Company='Gamma (GGG)', Country='USA', Tier='龍頭股',
         **{'Core Business': '半導體 測試設備'}),
    _row(Company='Delta (DDD)', Country='USA', Tier='龍頭股',
         **{'Core Business': '投資銀行業務'}),
]
META = {'rows': 4, 'complete': 4, 'source_mtime': '2026-07-26T23:43:26', 'source': '/tmp/x.csv',
        'cols': 22, 'header0': '國家 (Country)'}


def boom(prompt):
    raise AssertionError('provider must not be called on this path')


class CountryDetectionTests(unittest.TestCase):
    def test_two_char_cjk_country_is_detected(self):
        # A naive len(alias) > 2 guard drops every Chinese country name.
        self.assertEqual(detect_country('台灣有哪些隱形冠軍？'), 'Taiwan')
        self.assertEqual(detect_country('日本の半導體'), 'Japan')

    def test_ascii_alias_respects_word_boundaries(self):
        self.assertEqual(detect_country('US semiconductor leaders'), 'USA')
        self.assertIsNone(detect_country('thus we conclude'))
        self.assertIsNone(detect_country('a plus b'))

    def test_demonyms_resolve(self):
        self.assertEqual(detect_country('which Japanese suppliers'), 'Japan')
        self.assertEqual(detect_country('Korean memory makers'), 'South Korea')

    def test_longest_alias_wins(self):
        self.assertEqual(detect_country('United States chipmakers'), 'USA')

    def test_no_country_mentioned(self):
        self.assertIsNone(detect_country('who has the widest moat?'))


class PlanQueryTests(unittest.TestCase):
    def test_tier_phrase_with_space_and_plural(self):
        self.assertEqual(plan_query('which hidden champions')['tier'], 'hidden_champion')

    def test_facet_words_are_not_left_in_match_terms(self):
        # Leaving 'leaders' in the OR set is what surfaced a bank for a semiconductor query.
        sel = plan_query('US semiconductor leaders with CoWoS exposure')
        self.assertEqual(sel['tier'], 'leader')
        self.assertEqual(sel['country'], 'USA')
        self.assertNotIn('leaders', [t.lower() for t in sel['match_any']])
        self.assertNotIn('us', [t.lower() for t in sel['match_any']])

    def test_english_sector_words_carry_their_chinese_equivalent(self):
        sel = plan_query('US semiconductor leaders')
        self.assertIn('半導體', sel['match_any'])

    def test_long_cjk_runs_are_split_into_bigrams(self):
        # 液冷散熱 never appears literally; 液冷 and 散熱 do.
        terms = plan_query('台灣的液冷散熱供應商')['match_any']
        self.assertIn('液冷', terms)
        self.assertIn('散熱', terms)

    def test_term_count_is_bounded(self):
        sel = plan_query('a very long question ' * 20)
        self.assertLessEqual(len(sel['match_any']), 10)


class RankTests(unittest.TestCase):
    def test_rows_matching_more_terms_come_first(self):
        ordered = rank(CORPUS, ['ASML', '液冷'])
        self.assertIn(ordered[0][screen.COMPANY], ('Alpha (1234.TW)', 'Beta (6324.T)'))

    def test_no_terms_preserves_input_order(self):
        self.assertEqual(rank(CORPUS, []), CORPUS)


class CommandRoutingTests(unittest.TestCase):
    def test_screen_classifies_country_tier_and_text(self):
        kind, sel = parse_command('/screen japan 隱形冠軍 ASML')
        self.assertEqual(kind, 'screen')
        self.assertEqual(sel['country'], 'Japan')
        self.assertEqual(sel['tier'], 'hidden_champion')
        self.assertEqual(sel['match'], ['ASML'])

    def test_classify_token(self):
        self.assertEqual(classify_token('美國'), ('country', 'USA'))
        self.assertEqual(classify_token('hidden_champion'), ('tier', 'hidden_champion'))
        self.assertEqual(classify_token('CoWoS'), ('text', 'CoWoS'))

    def test_unknown_command_is_an_error_not_a_crash(self):
        with self.assertRaises(ValueError):
            parse_command('/nope')

    def test_brief_requires_an_argument(self):
        with self.assertRaises(ValueError):
            parse_command('/brief')

    def test_command_path_never_calls_the_provider(self):
        for cmd in ('/help', '/screen japan', '/industries', '/brief Alpha', '/nope',
                    '/quota', '/video'):
            reply = answer(cmd, rows=CORPUS, meta=META, provider=boom)
            self.assertEqual(reply['provider_calls'], 0, cmd)

    def test_screen_command_filters_correctly(self):
        reply = answer('/screen japan 隱形冠軍', rows=CORPUS, meta=META, provider=boom)
        self.assertEqual([r['Company'] for r in reply['results']], ['Beta (6324.T)'])

    def test_empty_input_returns_help(self):
        self.assertEqual(answer('', rows=CORPUS, meta=META, provider=boom)['kind'], 'help')


class SynthesisTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, 'quota.sqlite3')

    def tearDown(self):
        self.tmp.cleanup()

    def test_free_text_calls_provider_exactly_once(self):
        calls = []

        def stub(prompt):
            calls.append(prompt)
            return 'answer text'

        reply = answer('which Japanese hidden champions supply ASML?', rows=CORPUS, meta=META,
                       provider=stub, quota_db=self.db)
        self.assertEqual(reply['kind'], 'synthesis')
        self.assertEqual(reply['provider_calls'], 1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(reply['text'], 'answer text')

    def test_dry_run_makes_no_call_but_returns_the_prompt(self):
        reply = answer('Japanese hidden champions', rows=CORPUS, meta=META,
                       provider=boom, dry_run=True, quota_db=self.db)
        self.assertEqual(reply['kind'], 'dry-run')
        self.assertEqual(reply['provider_calls'], 0)
        self.assertIn('## Question', reply['prompt'])

    def test_dry_run_consumes_no_quota(self):
        answer('Japanese hidden champions', rows=CORPUS, meta=META,
               provider=boom, dry_run=True, quota_db=self.db, user='dad')
        self.assertEqual(botffet.quota_used('dad', self.db), 0)

    def test_provider_failure_degrades_gracefully(self):
        def failing(prompt):
            raise ProviderError('quota exhausted')

        reply = answer('Japanese hidden champions', rows=CORPUS, meta=META,
                       provider=failing, quota_db=self.db)
        self.assertEqual(reply['kind'], 'error')
        self.assertIn('quota exhausted', reply['text'])
        self.assertIn('/screen', reply['text'])  # points at the still-working path

    def test_daily_cap_blocks_further_calls(self):
        for _ in range(5):
            answer('Japanese hidden champions', rows=CORPUS, meta=META,
                   provider=lambda p: 'ok', quota_db=self.db, user='peter', daily_cap=5)
        reply = answer('Japanese hidden champions', rows=CORPUS, meta=META,
                       provider=boom, quota_db=self.db, user='peter', daily_cap=5)
        self.assertEqual(reply['kind'], 'error')
        self.assertIn('額度已用完', reply['text'])

    def test_no_matches_returns_guidance_without_calling_provider(self):
        reply = answer('zzzz nonexistent qqqq', rows=CORPUS, meta=META,
                       provider=boom, quota_db=self.db)
        self.assertEqual(reply['provider_calls'], 0)
        self.assertIn('/screen', reply['text'])


class PerUserQuotaTests(unittest.TestCase):
    """Peter (7512954760) and Dad (7108285456) are both on the roster, so a single global
    counter would let either exhaust the other's budget."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, 'quota.sqlite3')

    def tearDown(self):
        self.tmp.cleanup()

    def _ask(self, user, cap=3):
        return answer('Japanese hidden champions', rows=CORPUS, meta=META,
                      provider=lambda p: 'ok', quota_db=self.db, user=user, daily_cap=cap)

    def test_budgets_are_independent_between_users(self):
        for _ in range(3):
            self._ask('7512954760')
        self.assertEqual(self._ask('7512954760')['kind'], 'error', 'peter should be capped')
        self.assertEqual(self._ask('7108285456')['kind'], 'synthesis', 'dad must be unaffected')

    def test_claim_is_atomic_and_never_exceeds_the_cap(self):
        allowed = sum(botffet.claim_quota('dad', 3, self.db)[0] for _ in range(10))
        self.assertEqual(allowed, 3)
        self.assertEqual(botffet.quota_used('dad', self.db), 3)

    def test_zero_cap_denies_outright(self):
        self.assertFalse(botffet.claim_quota('dad', 0, self.db)[0])

    def test_claim_fails_closed_on_a_broken_database(self):
        allowed, _ = botffet.claim_quota('dad', 5, os.path.join(self.tmp.name, 'nope', 'x.db'))
        self.assertFalse(allowed, 'a DB error must deny, not silently allow')

    def test_tui_and_telegram_get_separate_budgets(self):
        for _ in range(3):
            self._ask(botffet.LOCAL_USER)
        self.assertEqual(self._ask(botffet.LOCAL_USER)['kind'], 'error')
        self.assertEqual(self._ask('7108285456')['kind'], 'synthesis')


class AuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, 'quota.sqlite3')

    def tearDown(self):
        self.tmp.cleanup()

    def _rows(self):
        with botffet.quota_connect(self.db) as db:
            return [dict(r) for r in db.execute(
                'SELECT * FROM chat_audit ORDER BY id').fetchall()]

    def test_every_answer_is_logged_with_its_user(self):
        answer('/screen japan', rows=CORPUS, meta=META, provider=boom,
               quota_db=self.db, user='7108285456')
        rows = self._rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['chat_id'], '7108285456')
        self.assertEqual(rows[0]['kind'], 'screen')
        self.assertEqual(rows[0]['provider_calls'], 0)

    def test_synthesis_records_retrieval_and_shortlist(self):
        answer('Japanese hidden champions exposed to ASML', rows=CORPUS, meta=META,
               provider=lambda p: 'ok', quota_db=self.db, user='peter')
        row = self._rows()[0]
        self.assertEqual(row['provider_calls'], 1)
        self.assertTrue(row['retrieval'])
        self.assertIn('Beta', row['shortlist'])

    def test_audit_can_be_disabled(self):
        answer('/help', rows=CORPUS, meta=META, provider=boom, quota_db=self.db, audit=False)
        self.assertEqual(self._rows(), [])

    def test_audit_failure_never_breaks_answering(self):
        reply = answer('/help', rows=CORPUS, meta=META, provider=boom,
                       quota_db=os.path.join(self.tmp.name, 'nope', 'x.db'))
        self.assertEqual(reply['kind'], 'help')


class ToolBoundaryTests(unittest.TestCase):
    """The synthesis call must carry no tools.

    Omitting --allowedTools does not achieve this: it is a permission-rule flag, while --tools
    decides which tools exist, and `claude -p` inherits ~/.claude/settings.json where
    defaultMode is bypassPermissions. These tests pin the flags that actually enforce it.
    """

    def _argv(self):
        seen = {}

        def fake_run(cmd, **kw):
            seen['cmd'] = cmd
            seen['cwd'] = kw.get('cwd')

            class R:
                returncode, stdout, stderr = 0, 'ok', ''
            return R()

        real = botffet.subprocess.run
        botffet.subprocess.run = fake_run
        try:
            botffet.claude_provider('hello')
        finally:
            botffet.subprocess.run = real
        return seen

    def test_tools_are_disabled_explicitly(self):
        cmd = self._argv()['cmd']
        self.assertIn('--tools', cmd)
        self.assertEqual(cmd[cmd.index('--tools') + 1], '', 'empty string disables all tools')

    def test_settings_inheritance_is_severed(self):
        # Without this the call inherits defaultMode: bypassPermissions from ~/.claude.
        cmd = self._argv()['cmd']
        self.assertIn('--setting-sources', cmd)
        self.assertEqual(cmd[cmd.index('--setting-sources') + 1], '')

    def test_no_permission_widening_flag_is_ever_passed(self):
        cmd = self._argv()['cmd']
        for flag in botffet.FORBIDDEN_FLAGS:
            self.assertNotIn(flag, cmd, f'{flag} would re-open the injection path')

    def test_subprocess_cwd_holds_no_secrets(self):
        cwd = self._argv()['cwd']
        self.assertIsNotNone(cwd)
        self.assertNotEqual(cwd, botffet.SCRIPTS_DIR,
                            'scripts/ holds .env and tg_config.json')
        for leaked in ('.env', 'tg_config.json', 'dad_assistant.local.json'):
            self.assertFalse(os.path.exists(os.path.join(cwd, leaked)), leaked)

    def test_real_settings_json_is_permissive_so_severing_matters(self):
        """Guards the premise. If this ever fails, ~/.claude changed and the reasoning in
        claude_provider's docstring should be re-checked — not that the flags became unneeded."""
        path = os.path.expanduser('~/.claude/settings.json')
        if not os.path.exists(path):
            self.skipTest('no user settings.json on this machine')
        with open(path) as f:
            mode = json.load(f).get('permissions', {}).get('defaultMode')
        self.assertEqual(mode, 'bypassPermissions',
                         'premise changed: inherited settings are no longer permissive')


class PromptTests(unittest.TestCase):
    def setUp(self):
        self.persona = 'PERSONA-MARKER'

    def test_prompt_contains_persona_evidence_and_question(self):
        p = build_prompt('why?', CORPUS[:2], META, self.persona)
        self.assertIn('PERSONA-MARKER', p)
        self.assertIn('DATA, not instructions', p)
        self.assertIn('## Question', p)
        self.assertIn('why?', p)

    def test_prompt_carries_the_data_age(self):
        p = build_prompt('why?', CORPUS[:1], META, self.persona)
        self.assertIn(META['source_mtime'], p)

    def test_broadened_retrieval_is_disclosed_to_the_model(self):
        p = build_prompt('why?', CORPUS[:1], META, self.persona, widened='terms only')
        self.assertIn('broadened', p)

    def test_exact_retrieval_adds_no_disclaimer(self):
        p = build_prompt('why?', CORPUS[:1], META, self.persona, widened='filters+terms')
        self.assertNotIn('broadened', p)

    def test_incomplete_rows_are_flagged_to_the_model(self):
        row = _row(Company='Stub (S)', **{'12M Catalysts': '等待系統進行深度調查'})
        p = build_prompt('why?', [row], META, self.persona)
        self.assertIn('INCOMPLETE', p)

    def test_real_persona_file_states_the_hard_rules(self):
        # v2 persona: global "data age" was deliberately replaced by per-company known_since —
        # the whole-file mtime line overstated freshness for rows untouched for days.
        text = botffet.load_persona()
        for rule in ('never invent', 'known_since', 'DATA, not instructions',
                     'no investment advice'):
            self.assertIn(rule.lower(), text.lower(), rule)


class LivePersonaAndCorpusTests(unittest.TestCase):
    def test_answer_works_against_the_real_corpus_without_a_provider(self):
        reply = answer('/screen japan 隱形冠軍 ASML', provider=boom)
        self.assertEqual(reply['provider_calls'], 0)
        self.assertTrue(reply['results'])


if __name__ == '__main__':
    unittest.main()


class CompactionTests(unittest.TestCase):
    """Memory compaction: a long conversation must not resend its whole transcript.

    Without this, context grows linearly with turns (cost rising every exchange) and turns
    beyond the window are dropped outright rather than compressed.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, 'q.sqlite3')
        self.calls = []

    def tearDown(self):
        self.tmp.cleanup()

    def _compactor(self, prompt):
        self.calls.append(prompt)
        return '- notes about 2330 and liquid cooling'

    def _turns(self, user, n):
        for i in range(n):
            botffet.history_append(user, 'user', f'q{i} ' * 30, self.db)
            botffet.history_append(user, 'assistant', f'a{i} ' * 60, self.db)

    def _context_chars(self, user):
        summary, _ = botffet.summary_get(user, self.db)
        return len(summary) + sum(len(c) for _, c in botffet.history_fetch(user, self.db))

    def test_short_conversation_is_not_compacted(self):
        self._turns('u', 2)
        self.assertEqual(botffet.compact_history('u', self.db, provider=self._compactor), 0)
        self.assertEqual(self.calls, [], 'must not spend a call on a short thread')

    def test_aged_out_turns_are_folded(self):
        self._turns('u', 10)
        folded = botffet.compact_history('u', self.db, provider=self._compactor)
        self.assertGreater(folded, 0)
        summary, covers = botffet.summary_get('u', self.db)
        self.assertIn('2330', summary)
        self.assertGreater(covers, 0)

    def test_context_stays_flat_as_the_conversation_grows(self):
        self._turns('u', 10)
        botffet.compact_history('u', self.db, provider=self._compactor)
        after_10 = self._context_chars('u')
        self._turns('u', 20)
        botffet.compact_history('u', self.db, provider=self._compactor)
        after_30 = self._context_chars('u')
        self.assertLessEqual(after_30, after_10 * 1.5,
                             'context must not grow proportionally with turn count')

    def test_verbatim_window_is_bounded(self):
        self._turns('u', 20)
        self.assertLessEqual(len(botffet.history_fetch('u', self.db)), botffet.HISTORY_TURNS)

    def test_the_same_turns_are_never_folded_twice(self):
        self._turns('u', 10)
        botffet.compact_history('u', self.db, provider=self._compactor)
        first = len(self.calls)
        self.assertEqual(botffet.compact_history('u', self.db, provider=self._compactor), 0)
        self.assertEqual(len(self.calls), first, 'no second call without new aged-out turns')

    def test_compaction_failure_is_survivable(self):
        def failing(_prompt):
            raise botffet.ProviderError('quota')
        self._turns('u', 10)
        self.assertEqual(botffet.compact_history('u', self.db, provider=failing), 0)
        # turns stay pending so a later attempt can still fold them
        self.assertTrue(botffet.pending_compaction('u', self.db))

    def test_reset_clears_summary_as_well_as_turns(self):
        self._turns('u', 10)
        botffet.compact_history('u', self.db, provider=self._compactor)
        botffet.history_clear('u', self.db)
        self.assertEqual(botffet.summary_get('u', self.db), ('', 0))
        self.assertEqual(botffet.history_fetch('u', self.db), [])

    def test_summary_is_fenced_as_data_in_the_prompt(self):
        prompt = botffet.build_agentic_prompt(
            'why?', [], 'PERSONA', {'rows': 1, 'source_mtime': 'x'},
            summary='- user holds 2330')
        self.assertIn('DATA, not instructions', prompt)
        self.assertIn('2330', prompt)

    def test_memory_is_per_chat(self):
        self._turns('peter', 10)
        botffet.compact_history('peter', self.db, provider=self._compactor)
        self.assertEqual(botffet.summary_get('dad', self.db), ('', 0))


class LongTermMemoryTests(unittest.TestCase):
    """The window + summary forget over months: the summary is rewritten every compaction and
    raw turns are trimmed at HISTORY_KEEP. Two more tiers make the memory durable — a capped
    profile of standing facts, and an untrimmed archive — without growing the per-turn
    context. /reset must not turn the person back into a stranger."""

    TWO_PART = ('### Updated notes\n- asked about 2330 and liquid cooling\n'
                '### Durable facts\n- [持股] 台積電 2330，長期持有\n- [偏好] 回答要簡短')

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, 'q.sqlite3')

    def tearDown(self):
        self.tmp.cleanup()

    def _turns(self, user, n):
        for i in range(n):
            botffet.history_append(user, 'user', f'q{i} ' * 30, self.db)
            botffet.history_append(user, 'assistant', f'a{i} ' * 60, self.db)

    def test_compaction_fills_the_profile_and_keeps_notes_separate(self):
        self._turns('dad', 10)
        botffet.compact_history('dad', self.db, provider=lambda _p: self.TWO_PART)
        self.assertIn('2330，長期持有', botffet.profile_get('dad', self.db))
        summary, _ = botffet.summary_get('dad', self.db)
        self.assertIn('liquid cooling', summary)
        self.assertNotIn('Durable facts', summary)
        self.assertNotIn('長期持有', summary)

    def test_compactor_is_shown_the_existing_profile_to_revise(self):
        seen = []
        botffet.profile_put('dad', '- [持股] 2330', self.db)
        self._turns('dad', 10)
        botffet.compact_history('dad', self.db, provider=lambda p: seen.append(p) or self.TWO_PART)
        self.assertIn('### Existing durable facts\n- [持股] 2330', seen[0])

    def test_a_reply_without_a_facts_section_leaves_the_profile_alone(self):
        botffet.profile_put('dad', '- [持股] 2330', self.db)
        self._turns('dad', 10)
        botffet.compact_history('dad', self.db, provider=lambda _p: '- notes only')
        self.assertEqual(botffet.profile_get('dad', self.db), '- [持股] 2330')

    def test_an_empty_facts_section_never_erases_the_profile(self):
        botffet.profile_put('dad', '- [持股] 2330', self.db)
        self._turns('dad', 10)
        botffet.compact_history('dad', self.db, provider=lambda _p:
                                '### Updated notes\n- x\n### Durable facts\n(none)')
        self.assertEqual(botffet.profile_get('dad', self.db), '- [持股] 2330')

    def test_profile_is_bounded_however_much_the_model_returns(self):
        botffet.profile_put('dad', '\n'.join(f'- [x] fact {i} ' + 'y' * 100 for i in range(200)),
                            self.db)
        stored = botffet.profile_get('dad', self.db)
        self.assertLessEqual(len(stored), botffet.PROFILE_CLIP)
        self.assertLessEqual(len(stored.splitlines()), botffet.PROFILE_MAX_LINES)

    def test_archive_keeps_every_turn_while_history_is_trimmed(self):
        self._turns('dad', botffet.HISTORY_KEEP)      # 2x HISTORY_KEEP rows
        with botffet.quota_connect(self.db) as db:
            kept = db.execute("SELECT COUNT(*) FROM chat_history WHERE chat_id='dad'").fetchone()[0]
        self.assertEqual(kept, botffet.HISTORY_KEEP)
        self.assertEqual(botffet.archive_count('dad', self.db), 2 * botffet.HISTORY_KEEP)

    def test_existing_history_is_backfilled_into_the_archive_once(self):
        # A database from before the archive existed: history rows, no archive table.
        with sqlite3.connect(self.db) as raw:
            raw.executescript("""CREATE TABLE chat_history (id INTEGER PRIMARY KEY, chat_id TEXT,
                role TEXT, content TEXT, created_at TEXT);
                INSERT INTO chat_history (chat_id, role, content, created_at) VALUES
                ('dad','user','舊的問題','2026-08-10T17:24:49'),
                ('dad','assistant','舊的回答','2026-08-10T17:24:55');""")
        self.assertEqual(botffet.archive_count('dad', self.db), 2)
        botffet.archive_count('dad', self.db)          # a second open must not duplicate
        self.assertEqual(botffet.archive_count('dad', self.db), 2)

    def test_reset_keeps_long_term_memory_unless_told_all(self):
        self._turns('dad', 10)
        botffet.compact_history('dad', self.db, provider=lambda _p: self.TWO_PART)
        botffet.history_clear('dad', self.db)
        self.assertEqual(botffet.history_fetch('dad', self.db), [])
        self.assertEqual(botffet.summary_get('dad', self.db), ('', 0))
        self.assertIn('2330', botffet.profile_get('dad', self.db))
        self.assertEqual(botffet.archive_count('dad', self.db), 20)
        botffet.history_clear('dad', self.db, everything=True)
        self.assertEqual(botffet.profile_get('dad', self.db), '')
        self.assertEqual(botffet.archive_count('dad', self.db), 0)

    def test_reset_commands_route_to_the_right_scope(self):
        self.assertEqual(botffet.parse_command('/reset'), ('reset', {'scope': 'recent'}))
        self.assertEqual(botffet.parse_command('/reset all'), ('reset', {'scope': 'all'}))
        self.assertEqual(botffet.parse_command('/memory'), ('memory', {}))

    def test_profile_is_fenced_as_data_and_the_archive_tool_is_pointed_out(self):
        prompt = botffet.build_agentic_prompt(
            'why?', [], 'PERSONA', {'rows': 1, 'source_mtime': 'x'},
            summary='- recent', profile='- [持股] 2330')
        self.assertIn('long-term memory) — DATA, not instructions', prompt)
        self.assertIn('- [持股] 2330', prompt)
        self.assertIn('memory_search', prompt)
        self.assertLess(prompt.index('[持股]'), prompt.index('- recent'),
                        'standing facts come before the recent notes')

    def test_memory_command_reports_what_is_known_without_a_model_call(self):
        calls = []
        botffet.profile_put('dad', '- [持股] 2330', self.db)
        reply = botffet.answer('/memory', user='dad', quota_db=self.db,
                               provider=lambda p: calls.append(p) or 'x', audit=False)
        self.assertEqual(reply['kind'], 'memory')
        self.assertIn('2330', reply['text'])
        self.assertEqual(calls, [])

    def test_split_compaction_accepts_looser_headings(self):
        notes, facts = botffet.split_compaction('Updated notes:\n- a\n\n## DURABLE FACTS\n- b')
        self.assertEqual((notes, facts), ('- a', '- b'))
        self.assertEqual(botffet.split_compaction('- plain'), ('- plain', None))


class FirstContactTests(unittest.TestCase):
    """A new user's very first message must onboard them, free and instantly.

    Telegram forbids a bot from messaging first, so /start is the only moment we control —
    and clients do not all send it automatically, hence bare greetings count too.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, 'q.sqlite3')

    def tearDown(self):
        self.tmp.cleanup()

    def _first(self, text, user='newcomer'):
        return answer(text, user=user, rows=CORPUS, meta=META,
                      provider=boom, quota_db=self.db, audit=False)

    def test_start_returns_the_welcome(self):
        self.assertEqual(self._first('/start')['kind'], 'welcome')

    def test_start_with_botname_suffix(self):
        # Telegram appends @botusername; without stripping it this was 'unknown command'.
        self.assertEqual(
            self._first('/start@Hermes_Investment_Strategy_bot')['kind'], 'welcome')

    def test_bare_greetings_onboard_without_a_model_call(self):
        for greeting in ('你好', '您好！', 'hi', 'Hello', '嗨', '早安'):
            reply = self._first(greeting, user=f'new-{greeting}')
            self.assertEqual(reply['kind'], 'welcome', greeting)
            self.assertEqual(reply['provider_calls'], 0, greeting)

    def test_welcome_states_the_live_corpus_size(self):
        self.assertIn(str(META['rows']), self._first('/start')['text'])

    def test_welcome_covers_the_documented_capabilities_and_limits(self):
        text = self._first('/start')['text']
        for topic in ('護城河', '隱形冠軍', 'known_since', 'CoWoS', '/screen', '/reset'):
            self.assertIn(topic, text, topic)
        for limit in ('不提供投資建議', '已研究'):
            self.assertIn(limit, text, limit)

    def test_a_real_question_beginning_with_a_greeting_is_answered(self):
        reply = answer('你好，請問台積電的護城河是什麼？', user='newcomer2', rows=CORPUS,
                       meta=META, provider=lambda p: 'answer', quota_db=self.db, audit=False)
        self.assertEqual(reply['kind'], 'synthesis')

    def test_greeting_from_a_returning_user_is_conversational(self):
        botffet.history_append('returning', 'user', 'earlier question', self.db)
        botffet.history_append('returning', 'assistant', 'earlier answer', self.db)
        reply = answer('hi', user='returning', rows=CORPUS, meta=META,
                       provider=lambda p: 'hello again', quota_db=self.db, audit=False)
        self.assertNotEqual(reply['kind'], 'welcome')

    def test_help_and_start_are_different_surfaces(self):
        self.assertNotEqual(self._first('/start')['text'], self._first('/help')['text'])
