"""Read-only extraction and deterministic rendering for maintenance-update posts."""
from __future__ import annotations

import datetime as dt
import json
from zoneinfo import ZoneInfo


def _maintenance_post_from_row(row):
    result = _json_object(row['result_json'])
    item = _json_object(row['item_json'])
    screening = item.get('_maintenance_screening')
    screening = screening if isinstance(screening, dict) else {}
    changes = {key: result[key] for key in sorted(result)
               if key != 'Company' and isinstance(result[key], str) and result[key].strip()}
    return {
        'batch_seq': row['batch_seq'],
        'batch_label': row['batch_label'],
        'completed_at': row['completed_at'],
        'position': row['position'],
        'company_id': row['company_id'],
        'ticker': row['ticker'] or '',
        'company_name': row['company_name'],
        'country': row['country'] or '',
        'changes': changes,
        'evidence': {
            'reason': screening.get('reason') or '',
            'evidence_date': screening.get('evidence_date') or '',
            'source_urls': screening.get('source_urls')
            if isinstance(screening.get('source_urls'), list) else [],
            'screened_at': screening.get('screened_at') or '',
        },
    }


def _digest_date_bounds(digest_date):
    if not isinstance(digest_date, str) or len(digest_date) != 10:
        raise ValueError('digest date must use YYYY-MM-DD')
    try:
        local_start = dt.datetime.combine(
            dt.date.fromisoformat(digest_date), dt.time.min,
            tzinfo=ZoneInfo('Asia/Taipei'),
        )
    except ValueError as exc:
        raise ValueError('digest date must use YYYY-MM-DD') from exc
    return (
        local_start.astimezone(dt.timezone.utc).isoformat(timespec='seconds'),
        (local_start + dt.timedelta(days=1)).astimezone(dt.timezone.utc).isoformat(
            timespec='seconds'),
    )


def _maintenance_rows(workflow, limit, applied_date=None, countries=None):
    date_clause, args = '', []
    if applied_date is not None:
        utc_start, utc_end = _digest_date_bounds(applied_date)
        date_clause = " AND bi.updated_at >= ? AND bi.updated_at < ?"
        args.extend([utc_start, utc_end])
    country_clause = ''
    if countries is not None:
        wanted = sorted({str(country).strip().casefold() for country in countries if str(country).strip()})
        if not wanted:
            return []
        country_clause = ' AND LOWER(TRIM(c.country)) IN (' + ', '.join('?' for _ in wanted) + ')'
        args.extend(wanted)
    limit_clause = ' LIMIT ?' if limit is not None else ''
    if limit is not None:
        args.append(limit)
    with workflow.connect() as db:
        return db.execute(f"""SELECT br.seq AS batch_seq, br.label AS batch_label,
                    br.completed_at, bi.position, bi.item_json, bi.result_json,
                    c.id AS company_id, c.ticker, c.company_name, c.country
                FROM batch_runs br
                JOIN batch_items bi ON bi.batch_id=br.id
                JOIN companies c ON c.id=bi.company_id
                WHERE br.mode='maintenance' AND bi.status='applied'{date_clause}{country_clause}
                ORDER BY {'bi.updated_at DESC, bi.id DESC' if applied_date is not None else 'br.seq DESC, bi.position ASC, bi.id ASC'}
                {limit_clause}""", args).fetchall()


def _json_object(value):
    try:
        parsed = json.loads(value or '{}')
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _json_urls(value):
    try:
        parsed = json.loads(value or '[]')
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def markdown_escape(value):
    """Escape Markdown punctuation in provider/model supplied text."""
    text = str(value or '')
    return ''.join('\\' + char if char in r'\\`*_{}[]<>()#+-.!|>' else char for char in text)


def extract_maintenance_posts(workflow, limit=20):
    """Return applied maintenance work joined to its persisted screening evidence.

    This boundary is deliberately read-only: CSV remains the research record and the
    workflow DB supplies operational status plus evidence for post previews/exports.
    """
    if limit < 1:
        return []
    return [_maintenance_post_from_row(row) for row in _maintenance_rows(workflow, limit)]


