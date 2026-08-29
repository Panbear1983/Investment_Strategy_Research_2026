import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

import maintenance_posts
from maintenance_posts import (
    daily_maintenance_digest_preview,
    export_maintenance_posts,
    extract_maintenance_posts,
    render_daily_maintenance_digest,
    render_maintenance_markdown,
)
import dashboard
from research_state import DEEP_RESEARCHED, WorkflowState


class MaintenancePostTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = WorkflowState(os.path.join(self.tmp.name, 'workflow.sqlite3'))

    def tearDown(self):
        self.tmp.cleanup()

    def test_extracts_applied_maintenance_items_with_screening_evidence_in_stable_order(self):
        first = self.state.upsert_company({'Company': 'Alpha *Co* (AAA)'}, DEEP_RESEARCHED)
        second = self.state.upsert_company({'Company': 'Beta (BBB)'}, DEEP_RESEARCHED)
        self.state.record_screening(
            first, '2026-07-20', 'gemini', True, 'growth [news]', '2026-07-19',
            ['https://example.com/alpha'])
        batch_id = self.state.create_batch(
            9, 'Maintenance Batch 9', 'maintenance', 'maintenance', 'gemini', [
                {'Company': 'Beta (BBB)', '_company_id': second},
                {'Company': 'Alpha *Co* (AAA)', '_company_id': first},
            ])
        self.state.checkpoint_item(
            batch_id, 0, 'applied', 'gemini', {'Company': 'Beta (BBB)', '12M Catalysts': 'beta'})
        self.state.checkpoint_item(
            batch_id, 1, 'applied', 'gemini', {'Company': 'Alpha *Co* (AAA)',
                                                '12M Catalysts': 'alpha'})
        self.state.finish_batch(batch_id)

        posts = extract_maintenance_posts(self.state)

        self.assertEqual([post['ticker'] for post in posts], ['BBB', 'AAA'])
        self.assertEqual(posts[1]['evidence']['source_urls'], ['https://example.com/alpha'])
        self.assertEqual(posts[1]['changes'], {'12M Catalysts': 'alpha'})
        rendered = render_maintenance_markdown(posts)
        self.assertIn('Alpha \\*Co\\* \\(AAA\\)', rendered)
        self.assertIn('growth \\[news\\]', rendered)
        self.assertIn('https://example\\.com/alpha', rendered)

    def test_extracts_evidence_snapshot_when_later_screening_exists(self):
        company_id = self.state.upsert_company({'Company': 'Alpha (AAA)'}, DEEP_RESEARCHED)
        self.state.record_screening(
            company_id, '2026-07-20', 'gemini', True, 'original evidence', '2026-07-19',
            ['https://example.com/original'])
        batch_id = self.state.create_batch(
            10, 'Maintenance Batch 10', 'maintenance', 'maintenance', 'gemini',
            [{'Company': 'Alpha (AAA)', '_company_id': company_id}])
        self.state.checkpoint_item(
            batch_id, 0, 'applied', 'gemini', {'Company': 'Alpha (AAA)',
                                                '12M Catalysts': 'original change'})
        self.state.record_screening(
            company_id, '2026-07-27', 'gemini', True, 'later evidence', '2026-07-26',
            ['https://example.com/later'])

        post = extract_maintenance_posts(self.state)[0]

        self.assertEqual(post['evidence'], {
            'reason': 'original evidence',
            'evidence_date': '2026-07-19',
            'source_urls': ['https://example.com/original'],
            'screened_at': post['evidence']['screened_at'],
        })
        self.assertNotEqual(post['evidence']['screened_at'], '')

    def test_sorts_change_fields_and_exports_partial_applied_records_deterministically(self):
        company_id = self.state.upsert_company({'Company': 'Partial (PAR)'}, DEEP_RESEARCHED)
        batch_id = self.state.create_batch(
            11, 'Maintenance Batch 11', 'maintenance', 'maintenance', 'gemini',
            [{'Company': 'Partial (PAR)', '_company_id': company_id}])
        self.state.checkpoint_item(
            batch_id, 0, 'applied', 'gemini',
            {'Company': 'Partial (PAR)', 'Zebra update': 'last', 'Alpha update': 'first'})

        posts = extract_maintenance_posts(self.state)

        self.assertEqual(list(posts[0]['changes']), ['Alpha update', 'Zebra update'])
        self.assertEqual(posts[0]['evidence'], {
            'reason': '', 'evidence_date': '', 'source_urls': [], 'screened_at': '',
        })
        self.assertEqual(export_maintenance_posts(posts), export_maintenance_posts(posts))

    def test_omits_missing_company_records_and_represents_missing_result_as_empty_changes(self):
        company_id = self.state.upsert_company({'Company': 'Incomplete (INC)'}, DEEP_RESEARCHED)
        batch_id = self.state.create_batch(
            12, 'Maintenance Batch 12', 'maintenance', 'maintenance', 'gemini',
            [{'Company': 'Incomplete (INC)', '_company_id': company_id}])
        self.state.checkpoint_item(batch_id, 0, 'applied', 'gemini')
        with self.state.connect() as db:
            db.execute('PRAGMA foreign_keys=OFF')
            db.execute("""INSERT INTO batch_items
                (batch_id, company_id, position, item_json, status, updated_at)
                VALUES (?, ?, ?, ?, 'applied', ?)""",
                       (batch_id, 999999, 1, '{}', '2026-07-25T00:00:00+00:00'))

        posts = extract_maintenance_posts(self.state)

        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0]['ticker'], 'INC')
        self.assertEqual(posts[0]['changes'], {})

    def test_dashboard_preview_uses_maintenance_post_extraction(self):
        company_id = self.state.upsert_company({'Company': 'Preview (P)'}, DEEP_RESEARCHED)
        batch_id = self.state.create_batch(
            3, 'Maintenance Batch 3', 'maintenance', 'maintenance', 'gemini',
            [{'Company': 'Preview (P)', '_company_id': company_id}])
        self.state.checkpoint_item(batch_id, 0, 'applied', 'gemini',
                                   {'Company': 'Preview (P)', '12M Catalysts': 'change'})

        preview = dashboard.maintenance_preview(self.state, limit=1)

        self.assertEqual(preview[0]['ticker'], 'P')

    def test_daily_digest_uses_only_material_updates_from_its_day_and_snapshot_evidence(self):
        first = self.state.upsert_company({'Company': 'Alpha *Co* (AAA)'}, DEEP_RESEARCHED)
        second = self.state.upsert_company({'Company': 'Beta (BBB)'}, DEEP_RESEARCHED)
        self.state.record_screening(
            first, '2026-07-25', 'gemini', True, 'new [filing]', '2026-07-24',
            ['https://example.com/alpha'])
        batch_id = self.state.create_batch(
            13, 'Maintenance Batch 13', 'maintenance', 'maintenance', 'gemini', [
                {'Company': 'Beta (BBB)', '_company_id': second},
                {'Company': 'Alpha *Co* (AAA)', '_company_id': first},
            ])
        self.state.checkpoint_item(batch_id, 0, 'applied', 'gemini',
                                   {'Company': 'Beta (BBB)'})
        self.state.checkpoint_item(batch_id, 1, 'applied', 'gemini',
                                   {'Company': 'Alpha *Co* (AAA)', 'Catalyst': 'gain *share*'})
        with self.state.connect() as db:
            db.execute("UPDATE batch_items SET updated_at='2026-07-25T10:00:00+00:00' "
                       "WHERE batch_id=?", (batch_id,))

        preview = daily_maintenance_digest_preview(self.state, '2026-07-25', [], countries=None)

        self.assertEqual([post['ticker'] for post in preview['posts']], ['AAA'])
        self.assertEqual(preview['recipients'], [])
        self.assertEqual(preview['delivery'], [])
        self.assertIn('1. Alpha *Co* (AAA)', preview['markdown'])
        self.assertIn('催化劑：gain *share*', preview['markdown'])
        self.assertIn('資料來源：https://example.com/alpha', preview['markdown'])

    def test_daily_digest_is_empty_without_material_updates_and_ledger_is_idempotent(self):
        company_id = self.state.upsert_company({'Company': 'Empty (EMP)'}, DEEP_RESEARCHED)
        batch_id = self.state.create_batch(
            14, 'Maintenance Batch 14', 'maintenance', 'maintenance', 'gemini',
            [{'Company': 'Empty (EMP)', '_company_id': company_id}])
        self.state.checkpoint_item(batch_id, 0, 'applied', 'gemini', {'Company': 'Empty (EMP)'})
        with self.state.connect() as db:
            db.execute("UPDATE batch_items SET updated_at='2026-07-25T10:00:00+00:00' "
                       "WHERE batch_id=?", (batch_id,))
        recipients = [
            {'label': 'Peter', 'chat_id': '2'},
            {'label': 'Dad', 'chat_id': '1'},
        ]

        preview = daily_maintenance_digest_preview(self.state, '2026-07-25', recipients, countries=None)
        self.assertEqual(preview['posts'], [])
        self.assertEqual(preview['markdown'], '')
        self.assertEqual([row['status'] for row in preview['delivery']], ['pending', 'pending'])
        self.assertEqual(self.state.register_digest_recipients('2026-07-25', recipients), 2)
        self.assertEqual(self.state.register_digest_recipients('2026-07-25', recipients), 0)
        self.assertEqual(
            self.state.digest_delivery_status('2026-07-25', recipients),
            [
                {'label': 'Dad', 'chat_id': '1', 'status': 'pending'},
                {'label': 'Peter', 'chat_id': '2', 'status': 'pending'},
            ])
        self.assertEqual(render_daily_maintenance_digest([], '2026-07-25'), '')

    def test_daily_digest_limit_counts_material_updates_not_empty_applied_records(self):
        empty_id = self.state.upsert_company({'Company': 'Empty first (E)'}, DEEP_RESEARCHED)
        material_id = self.state.upsert_company({'Company': 'Material second (M)'}, DEEP_RESEARCHED)
        batch_id = self.state.create_batch(
            15, 'Maintenance Batch 15', 'maintenance', 'maintenance', 'gemini', [
                {'Company': 'Empty first (E)', '_company_id': empty_id},
                {'Company': 'Material second (M)', '_company_id': material_id},
            ])
        self.state.checkpoint_item(batch_id, 0, 'applied', 'gemini', {'Company': 'Empty first (E)'})
        self.state.checkpoint_item(batch_id, 1, 'applied', 'gemini',
                                   {'Company': 'Material second (M)', 'Catalyst': 'material'})
        with self.state.connect() as db:
            db.execute("UPDATE batch_items SET updated_at='2026-07-25T10:00:00+00:00' "
                       "WHERE batch_id=?", (batch_id,))

        preview = daily_maintenance_digest_preview(self.state, '2026-07-25', [], limit=1, countries=None)

        self.assertEqual([post['ticker'] for post in preview['posts']], ['M'])

    def test_daily_digest_uses_taipei_day_boundary_for_utc_batch_item_timestamps(self):
        prior_id = self.state.upsert_company({'Company': 'Prior (P)'}, DEEP_RESEARCHED)
        boundary_id = self.state.upsert_company({'Company': 'Boundary (B)'}, DEEP_RESEARCHED)
        batch_id = self.state.create_batch(
            16, 'Maintenance Batch 16', 'maintenance', 'maintenance', 'gemini', [
                {'Company': 'Prior (P)', '_company_id': prior_id},
                {'Company': 'Boundary (B)', '_company_id': boundary_id},
            ])
        self.state.checkpoint_item(batch_id, 0, 'applied', 'gemini',
                                   {'Company': 'Prior (P)', 'Catalyst': 'before midnight'})
        self.state.checkpoint_item(batch_id, 1, 'applied', 'gemini',
                                   {'Company': 'Boundary (B)', 'Catalyst': 'after midnight'})
        with self.state.connect() as db:
            db.execute("UPDATE batch_items SET updated_at='2026-07-25T15:59:59+00:00' "
                       "WHERE batch_id=? AND position=0", (batch_id,))
            db.execute("UPDATE batch_items SET updated_at='2026-07-25T16:00:00+00:00' "
                       "WHERE batch_id=? AND position=1", (batch_id,))

        july_25 = daily_maintenance_digest_preview(self.state, '2026-07-25', [], countries=None)
        july_26 = daily_maintenance_digest_preview(self.state, '2026-07-26', [], countries=None)

        self.assertEqual([post['ticker'] for post in july_25['posts']], ['P'])
        self.assertEqual([post['ticker'] for post in july_26['posts']], ['B'])

    def test_daily_digest_ranks_newest_material_maintenance_updates_first(self):
        older_id = self.state.upsert_company({'Company': 'Older (OLD)'}, DEEP_RESEARCHED)
        newer_id = self.state.upsert_company({'Company': 'Newer (NEW)'}, DEEP_RESEARCHED)
        batch_id = self.state.create_batch(
            17, 'Maintenance Batch 17', 'maintenance', 'maintenance', 'gemini', [
                {'Company': 'Older (OLD)', '_company_id': older_id},
                {'Company': 'Newer (NEW)', '_company_id': newer_id},
            ])
        self.state.checkpoint_item(batch_id, 0, 'applied', 'gemini',
                                   {'Company': 'Older (OLD)', 'Catalyst': 'older change'})
        self.state.checkpoint_item(batch_id, 1, 'applied', 'gemini',
                                   {'Company': 'Newer (NEW)', 'Catalyst': 'newer change'})
        with self.state.connect() as db:
            db.execute("UPDATE batch_items SET updated_at='2026-07-25T08:00:00+00:00' "
                       "WHERE batch_id=? AND position=0", (batch_id,))
            db.execute("UPDATE batch_items SET updated_at='2026-07-25T09:00:00+00:00' "
                       "WHERE batch_id=? AND position=1", (batch_id,))

        preview = daily_maintenance_digest_preview(
            self.state, '2026-07-25', [], limit=5, countries=None)

        self.assertEqual([post['ticker'] for post in preview['posts']], ['NEW', 'OLD'])

    def test_daily_digest_renders_a_concise_traditional_chinese_summary(self):
        posts = [{
            'company_name': '測試公司',
            'ticker': 'TEST',
            'batch_seq': 1,
            'changes': {
                '12M Catalysts': '甲' * 220,
                'Key Investment Risks': '乙' * 220,
                'Geopolitical Exposure': '丙' * 220,
                '3-Year M&A': '不應顯示',
            },
            'evidence': {
                'reason': '', 'evidence_date': '', 'source_urls': [], 'screened_at': '',
            },
        }]

        rendered = render_daily_maintenance_digest(posts, '2026-07-25')

        self.assertTrue(rendered.startswith('📊 每日投資維護摘要｜2026-07-25'))
        self.assertIn('最近完成維護的 1 家公司', rendered)
        self.assertIn('1. 測試公司（TEST）', rendered)
        self.assertIn('催化劑：', rendered)
        self.assertIn('主要風險：', rendered)
        self.assertIn('地緣風險：', rendered)
        self.assertNotIn('3-Year M&A', rendered)
        self.assertNotIn('Daily maintenance digest', rendered)
        self.assertLess(len(rendered), 600)

    def test_daily_digest_defaults_to_the_newest_taiwan_and_usa_updates(self):
        taiwan_id = self.state.upsert_company(
            {'Company': 'Taiwan (TW)', 'Country': 'Taiwan'}, DEEP_RESEARCHED)
        usa_id = self.state.upsert_company(
            {'Company': 'USA (US)', 'Country': 'USA'}, DEEP_RESEARCHED)
        other_id = self.state.upsert_company(
            {'Company': 'Other (OT)', 'Country': 'Japan'}, DEEP_RESEARCHED)
        batch_id = self.state.create_batch(
            18, 'Maintenance Batch 18', 'maintenance', 'maintenance', 'gemini', [
                {'Company': 'Taiwan (TW)', '_company_id': taiwan_id},
                {'Company': 'USA (US)', '_company_id': usa_id},
                {'Company': 'Other (OT)', '_company_id': other_id},
            ])
        for position, name in enumerate(('Taiwan (TW)', 'USA (US)', 'Other (OT)')):
            self.state.checkpoint_item(batch_id, position, 'applied', 'gemini',
                                       {'Company': name, '12M Catalysts': 'material'})
        with self.state.connect() as db:
            for position, timestamp in enumerate((
                    '2026-07-25T07:00:00+00:00', '2026-07-25T08:00:00+00:00',
                    '2026-07-25T09:00:00+00:00')):
                db.execute('UPDATE batch_items SET updated_at=? WHERE batch_id=? AND position=?',
                           (timestamp, batch_id, position))

        preview = daily_maintenance_digest_preview(self.state, '2026-07-25', [])

        self.assertEqual([post['ticker'] for post in preview['posts']], ['US', 'TW'])

    def test_daily_group_candidates_prioritize_new_taiwan_usa_research_and_cap_at_five(self):
        new_company = self.state.upsert_company(
            {'Company': 'New Taiwan (NEW.TW)', 'ticker': 'NEW.TW', 'Country': 'Taiwan'},
            DEEP_RESEARCHED, source='new')
        usa_companies = [
            self.state.upsert_company(
                {'Company': f'USA {index} (US{index})', 'ticker': f'US{index}', 'Country': 'USA'},
                DEEP_RESEARCHED, source='csv')
            for index in range(1, 6)
        ]
        europe = self.state.upsert_company(
            {'Company': 'Europe (EU)', 'ticker': 'EU', 'Country': 'Germany'},
            DEEP_RESEARCHED, source='new')
        research_batch = self.state.create_batch(
            19, 'Research Batch 19', 'research', 'new', 'claude', [
                {'Company': 'New Taiwan (NEW.TW)', '_company_id': new_company},
                {'Company': 'Europe (EU)', '_company_id': europe},
            ])
        self.state.checkpoint_item(
            research_batch, 0, 'applied', 'claude',
            {'Company': 'New Taiwan (NEW.TW)', '12M Catalysts': 'new catalyst'})
        self.state.checkpoint_item(
            research_batch, 1, 'applied', 'claude',
            {'Company': 'Europe (EU)', '12M Catalysts': 'excluded country'})
        maintenance_batch = self.state.create_batch(
            20, 'Maintenance Batch 20', 'maintenance', 'maintenance', 'gemini',
            [{'Company': 'New Taiwan (NEW.TW)', '_company_id': new_company}]
            + [{'Company': f'USA {index} (US{index})', '_company_id': company_id}
               for index, company_id in enumerate(usa_companies, 1)])
        self.state.checkpoint_item(
            maintenance_batch, 0, 'applied', 'gemini',
            {'Company': 'New Taiwan (NEW.TW)', '12M Catalysts': 'duplicate maintenance'})
        for index in range(1, 6):
            self.state.checkpoint_item(
                maintenance_batch, index, 'applied', 'gemini',
                {'Company': f'USA {index} (US{index})', '12M Catalysts': f'catalyst {index}'})

        # Set updated_at timestamps to fall within the digest date
        with self.state.connect() as db:
            for position, timestamp in enumerate((
                    '2026-07-25T07:00:00+00:00', '2026-07-25T08:00:00+00:00',
                    '2026-07-25T09:00:00+00:00', '2026-07-25T10:00:00+00:00',
                    '2026-07-25T11:00:00+00:00', '2026-07-25T12:00:00+00:00')):
                db.execute('UPDATE batch_items SET updated_at=? WHERE batch_id=? AND position=?',
                           (timestamp, maintenance_batch, position))
            # Also update the research batch item (position 0)
            db.execute('UPDATE batch_items SET updated_at=? WHERE batch_id=? AND position=?',
                       ('2026-07-25T06:00:00+00:00', research_batch, 0))

        posts = maintenance_posts.extract_daily_group_digest_posts(self.state, '2026-07-25')

        self.assertEqual([post['ticker'] for post in posts], ['NEW.TW', 'US5', 'US4', 'US3', 'US2'])
        self.assertEqual(posts[0]['digest_kind'], 'new_company')
        self.assertEqual(len(posts), 5)

    def test_daily_group_candidates_return_empty_without_taiwan_or_usa_updates(self):
        company_id = self.state.upsert_company(
            {'Company': 'Europe (EU)', 'ticker': 'EU', 'Country': 'Germany'},
            DEEP_RESEARCHED, source='new')
        batch_id = self.state.create_batch(
            21, 'Research Batch 21', 'research', 'new', 'claude',
            [{'Company': 'Europe (EU)', '_company_id': company_id}])
        self.state.checkpoint_item(
            batch_id, 0, 'applied', 'claude',
            {'Company': 'Europe (EU)', '12M Catalysts': 'not eligible'})

        posts = maintenance_posts.extract_daily_group_digest_posts(self.state, '2026-07-25')

        self.assertEqual(posts, [])

    def test_model_digest_renderer_uses_one_short_traditional_chinese_summary_per_company(self):
        posts = [
            {'company_name': '台灣公司 (TW)', 'ticker': 'TW'},
            {'company_name': '美國公司 (US)', 'ticker': 'US'},
        ]
        summaries = {'TW': '受惠國防訂單增加，但需留意單一客戶集中風險。',
                     'US': '雲端需求帶動成長，但估值與監管風險仍高。'}

        rendered = getattr(maintenance_posts, 'render_model_daily_digest', lambda *_: '')(
            posts, summaries, '2026-07-25')

        self.assertTrue(rendered.startswith('📊 每日投資維護摘要｜2026-07-25'))
        self.assertIn('1. 台灣公司 (TW)\n受惠國防訂單增加', rendered)
        self.assertIn('2. 美國公司 (US)\n雲端需求帶動成長', rendered)
        self.assertNotIn('資料來源', rendered)
        self.assertLess(len(rendered), 300)

    def test_model_digest_prompt_and_parser_keep_only_expected_tickers(self):
        posts = [
            {'company_name': '台灣公司 (TW)', 'ticker': 'TW', 'country': 'Taiwan',
             'changes': {'12M Catalysts': '催化劑'}},
            {'company_name': '美國公司 (US)', 'ticker': 'US', 'country': 'USA',
             'changes': {'Key Investment Risks': '風險'}},
        ]

        prompt = getattr(maintenance_posts, 'model_digest_prompt', lambda _: '')(posts)
        summaries = getattr(maintenance_posts, 'parse_model_digest_summaries', lambda *_: {})(
            posts,
            json.dumps([
                {'ticker': 'TW', 'summary': '台灣摘要。'},
                {'ticker': 'US', 'summary': '美國摘要。'},
                {'ticker': 'OTHER', 'summary': '不得出現。'},
            ]),
        )

        self.assertIn('繁體中文', prompt)
        self.assertIn('一至兩句', prompt)
        self.assertEqual(summaries, {'TW': '台灣摘要。', 'US': '美國摘要。'})

    def test_model_digest_parser_accepts_a_json_code_fence(self):
        posts = [
            {'company_name': '台灣公司 (TW)', 'ticker': 'TW'},
            {'company_name': '美國公司 (US)', 'ticker': 'US'},
        ]
        response = '''```json
[{"ticker":"TW","summary":"台灣摘要。"},
 {"ticker":"US","summary":"美國摘要。"}]
```'''

        summaries = maintenance_posts.parse_model_digest_summaries(posts, response)

        self.assertEqual(summaries, {'TW': '台灣摘要。', 'US': '美國摘要。'})


if __name__ == '__main__':
    unittest.main()
