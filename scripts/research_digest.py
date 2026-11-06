#!/usr/bin/env python3
"""One short Telegram message a day counting what the research loop did.

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
# The loop's own stdout (run_research_loop.sh / the launchd plist append to it). Read-only here:
# its stall markers are the only record of the day's alerts.
LOOP_LOG_PATH = os.path.join(SCRIPTS_DIR, 'loop.log')

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


def collect_day(day, batches_dir=None, progress_path=None, log_path=None):
    """Fold one day's batch files into counts and company lists.

    A company is `new` when the batch claimed it (first deep research) and `refreshed` when
    it updated a row that already existed — maintenance batches, and discoveries that turned
    out to be duplicates of a company already held. Country and sector come from the batch
    record so the digest reads as a list of businesses, not tickers.

    `alerts` holds the day's stall markers from loop.log (see `day_alerts`).

    `requeued` holds each company once, in first-seen order; `requeued_times` counts every
    requeue. When no provider can work, the loop requeues the same front-of-queue companies
    on every slot (2026-11-05: 46 companies requeued 274 times), and counting those repeats
    made the digest read as hundreds of companies.
    """
    out = {'day': day, 'batches': [], 'new': [], 'refreshed': [], 'review': [],
           'requeued': [], 'requeued_times': 0, 'excluded': [], 'providers': {},
           'progress': None, 'requested': [], 'alerts': []}
    seen = set()
    requeued_seen = set()
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
        for name in batch.get('requeued') or []:
            out['requeued_times'] += 1
            key = _ticker(name) or name
            if key not in requeued_seen:
                requeued_seen.add(key)
                out['requeued'].append(name)
        out['excluded'].extend(batch.get('excluded') or [])
    try:
        with open(progress_path or PROGRESS_PATH, encoding='utf-8') as f:
            progress = json.load(f)
        out['progress'] = (int(progress.get('deep_research_completed', 0)),
                           int(progress.get('total_capacity', 100000)))
    except (OSError, ValueError, TypeError):
        pass
    first_label = out['batches'][0]['label'] if out['batches'] else None
    out['alerts'] = day_alerts(day, LOOP_LOG_PATH if log_path is None else log_path,
                               first_label)
    return out


# The loop's stall markers in loop.log, mapped to the short phrase the Alerts line uses. Each is
# the print() that sits beside one of the loop's own Telegram stall messages in research_loop.py;
# the loop itself is unchanged, the digest only reads what it already prints.
_ALERT_ORDER = ('direction list mined out', 'discovery failed', 'discovery unproductive',
                'deep research queue empty', 'research loop idle')
_ALERT_MARKERS = (
    (re.compile(r'direction list mined out'), 'direction list mined out'),
    (re.compile(r'discovery \[(.+?)\] produced nothing — every provider failed'),
     'discovery failed'),
    (re.compile(r'discovery unproductive \[(.+?)\]'), 'discovery unproductive'),
    (re.compile(r'^Queue empty \(\w+\), auto-falling back to maintenance'),
     'deep research queue empty'),
    (re.compile(r'^Queue empty \(\w+\) — '), 'research loop idle'),
)


def _day_lines(lines, day, first_label=None):
    """The slice of loop.log that belongs to `day`.

    loop.log has no timestamps, but the loop prints "📋 daily digest for <day> sent" at its
    first idle tick after 00:05 the next morning, so the lines between the previous day's
    digest marker and this day's are this day. Before this day's digest has gone out (the
    loop calls send_digest just before printing the marker) the slice runs to the end of the
    log. Without the previous day's marker — the loop was down at midnight — the slice starts
    at this day's first batch header, missing at most the alerts printed before it.
    """
    def marker(d):
        text = f'📋 daily digest for {d} sent'
        for i, line in enumerate(lines):
            if line.startswith(text):
                return i
        return None

    try:
        prev = (datetime.date.fromisoformat(day) - datetime.timedelta(days=1)).isoformat()
    except ValueError:
        return []
    start = marker(prev)
    if start is None and first_label:
        header = f'=== {first_label}:'
        start = next((i for i, line in enumerate(lines) if line.startswith(header)), None)
    if start is None:
        return []
    end = marker(day)
    if end is None or end < start:
        end = len(lines)
    return lines[start:end]


def day_alerts(day, log_path, first_label=None):
    """The day's stall alerts from loop.log, as "<what>" or "<what> for <place>" phrases in a
    fixed order (worst first); `_alert_line` folds repeats. A missing log means no alerts."""
    try:
        with open(log_path, encoding='utf-8', errors='replace') as f:
            lines = f.read().split('\n')
    except OSError:
        return []
    found = []
    for line in _day_lines(lines, day, first_label):
        line = line.strip().lstrip('⚠️').strip()
        for pattern, what in _ALERT_MARKERS:
            m = pattern.search(line)
            if m:
                where = m.group(1).strip() if m.groups() else ''
                alert = f'{what} for {where}' if where else what
                if alert not in found:
                    found.append(alert)
                break
    return sorted(found, key=lambda a: next(i for i, w in enumerate(_ALERT_ORDER)
                                            if a.startswith(w)))


def _alert_line(alerts):
    """Fold stall alerts into one line, each said once.

    Alerts of the form "<what> for <place>" share one phrase and list the places, so ten
    "discovery failed for X" alerts read as "discovery failed for A, B, C".
    """
    order, places = [], {}
    for alert in alerts or []:
        alert = str(alert).strip()
        if not alert:
            continue
        m = re.match(r'^(.*\S) for (.+)$', alert)
        what, where = (m.group(1), m.group(2).strip()) if m else (alert, None)
        if what not in places:
            order.append(what)
            places[what] = []
        if where and where not in places[what]:
            places[what].append(where)
    parts = [f"{what} for {', '.join(places[what])}" if places[what] else what
             for what in order]
    return f"Alerts: {'; '.join(parts)}" if parts else ''


def format_digest(data):
    """The digest as Telegram Markdown: a header, one line of counts, and only what needs
    Peter. Empty day → one honest line, never silence.

    Shrunk on 2026-11-03 (Peter: "These reports are too long" ... "Yes to all counts"): no
    company-by-company lists for new, refreshed, requeued or excluded. The full detail stays
    in scripts/batches/ and on the dashboard.
    """
    day = data['day']
    try:
        weekday = datetime.date.fromisoformat(day).strftime('%a')
    except ValueError:
        weekday = ''
    head = f"📋 *Daily research digest — {weekday} {day}*".replace('  ', ' ')
    if not data['batches']:
        return head + "\nNo batches ran. Check the dashboard if that was not expected."
    requeued = f"{len(data['requeued'])} requeued"
    times = data.get('requeued_times', 0)
    if times > len(data['requeued']):
        requeued += f" ({times} times)"
    counts = (f"{len(data['batches'])} batches · {len(data['new'])} new · "
              f"{len(data['refreshed'])} refreshed · {len(data['review'])} for review · "
              f"{requeued} · {len(data['excluded'])} excluded")
    if data.get('progress'):
        done, cap = data['progress']
        counts += f" · Fully researched: {done:,} / {cap:,}"
    lines = [head, counts]
    if data['review']:
        lines.append(f"⚠ Needs you: {len(data['review'])} for manual review — "
                     + ', '.join(data['review']))
    alert = _alert_line(data.get('alerts'))
    if alert:
        lines.append(alert)
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
