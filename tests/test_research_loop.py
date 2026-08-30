"""Tests for research_loop validation, ticker-correction salvage, and unlisted handling.

Regression coverage for the four 2026-07-20 quarantine cases:
  - 台揚科技 (2483.TW -> 2314.TW)  : nominator gave a wrong ticker; researcher corrected it.
  - 健策精密 / 嘉澤端子             : same, previously returned prose ("not a JSON object").
  - 訊凱國際 Cooler Master          : genuinely unlisted -> must quarantine, must not retry.
"""

import csv
import json
import os
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))
import research_loop as rl
from research_state import RESEARCH_PENDING, WorkflowState


def make_valid_record(company, timeframe='中期'):
    """A record that passes every field check, parametrised by Company."""
    rec = {k: '這是一段具體、資訊密集且長度足夠的繁體中文投資研究敘述內容。' for k in rl.RESEARCH_KEYS}
    rec['Company'] = company
    rec['Timeframe'] = timeframe
    return rec


class SameCompanyTests(unittest.TestCase):
    def test_name_preserved_with_english_suffix(self):
        self.assertTrue(rl.same_company(
            '台揚科技 Microelectronics Technology Inc. (2483.TW)', '台揚科技 (2314.TW)'))

    def test_drift_to_different_company_rejected(self):
        self.assertFalse(rl.same_company('嘉澤端子 Speed Tech (6153.TW)', '嘉聯益 (6153.TW)'))

    def test_english_only_names(self):
        self.assertTrue(rl.same_company('NVIDIA (NVDA)', 'NVIDIA Corporation (NVDA)'))
        self.assertFalse(rl.same_company('NVIDIA (NVDA)', 'Advanced Micro Devices (AMD)'))


class ValidateRecordTests(unittest.TestCase):
    def test_corrected_ticker_accepted_when_name_matches(self):
        item = {'Company': '台揚科技 Microelectronics Technology Inc. (2483.TW)',
                'Country': '台灣', 'Industry': '衛星通訊'}
        rec = make_valid_record('台揚科技 (2314.TW)')
        self.assertEqual(rl.validate_record(rec, item), [])

    def test_ticker_mismatch_rejected_when_company_drifts(self):
        item = {'Company': '嘉澤端子 Speed Tech (6153.TW)', 'Country': '台灣', 'Industry': 'AI伺服器'}
        rec = make_valid_record('嘉聯益 (9999.TW)')
        problems = rl.validate_record(rec, item)
        self.assertTrue(any('代號不符' in p for p in problems), problems)

    def test_matching_ticker_still_passes(self):
        item = {'Company': '台積電 (2330.TW)', 'Country': '台灣', 'Industry': '晶圓代工'}
        rec = make_valid_record('台積電 (2330.TW)')
        self.assertEqual(rl.validate_record(rec, item), [])

    def test_unlisted_is_terminal(self):
        item = {'Company': '訊凱國際 Cooler Master (3691.TW)', 'Country': '台灣', 'Industry': '散熱'}
        problems = rl.validate_record({'unlisted': '訊凱國際為未公開上市之私人企業'}, item)
        self.assertEqual(len(problems), 1)
        self.assertTrue(problems[0].startswith('UNLISTED:'), problems)

    def test_non_dict_output(self):
        self.assertEqual(rl.validate_record(None, {'Company': 'X (X)'}),
                         ['output is not a JSON object'])


class ResearchCompanyFlowTests(unittest.TestCase):
    """research_company drives retries; stub llm_call to count calls without subprocess."""

    def setUp(self):
        self.cfg = {'max_retries': 2, 'delay_seconds': 0}
        self.calls = 0
        self._orig = rl.llm_call

    def tearDown(self):
        rl.llm_call = self._orig

    def _patch(self, raw_text):
        def stub(cfg, key, state, prompt, provider='gemini', use_search=True):
            self.calls += 1
            return raw_text
        rl.llm_call = stub

    def test_unlisted_does_not_retry(self):
        self._patch('{"unlisted": "訊凱國際為未公開上市之私人企業，無台股代號"}')
        item = {'Company': '訊凱國際 Cooler Master (3691.TW)', 'Country': '台灣', 'Industry': '散熱'}
        rec, problems, _ = rl.research_company(self.cfg, None, {}, item)
        self.assertIsNone(rec)
        self.assertEqual(self.calls, 1, 'terminal UNLISTED must not consume retries')
        self.assertTrue(problems[0].startswith('UNLISTED:'))

    def test_corrected_ticker_returns_on_first_try(self):
        raw = json.dumps(make_valid_record('健策精密工業 (3653.TW)'), ensure_ascii=False)
        self._patch(raw)
        item = {'Company': '健策精密工業 (6485.TW)', 'Country': '台灣', 'Industry': '散熱'}
        rec, problems, _ = rl.research_company(self.cfg, None, {}, item)
        self.assertEqual(problems, [])
        self.assertEqual(self.calls, 1)
        self.assertEqual(rl.extract_ticker(rec['Company']), '3653.TW')


class CanonicalCountryTests(unittest.TestCase):
    def test_chinese_to_english(self):
        for raw, want in [('美國', 'USA'), ('日本', 'Japan'), ('南韓', 'South Korea'),
                          ('韓國', 'South Korea'), ('荷蘭', 'Netherlands'), ('德國', 'Germany'),
                          ('法國', 'France'), ('以色列', 'Israel'), ('台灣', 'Taiwan')]:
            self.assertEqual(rl.canonical_country(raw), want)

    def test_new_countries(self):
        self.assertEqual(rl.canonical_country('瑞士'), 'Switzerland')
        self.assertEqual(rl.canonical_country('巴西 (Brazil)'), 'Brazil')
        self.assertEqual(rl.canonical_country('義大利'), 'Italy')

    def test_bilingual_and_prose_resolve_to_leading_country(self):
        self.assertEqual(rl.canonical_country('美國（USA）。'), 'USA')
        self.assertEqual(rl.canonical_country('Taiwan（台灣）'), 'Taiwan')
        self.assertEqual(rl.canonical_country('總部位於美國。'), 'USA')
        # paragraph led by the primary country, later mentions must not win
        self.assertEqual(rl.canonical_country('英國（與澳洲雙重上市，資產分布於加拿大、智利）'), 'UK')
        self.assertEqual(rl.canonical_country('荷蘭（Stellantis 註冊地阿姆斯特丹，營運分散於義大利、法國）'), 'Netherlands')
        self.assertEqual(rl.canonical_country('South韓（大韓民國）'), 'South Korea')

    def test_canonical_passthrough_and_empty(self):
        self.assertEqual(rl.canonical_country('USA'), 'USA')
        self.assertEqual(rl.canonical_country(''), '')
        self.assertEqual(rl.canonical_country('   '), '')

    def test_validate_record_normalizes_country(self):
        item = {'Company': '台積電 (2330.TW)', 'Country': 'Taiwan', 'Industry': '晶圓代工'}
        rec = make_valid_record('台積電 (2330.TW)')
        rec['Country'] = '美國'  # model returned Chinese/mismatched form
        self.assertEqual(rl.validate_record(rec, item), [])
        self.assertEqual(rec['Country'], 'USA')  # normalized in place