def extract_daily_maintenance_posts(workflow, digest_date, limit=20, countries=None):
    """Return only that day's applied, material maintenance updates in stable order."""
    if limit < 1:
        return []
    return [post for post in (
        _maintenance_post_from_row(row)
        for row in _maintenance_rows(workflow, None, applied_date=digest_date, countries=countries)
    ) if post['changes']][:limit]


def extract_daily_group_digest_posts(workflow, digest_date, limit=5):
    """Select one day's Taiwan/USA research and maintenance updates for group delivery."""
    if limit < 1:
        return []
    utc_start, utc_end = _digest_date_bounds(digest_date)
    with workflow.connect() as db:
        rows = db.execute("""SELECT br.seq AS batch_seq, br.label AS batch_label,
                    br.completed_at, br.mode, bi.position, bi.id AS item_id,
                    bi.item_json, bi.result_json, c.id AS company_id, c.ticker,
                    c.company_name, c.country, c.source AS company_source
                FROM batch_runs br
                JOIN batch_items bi ON bi.batch_id=br.id
                JOIN companies c ON c.id=bi.company_id
                WHERE br.mode IN ('research', 'maintenance') AND bi.status='applied'
                    AND bi.updated_at >= ? AND bi.updated_at < ?
                    AND LOWER(TRIM(c.country)) IN ('taiwan', 'usa')
                ORDER BY CASE WHEN br.mode='research' AND c.source='new' THEN 0 ELSE 1 END,
                    bi.updated_at DESC, bi.id DESC""", (utc_start, utc_end)).fetchall()
    posts, seen = [], set()
    for row in rows:
        if row['company_id'] in seen:
            continue
        post = _maintenance_post_from_row(row)
        if not post['changes']:
            continue
        post['digest_kind'] = ('new_company' if row['mode'] == 'research'
                               and row['company_source'] == 'new' else row['mode'])
        post['priority'] = 0 if post['digest_kind'] == 'new_company' else 1
        seen.add(row['company_id'])
        posts.append(post)
        if len(posts) == limit:
            break
    return posts


def render_maintenance_markdown(posts):
    """Render extracted posts deterministically without formatting provider text as Markdown."""
    lines = ['# Maintenance updates']
    for post in posts:
        lines.extend([
            '',
            f"## {markdown_escape(post['company_name'])}",
            f"Batch: {post['batch_seq']}",
        ])
        evidence = post['evidence']
        if evidence['evidence_date']:
            lines.append(f"Evidence date: {markdown_escape(evidence['evidence_date'])}")
        if evidence['reason']:
            lines.append(f"Screening: {markdown_escape(evidence['reason'])}")
        for key, value in post['changes'].items():
            lines.append(f"- {markdown_escape(key)}: {markdown_escape(value)}")
        for url in evidence['source_urls']:
            lines.append(f"- Source: {markdown_escape(url)}")
    return '\n'.join(lines) + '\n'


def export_maintenance_posts(posts):
    """Return deterministic JSON for a future sender/exporter without performing I/O."""
    return json.dumps(posts, ensure_ascii=False, sort_keys=True, indent=2) + '\n'


