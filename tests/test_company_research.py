"""Canonical research store: the corpus must never silently lose a company again.

Regression cover for the 2026-08-10 finding: research content lived only in the CSV, which
admits a new company solely by claiming a 'Global_Entity' placeholder row. Once those ran out
every new company landed in an `unmatched` list that nothing read — 342 lost over two weeks
while the workflow DB marked them applied.
"""

import csv
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

import apply_batch
from apply_batch import COLS, CSV_HEADER, export_csv, is_placeholder_cell
from research_state import DEEP_RESEARCHED, WorkflowState


def record(name, **over):
    rec = {c: f'{c} value' for c in COLS}
    rec['Company'] = name
    rec['Country'] = 'Taiwan'
    rec['Industry'] = '半導體'
    rec['Tier'] = '龍頭股'
    rec.update(over)
    return rec


class ResearchStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.wf = WorkflowState(os.path.join(self.tmp.name, 'w.sqlite3'))

    def tearDown(self):
        self.tmp.cleanup()

    def _add(self, name, **over):
        rec = record(name, **over)
        cid = self.wf.upsert_company(
            {'Company': name, 'Country': rec['Country'], 'Industry': rec['Industry'],
             'Tier': rec['Tier']}, DEEP_RESEARCHED)
        self.wf.upsert_research(cid, rec, COLS, is_placeholder_cell=is_placeholder_cell)
        return cid, rec

    def test_store_has_no_capacity_limit(self):
        """The defect in one sentence: the old path could store a company only if a
        placeholder row happened to be free."""
        for i in range(50):
            self._add(f'Company {i} (C{i})')
        self.assertEqual(self.wf.research_content_count(), 50)

    def test_upsert_is_idempotent(self):
        cid, rec = self._add('Alpha (AAA)')
        self.wf.upsert_research(cid, rec, COLS)
        self.assertEqual(self.wf.research_content_count(), 1)

    def test_record_round_trips_all_22_fields(self):
        cid, rec = self._add('Alpha (AAA)')
        stored = self.wf.research_record(cid)
        self.assertEqual(set(stored), set(COLS))
        self.assertEqual(stored['Technical Moat'], rec['Technical Moat'])

    def test_lookup_by_exact_name_then_ticker(self):
        cid, _ = self._add('Alpha Corp (AAA)')
        self.assertEqual(self.wf.research_company_id('Alpha Corp (AAA)'), cid)
        self.assertEqual(self.wf.research_company_id('Renamed (AAA)', 'AAA'), cid)
        self.assertIsNone(self.wf.research_company_id('Nobody (ZZZ)', 'ZZZ'))

    def test_placeholder_cells_mark_a_record_incomplete(self):
        cid, _ = self._add('Alpha (AAA)', **{'12M Catalysts': '等待系統進行深度調查'})
        with self.wf.connect() as db:
            row = db.execute('SELECT complete FROM company_research WHERE company_id=?',
                             (cid,)).fetchone()
        self.assertEqual(row['complete'], 0)

    def test_record_without_company_is_rejected(self):
        with self.assertRaises(ValueError):
            self.wf.upsert_research(1, {'Country': 'Taiwan'}, COLS)


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.wf = WorkflowState(os.path.join(self.tmp.name, 'w.sqlite3'))
        self.csv_path = os.path.join(self.tmp.name, 'export.csv')

    def tearDown(self):
        self.tmp.cleanup()

    def _add(self, name):
        rec = record(name)
        cid = self.wf.upsert_company({'Company': name}, DEEP_RESEARCHED)
        self.wf.upsert_research(cid, rec, COLS)
        return rec

    def test_export_reproduces_the_bilingual_header_exactly(self):
        # dashboard.scan_db and screen.load_corpus both key off this header; screen's
        # self-test asserts header[0] == '國家 (Country)' after BOM stripping.
        self._add('Alpha (AAA)')
        export_csv(self.wf, self.csv_path)
        with open(self.csv_path, encoding='utf-8-sig') as f:
            self.assertEqual(next(csv.reader(f)), CSV_HEADER)

    def test_export_is_readable_by_the_screener(self):
        self._add('Alpha (AAA)')
        self._add('Beta (BBB)')
        export_csv(self.wf, self.csv_path)
        import screen
        rows, meta = screen.load_corpus(self.csv_path)
        self.assertEqual(meta['rows'], 2)
        self.assertEqual(meta['cols'], len(COLS))
        self.assertEqual(meta['header0'], '國家 (Country)')

    def test_export_loses_nothing(self):
        names = {self._add(f'C{i} (C{i})')['Company'] for i in range(30)}
        export_csv(self.wf, self.csv_path)
        with open(self.csv_path, encoding='utf-8-sig') as f:
            rows = list(csv.reader(f))[1:]
        self.assertEqual({r[5] for r in rows}, names)

    def test_export_is_atomic_leaving_no_temp_file(self):
        self._add('Alpha (AAA)')
        export_csv(self.wf, self.csv_path)
        leftovers = [p for p in os.listdir(self.tmp.name) if '.tmp' in p]
        self.assertEqual(leftovers, [])


