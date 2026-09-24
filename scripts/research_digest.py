#!/usr/bin/env python3
"""One Telegram message a day listing every company the research loop touched.

Until 2026-09-08 the loop sent two messages per batch ("started", "Complete!") — with twenty
slots a day that was a forty-message barrage every couple of minutes (Peter). Now the batch
files under scripts/batches/ are the record, and this module folds one calendar day of them
into a single digest: new companies, refreshed ones, and anything that needs a human.

Pure functions over the batch files; the only side effect is in `send_digest`, and only when
asked. `python3 research_digest.py --date 2026-09-08` prints; add `--send` to deliver.
"""

import argparse
import datetime
import glob
import json
import os
import re
import sys

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
BATCHES_DIR = os.path.join(SCRIPTS_DIR, 'batches')
PROGRESS_PATH = os.path.join(SCRIPTS_DIR, 'build_progress.json')

# Telegram caps a message at 4096 characters; leave room for the "(2/3)" marker and for the
# transport's own framing.
CHUNK_LIMIT = 3500

PROVIDER_NAMES = {'gemini': 'Gemini', 'agy_claude': 'Claude via Antigravity',
                  'claude': 'Claude', 'codex': 'Codex'}


def _ticker(name):
    m = re.search(r'\(([^)]+)\)\s*$', name or '')
    return m.group(1).strip() if m else ''


def batch_files(day, batches_dir=None):
    """The batch files written on `day` (YYYY-MM-DD), oldest first."""
    pattern = os.path.join(batches_dir or BATCHES_DIR, f'batch_api_*_{day}.json')
    return sorted(glob.glob(pattern))


def collect_day(day, batches_dir=None, progress_path=None):
    """Fold one day's batch files into counts and company lists.

    A company is `new` when the batch claimed it (first deep research) and `refreshed` when
    it updated a row that already existed — maintenance batches, and discoveries that turned
    out to be duplicates of a company already held. Country and sector come from the batch
    record so the digest reads as a list of businesses, not tickers.
    """
    out = {'day': day, 'batches': [], 'new': [], 'refreshed': [], 'review': [],
           'requeued': [], 'excluded': [], 'providers': {}, 'progress': None,
           'requested': []}
    seen = set()
    for path in batch_files(day, batches_dir):
        try:
            with open(path, encoding='utf-8') as f:
                batch = json.load(f)
        except (OSError, ValueError):
            continue
        summary = batch.get('summary') or {}
        by_ticker = {}
        for record in batch.get('records') or []:
            ticker = _ticker(record.get('Company', ''))
            if ticker:
                by_ticker[ticker] = record
        provider = batch.get('provider', '')
        source = batch.get('source', '')
        requested = {_ticker(n) or n for n in batch.get('requested') or []}
        out['requested'].extend(n for n in batch.get('requested') or []
                                if n not in out['requested'])
        out['providers'][provider] = out['providers'].get(provider, 0) + 1
        out['batches'].append({'label': batch.get('label', os.path.basename(path)),
                               'provider': provider, 'source': source,
                               'new': len(summary.get('claimed') or []),
                               'refreshed': len(summary.get('updated') or [])})

        def describe(name):
            record = by_ticker.get(_ticker(name)) or {}
            return {'name': name, 'country': record.get('Country', ''),
                    'sector': record.get('Sub-Sector') or record.get('Industry', ''),
                    'tier': record.get('Tier', ''),
                    'requested': (_ticker(name) or name) in requested}

        for name in summary.get('claimed') or []:
            key = _ticker(name) or name
            if key not in seen:
                seen.add(key)
                out['new'].append(describe(name))
        for name in summary.get('updated') or []:
            key = _ticker(name) or name
            if key not in seen:
                seen.add(key)
                out['refreshed'].append(describe(name))
        out['review'].extend(batch.get('failed') or [])
        out['requeued'].extend(batch.get('requeued') or [])
        out['excluded'].extend(batch.get('excluded') or [])
    try:
        with open(progress_path or PROGRESS_PATH, encoding='utf-8') as f:
            progress = json.load(f)
        out['progress'] = (int(progress.get('deep_research_completed', 0)),
                           int(progress.get('total_capacity', 100000)))
    except (OSError, ValueError, TypeError):
        pass
    return out


