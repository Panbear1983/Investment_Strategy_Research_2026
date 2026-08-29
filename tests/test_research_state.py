import json
import os
import sqlite3
import sys
import tempfile
import unittest
import datetime as dt
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

import research_loop as rl
import dashboard
from research_state import (DEEP_RESEARCHED, EXCLUDED, MAINT_NOT_DUE, MAINT_SCREEN_DUE,
                            MAINT_UPDATE_DUE, RESEARCH_PENDING, TICKER_REVIEW,
                            WorkflowState, normalize_source_urls)


class WorkflowLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = WorkflowState(os.path.join(self.tmp.name, 'workflow.sqlite3'))

    def tearDown(self):
        self.tmp.cleanup()

    def test_retry_cap_preserves_half_batch_for_ordinary_work(self):
        for i in range(12):
            cid = self.state.upsert_company({'Company': f'Retry {i} (R{i})'})
            self.state.requeue_research(cid, 'validation', 'bad output', count_attempt=True)
        for i in range(12):
            self.state.upsert_company({'Company': f'New {i} (N{i})'})
        queue = self.state.research_queue(20, retry_cap=10)
        self.assertEqual(sum(r['attempt_count'] > 0 for r in queue), 10)
        self.assertEqual(sum(r['attempt_count'] == 0 for r in queue), 10)

    def test_completed_and_maintenance_are_independent_states(self):
        cid = self.state.upsert_company(
            {'Company': 'Alpha (AAA)', 'Tier': '龍頭股'}, DEEP_RESEARCHED)
        self.state.begin_maintenance_cycle('2026-07-20', 100, {'leader': 7, 'unknown': 28})
        row = self.state.company_by_ticker('AAA')
        self.assertEqual(row['research_status'], DEEP_RESEARCHED)
        self.assertEqual(row['maintenance_status'], MAINT_SCREEN_DUE)
        self.state.record_screening(cid, '2026-07-20', 'gemini', True, 'material news')
        row = self.state.company_by_ticker('AAA')
        self.assertEqual(row['research_status'], DEEP_RESEARCHED)
        self.assertEqual(row['maintenance_status'], MAINT_UPDATE_DUE)
        self.state.mark_maintained(cid)
        self.assertEqual(self.state.company_by_ticker('AAA')['maintenance_status'], MAINT_NOT_DUE)

    def test_screening_persists_ordered_https_sources(self):
        cid = self.state.upsert_company({'Company': 'Alpha (AAA)'}, DEEP_RESEARCHED)
        sources = ['https://example.com/first', 'https://example.com/second']

        self.state.record_screening(
            cid, '2026-07-20', 'gemini', True, 'material news',
            '2026-07-19', sources)

        with self.state.connect() as db:
            row = db.execute("SELECT source_urls_json FROM maintenance_screenings "
                             "WHERE company_id=?", (cid,)).fetchone()
        self.assertEqual(json.loads(row['source_urls_json']), sources)

    def test_screening_rejects_https_url_without_a_host(self):
        with self.assertRaisesRegex(ValueError, 'HTTPS URL'):
            normalize_source_urls(['https://user@'])

    def test_screening_deduplicates_source_urls_in_input_order(self):
        self.assertEqual(
            normalize_source_urls([
                'https://example.com/first', 'https://example.com/second',
                'https://example.com/first',
            ]),
            ['https://example.com/first', 'https://example.com/second'])

    def test_screening_rejects_non_https_source_urls(self):
        with self.assertRaisesRegex(ValueError, 'HTTPS URL'):
            normalize_source_urls(['http://example.com/news'])

    def test_screening_rejects_more_than_five_source_urls(self):
        with self.assertRaisesRegex(ValueError, 'at most five'):
            normalize_source_urls([f'https://example.com/{index}' for index in range(6)])

    def test_schema_migrates_legacy_screening_sources_idempotently(self):
        legacy_path = os.path.join(self.tmp.name, 'legacy.sqlite3')
        with sqlite3.connect(legacy_path) as db:
            db.execute("""CREATE TABLE maintenance_screenings (
                id INTEGER PRIMARY KEY, company_id INTEGER NOT NULL, week_start TEXT NOT NULL,
                provider TEXT NOT NULL, needs_update INTEGER NOT NULL,
                reason TEXT NOT NULL DEFAULT '', evidence_date TEXT, created_at TEXT NOT NULL,
                UNIQUE(company_id, week_start))""")
            db.execute("""INSERT INTO maintenance_screenings
                (company_id, week_start, provider, needs_update, created_at)
                VALUES (1, '2026-07-20', 'gemini', 1, '2026-07-20T00:00:00+00:00')""")

        migrated = WorkflowState(legacy_path)
        WorkflowState(legacy_path)
        company_id = migrated.upsert_company({'Company': 'Legacy (LEG)'}, DEEP_RESEARCHED)
        batch_id = migrated.create_batch(
            1, 'Legacy maintenance', 'maintenance', 'maintenance', 'gemini',
            [{'Company': 'Legacy (LEG)', '_company_id': company_id}])

        with migrated.connect() as db:
            screening = db.execute("SELECT source_urls_json FROM maintenance_screenings "
                                   "WHERE company_id=?", (company_id,)).fetchone()
            batch = db.execute('SELECT item_json FROM batch_items WHERE batch_id=?',
                               (batch_id,)).fetchone()
        self.assertEqual(json.loads(screening['source_urls_json']), [])
        self.assertEqual(json.loads(batch['item_json'])['_maintenance_screening']['source_urls'], [])

    def test_digest_ledger_records_a_state_per_digest_and_recipient(self):
        recipient = {'label': 'Peter', 'chat_id': '42'}

        self.state.record_digest_delivery_state('2026-07-25', recipient, 'claimed')

        self.assertEqual(
            self.state.digest_delivery_status('2026-07-25', [recipient]),
            [{'label': 'Peter', 'chat_id': '42', 'status': 'claimed'}],
        )

    def test_daily_digest_claim_is_atomic_and_refuses_second_same_date_recipient(self):
        recipient = {'label': 'Group', 'chat_id': '-10042'}
        payload = {'posts': [{'ticker': 'NEW.TW'}]}

        first = self.state.claim_digest_delivery('2026-07-25', recipient, payload)
        second = self.state.claim_digest_delivery('2026-07-25', recipient, payload)

        self.assertTrue(first['claimed'])
        self.assertFalse(second['claimed'])
        self.assertEqual(first['payload_sha256'], second['payload_sha256'])

    def test_daily_digest_no_updates_is_a_terminal_non_delivery_outcome(self):
        recipient = {'label': 'Group', 'chat_id': '-10042'}

        self.state.record_digest_no_updates('2026-07-25', recipient)

        self.assertEqual(
            self.state.digest_delivery_status('2026-07-25', [recipient]),
            [{'label': 'Group', 'chat_id': '-10042', 'status': 'skipped_no_updates'}],
        )

    def test_schema_migrates_legacy_digest_ledger_and_preserves_existing_state(self):
        legacy_path = os.path.join(self.tmp.name, 'legacy-digest.sqlite3')
        with sqlite3.connect(legacy_path) as db:
            db.execute("""CREATE TABLE maintenance_digest_deliveries (
                digest_date TEXT NOT NULL, recipient_chat_id TEXT NOT NULL,
                recipient_label TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending'
                    CHECK(status IN ('pending', 'claimed', 'sent', 'failed')),
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                PRIMARY KEY(digest_date, recipient_chat_id))""")
            db.execute("""INSERT INTO maintenance_digest_deliveries
                VALUES ('2026-07-25', '-10042', 'Group', 'sent', 'old', 'old')""")

        migrated = WorkflowState(legacy_path)

        self.assertEqual(
            migrated.digest_delivery_status('2026-07-25', [{'label': 'Group', 'chat_id': '-10042'}]),
            [{'label': 'Group', 'chat_id': '-10042', 'status': 'sent'}],
        )
        migrated.record_digest_no_updates('2026-07-26', {'label': 'Group', 'chat_id': '-10042'})
        self.assertEqual(
            migrated.digest_delivery_status('2026-07-26', [{'label': 'Group', 'chat_id': '-10042'}]),
            [{'label': 'Group', 'chat_id': '-10042', 'status': 'skipped_no_updates'}],
        )

    def test_maintenance_queues_require_every_selected_focus_dimension(self):
        companies = [
            ('Taiwan Chip (TC)', 'Taiwan', 'Semiconductor'),
            ('US Chip (UC)', 'USA', 'Semiconductor'),
            ('Taiwan Cloud (TW)', 'Taiwan', 'Cloud'),
        ]
        ids = []
        for name, country, industry in companies:
            ids.append(self.state.upsert_company(
                {'Company': name, 'Country': country, 'Industry': industry},
                DEEP_RESEARCHED))
        self.state.begin_maintenance_cycle(
            '2026-07-20', 100, {'leader': 7, 'unknown': 28})
        screened = self.state.maintenance_screen_queue(
            20, country='taiwan', industry='semiconductor')
        self.assertEqual([r['ticker'] for r in screened], ['TC'])
        for cid in ids:
            self.state.record_screening(cid, '2026-07-20', 'gemini', True)
        updates = self.state.maintenance_update_queue(
            20, country='TAIWAN', industry='SEMICONDUCTOR')
        self.assertEqual([r['ticker'] for r in updates], ['TC'])

    def _screened_leader(self, name, screened_on):
        cid = self.state.upsert_company({'Company': name, 'Tier': '龍頭股'}, DEEP_RESEARCHED)
        with self.state.connect() as db:
            db.execute('UPDATE companies SET last_screened_at=?, maintenance_status=? '
                       'WHERE id=?', (f'{screened_on}T00:00:00+00:00', MAINT_NOT_DUE, cid))
        return cid

    def test_cadence_measures_staleness_from_today_not_the_week_start(self):
        """The 2026-08-25/26 stall: with the tiered branch active, staleness was measured
        from week_start, so a 7-day-tier company screened on the 18th read as 6 days old all
        week and never came due. 26 consecutive slots skipped with 'No matching candidates'.
        """
        self._screened_leader('Stale (STL)', '2026-08-18')
        # Capacity of 0 forces the tiered-cadence branch (total > weekly_capacity).
        cadence = {'leader': 7, 'unknown': 28}

        self.state.begin_maintenance_cycle('2026-08-24', 0, cadence,
                                           today=dt.date(2026, 8, 24))
        self.assertEqual(self.state.maintenance_screen_queue(20), [],
                         'Monday: 6 days stale, correctly not yet due')

        self.state.begin_maintenance_cycle('2026-08-24', 0, cadence,
                                           today=dt.date(2026, 8, 26))
        self.assertEqual([r['ticker'] for r in self.state.maintenance_screen_queue(20)],
                         ['STL'], 'Wednesday: 8 days stale, must be due mid-week')

    def test_cadence_defaults_to_the_real_today(self):
        self._screened_leader('Ancient (ANC)', '2020-01-01')
        self.state.begin_maintenance_cycle('2026-08-24', 0, {'leader': 7, 'unknown': 28})
        self.assertEqual([r['ticker'] for r in self.state.maintenance_screen_queue(20)],
                         ['ANC'])

    def test_reaper_reclaims_rows_abandoned_mid_update(self):
        """begin_maintenance_cycle skips 'updating' rows, so without a reaper a row orphaned
        by a dead process is invisible to BOTH queues forever."""
        cid = self.state.upsert_company({'Company': 'Stuck (STK)'}, DEEP_RESEARCHED)
        self.state.mark_maintenance_updating(cid)
        with self.state.connect() as db:
            db.execute("UPDATE companies SET updated_at='2020-01-01T00:00:00+00:00' "
                       'WHERE id=?', (cid,))
        self.assertEqual(self.state.maintenance_update_queue(20), [])

        self.assertEqual(self.state.reap_stuck_updating(12), 1)
        self.assertEqual([r['ticker'] for r in self.state.maintenance_update_queue(20)],
                         ['STK'])

    def test_reaper_leaves_in_flight_rows_alone(self):
        cid = self.state.upsert_company({'Company': 'Live (LIV)'}, DEEP_RESEARCHED)
        self.state.mark_maintenance_updating(cid)
        self.assertEqual(self.state.reap_stuck_updating(12), 0)

    def test_covered_tickers_scopes_to_one_country(self):
        self.state.upsert_company({'Company': 'JP One (1111.T)', 'Country': 'Japan'})
        self.state.upsert_company({'Company': 'JP Two (2222.T)', 'Country': 'Japan'})
        self.state.upsert_company({'Company': 'FR One (FR.PA)', 'Country': 'France'})
        self.assertEqual(self.state.covered_tickers('japan'), ['1111.T', '2222.T'])
        self.assertEqual(len(self.state.covered_tickers()), 3)

    def test_ticker_review_requeue_rejects_collision(self):
        first = self.state.upsert_company({'Company': 'Alpha (AAA)'})
        second = self.state.upsert_company({'Company': 'Beta (BBB)'})
        self.state.send_to_review(second, 'ticker', 'ambiguous')
        with self.assertRaises(ValueError):
            self.state.correct_ticker_and_requeue(second, 'Beta', 'AAA')
        self.state.correct_ticker_and_requeue(second, 'Beta', 'CCC')
        row = self.state.company_by_ticker('CCC')
        self.assertEqual(row['research_status'], RESEARCH_PENDING)

    def test_batch_manifest_checkpoints_full_input(self):
        items = []
        for i in range(3):
            cid = self.state.upsert_company({'Company': f'Company {i} (C{i})'})
            items.append({'Company': f'Company {i} (C{i})', '_company_id': cid})
        bid = self.state.create_batch(7, 'API Batch 7 (new, gemini)',
                                      'research', 'new', 'gemini', items)
        self.state.checkpoint_item(bid, 0, 'validated', 'gemini', {'Company': 'Company 0 (C0)'})
        runs = self.state.incomplete_batches()
        self.assertEqual(len(runs), 1)
        self.assertEqual(len(runs[0][1]), 3)
        self.assertEqual(runs[0][1][0]['status'], 'validated')

    def test_focused_queue_requires_every_selected_dimension(self):
        self.state.upsert_company({'Company': 'Taiwan Chip (TC)', 'Country': 'Taiwan',
                                   'Industry': 'Semiconductor'})
        self.state.upsert_company({'Company': 'US Chip (UC)', 'Country': 'USA',
                                   'Industry': 'Semiconductor'})
        self.state.upsert_company({'Company': 'Taiwan Cloud (TW)', 'Country': 'Taiwan',
                                   'Industry': 'Cloud'})
        rows = self.state.research_queue(20, country='Taiwan', industry='Semiconductor')
        self.assertEqual([r['ticker'] for r in rows], ['TC'])

    def test_research_result_merges_duplicate_corrected_ticker_without_crashing(self):
        canonical = self.state.upsert_company(
            {'Company': 'Canonical (BBB)'}, DEEP_RESEARCHED)
        duplicate = self.state.upsert_company({'Company': 'Candidate (AAA)'})
        result = self.state.resolve_deep_research_result(
            duplicate, 'BBB', 'Candidate corrected (BBB)')
        self.assertTrue(result['merged'])
        self.assertEqual(result['company_id'], canonical)
        with self.state.connect() as db:
            row = db.execute('SELECT * FROM companies WHERE id=?', (duplicate,)).fetchone()
        self.assertEqual(row['research_status'], EXCLUDED)
        self.assertEqual(row['last_error_class'], 'duplicate_ticker')


class DailyConductorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = WorkflowState(os.path.join(self.tmp.name, 'workflow.sqlite3'))
        self.schedule = [['08:00', 'gemini'], ['09:00', 'claude', 'maintenance'],
                         ['10:00', 'codex']]
        self.state.seed_daily_slots('2026-07-22', self.schedule)

    def tearDown(self):
        self.tmp.cleanup()

    def test_future_slot_can_change_provider_mode_focus_and_enabled_state(self):
        slot = self.state.daily_slots('2026-07-22')[2]
        changed = self.state.update_daily_slot(
            slot['id'], provider='gemini', mode='maintenance', enabled=False,
            country_focus='Taiwan', industry_focus='半導體業',
            now=dt.datetime(2026, 7, 22, 9, 30))
        self.assertEqual(changed['provider'], 'gemini')
        self.assertEqual(changed['mode'], 'maintenance')
        self.assertFalse(changed['enabled'])
        self.assertEqual((changed['country_focus'], changed['industry_focus']),
                         ('Taiwan', '半導體業'))
        with self.assertRaises(ValueError):
            self.state.update_daily_slot(slot['id'], enabled=True,
                                         now=dt.datetime(2026, 7, 22, 10, 1))

    def test_claim_marks_older_overrun_slot_missed(self):
        claimed = self.state.claim_due_slot(dt.datetime(2026, 7, 22, 9, 2))
        self.assertEqual(claimed['slot_time'], '09:00')
        rows = self.state.daily_slots('2026-07-22')
        self.assertEqual(rows[0]['status'], 'missed')
        self.assertEqual(rows[1]['status'], 'running')
        self.assertEqual(rows[2]['status'], 'pending')

    def test_disabled_due_slot_is_not_claimed(self):
        slot = self.state.daily_slots('2026-07-22')[1]
        self.state.update_daily_slot(slot['id'], enabled=False,
                                     now=dt.datetime(2026, 7, 22, 8, 30))
        self.assertIsNone(self.state.claim_due_slot(dt.datetime(2026, 7, 22, 9, 1)))
        self.assertEqual(self.state.daily_slot(slot['id'])['status'], 'pending')

    def test_overrun_marks_every_crossed_pending_slot_missed(self):
        first = self.state.claim_due_slot(dt.datetime(2026, 7, 22, 8, 1))
        self.state.finish_daily_slot(first['id'], 'complete')
        self.assertEqual(self.state.miss_due_slots(dt.datetime(2026, 7, 22, 10, 1)), 2)
        self.assertEqual([s['status'] for s in self.state.daily_slots('2026-07-22')],
                         ['complete', 'missed', 'missed'])

    def test_batch_completion_updates_linked_slot(self):
        slot = self.state.claim_due_slot(dt.datetime(2026, 7, 22, 8, 1))
        cid = self.state.upsert_company({'Company': 'Alpha (AAA)'})
        bid = self.state.create_batch(1, 'API Batch 1 (new, gemini)', 'research',
                                      'new', 'gemini',
                                      [{'Company': 'Alpha (AAA)', '_company_id': cid}],
                                      slot['id'])
        self.state.checkpoint_item(bid, 0, 'applied', 'gemini',
                                   {'Company': 'Alpha (AAA)'})
        self.state.finish_batch(bid, 'complete')
        linked = self.state.daily_slots('2026-07-22')[0]
        self.assertEqual((linked['status'], linked['applied_count']), ('complete', 1))

    def test_interrupted_maintenance_batch_shows_persisted_progress(self):
        slot = self.state.claim_due_slot(dt.datetime(2026, 7, 22, 9, 1))
        items = []
        for i in range(20):
            cid = self.state.upsert_company({'Company': f'Company {i} (C{i})'},
                                            DEEP_RESEARCHED)
            items.append({'Company': f'Company {i} (C{i})', '_company_id': cid})
        bid = self.state.create_batch(2, 'Maintenance Batch 2 (maintenance, gemini)',
                                      'maintenance', 'maintenance', 'gemini', items,
                                      slot['id'])
        for pos in range(3):
            self.state.checkpoint_item(bid, pos, 'validated', 'gemini',
                                       {'Company': f'Company {pos} (C{pos})'})
        with self.state.connect() as db:
            db.execute("UPDATE batch_runs SET status='partial' WHERE id=?", (bid,))
            db.execute("UPDATE daily_slots SET status='running' WHERE id=?", (slot['id'],))
        rail = dashboard.conductor_rail(
            self.state, {'batch_size': 20, 'schedule': self.schedule},
            {'current': None}, dt.datetime(2026, 7, 22, 9, 2))
        self.assertEqual(rail[1]['detail'], '3/20')
        self.assertEqual(rail[1]['status'], 'running')


class ProviderAllocationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = WorkflowState(os.path.join(self.tmp.name, 'workflow.sqlite3'))
        self.saved_workflow = rl._WORKFLOW
        rl._WORKFLOW = self.state
        self.cfg = {
            'daily_call_budgets': {'gemini': 100, 'claude': 100, 'codex': 100},
            'provider_fallback_order': ['gemini', 'claude', 'codex'],
            'provider_low_budget_threshold': 0.10,
        }

    def tearDown(self):
        rl._WORKFLOW = self.saved_workflow
        self.tmp.cleanup()

    def test_manual_disable_persists_and_forces_fallback(self):
        self.state.set_provider_enabled('gemini', False)
        self.assertEqual(rl.select_provider(
            self.cfg, {'calls': {'gemini': 0, 'claude': 0, 'codex': 0}}, 'gemini'), 'claude')
        self.assertFalse(bool(self.state.provider_state('gemini')['enabled']))

    def test_force_next_is_consumed_once(self):
        self.state.force_provider_next('codex')
        state = {'calls': {'gemini': 0, 'claude': 0, 'codex': 0}}
        self.assertEqual(rl.select_provider(self.cfg, state, 'gemini', consume_force=True), 'codex')
        self.assertEqual(rl.select_provider(self.cfg, state, 'gemini', consume_force=True), 'gemini')

    def test_observed_limit_reset_parsing(self):
        stamp = rl._reset_from_message(
            "You've hit your weekly limit · resets Jul 24 at 5pm (Asia/Taipei)")
        self.assertIn('T', stamp)
        self.assertTrue(stamp.endswith('+00:00'))