def _digest_excerpt(value, limit=90):
    """Keep each company line short enough for a five-company Telegram digest."""
    text = ' '.join(str(value or '').split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + '…'


def _digest_company_title(post):
    name, ticker = post['company_name'], post['ticker']
    return name if not ticker or f'({ticker})' in name else f'{name}（{ticker}）'


def render_daily_maintenance_digest(posts, digest_date):
    """Render the newest five maintenance updates as concise Traditional Chinese text."""
    if not posts:
        return ''
    lines = [
        f'📊 每日投資維護摘要｜{digest_date}',
        f'最近完成維護的 {len(posts)} 家公司（依更新時間排序）',
    ]
    fields = (
        ('催化劑', ('12M Catalysts', 'Catalyst')),
        ('主要風險', ('Key Investment Risks',)),
        ('地緣風險', ('Geopolitical Exposure',)),
    )
    for index, post in enumerate(posts, 1):
        lines.extend(['', f'{index}. {_digest_company_title(post)}'])
        for label, keys in fields:
            value = next((post['changes'][key] for key in keys if post['changes'].get(key)), '')
            if value:
                lines.append(f'• {label}：{_digest_excerpt(value)}')
        urls = post['evidence']['source_urls']
        if urls:
            lines.append(f'• 資料來源：{urls[0]}')
        else:
            lines.append('• 資料來源：此舊資料尚未儲存連結。')
    return '\n'.join(lines)


def model_digest_prompt(posts):
    """Build one bounded, source-faithful request for concise company summaries."""
    items = []
    for post in posts:
        changes = post.get('changes', {})
        items.append({
            'ticker': post['ticker'],
            'company': post['company_name'],
            'country': post.get('country', ''),
            'catalyst': _digest_excerpt(changes.get('12M Catalysts') or changes.get('Catalyst'), 220),
            'risk': _digest_excerpt(changes.get('Key Investment Risks'), 220),
            'geopolitical_risk': _digest_excerpt(changes.get('Geopolitical Exposure'), 220),
        })
    return (
        '你是投資研究摘要編輯。只能根據下列資料，為每家公司寫一至兩句繁體中文摘要；'
        '合併催化劑、主要風險與地緣風險，不得補充未提供的事實或投資建議。'
        '每則摘要不得超過120個中文字。僅輸出 JSON 陣列，格式為 '
        '[{"ticker":"代號","summary":"摘要"}]。\n\n資料：\n'
        + json.dumps(items, ensure_ascii=False, separators=(',', ':'))
    )


def parse_model_digest_summaries(posts, response):
    """Accept only the requested ticker summaries from a model JSON response."""
    try:
        text = str(response or '').strip()
        if text.startswith('```') and text.endswith('```'):
            text = '\n'.join(text.splitlines()[1:-1]).strip()
        data = json.loads(text)
    except (TypeError, ValueError):
        return {}
    expected = {post['ticker'] for post in posts}
    summaries = {}
    if not isinstance(data, list):
        return summaries
    for item in data:
        if not isinstance(item, dict):
            continue
        ticker = str(item.get('ticker', '')).strip()
        summary = ' '.join(str(item.get('summary', '')).split())
        if ticker in expected and summary and ticker not in summaries:
            summaries[ticker] = _digest_excerpt(summary, 120)
    return summaries


def render_model_daily_digest(posts, summaries, digest_date):
    """Render model summaries without sources; missing summaries are never invented."""
    lines = [
        f'📊 每日投資維護摘要｜{digest_date}',
        f'最近完成維護的 {len(posts)} 家台灣／美國公司（依更新時間排序）',
    ]
    for index, post in enumerate(posts, 1):
        summary = summaries.get(post['ticker'])
        if summary:
            lines.extend(['', f'{index}. {_digest_company_title(post)}', summary])
    return '\n'.join(lines)


def export_daily_maintenance_digest(posts, digest_date):
    """Return deterministic JSON for a later explicit delivery gate without I/O."""
    return json.dumps({'date': digest_date, 'posts': posts}, ensure_ascii=False,
                      sort_keys=True, indent=2) + '\n'


def daily_maintenance_digest_preview(workflow, digest_date, recipients, limit=5,
                                     countries=('Taiwan', 'USA')):
    """Build a read-only daily preview and expose ledger status without any delivery."""
    recipients = sorted(
        ({'label': str(row['label']), 'chat_id': str(row['chat_id'])} for row in recipients),
        key=lambda row: (row['label'].casefold(), row['chat_id']),
    )
    posts = extract_daily_maintenance_posts(workflow, digest_date, limit, countries=countries)
    return {
        'date': digest_date,
        'posts': posts,
        'markdown': render_daily_maintenance_digest(posts, digest_date),
        'export': export_daily_maintenance_digest(posts, digest_date),
        'recipients': recipients,
        'delivery': workflow.digest_delivery_status(digest_date, recipients),
        'preview_only': True,
    }