class MaintenanceTests(unittest.TestCase):
    """News-refresh maintenance mode: scoped prompt/validation, queue ranking, and the
    critical guarantee that a maintenance record only touches MAINTENANCE_KEYS."""

    def _complete_row(self, name, tier, placeholder_at=None):
        r = ['內容內容內容'] * 22
        r[0], r[4], r[5] = 'Taiwan', tier, name
        if placeholder_at is not None:
            r[placeholder_at] = '等待系統進行深度調查'  # makes the row incomplete
        return r

    def _write_csv(self, path, rows):
        with open(path, 'w', newline='', encoding='utf-8-sig') as f:
            w = csv.writer(f)
            w.writerow([f'col{i}' for i in range(22)])
            for r in rows:
                w.writerow(r)

    def test_prompt_and_scoped_validation(self):
        item = {'Company': '台積電 (2330)', 'Country': 'Taiwan',
                'values': {k: f'舊{k}' for k in rl.MAINTENANCE_KEYS}}
        p = rl.maintenance_prompt(item)
        self.assertTrue(all(k in p for k in rl.MAINTENANCE_KEYS))
        good = {k: '這是一段有效、具體且長度足夠的最新消息更新繁體中文敘述。' for k in rl.MAINTENANCE_KEYS}
        good['Core Business'] = 'should be stripped'
        self.assertEqual(rl.validate_maintenance(good, item), [])
        self.assertEqual(sorted(good), sorted(rl.MAINTENANCE_KEYS + ['Company']))  # extras stripped
        self.assertTrue(rl.validate_maintenance({k: 'x' for k in rl.MAINTENANCE_KEYS[:2]}, item))  # missing
        placeholder = {k: '這是一段有效、具體且長度足夠的最新消息更新繁體中文敘述。' for k in rl.MAINTENANCE_KEYS}
        placeholder['12M Catalysts'] = '調查中'
        self.assertTrue(rl.validate_maintenance(placeholder, item))

    def test_screening_parser_keeps_only_bounded_ordered_https_sources(self):
        items = [{'Company': 'Alpha (AAA)'}]
        raw = json.dumps([{
            'Company': 'Alpha (AAA)', 'needs_update': True, 'reason': 'material news',
            'evidence_date': '2026-07-20',
            'source_urls': ['https://example.com/first', 'https://example.com/second'],
        }])

        result = rl.parse_maintenance_screen(raw, items)

        self.assertEqual(result['AAA']['source_urls'], [
            'https://example.com/first', 'https://example.com/second'])

    def test_queue_ranks_by_staleness_then_tier(self):
        with tempfile.TemporaryDirectory() as d:
            csvp = Path(d) / 'db.csv'
            self._write_csv(csvp, [
                self._complete_row('AAA (AAA)', '潛力股'),
                self._complete_row('BBB (BBB)', '龍頭股'),
                self._complete_row('CCC (CCC)', '龍頭股', placeholder_at=7),  # incomplete -> excluded
            ])
            saved = (rl.CSV_PATH, rl.MAINTENANCE_STATE_PATH)
            rl.CSV_PATH, rl.MAINTENANCE_STATE_PATH = str(csvp), str(Path(d) / 'm.json')
            try:
                q, source, total = rl.build_maintenance_queue({}, {}, 10)
                self.assertEqual((source, total), ('maintenance', 2))          # only completed rows
                self.assertEqual([i['ticker'] for i in q], ['BBB', 'AAA'])     # never-maintained; leader first
                rl.stamp_maintained(['BBB'])                                   # BBB now fresh
                q2, _, _ = rl.build_maintenance_queue({}, {}, 10)
                self.assertEqual(q2[0]['ticker'], 'AAA')                       # never-maintained ranks first
            finally:
                rl.CSV_PATH, rl.MAINTENANCE_STATE_PATH = saved

    def test_partial_merge_touches_only_maintenance_keys(self):
        import apply_batch
        with tempfile.TemporaryDirectory() as d:
            csvp = Path(d) / 'db.csv'
            self._write_csv(csvp, [self._complete_row('台積電 (2330)', '龍頭股')])
            # WORKFLOW_DB_PATH must be redirected too: the canonical store is now the DB and
            # the CSV is its export, so redirecting CSV_PATH alone would let this test write
            # into the live production database.
            saved = (apply_batch.CSV_PATH, apply_batch.PROGRESS_PATH,
                     apply_batch.WORKFLOW_DB_PATH)
            apply_batch.CSV_PATH, apply_batch.PROGRESS_PATH = str(csvp), str(Path(d) / 'p.json')
            apply_batch.WORKFLOW_DB_PATH = str(Path(d) / 'w.sqlite3')
            try:
                # Seed the canonical store from the fixture CSV, then apply a partial record.
                import backfill_research
                backfill_research.from_csv(apply_batch._workflow(), dry_run=False)

                def get():
                    with open(csvp, encoding='utf-8-sig') as infile:
                        return next(r for r in csv.reader(infile)
                                    if apply_batch.extract_ticker(r[5]) == '2330')
                before = get()
                rec = {k: f'NEW {k}' for k in rl.MAINTENANCE_KEYS}
                rec['Company'] = '台積電 (2330)'
                apply_batch.apply_records([rec], 'Maint Test', fetch_insiders=False)
                after = get()
                changed = {apply_batch.COLS[i] for i in range(22) if before[i] != after[i]}
                self.assertEqual(changed, set(rl.MAINTENANCE_KEYS))  # foundational fields untouched
            finally:
                (apply_batch.CSV_PATH, apply_batch.PROGRESS_PATH,
                 apply_batch.WORKFLOW_DB_PATH) = saved

    def test_schedule_parses_optional_mode(self):
        sched = rl.parse_schedule({'schedule': [['09:30', 'gemini', 'maintenance'],
                                                 ['16:30', 'codex']]})
        modes = {p: m for _, p, m in sched}
        self.assertEqual(modes['gemini'], 'maintenance')
        self.assertEqual(modes['codex'], 'research')  # 2-element entry defaults to research