def format_digest(data):
    """The digest as Telegram Markdown. Empty day → one honest line, never silence."""
    day = data['day']
    try:
        weekday = datetime.date.fromisoformat(day).strftime('%a')
    except ValueError:
        weekday = ''
    head = f"📋 *Daily research digest — {weekday} {day}*".replace('  ', ' ')
    if not data['batches']:
        return head + "\nNo batches ran. Check the dashboard if that was not expected."
    providers = ', '.join(f"{n} {PROVIDER_NAMES.get(p, p)}" for p, n in
                          sorted(data['providers'].items(), key=lambda kv: -kv[1]))
    maintenance = sum(1 for b in data['batches'] if b['source'] == 'maintenance')
    lines = [head,
             f"{len(data['batches'])} batches ({providers}"
             f"{f', {maintenance} maintenance' if maintenance else ''}) · "
             f"{len(data['new'])} new · {len(data['refreshed'])} refreshed · "
             f"{len(data['review'])} for review · {len(data['requeued'])} requeued · "
             f"{len(data['excluded'])} excluded"]
    if data['progress']:
        done, cap = data['progress']
        lines.append(f"Fully researched: {done:,} / {cap:,}")
    if data['new']:
        lines.append(f"\n*New companies ({len(data['new'])})*")
        for i, c in enumerate(data['new'], 1):
            extra = ' · '.join(x for x in (c['country'], c['sector']) if x)
            lines.append(f"{i}. {c['name']}" + (f" · {extra}" if extra else '')
                         + (' ★ requested by you' if c.get('requested') else ''))
    if data['refreshed']:
        lines.append(f"\n*Refreshed ({len(data['refreshed'])})*")
        lines.append(', '.join(c['name'] for c in data['refreshed']))
    if data['review']:
        lines.append(f"\n⚠ *Manual review ({len(data['review'])})*: "
                     + ', '.join(data['review']))
    if data['requeued']:
        lines.append(f"\n↩ *Requeued ({len(data['requeued'])})*: "
                     + ', '.join(data['requeued']))
    if data['excluded']:
        lines.append(f"\n⊘ *Excluded ({len(data['excluded'])})*: "
                     + ', '.join(data['excluded']))
    return '\n'.join(lines)


def chunk(text, limit=CHUNK_LIMIT):
    """Split on line boundaries into pieces under `limit`, marked (i/n) when more than one."""
    if len(text) <= limit:
        return [text]
    pieces, current = [], ''
    for line in text.split('\n'):
        candidate = f'{current}\n{line}' if current else line
        if len(candidate) > limit and current:
            pieces.append(current)
            current = line
        else:
            current = candidate
    if current:
        pieces.append(current)
    total = len(pieces)
    return [f'{piece}\n({i}/{total})' for i, piece in enumerate(pieces, 1)]


def send_digest(day, sender, batches_dir=None, progress_path=None):
    """Build and deliver the digest for `day` through `sender(text)`. Returns the pieces."""
    pieces = chunk(format_digest(collect_day(day, batches_dir, progress_path)))
    for piece in pieces:
        sender(piece)
    return pieces


def main(argv=None):
    ap = argparse.ArgumentParser(description='Daily research digest for the Orchestrator bot')
    ap.add_argument('--date', default=(datetime.date.today()
                                       - datetime.timedelta(days=1)).isoformat(),
                    help='YYYY-MM-DD (default: yesterday)')
    ap.add_argument('--send', action='store_true', help='deliver instead of printing')
    args = ap.parse_args(argv)
    if args.send:
        sys.path.insert(0, SCRIPTS_DIR)
        from apply_batch import send_telegram
        pieces = send_digest(args.date, send_telegram)
        print(f'sent {len(pieces)} message(s) for {args.date}')
    else:
        for piece in chunk(format_digest(collect_day(args.date))):
            print(piece)
            print('-' * 40)


if __name__ == '__main__':
    main()