class SameSlotFailoverTests(unittest.TestCase):
    def test_quota_failure_continues_with_next_provider(self):
        with tempfile.TemporaryDirectory() as d:
            workflow = WorkflowState(os.path.join(d, 'workflow.sqlite3'))
            cid = workflow.upsert_company({'Company': 'Alpha (AAA)'})
            item = {'Company': 'Alpha (AAA)', 'Country': 'USA', 'Industry': 'Tech',
                    '_company_id': cid}
            cfg = {
                'model': 'test', 'delay_seconds': 0, 'max_retries': 0,
                'daily_call_budgets': {'gemini': 100, 'claude': 100, 'codex': 100},
                'provider_fallback_order': ['gemini', 'claude', 'codex'],
                'provider_low_budget_threshold': 0.10,
            }
            state = {'date': '2026-07-22',
                     'calls': {'gemini': 0, 'claude': 0, 'codex': 0}, 'batch_seq': 0}
            saved = (rl._WORKFLOW, rl.BATCHES_DIR, rl.save_state, rl.send_telegram,
                     rl.research_company, rl.apply_batch.apply_records, rl.halt_requested)
            rl._WORKFLOW, rl.BATCHES_DIR = workflow, d
            rl.save_state = lambda _state: None
            rl.send_telegram = lambda _text: None
            rl.halt_requested = lambda: False

            def fake_research(_cfg, _key, _state, _item, provider='gemini', mode='research'):
                if provider == 'gemini':
                    raise rl.BudgetExhausted('gemini quota', provider='gemini',
                                             error_class='usage_limit')
                return {'Company': 'Alpha (AAA)'}, [], '{}'

            rl.research_company = fake_research
            rl.apply_batch.apply_records = lambda records, label: {
                'updated': [], 'claimed': ['Alpha (AAA)'],
                'progress': {'real_tickers_harvested': 1, 'deep_research_completed': 1},
            }
            try:
                self.assertEqual(rl.run_batch(cfg, None, state, [item], 'new',
                                              'gemini', 'research', workflow), 1)
                archive = json.loads(next(Path(d).glob('batch_api_*.json')).read_text())
                self.assertEqual(archive['providers_used'], ['gemini', 'claude'])
                self.assertEqual(workflow.company_by_ticker('AAA')['research_status'],
                                 DEEP_RESEARCHED)
            finally:
                (rl._WORKFLOW, rl.BATCHES_DIR, rl.save_state, rl.send_telegram,
                 rl.research_company, rl.apply_batch.apply_records, rl.halt_requested) = saved

    def test_incomplete_batch_recovery_survives_corrected_ticker_collision(self):
        with tempfile.TemporaryDirectory() as d:
            workflow = WorkflowState(os.path.join(d, 'workflow.sqlite3'))
            canonical = workflow.upsert_company({'Company': 'Canonical (BBB)'}, DEEP_RESEARCHED)
            duplicate = workflow.upsert_company({'Company': 'Candidate (AAA)'})
            item = {'Company': 'Candidate (AAA)', '_company_id': duplicate}
            bid = workflow.create_batch(8, 'API Batch 8 (new, gemini)',
                                        'research', 'new', 'gemini', [item])
            workflow.checkpoint_item(bid, 0, 'validated', 'gemini',
                                     {'Company': 'Candidate corrected (BBB)'})
            saved_apply = rl.apply_batch.apply_records
            rl.apply_batch.apply_records = lambda *_args, **_kwargs: {
                'updated': ['Canonical (BBB)'], 'claimed': [],
                'progress': {'real_tickers_harvested': 1, 'deep_research_completed': 1}}
            try:
                rl.recover_incomplete_batches(workflow)
            finally:
                rl.apply_batch.apply_records = saved_apply
            self.assertEqual(workflow.batch_run(8)['status'], 'complete')
            with workflow.connect() as db:
                item_row = db.execute('SELECT status FROM batch_items WHERE batch_id=?',
                                      (bid,)).fetchone()
                duplicate_row = db.execute('SELECT research_status FROM companies WHERE id=?',
                                           (duplicate,)).fetchone()
            self.assertEqual(item_row['status'], 'applied')
            self.assertEqual(duplicate_row['research_status'], EXCLUDED)
            self.assertEqual(workflow.company_by_ticker('BBB')['id'], canonical)