class FocusedNominationTests(unittest.TestCase):
    def test_country_is_strict_and_selected_industry_is_canonical_for_slot(self):
        raw = json.dumps([
            {'Country': 'Taiwan', 'Company': 'Alpha (1111.TW)', 'Industry': 'Other'},
            {'Country': 'USA', 'Company': 'Beta (BETA)', 'Industry': '半導體業'},
        ])
        saved = rl.llm_call
        rl.llm_call = lambda *_args, **_kwargs: raw
        try:
            items = rl.nominate_new({}, None, {'calls': {}}, 20, [], 'gemini',
                                    focus_country='Taiwan', focus_industry='半導體業')
        finally:
            rl.llm_call = saved
        self.assertEqual(items, [{'Company': 'Alpha (1111.TW)', 'Country': 'Taiwan',
                                  'Industry': '半導體業'}])


class DiscoveryBudgetTests(unittest.TestCase):
    """Discovery calls are optional growth work, not something to spend every slot."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.workflow = WorkflowState(os.path.join(self.tmp.name, 'workflow.sqlite3'))
        self.saved = (rl.scan_csv, rl.nominate_new, rl.save_state, rl.select_provider)
        rl.scan_csv = lambda: ([], [])
        rl.save_state = lambda _state: None
        rl.select_provider = lambda *_args, **_kwargs: 'gemini'

    def tearDown(self):
        rl.scan_csv, rl.nominate_new, rl.save_state, rl.select_provider = self.saved
        self.tmp.cleanup()

    def test_empty_queue_with_exhausted_discovery_budget_skips_nomination(self):
        state = {'calls': {'gemini': 0, 'claude': 0, 'codex': 0},
                 'discovery': {'attempts': 1, 'empty_results': 0}}
        cfg = {'retry_slots_per_batch': 10, 'daily_discovery_attempt_budget': 1,
               'daily_empty_discovery_limit': 2}
        nominations = []
        rl.nominate_new = lambda *_args, **_kwargs: nominations.append('called') or []

        items, source, remaining = rl.build_queue(
            cfg, None, state, 20, 'gemini', 'research', self.workflow)

        self.assertEqual(items, [])
        self.assertEqual(source, 'new')
        self.assertEqual(remaining, 0)
        self.assertEqual(nominations, [])

    def test_empty_discovery_result_stops_more_discovery_for_the_day(self):
        state = {'calls': {'gemini': 0, 'claude': 0, 'codex': 0},
                 'discovery': {'attempts': 0, 'empty_results': 0}}
        cfg = {'retry_slots_per_batch': 10, 'daily_discovery_attempt_budget': 2,
               'daily_empty_discovery_limit': 1}
        nominations = []
        rl.nominate_new = lambda *_args, **_kwargs: nominations.append('called') or []

        first = rl.build_queue(cfg, None, state, 20, 'gemini', 'research', self.workflow)
        second = rl.build_queue(cfg, None, state, 20, 'gemini', 'research', self.workflow)

        self.assertEqual(first, ([], 'new', 0))
        self.assertEqual(second, ([], 'new', 0))
        self.assertEqual(nominations, ['called'])
        self.assertEqual(state['discovery'], {'attempts': 1, 'empty_results': 1})


class DiscoveryAlertTests(unittest.TestCase):
    """Every alert on this path was present in the code but structurally unable to fire."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.workflow = WorkflowState(os.path.join(self.tmp.name, 'workflow.sqlite3'))
        self.saved = (rl.scan_csv, rl.nominate_new, rl.save_state, rl.select_provider)
        rl.scan_csv = lambda: ([], [])
        rl.save_state = lambda _state: None
        rl.select_provider = lambda *_a, **_k: 'gemini'
        self.cfg = {'retry_slots_per_batch': 10, 'daily_discovery_attempt_budget': 4,
                    'daily_empty_discovery_limit': 2}
        self.state = {'calls': {'gemini': 0, 'claude': 0, 'codex': 0},
                      'discovery': {'attempts': 0, 'empty_results': 0},
                      'discovery_cursor': 0}

    def tearDown(self):
        rl.scan_csv, rl.nominate_new, rl.save_state, rl.select_provider = self.saved
        self.tmp.cleanup()

    def _build(self):
        sent = []
        with mock.patch.object(rl, 'send_telegram', side_effect=sent.append):
            rl.build_queue(self.cfg, None, self.state, 20, 'gemini', 'research',
                           self.workflow)
        return sent

    def test_alerts_when_every_nomination_was_already_covered(self):
        """The exact 2026-08-25/26 shape: 20 names back, all covered, so nominated == 0.
        The old `if not produced and nominated` gate made this fire zero times, ever."""
        def fake(*_a, stats=None, **_k):
            if stats is not None:
                stats.update(returned=20, kept=0, covered=20, off_focus=0, malformed=0)
            return []
        rl.nominate_new = fake

        sent = self._build()

        self.assertEqual(len(sent), 1, 'saturation must reach Telegram')
        self.assertIn('20 nominations returned, none usable', sent[0])
        self.assertIn('20 already covered', sent[0])

    def test_alerts_when_the_provider_returns_nothing_at_all(self):
        def fake(*_a, stats=None, **_k):
            if stats is not None:
                stats.update(returned=0, kept=0)
            return []
        rl.nominate_new = fake

        sent = self._build()

        self.assertIn('empty nomination list', sent[0])

    def test_alert_is_throttled_to_once_a_day(self):
        def fake(*_a, stats=None, **_k):
            if stats is not None:
                stats.update(returned=20, covered=20)
            return []
        rl.nominate_new = fake

        self.assertEqual(len(self._build()), 1)
        self.assertEqual(self._build(), [], 'second empty attempt must not re-alert')

    def test_unparseable_response_falls_back_to_the_next_provider(self):
        """Discovery used to abandon the slot on the first failure. mark_provider_failure
        alone does not reroute — it sets health to 'degraded', which provider_eligible still
        accepts — so only an explicit exclusion set moves the attempt to another provider."""
        tried = []

        def fake(_cfg, _key, _state, _n, _covered, provider='gemini', *_a, stats=None, **_k):
            tried.append(provider)
            if provider == 'gemini':
                raise rl.NominationFailed('gemini returned no JSON array of nominations')
            if stats is not None:
                stats.update(returned=1, kept=1)
            return [{'Company': 'Neo (NNN)', 'Country': 'Japan', 'Industry': 'x'}]

        rl.nominate_new = fake
        rl.select_provider = lambda _c, _s, _sched, excluded=(), **_k: next(
            (p for p in ('gemini', 'claude', 'codex') if p not in excluded), None)

        with mock.patch.object(rl, 'get_workflow', return_value=self.workflow):
            self._build()

        self.assertEqual(tried, ['gemini', 'claude'], 'must retry on the next provider')
        self.assertLess(self.workflow.recent_success_rate('gemini'), 1.0)
        # A fault is not saturation: it must NOT burn the daily empty-discovery budget.
        self.assertEqual(self.state['discovery']['empty_results'], 0)

    def test_gives_up_and_alerts_once_every_provider_has_failed(self):
        def fake(*_a, **_k):
            raise rl.NominationFailed('no JSON array')
        rl.nominate_new = fake
        rl.select_provider = lambda _c, _s, _sched, excluded=(), **_k: next(
            (p for p in ('gemini', 'claude', 'codex') if p not in excluded), None)

        with mock.patch.object(rl, 'get_workflow', return_value=self.workflow):
            sent = self._build()

        self.assertEqual(len(sent), 1)
        self.assertIn('no provider could nominate', sent[0].lower())
        self.assertEqual(self.state['discovery']['empty_results'], 0)