class ApplyRecordsMergeTests(unittest.TestCase):
    """apply_records must merge partial maintenance records, never erase stored research."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, 'w.sqlite3')
        self.csv = os.path.join(self.tmp.name, 'export.csv')
        self._saved = (apply_batch.CSV_PATH, apply_batch.PROGRESS_PATH,
                       apply_batch.WORKFLOW_DB_PATH)
        apply_batch.CSV_PATH = self.csv
        apply_batch.PROGRESS_PATH = os.path.join(self.tmp.name, 'progress.json')
        apply_batch.WORKFLOW_DB_PATH = self.db

    def tearDown(self):
        (apply_batch.CSV_PATH, apply_batch.PROGRESS_PATH,
         apply_batch.WORKFLOW_DB_PATH) = self._saved
        self.tmp.cleanup()

    def test_redirecting_paths_keeps_production_untouched(self):
        """Guards the isolation itself: a test run once polluted two live records because
        CSV_PATH was redirected but the database handle was not."""
        apply_batch.apply_records([record('Alpha (AAA)')], 'T', fetch_insiders=False)
        self.assertNotEqual(apply_batch.WORKFLOW_DB_PATH, self._saved[2])
        self.assertTrue(os.path.exists(self.db))

    def test_new_company_is_stored_and_exported(self):
        summary = apply_batch.apply_records([record('Alpha (AAA)')], 'T', fetch_insiders=False)
        self.assertEqual(summary['unmatched'], [])
        self.assertEqual(summary['claimed'], ['Alpha (AAA)'])
        with open(self.csv, encoding='utf-8-sig') as f:
            self.assertIn('Alpha (AAA)', {r[5] for r in list(csv.reader(f))[1:]})

    def test_fifty_new_companies_all_land(self):
        """The exact scenario that silently failed: more new companies than free capacity."""
        recs = [record(f'C{i} (C{i})') for i in range(50)]
        summary = apply_batch.apply_records(recs, 'T', fetch_insiders=False)
        self.assertEqual(summary['unmatched'], [])
        self.assertEqual(summary['rows_exported'], 50)

    def test_partial_maintenance_record_merges_and_does_not_erase(self):
        apply_batch.apply_records([record('Alpha (AAA)')], 'T', fetch_insiders=False)
        apply_batch.apply_records(
            [{'Company': 'Alpha (AAA)', '12M Catalysts': 'fresh news'}], 'M',
            fetch_insiders=False)
        wf = WorkflowState(self.db)
        stored = wf.research_record(wf.research_company_id('Alpha (AAA)'))
        self.assertEqual(stored['12M Catalysts'], 'fresh news')
        self.assertEqual(stored['Technical Moat'], 'Technical Moat value')  # untouched

    def test_second_apply_updates_rather_than_duplicating(self):
        apply_batch.apply_records([record('Alpha (AAA)')], 'T', fetch_insiders=False)
        summary = apply_batch.apply_records([record('Alpha (AAA)')], 'T2', fetch_insiders=False)
        self.assertEqual(summary['updated'], ['Alpha (AAA)'])
        self.assertEqual(summary['rows_exported'], 1)

    def test_nameless_record_is_reported_not_dropped(self):
        summary = apply_batch.apply_records(
            [{'Country': 'Taiwan'}], 'T', fetch_insiders=False)
        self.assertEqual(summary['unmatched'], ['<missing Company field>'])


if __name__ == '__main__':
    unittest.main()