class MaintenanceAndDashboardTests(unittest.TestCase):
    def test_prepare_maintenance_slot_passes_focus_to_both_phases(self):
        workflow = MagicMock()
        workflow.daily_slots.return_value = [
            {'enabled': 1, 'mode': 'maintenance'}]
        workflow.maintenance_screen_queue.return_value = []
        workflow.maintenance_update_queue.return_value = []
        cfg = {
            'batch_size': 20,
            'maintenance_screen_group_size': 20,
            'maintenance_screen_groups_per_slot': 4,
        }
        items, source, remaining = rl.prepare_maintenance_slot(
            cfg, None, {'calls': {}}, 'gemini', workflow,
            focus_country='Taiwan', focus_industry='Semiconductor')
        self.assertEqual((items, source, remaining), ([], 'maintenance', 0))
        workflow.maintenance_screen_queue.assert_called_once_with(
            80, 'Taiwan', 'Semiconductor')
        # 500, not batch_size: the queue is over-fetched so csv-orphans at its head (which the
        # ticker join drops) cannot blind a slot to real due companies further back.
        self.assertEqual(workflow.maintenance_update_queue.call_args_list[0].args,
                         (500, 'Taiwan', 'Semiconductor'))
        self.assertEqual(workflow.maintenance_update_queue.call_args_list[1].args,
                         (1000000, 'Taiwan', 'Semiconductor'))

    def test_screen_parser_requires_every_requested_ticker(self):
        items = [{'Company': 'Alpha (AAA)'}, {'Company': 'Beta (BBB)'}]
        raw = json.dumps([
            {'Company': 'Alpha (AAA)', 'needs_update': False,
             'reason': 'no material news', 'evidence_date': ''},
            {'Company': 'Beta (BBB)', 'needs_update': True,
             'reason': 'new acquisition', 'evidence_date': '2026-07-21'},
        ])
        parsed = rl.parse_maintenance_screen(raw, items)
        self.assertFalse(parsed['AAA']['needs_update'])
        self.assertTrue(parsed['BBB']['needs_update'])
        with self.assertRaises(ValueError):
            rl.parse_maintenance_screen(json.dumps([json.loads(raw)[0]]), items)

    def test_dashboard_parses_running_maintenance_batch(self):
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / 'loop.log'
            log.write_text(
                '=== Maintenance Batch 9 (maintenance, gemini): 20 companies ===\n'
                '  researching Alpha (AAA) ...\n    ok (codex)\n'
                '  researching Beta (BBB) ...\n', encoding='utf-8')
            saved = dashboard.LOG_PATH
            dashboard.LOG_PATH = str(log)
            try:
                parsed = dashboard.parse_log()
                self.assertEqual(parsed['current']['seq'], 9)
                self.assertEqual(parsed['current']['scheduled_provider'], 'gemini')
                self.assertEqual(parsed['current']['provider'], 'codex')
            finally:
                dashboard.LOG_PATH = saved

    def test_conductor_rail_shows_upcoming_progress_and_focus(self):
        with tempfile.TemporaryDirectory() as d:
            workflow = WorkflowState(os.path.join(d, 'workflow.sqlite3'))
            cfg = {'batch_size': 20, 'schedule': [['10:00', 'gemini']]}
            workflow.seed_daily_slots('2026-07-22', cfg['schedule'])
            slot = workflow.daily_slots('2026-07-22')[0]
            workflow.update_daily_slot(slot['id'], mode='maintenance', country_focus='Taiwan',
                                       industry_focus='半導體業',
                                       now=dt.datetime(2026, 7, 22, 9, 0))
            rail = dashboard.conductor_rail(
                workflow, cfg, {'current': None}, dt.datetime(2026, 7, 22, 9, 30))
            self.assertEqual(rail[0]['detail'], '0/20')
            self.assertEqual(rail[0]['focus'], 'Taiwan • 半導體業')
            self.assertEqual(
                rail[0]['focus_detail'], 'Country: Taiwan • Industry: 半導體業')
            self.assertTrue(rail[0]['focused'])
            self.assertEqual(rail[0]['mode_label'], 'MAINT')
            self.assertEqual(rail[0]['hhmm_display'], '★10:00')

    def test_focus_summary_lists_only_active_future_slots(self):
        with tempfile.TemporaryDirectory() as d:
            workflow = WorkflowState(os.path.join(d, 'workflow.sqlite3'))
            schedule = [
                ['10:00', 'gemini', 'maintenance'],
                ['11:00', 'claude'],
                ['12:00', 'codex', 'maintenance'],
            ]
            workflow.seed_daily_slots('2026-07-22', schedule)
            slots = workflow.daily_slots('2026-07-22')
            workflow.update_daily_slot(
                slots[0]['id'], country_focus='Taiwan',
                now=dt.datetime(2026, 7, 22, 9, 0))
            workflow.update_daily_slot(
                slots[1]['id'], industry_focus='Semiconductor',
                now=dt.datetime(2026, 7, 22, 9, 0))
            workflow.update_daily_slot(
                slots[2]['id'], country_focus='Japan', enabled=False,
                now=dt.datetime(2026, 7, 22, 9, 0))
            rail = dashboard.conductor_rail(
                workflow, {'batch_size': 20, 'schedule': schedule},
                {'current': None}, dt.datetime(2026, 7, 22, 9, 30))
            summary = dashboard.focused_slot_summary(
                rail, dt.datetime(2026, 7, 22, 9, 30))
            self.assertIn('10:00 MAINT — Country: Taiwan', summary)
            self.assertIn('11:00 DEEP — Industry: Semiconductor', summary)
            self.assertNotIn('Japan', summary)
            self.assertEqual(rail[2]['hhmm_display'], '☆12:00')

    def test_dashboard_suppresses_stale_completed_log_header(self):
        with tempfile.TemporaryDirectory() as d:
            workflow = WorkflowState(os.path.join(d, 'workflow.sqlite3'))
            cid = workflow.upsert_company({'Company': 'Alpha (AAA)'})
            bid = workflow.create_batch(9, 'API Batch 9 (new, gemini)', 'research',
                                        'new', 'gemini',
                                        [{'Company': 'Alpha (AAA)', '_company_id': cid}])
            workflow.finish_batch(bid, 'complete')
            log_info = {'current': {'seq': 9}, 'skips': []}
            cleaned = dashboard.suppress_completed_log_batch(log_info, workflow)
            self.assertIsNone(cleaned['current'])
            self.assertIsNotNone(log_info['current'])