class LoadControlTests(unittest.TestCase):
    """The global kill switch: an absent or corrupt control file must never wedge the loop
    into a permanent halt."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._orig = rl.CONTROL_PATH
        rl.CONTROL_PATH = os.path.join(self.tmp.name, 'loop_control.json')

    def tearDown(self):
        rl.CONTROL_PATH = self._orig
        self.tmp.cleanup()

    def test_missing_file_is_not_halted(self):
        self.assertFalse(rl.load_control()['halted'])
        self.assertFalse(rl.halt_requested())

    def test_corrupt_file_is_not_halted(self):
        with open(rl.CONTROL_PATH, 'w', encoding='utf-8') as f:
            f.write('{ not valid json')
        self.assertFalse(rl.load_control()['halted'])

    def test_halt_round_trip(self):
        with open(rl.CONTROL_PATH, 'w', encoding='utf-8') as f:
            json.dump({'halted': True, 'by': 'dashboard'}, f)
        self.assertTrue(rl.load_control()['halted'])
        self.assertTrue(rl.halt_requested())
        self.assertEqual(rl.load_control()['by'], 'dashboard')


class RunBatchHaltTests(unittest.TestCase):
    """When the kill switch trips, run_batch must abort at the company boundary, requeue the
    untouched companies, and never invoke a provider."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.wf = WorkflowState(os.path.join(self.tmp.name, 'wf.sqlite3'))
        self._saved = (rl.BATCHES_DIR, rl.STATE_PATH, rl.halt_requested,
                       rl.send_telegram, rl.research_company)
        rl.BATCHES_DIR = self.tmp.name
        rl.STATE_PATH = os.path.join(self.tmp.name, 'loop_state.json')
        rl.send_telegram = lambda *a, **k: None
        self.researched = []

        def no_research(*a, **k):
            self.researched.append(a)
            raise AssertionError('research_company must not run once halted')
        rl.research_company = no_research

    def tearDown(self):
        (rl.BATCHES_DIR, rl.STATE_PATH, rl.halt_requested,
         rl.send_telegram, rl.research_company) = self._saved
        self.tmp.cleanup()

    def test_halt_requeues_untouched_companies(self):
        items = []
        for i in range(3):
            cid = self.wf.upsert_company({'Company': f'Company {i} (C{i})'})
            items.append({'Company': f'Company {i} (C{i})', '_company_id': cid})
        rl.halt_requested = lambda: True
        cfg = {'model': 'TestModel', 'delay_seconds': 0, 'batch_size': 20}
        state = {'batch_seq': 0, 'calls': {'gemini': 0, 'claude': 0, 'codex': 0}}
        applied = rl.run_batch(cfg, None, state, items, 'new', 'gemini', 'research', self.wf)
        self.assertEqual(applied, 0)
        self.assertEqual(self.researched, [])  # aborted before any provider call
        for i in range(3):
            row = self.wf.company_by_ticker(f'C{i}')
            self.assertEqual(row['research_status'], RESEARCH_PENDING)
            self.assertEqual(row['last_error_class'], 'halted')


class SelectMaintainableTests(unittest.TestCase):
    """Regression for the 2026-07-27 20:54/22:06 'No matching candidates' skips.

    The maintenance queue orders never-maintained-first, and DB rows whose ticker is absent
    from the CSV (csv-orphans) can never be maintained — so they permanently clog the head of
    the queue. The join must skip past them, and must report rather than silently drop.
    """

    @staticmethod
    def _rows(*tickers):
        return [{'ticker': t, 'id': i} for i, t in enumerate(tickers, 1)]

    @staticmethod
    def _csv(*tickers):
        return {t: {'Company': f'X ({t})', 'ticker': t} for t in tickers}

    def test_orphan_head_no_longer_blinds_the_slot(self):
        # The live bug: first 20 rows were all orphans, real work sat behind them.
        rows = self._rows(*(f'ORPHAN{i}' for i in range(20)), '2330', '2454.TW')
        items, dropped = rl.select_maintainable(rows, self._csv('2330', '2454.TW'), 20)
        self.assertEqual([i['ticker'] for i in items], ['2330', '2454.TW'])
        self.assertEqual(len(dropped), 20)

    def test_mixed_queue_fills_batch_in_order(self):
        rows = self._rows('A', 'GONE', 'B', 'C')
        items, dropped = rl.select_maintainable(rows, self._csv('A', 'B', 'C'), 2)
        self.assertEqual([i['ticker'] for i in items], ['A', 'B'])
        self.assertEqual(dropped, ['GONE'])

    def test_all_orphans_returns_empty_and_reports_every_drop(self):
        rows = self._rows('G1', 'G2', 'G3')
        items, dropped = rl.select_maintainable(rows, self._csv('OTHER'), 20)
        self.assertEqual(items, [])
        self.assertEqual(dropped, ['G1', 'G2', 'G3'])

    def test_company_id_is_attached_to_survivors(self):
        items, _ = rl.select_maintainable(self._rows('2330'), self._csv('2330'), 20)
        self.assertEqual(items[0]['_company_id'], 1)

    def test_empty_queue(self):
        self.assertEqual(rl.select_maintainable([], self._csv('2330'), 20), ([], []))

    def test_null_ticker_is_reported_by_id_not_crashed_on(self):
        items, dropped = rl.select_maintainable(
            [{'ticker': None, 'id': 7}], self._csv('2330'), 20)
        self.assertEqual(items, [])
        self.assertEqual(dropped, ['id=7'])


