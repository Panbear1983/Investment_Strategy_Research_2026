#!/usr/bin/env python3
"""Which sectors the research corpus actually holds, and which it is starving.

The `industry` column is free text written by whichever model researched the company: ~1,690
distinct labels for ~2,400 companies, ranging from '半導體' to a full descriptive sentence. A
count of distinct values is therefore meaningless — it reports 1,690 and tells you nothing.

So coverage is measured against a curated theme vocabulary by substring, the same way
screen.select() and the work queues match industry. Measured on the live corpus that maps ~70%
of researched companies and leaves ~29% unmapped. The unmapped bucket is reported, never hidden:
a panel that quietly dropped a third of the corpus would misrepresent the very thing it exists
to show. Companies can count toward several themes (several hundred do), so the theme column
deliberately does not sum to the corpus.

Read-only. No model calls, no writes.
"""

import datetime
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from research_state import DEEP_RESEARCHED, RESEARCH_PENDING

# Seeded from the theme sentence inside research_loop.nominate_new()'s prompt, so the panel and
# the nomination prompt speak one language, then extended with the labels that proved to carry
# real volume in the corpus. Override in config.json via "industry_themes".
DEFAULT_THEMES = [
    'AI伺服器', '半導體', '矽光子', '先進封裝', '低軌衛星', '電動車', '先進機器人',
    '生技', '航太', '國防', '航運', '平台經濟', '電子零組件', '光學', '光通訊',
    '記憶體', '設備', '材料', '散熱', '液冷', '網通', '面板', '資安', '能源',
    '金融', '消費', '醫療', '化工', '機械', '汽車', '軟體', '雲端',
]

DEFAULT_THIN_FLOOR = 10
DEFAULT_WINDOW_DAYS = 7


def load_themes(cfg=None):
    """Curated vocabulary from config, falling back to the built-in list.

    A malformed or empty setting falls back rather than yielding an empty panel — the same
    posture load_config takes toward a bad config elsewhere in this repo.
    """
    themes = (cfg or {}).get('industry_themes')
    if not isinstance(themes, list):
        return list(DEFAULT_THEMES)
    cleaned = [str(t).strip() for t in themes if str(t).strip()]
    return cleaned or list(DEFAULT_THEMES)


def _matches(label, theme):
    return theme.lower() in (label or '').lower()


def theme_coverage(workflow, window_days=DEFAULT_WINDOW_DAYS, themes=None,
                   thin_floor=DEFAULT_THIN_FLOOR, now=None):
    """Per-theme coverage plus the honest totals around it.

    One pass over companies and one over recent batch work, then matching in Python — the same
    trade research_state makes for company_research ('filtering stays in Python, where a full
    scan of the corpus costs ~10 ms'). Doing it as 4 SQL queries per theme would be 128 round
    trips for the same answer.
    """
    themes = themes or list(DEFAULT_THEMES)
    now = now or datetime.datetime.now(datetime.timezone.utc)
    cutoff = (now - datetime.timedelta(days=window_days)).isoformat()

    with workflow.connect() as db:
        companies = db.execute(
            'SELECT id, industry, research_status FROM companies').fetchall()
        worked = db.execute(
            """SELECT company_id, MAX(updated_at) last_at,
                      SUM(CASE WHEN updated_at >= ? THEN 1 ELSE 0 END) recent
               FROM batch_items WHERE company_id IS NOT NULL
               GROUP BY company_id""", (cutoff,)).fetchall()
    work = {r['company_id']: (r['last_at'], r['recent']) for r in worked}

    rows = []
    for theme in themes:
        held = pending = recent = 0
        last_touched = ''
        for company in companies:
            if not _matches(company['industry'], theme):
                continue
            if company['research_status'] == DEEP_RESEARCHED:
                held += 1
            elif company['research_status'] == RESEARCH_PENDING:
                pending += 1
            last_at, recent_n = work.get(company['id'], ('', 0))
            if recent_n:
                recent += 1
            if last_at and last_at > last_touched:
                last_touched = last_at
        rows.append({'theme': theme, 'held': held, 'pending': pending,
                     'worked': recent, 'last_touched': last_touched,
                     'thin': held < thin_floor})
    rows.sort(key=lambda r: (-r['held'], r['theme']))

    researched = [c for c in companies if c['research_status'] == DEEP_RESEARCHED]
    hits = [sum(1 for t in themes if _matches(c['industry'], t)) for c in researched]
    return {
        'themes': rows,
        'window_days': window_days,
        'thin_floor': thin_floor,
        'researched': len(researched),
        'unmapped': sum(1 for h in hits if h == 0),
        'multi': sum(1 for h in hits if h > 1),
        'worked_themes': sum(1 for r in rows if r['worked']),
        'thin_themes': sum(1 for r in rows if r['thin']),
    }


def focus_match_count(workflow, industry='', country=''):
    """How many companies a focus would actually match, split by whether they are researchable.

    This is the number to echo back when a focus is SET, so a typo or a sector that is not in
    the corpus is caught at that moment rather than discovered as a silently skipped slot.
    """
    from research_state import focus_filters
    clause, args = focus_filters(country or None, industry or None)
    with workflow.connect() as db:
        held = db.execute(
            f'SELECT COUNT(*) n FROM companies WHERE research_status=?{clause}',
            (DEEP_RESEARCHED, *args)).fetchone()['n']
        pending = db.execute(
            f'SELECT COUNT(*) n FROM companies WHERE research_status=?{clause}',
            (RESEARCH_PENDING, *args)).fetchone()['n']
    return {'held': held, 'pending': pending}


def slot_focus_summary(workflow, day=None):
    """How many of a day's slots carry a focus of their own, and which values."""
    day = day or datetime.date.today().isoformat()
    slots = workflow.daily_slots(day)
    focused = [s for s in slots
               if (s.get('country_focus') or '').strip() or (s.get('industry_focus') or '').strip()]
    values = sorted({'/'.join(x for x in ((s.get('country_focus') or '').strip(),
                                          (s.get('industry_focus') or '').strip()) if x)
                     for s in focused})
    return {'total': len(slots), 'focused': len(focused), 'values': values}


def summary_line(coverage, focus=None, slot_summary=None):
    """The one-line form for the always-visible dashboard panel."""
    total = len(coverage['themes'])
    pct = round(100 * coverage['unmapped'] / coverage['researched']) if coverage['researched'] else 0
    bits = [f"{coverage['worked_themes']}/{total} worked {coverage['window_days']}d",
            f"{coverage['thin_themes']} thin", f"{pct}% unmapped"]
    if focus:
        scope = ('until cleared' if focus.get('batches_remaining') is None
                 else f"{focus['batches_remaining']} batches")
        where = '/'.join(x for x in (focus.get('country'), focus.get('industry')) if x)
        bits.append(f"focus: {where} ({scope})")
    elif slot_summary:
        bits.append(f"slot focus {slot_summary['focused']}/{slot_summary['total']}")
    return ' · '.join(bits)
