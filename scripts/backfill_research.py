#!/usr/bin/env python3
"""One-shot, idempotent backfill of the canonical `company_research` table.

Why this exists: research content lived only in the CSV, which admits a new company only by
claiming a 'Global_Entity' placeholder row. Those ran out, so from that moment every newly
researched company was written into `unmatched` — a return value nothing reads — and lost,
while the workflow DB happily marked it `applied`. 342 companies vanished that way over two
weeks; their full records survive in `batch_items.result_json`, so nothing needs re-researching.

Two sources, in priority order:
  1. the CSV      — the 1,209 companies that did land
  2. batch_items  — every applied research/maintenance record, newest per company; this is
                    where the lost companies are recovered from

Safe to re-run: every write is an UPSERT keyed on company_id.

    python3 scripts/backfill_research.py --dry-run    # report only, write nothing
    python3 scripts/backfill_research.py
"""

import argparse
import csv
import json
import os
import sys

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPTS_DIR)

from apply_batch import (COLS, CSV_PATH, extract_ticker, is_placeholder_cell,
                         is_placeholder_row)
from research_state import DEEP_RESEARCHED, RESEARCH_PENDING, WorkflowState

DB_PATH = os.path.join(SCRIPTS_DIR, 'research_workflow.sqlite3')


def record_from_row(row):
    return {col: (row[i] if i < len(row) else '') for i, col in enumerate(COLS)}


def looks_complete(record):
    return not any(is_placeholder_cell(v) for v in record.values())


def from_csv(wf, dry_run):
    """Every real CSV row becomes canonical content."""
    csv.field_size_limit(10_000_000)
    written = 0
    with open(CSV_PATH, 'r', encoding='utf-8-sig') as f:
        reader = csv.reader(f)
        next(reader, None)
        for row in reader:
            if len(row) < 6 or not row[5].strip() or is_placeholder_row(row):
                continue
            record = record_from_row(row)
            if dry_run:
                written += 1
                continue
            cid = wf.upsert_company(
                {'Company': record['Company'], 'Country': record['Country'],
                 'Industry': record['Industry'], 'Tier': record['Tier']},
                DEEP_RESEARCHED if looks_complete(record) else RESEARCH_PENDING,
                source='csv')
            wf.upsert_research(cid, record, COLS, source='csv',
                               is_placeholder_cell=is_placeholder_cell)
            written += 1
    return written


def from_batches(wf, dry_run, already):
    """Recover companies whose research never reached the CSV, and layer field-level updates.

    Applied records are replayed OLDEST FIRST and MERGED field-by-field over whatever is
    already stored. Merging is essential: a maintenance record carries only MAINTENANCE_KEYS
    (~6 of 22 fields), so replacing wholesale would blank the other 16 — it degraded 611
    records on the first attempt before this was fixed.
    """
    recovered = 0
    with wf.connect() as db:
        rows = db.execute("""SELECT bi.company_id, bi.result_json, bi.batch_id, bi.updated_at,
                                    c.company_name
                             FROM batch_items bi
                             LEFT JOIN companies c ON c.id = bi.company_id
                             WHERE bi.status='applied' AND bi.result_json IS NOT NULL
                             ORDER BY bi.updated_at ASC, bi.id ASC""").fetchall()
    for r in rows:
        try:
            record = json.loads(r['result_json'])
        except ValueError:
            continue
        name = (record.get('Company') or r['company_name'] or '').strip()
        if not name:
            continue
        filled = sum(1 for k in COLS if record.get(k))
        cid = r['company_id'] or wf.research_company_id(name, extract_ticker(name))
        stored = wf.research_record(cid) if cid else None
        # A partial patch may UPDATE an existing company but must never CREATE one — a
        # 6-field maintenance record is not a research record.
        if stored is None and filled < 15:
            continue
        if dry_run:
            if name not in already:
                recovered += 1
                already.add(name)
            continue

        merged = dict(stored) if stored else {k: '' for k in COLS}
        for key in COLS:
            val = record.get(key, '')
            if val:
                merged[key] = val
        merged['Company'] = (stored or {}).get('Company') or name

        if not cid:
            cid = wf.upsert_company(
                {'Company': name, 'Country': merged.get('Country', ''),
                 'Industry': merged.get('Industry', ''), 'Tier': merged.get('Tier', '')},
                DEEP_RESEARCHED, source='new')
        wf.upsert_research(cid, merged, COLS, source='batch', batch_id=r['batch_id'],
                           is_placeholder_cell=is_placeholder_cell)
        if name not in already:
            recovered += 1
            already.add(name)
    return recovered


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--dry-run', action='store_true', help='report only, write nothing')
    args = ap.parse_args(argv)

    wf = WorkflowState(DB_PATH)
    before = wf.research_content_count()

    csv_names = set()
    csv.field_size_limit(10_000_000)
    with open(CSV_PATH, 'r', encoding='utf-8-sig') as f:
        reader = csv.reader(f)
        next(reader, None)
        for row in reader:
            if len(row) >= 6 and row[5].strip() and not is_placeholder_row(row):
                csv_names.add(row[5].strip())

    n_csv = from_csv(wf, args.dry_run)
    n_batch = from_batches(wf, args.dry_run, set(csv_names))
    after = wf.research_content_count()

    with wf.connect() as db:
        researched = db.execute(
            "SELECT COUNT(*) c FROM companies WHERE research_status=?",
            (DEEP_RESEARCHED,)).fetchone()['c']

    print(f"  csv rows ingested      : {n_csv}")
    print(f"  recovered from batches : {n_batch}   <- were lost to the CSV capacity ceiling")
    print(f"  company_research rows  : {before} -> {after}")
    print(f"  companies deep_researched: {researched}")
    if args.dry_run:
        print("\n  --dry-run: nothing written.")
    return 0


if __name__ == '__main__':
    sys.exit(main())