class ClaudeToolBoundaryTests(unittest.TestCase):
    """The loop's claude call runs unattended on prompts built from unsanitized corpus text.

    Omitting --allowedTools does NOT remove tools: it is a permission-rule flag, while --tools
    decides which tools exist, and `claude -p` inherits ~/.claude/settings.json where
    defaultMode is bypassPermissions. Verified empirically on 2026-07-27 — the pre-hardening
    argv could read an arbitrary file off disk. These tests pin the capability boundary.
    """

    def setUp(self):
        path = os.path.join(os.path.dirname(__file__), '..', 'scripts', 'config.json')
        with open(path, encoding='utf-8') as f:
            self.args = json.load(f)['claude_args']

    def test_tools_are_restricted_to_websearch(self):
        self.assertIn('--tools', self.args)
        self.assertEqual(self.args[self.args.index('--tools') + 1], 'WebSearch',
                         'the loop needs WebSearch and nothing else')

    def test_settings_inheritance_is_severed(self):
        # Otherwise an edit to ~/.claude/settings.json silently re-widens this call.
        self.assertIn('--setting-sources', self.args)
        self.assertEqual(self.args[self.args.index('--setting-sources') + 1], '')

    def test_no_filesystem_or_shell_tool_is_enabled(self):
        enabled = self.args[self.args.index('--tools') + 1].split(',')
        for tool in ('Bash', 'Read', 'Write', 'Edit', 'WebFetch', 'Task', 'NotebookEdit'):
            self.assertNotIn(tool, enabled, f'{tool} must not be reachable from the loop')

    def test_allowedtools_alone_is_not_relied_on(self):
        """--allowedTools narrows nothing without --tools; its presence here would signal
        someone reintroduced the mistaken belief that it constrains the surface."""
        if '--allowedTools' in self.args:
            self.assertIn('--tools', self.args,
                          '--allowedTools without --tools is security theatre')

    def test_web_fetch_is_absent_so_preapproved_hosts_are_moot(self):
        # WebFetch carries ~90 hardcoded preapproved hosts that bypass any allowlist, and is
        # the only local-egress path. WebSearch is server-side and returns titles/URLs only.
        self.assertNotIn('WebFetch', ' '.join(self.args))


if __name__ == '__main__':
    unittest.main(verbosity=2)


class DiscoveryMetricTests(unittest.TestCase):
    """Discovery success must mean NEW companies, not names returned.

    Regression for 2026-08-11/12: nominations of already-covered tickers reset empty_results
    to 0, so daily_empty_discovery_limit never tripped and a two-day corpus stall went
    unreported while every research slot fell back to maintenance.
    """

    def setUp(self):
        self._saved = rl.save_state
        rl.save_state = lambda _s: None          # never touch the real loop_state.json
        self.state = {'discovery': {'attempts': 0, 'empty_results': 0}}

    def tearDown(self):
        rl.save_state = self._saved

    def test_all_duplicate_nominations_count_as_empty(self):
        rl.record_discovery_result(self.state, ['dup'] * 20, created=0)
        self.assertEqual(self.state['discovery']['empty_results'], 1)

    def test_new_companies_reset_the_counter(self):
        self.state['discovery']['empty_results'] = 1
        rl.record_discovery_result(self.state, ['a'] * 20, created=5)
        self.assertEqual(self.state['discovery']['empty_results'], 0)

    def test_saturation_eventually_trips_the_budget_gate(self):
        cfg = {'daily_discovery_attempt_budget': 6, 'daily_empty_discovery_limit': 2}
        self.assertTrue(rl.discovery_budget_available(cfg, self.state))
        for _ in range(2):
            rl.record_discovery_result(self.state, ['dup'] * 20, created=0)
        self.assertFalse(rl.discovery_budget_available(cfg, self.state),
                         'two barren runs must stop further spend')

    def test_returns_the_produced_count(self):
        self.assertEqual(rl.record_discovery_result(self.state, ['a'], created=7), 7)

    def test_omitting_created_keeps_legacy_nomination_behaviour(self):
        rl.record_discovery_result(self.state, ['a', 'b'])
        self.assertEqual(self.state['discovery']['empty_results'], 0)
        rl.record_discovery_result(self.state, [])
        self.assertEqual(self.state['discovery']['empty_results'], 1)


class DiscoveryReasonTests(unittest.TestCase):
    """`nominated 0` used to mean either "provider is broken" or "slice is saturated",
    with no way to tell them apart and no alert for either."""

    def test_empty_provider_response(self):
        self.assertIn('empty nomination list', rl._discovery_reason({'returned': 0}))

    def test_everything_already_covered(self):
        msg = rl._discovery_reason({'returned': 20, 'kept': 0, 'covered': 20})
        self.assertIn('20 nominations returned, none usable', msg)
        self.assertIn('20 already covered', msg)

    def test_off_focus_and_malformed_are_broken_out(self):
        msg = rl._discovery_reason({'returned': 9, 'covered': 4, 'off_focus': 3,
                                    'malformed': 2})
        self.assertIn('4 already covered', msg)
        self.assertIn('3 off-focus', msg)
        self.assertIn('2 malformed', msg)


class NominateNewFailureTests(unittest.TestCase):
    """A non-array response must be a provider FAULT, not a silent zero."""

    CFG = {'daily_call_budgets': {'gemini': 10}}

    def _run(self, response, **kw):
        state = {'calls': {'gemini': 0}}
        with mock.patch.object(rl, 'llm_call', return_value=response):
            return rl.nominate_new(self.CFG, None, state, 5, ['Acme (AAA)'], 'gemini', **kw)

    def test_non_array_response_raises(self):
        with self.assertRaises(rl.NominationFailed):
            self._run('I could not find any companies.')

    def test_stats_report_why_nominations_were_dropped(self):
        stats = {}
        items = self._run(
            '[{"Country":"Japan","Company":"Acme (AAA)","Industry":"x"},'
            ' {"Country":"France","Company":"Zeta (ZZZ)","Industry":"x"},'
            ' {"Country":"Japan","Company":"Neo (NNN)","Industry":"x"}]',
            focus_country='Japan', stats=stats)
        self.assertEqual([i['Company'] for i in items], ['Neo (NNN)'])
        self.assertEqual(stats['returned'], 3)
        self.assertEqual(stats['covered'], 1)     # AAA already in `covered`
        self.assertEqual(stats['off_focus'], 1)   # France, against a Japan focus

    def test_show_tickers_trims_the_prompt_but_not_the_dedupe(self):
        seen = {}

        def capture(cfg, key, state, prompt, provider, use_search=False, **kw):
            seen['prompt'] = prompt
            return '[{"Country":"Japan","Company":"Acme (AAA)","Industry":"x"}]'

        state = {'calls': {'gemini': 0}}
        with mock.patch.object(rl, 'llm_call', side_effect=capture):
            items = rl.nominate_new(self.CFG, None, state, 5, ['Acme (AAA)', 'Zeta (ZZZ)'],
                                    'gemini', show_tickers=['ZZZ'])
        self.assertNotIn('AAA', seen['prompt'])   # trimmed from what the model sees
        self.assertEqual(items, [])               # but still deduped against the full set