if __name__ == '__main__':
    unittest.main(verbosity=2)


class IndustryFocusFilterTests(unittest.TestCase):
    """The work queues must match industry the way screen.select and the MCP tools do.

    Equality here was a silent steering failure: the column holds ~1,690 free-text labels for
    ~2,400 companies, so a slot focused on 國防 matched 0 rows while the corpus held 103, and
    the slot skipped with the same message a genuinely drained queue produces.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = WorkflowState(os.path.join(self.tmp.name, 'workflow.sqlite3'))
        for name, industry in (
                ('Exact (E1)', '半導體'),
                ('Suffixed (E2)', '半導體業'),
                ('Phrase (E3)', '精密聲學與光學組件代工，跨足半導體與機器人'),
                ('Defence (D1)', '航太與國防'),
                ('Unrelated (U1)', '航運')):
            self.state.upsert_company({'Company': name, 'Country': 'Taiwan',
                                       'Industry': industry})

    def tearDown(self):
        self.tmp.cleanup()

    def _tickers(self, **kw):
        return sorted(r['ticker'] for r in self.state.research_queue(100, **kw))

    def test_focus_matches_suffixed_and_embedded_labels(self):
        self.assertEqual(self._tickers(industry='半導體'), ['E1', 'E2', 'E3'])

    def test_focus_finds_a_label_that_is_only_ever_a_substring(self):
        # 國防 never appears as a whole label — only inside 航太與國防. Equality found nothing.
        self.assertEqual(self._tickers(industry='國防'), ['D1'])

    def test_focus_still_excludes_unrelated_industries(self):
        self.assertNotIn('U1', self._tickers(industry='半導體'))

    def test_country_focus_stays_exact(self):
        self.state.upsert_company({'Company': 'Partial (P1)', 'Country': 'Taiwanese Holdings',
                                   'Industry': '半導體'})
        self.assertNotIn('P1', self._tickers(country='Taiwan'))

    def test_focus_term_containing_a_wildcard_is_not_a_wildcard(self):
        # INSTR rather than LIKE: under LIKE this would have matched every row.
        self.assertEqual(self._tickers(industry='%'), [])

    def test_maintenance_queues_use_the_same_rule(self):
        cid = self.state.upsert_company(
            {'Company': 'Maint (M1)', 'Country': 'Taiwan', 'Industry': '航太與國防'},
            DEEP_RESEARCHED)
        self.state.begin_maintenance_cycle('2026-07-20', 100, {'unknown': 28})
        self.assertEqual([r['id'] for r in self.state.maintenance_screen_queue(10, industry='國防')],
                         [cid])
        self.state.record_screening(cid, '2026-07-20', 'gemini', True)
        self.assertEqual([r['id'] for r in self.state.maintenance_update_queue(10, industry='國防')],
                         [cid])


class StandingFocusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = WorkflowState(os.path.join(self.tmp.name, 'workflow.sqlite3'))

    def tearDown(self):
        self.tmp.cleanup()

    def test_absent_focus_reads_as_none(self):
        self.assertIsNone(self.state.standing_focus())

    def test_set_and_read_round_trip(self):
        self.state.set_standing_focus('低軌衛星', reason='from a video chat')
        focus = self.state.standing_focus()
        self.assertEqual(focus['industry'], '低軌衛星')
        self.assertIsNone(focus['batches_remaining'])
        self.assertEqual(focus['reason'], 'from a video chat')

    def test_empty_focus_is_rejected(self):
        with self.assertRaises(ValueError):
            self.state.set_standing_focus('   ', '  ')

    def test_corrupt_value_reads_as_none_rather_than_raising(self):
        self.state.set_meta(WorkflowState.FOCUS_KEY, 'not json')
        self.assertIsNone(self.state.standing_focus())

    def test_batch_budget_counts_down_and_clears_itself(self):
        self.state.set_standing_focus('低軌衛星', batches=2)
        self.assertEqual(self.state.consume_standing_focus()['batches_remaining'], 1)
        self.assertIsNone(self.state.consume_standing_focus())
        self.assertIsNone(self.state.standing_focus())

    def test_until_cleared_focus_is_never_consumed(self):
        self.state.set_standing_focus('低軌衛星')
        for _ in range(5):
            self.state.consume_standing_focus()
        self.assertIsNotNone(self.state.standing_focus())
        self.assertIsNotNone(self.state.clear_standing_focus())
        self.assertIsNone(self.state.standing_focus())


class EffectiveFocusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = WorkflowState(os.path.join(self.tmp.name, 'workflow.sqlite3'))

    def tearDown(self):
        self.tmp.cleanup()

    def test_slot_focus_overrides_a_standing_focus(self):
        self.state.set_standing_focus('低軌衛星')
        slot = {'country_focus': 'Japan', 'industry_focus': '半導體'}
        self.assertEqual(rl.effective_focus(self.state, slot), ('Japan', '半導體', 'slot'))

    def test_standing_focus_fills_an_unfocused_slot(self):
        self.state.set_standing_focus('低軌衛星', country='Taiwan')
        slot = {'country_focus': '', 'industry_focus': ''}
        self.assertEqual(rl.effective_focus(self.state, slot), ('Taiwan', '低軌衛星', 'standing'))

    def test_no_focus_anywhere_is_unfocused(self):
        slot = {'country_focus': '', 'industry_focus': ''}
        self.assertEqual(rl.effective_focus(self.state, slot), ('', '', 'none'))