class DiscoveryFocusTests(unittest.TestCase):
    """Coverage is skewed (Taiwan 538, USA 424, Japan 182 …), so undirected nomination
    re-proposes saturated names. Each attempt probes a different slice instead."""

    ROTATION = {'discovery_focus_rotation': [['Japan', ''], ['South Korea', ''],
                                             ['USA', '生技新藥']]}

    def _state(self, cursor, attempts=None):
        # The rotation is driven by discovery_cursor, which persists across midnight;
        # `attempts` resets daily and must NOT influence the slice.
        return {'discovery': {'attempts': attempts if attempts is not None else cursor,
                              'empty_results': 0},
                'discovery_cursor': cursor}

    def test_rotation_advances_with_each_attempt(self):
        seen = [rl.discovery_focus(self.ROTATION, self._state(i)) for i in (1, 2, 3)]
        self.assertEqual(seen, [('Japan', ''), ('South Korea', ''), ('USA', '生技新藥')])

    def test_rotation_wraps(self):
        self.assertEqual(rl.discovery_focus(self.ROTATION, self._state(4)), ('Japan', ''))

    def test_rotation_survives_the_daily_attempt_reset(self):
        """The 2026-08-25/26 stall: daily_empty_discovery_limit=2 stopped discovery after
        two attempts, and indexing off the midnight-reset `attempts` counter restarted the
        rotation at Japan every single day. Ten of the twelve slices became unreachable."""
        # Day 2, attempt 1: attempts has reset to 1, but the cursor carried on to 3.
        self.assertEqual(rl.discovery_focus(self.ROTATION, self._state(3, attempts=1)),
                         ('USA', '生技新藥'))

    def test_missing_cursor_starts_at_the_first_slice(self):
        self.assertEqual(rl.discovery_focus(self.ROTATION, {'discovery': {}}), ('Japan', ''))

    def test_explicit_slot_focus_overrides_rotation(self):
        self.assertEqual(
            rl.discovery_focus(self.ROTATION, self._state(1), 'Taiwan', '半導體'),
            ('Taiwan', '半導體'))

    def test_no_rotation_configured_is_global_search(self):
        self.assertEqual(rl.discovery_focus({}, self._state(1)), ('', ''))

    def test_plain_string_entry_is_treated_as_a_country(self):
        self.assertEqual(
            rl.discovery_focus({'discovery_focus_rotation': ['Japan']}, self._state(1)),
            ('Japan', ''))

    def test_configured_rotation_is_valid(self):
        """The shipped config must actually parse into usable slices."""
        with open(os.path.join(os.path.dirname(__file__), '..', 'scripts',
                               'config.json'), encoding='utf-8') as f:
            cfg = json.load(f)
        rotation = cfg.get('discovery_focus_rotation') or []
        self.assertTrue(rotation, 'rotation should be configured')
        for i in range(1, len(rotation) + 1):
            country, industry = rl.discovery_focus(cfg, self._state(i))
            self.assertTrue(country or industry, f'slice {i} is empty')


class ConfigValidationTests(unittest.TestCase):
    """A partial config must fail loudly, never degrade silently.

    Regression for 2026-08-13: a config swap replaced 18 operational keys with a different
    schema. The loop kept running — killing slots one by one on KeyError ('model' and
    'batch_size' appear verbatim as skip reasons in daily_slots), while silently dropping the
    claude tool boundary and the daily schedule, which fail via .get() defaults instead.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._saved = rl.CONFIG_PATH
        with open(os.path.join(os.path.dirname(__file__), '..', 'scripts',
                               'config.json.clean'), encoding='utf-8') as f:
            self.good = json.load(f)

    def tearDown(self):
        rl.CONFIG_PATH = self._saved
        self.tmp.cleanup()

    def _write(self, cfg):
        path = os.path.join(self.tmp.name, 'c.json')
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(cfg, f)
        rl.CONFIG_PATH = path
        return path

    def test_reference_config_is_valid(self):
        self._write(self.good)
        self.assertEqual(len(rl.load_config()), len(self.good))

    def test_each_required_key_is_individually_enforced(self):
        for key in rl.REQUIRED_CONFIG_KEYS:
            cfg = dict(self.good)
            cfg.pop(key)
            self._write(cfg)
            with self.assertRaises(rl.ConfigError, msg=f'{key} should be required') as ctx:
                rl.load_config()
            self.assertIn(key, str(ctx.exception))

    def test_the_actual_2026_08_13_config_is_refused(self):
        """The precise shape that broke production."""
        broken = {'backend': 'cli', 'cli_command': 'agy', 'gemini_model': 'gemini-2.5-pro',
                  'dry_run': False, 'max_companies_per_batch': 20, 'max_retries': 2}
        self._write(broken)
        with self.assertRaises(rl.ConfigError) as ctx:
            rl.load_config()
        for lost in ('schedule', 'claude_args'):
            self.assertIn(lost, str(ctx.exception))

    def test_empty_schedule_is_refused(self):
        cfg = dict(self.good, schedule=[])
        self._write(cfg)
        with self.assertRaises(rl.ConfigError):
            rl.load_config()

    def test_missing_file_and_bad_json_are_refused_clearly(self):
        rl.CONFIG_PATH = os.path.join(self.tmp.name, 'absent.json')
        with self.assertRaises(rl.ConfigError):
            rl.load_config()
        path = os.path.join(self.tmp.name, 'bad.json')
        with open(path, 'w') as f:
            f.write('{not json')
        rl.CONFIG_PATH = path
        with self.assertRaises(rl.ConfigError):
            rl.load_config()

    def test_error_names_the_recovery_reference(self):
        self._write({'backend': 'cli'})
        with self.assertRaises(rl.ConfigError) as ctx:
            rl.load_config()
        self.assertIn('config.json.clean', str(ctx.exception))


class MandatoryClaudeFlagTests(unittest.TestCase):
    """The tool boundary must live in CODE, not config.

    It was config-only until 2026-08-13, so one file swap silently downgraded the research
    call to a fully-capable `claude -p` — an argv canary-proved on 2026-07-27 to read
    arbitrary local files, running unattended on untrusted corpus text.
    """

    def _flags(self, args):
        out = rl.enforce_claude_flags(args)
        return out[out.index('--tools') + 1], out[out.index('--setting-sources') + 1]

    def test_added_when_config_omits_them(self):
        self.assertEqual(self._flags(['--model', 'sonnet']), ('WebSearch', ''))

    def test_preserved_when_config_supplies_them(self):
        self.assertEqual(
            self._flags(['--tools', 'WebSearch', '--setting-sources', '', '--model', 'sonnet']),
            ('WebSearch', ''))

    def test_a_weakened_config_value_is_overridden(self):
        # '--tools default' would restore Bash/Read/Write/WebFetch; must not be honoured.
        self.assertEqual(self._flags(['--tools', 'default', '--setting-sources', 'user']),
                         ('WebSearch', ''))

    def test_empty_and_none_are_safe(self):
        self.assertEqual(self._flags([]), ('WebSearch', ''))
        self.assertEqual(self._flags(None), ('WebSearch', ''))

    def test_other_config_flags_survive(self):
        out = rl.enforce_claude_flags(['--model', 'sonnet', '--permission-mode',
                                       'bypassPermissions'])
        self.assertIn('--model', out)
        self.assertIn('bypassPermissions', out)

    def test_load_config_applies_enforcement(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            with open(os.path.join(os.path.dirname(__file__), '..', 'scripts',
                                   'config.json.clean'), encoding='utf-8') as f:
                cfg = json.load(f)
            cfg['claude_args'] = ['--model', 'sonnet']      # boundary stripped
            path = os.path.join(tmp.name, 'c.json')
            with open(path, 'w') as f:
                json.dump(cfg, f)
            saved, rl.CONFIG_PATH = rl.CONFIG_PATH, path
            try:
                loaded = rl.load_config()
            finally:
                rl.CONFIG_PATH = saved
            self.assertIn('--tools', loaded['claude_args'])
            self.assertEqual(loaded['claude_args'][loaded['claude_args'].index('--tools') + 1],
                             'WebSearch')
        finally:
            tmp.cleanup()


class ConfigTypeValidationTests(unittest.TestCase):
    """Key PRESENCE is not enough — wrong TYPES degrade silently too.

    The 2026-08-13 replacement config kept `cli_extra_args` but changed it from a list to "".
    `cmd += ""` iterates an empty string and appends nothing, so the agy call silently lost
    `--mode plan` (its read-only mode) and `--print-timeout`. A presence-only check — the first
    version of this validation — accepted that config unchanged.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._saved = rl.CONFIG_PATH
        with open(os.path.join(os.path.dirname(__file__), '..', 'scripts',
                               'config.json.clean'), encoding='utf-8') as f:
            self.good = json.load(f)

    def tearDown(self):
        rl.CONFIG_PATH = self._saved
        self.tmp.cleanup()

    def _load(self, **overrides):
        cfg = json.loads(json.dumps(self.good))
        cfg.update(overrides)
        path = os.path.join(self.tmp.name, 'c.json')
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(cfg, f)
        rl.CONFIG_PATH = path
        return rl.load_config()

    def test_the_exact_cli_extra_args_regression(self):
        with self.assertRaises(rl.ConfigError) as ctx:
            self._load(cli_extra_args="")
        self.assertIn('cli_extra_args', str(ctx.exception))

    def test_a_string_that_would_splat_per_character_is_refused(self):
        with self.assertRaises(rl.ConfigError):
            self._load(cli_extra_args="--mode plan")

    def test_every_command_line_list_is_type_checked(self):
        for key in ('schedule', 'claude_args', 'cli_extra_args', 'provider_fallback_order',
                    'discovery_focus_rotation'):
            with self.assertRaises(rl.ConfigError, msg=key):
                self._load(**{key: 'a string'})

    def test_scalar_and_mapping_types_are_checked(self):
        with self.assertRaises(rl.ConfigError):
            self._load(batch_size='20')
        with self.assertRaises(rl.ConfigError):
            self._load(daily_call_budgets=[160, 150, 80])
        with self.assertRaises(rl.ConfigError):
            self._load(maintenance_cadence_days=[7, 14, 28])

    def test_delay_seconds_accepts_int_or_float(self):
        self.assertEqual(self._load(delay_seconds=10)['delay_seconds'], 10)
        self.assertEqual(self._load(delay_seconds=2.5)['delay_seconds'], 2.5)

    def test_optional_keys_are_only_checked_when_present(self):
        cfg = json.loads(json.dumps(self.good))
        cfg.pop('cli_extra_args', None)
        path = os.path.join(self.tmp.name, 'c.json')
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(cfg, f)
        rl.CONFIG_PATH = path
        self.assertNotIn('cli_extra_args', rl.load_config())

    def test_the_shipped_configs_pass_type_validation(self):
        scripts_dir = os.path.join(os.path.dirname(__file__), '..', 'scripts')
        for name in ('config.json', 'config.json.clean'):
            rl.CONFIG_PATH = os.path.join(scripts_dir, name)
            rl.load_config()          # must not raise


class ResetDurationParsingTests(unittest.TestCase):
    """Antigravity reports exhaustion as a relative duration ("Resets in 75h59m52s"). Until
    2026-08-30 that fell through to the 30-minute rate_limit default, so a 76-hour Gemini outage
    was re-probed every slot (3 agy calls + 180 s of backoff each time)."""

    def _hours_from_now(self, iso):
        import datetime as dt
        value = dt.datetime.fromisoformat(iso)
        return (value - dt.datetime.now(dt.timezone.utc)).total_seconds() / 3600

    def test_full_duration_is_parsed(self):
        iso = rl._reset_from_message(
            'Error: Individual quota reached. Please upgrade your subscription to increase '
            'your limits. Resets in 75h59m52s.')
        self.assertIsNotNone(iso)
        self.assertAlmostEqual(self._hours_from_now(iso), 76.0, delta=0.05)

    def test_hours_and_minutes_only(self):
        iso = rl._reset_from_message('Resets in 4h12m')
        self.assertAlmostEqual(self._hours_from_now(iso), 4.2, delta=0.05)

    def test_minutes_only_lowercase(self):
        iso = rl._reset_from_message('quota hit, resets in 30m')
        self.assertAlmostEqual(self._hours_from_now(iso), 0.5, delta=0.05)

    def test_no_duration_returns_none(self):
        self.assertIsNone(rl._reset_from_message('Resource has been exhausted'))

    def test_existing_claude_and_codex_forms_still_work(self):
        self.assertIsNotNone(rl._reset_from_message('resets 7am'))
        self.assertIsNotNone(rl._reset_from_message('try again at Jul 25th, 2026 11:24 AM'))

    def test_individual_quota_message_is_a_long_cooldown(self):
        exc = rl._quota_error('gemini', 'Error: Individual quota reached. Resets in 75h59m52s.')
        self.assertEqual(exc.error_class, 'usage_limit')
        self.assertEqual(exc.provider, 'gemini')
        self.assertAlmostEqual(self._hours_from_now(exc.cooldown_until), 76.0, delta=0.05)


class AgyClaudeCallTests(unittest.TestCase):
    """Claude through the Antigravity CLI reuses the agy call path, bills its own counter and
    raises quota errors under its own provider name."""

    def setUp(self):
        self.cfg = {'cli_command': 'agy', 'cli_extra_args': ['--mode', 'plan'],
                    'model': 'Gemini 3.5 Flash (High)'}
        self.state = {'calls': {'gemini': 0, 'agy_claude': 0, 'claude': 0, 'codex': 0}}
        self.saved_save = rl.save_state
        rl.save_state = lambda _s: None

    def tearDown(self):
        rl.save_state = self.saved_save

    def test_model_override_and_separate_counter(self):
        seen = {}

        def fake_run(cmd, **_kw):
            seen['cmd'] = cmd
            return mock.Mock(returncode=0, stdout='answer', stderr='')

        with mock.patch.object(rl.subprocess, 'run', side_effect=fake_run):
            out = rl.gemini_call_cli(self.cfg, self.state, 'p', provider='agy_claude',
                                     model='claude-sonnet-4-6')
        self.assertEqual(out, 'answer')
        cmd = seen['cmd']
        self.assertEqual(cmd[cmd.index('--model') + 1], 'claude-sonnet-4-6')
        self.assertIn('--mode', cmd, 'plan-mode args must still be applied')
        self.assertEqual(self.state['calls']['agy_claude'], 1)
        self.assertEqual(self.state['calls']['gemini'], 0)

    def test_default_path_is_unchanged_for_gemini(self):
        seen = {}

        def fake_run(cmd, **_kw):
            seen['cmd'] = cmd
            return mock.Mock(returncode=0, stdout='answer', stderr='')

        with mock.patch.object(rl.subprocess, 'run', side_effect=fake_run):
            rl.gemini_call_cli(self.cfg, self.state, 'p')
        cmd = seen['cmd']
        self.assertEqual(cmd[cmd.index('--model') + 1], 'Gemini 3.5 Flash (High)')
        self.assertEqual(self.state['calls']['gemini'], 1)
        self.assertEqual(self.state['calls']['agy_claude'], 0)

    def test_quota_error_is_billed_to_agy_claude(self):
        def fake_run(cmd, **_kw):
            return mock.Mock(returncode=1, stdout='',
                             stderr='Error: Individual quota reached. Resets in 4h0m0s.')

        with mock.patch.object(rl.subprocess, 'run', side_effect=fake_run), \
                mock.patch.object(rl.time, 'sleep'):
            with self.assertRaises(rl.BudgetExhausted) as ctx:
                rl.gemini_call_cli(self.cfg, self.state, 'p', provider='agy_claude',
                                   model='claude-sonnet-4-6')
        self.assertEqual(ctx.exception.provider, 'agy_claude')
        self.assertEqual(ctx.exception.error_class, 'usage_limit')
        self.assertEqual(self.state['calls']['agy_claude'], 0)

    def test_llm_call_routes_agy_claude(self):
        seen = {}

        def fake_cli(cfg, state, prompt, json_schema=None, provider='gemini', model=None):
            seen.update(provider=provider, model=model)
            return 'ok'

        cfg = dict(self.cfg, daily_call_budgets={'agy_claude': 60}, agy_claude_model='claude-sonnet-4-6')
        workflow = mock.Mock()
        with mock.patch.object(rl, 'gemini_call_cli', side_effect=fake_cli), \
                mock.patch.object(rl, 'get_workflow', return_value=workflow):
            rl.llm_call(cfg, None, self.state, 'p', provider='agy_claude')
        self.assertEqual(seen, {'provider': 'agy_claude', 'model': 'claude-sonnet-4-6'})
        workflow.mark_provider_success.assert_called_once_with('agy_claude')


class AgyClaudeSelectionTests(unittest.TestCase):
    """With the Gemini bank dry and Codex cooling down, the fallback ranking must reach Claude
    through Antigravity before Peter's own Claude subscription, and then Claude once that is out."""

    def setUp(self):
        import datetime as dt
        self.tmp = tempfile.TemporaryDirectory()
        self.workflow = WorkflowState(os.path.join(self.tmp.name, 'workflow.sqlite3'))
        future = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=70)).isoformat(timespec='seconds')
        self.workflow.mark_provider_failure('gemini', 'usage_limit', 'Individual quota reached', future)
        self.workflow.mark_provider_failure('codex', 'usage_limit', 'usage limit', future)
        self.future = future
        self.cfg = {'daily_call_budgets': {'gemini': 300, 'agy_claude': 60, 'claude': 150, 'codex': 80},
                    'provider_fallback_order': ['gemini', 'agy_claude', 'claude', 'codex'],
                    'provider_low_budget_threshold': 0.1}
        self.state = {'calls': {'gemini': 100, 'agy_claude': 0, 'claude': 57, 'codex': 26}}

    def tearDown(self):
        self.tmp.cleanup()

    def _select(self):
        with mock.patch.object(rl, 'get_workflow', return_value=self.workflow):
            return rl.select_provider(self.cfg, self.state, 'gemini')

    def test_provider_row_exists_for_agy_claude(self):
        self.assertIsNotNone(self.workflow.provider_state('agy_claude'))

    def test_gemini_slot_falls_to_agy_claude_first(self):
        self.assertEqual(self._select(), 'agy_claude')

    def test_then_claude_when_agy_claude_cools_down(self):
        self.workflow.mark_provider_failure('agy_claude', 'usage_limit', 'Resets in 4h', self.future)
        self.assertEqual(self._select(), 'claude')

    def test_then_claude_when_agy_claude_budget_is_spent(self):
        self.state['calls']['agy_claude'] = 60
        self.assertEqual(self._select(), 'claude')

    def test_state_file_without_the_new_counter_migrates(self):
        import datetime as dt
        path = os.path.join(self.tmp.name, 'loop_state.json')
        with open(path, 'w', encoding='utf-8') as f:
            json.dump({'date': dt.date.today().isoformat(),
                       'calls': {'gemini': 100, 'claude': 57, 'codex': 26}, 'batch_seq': 416}, f)
        saved = rl.STATE_PATH
        rl.STATE_PATH = path
        try:
            state = rl.load_state()
        finally:
            rl.STATE_PATH = saved
        self.assertEqual(state['calls']['agy_claude'], 0)
        self.assertEqual(state['calls']['gemini'], 100)
        self.assertEqual(state['batch_seq'], 416)
