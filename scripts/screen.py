#!/usr/bin/env python3
"""爸菲特 / Wanna Botffet — deterministic screener over the research corpus.

Answers the command path of the assistant: filter/search the 22-column research CSV and
return rows. No model, no network, no database — a full scan of ~1,200 rows is ~3 ms, so
an index would add a staleness contract and buy nothing.

Two deliberate constraints:

1. **Stdlib only.** Restricted callers may run this with no network and no site-packages.
2. **Self-contained — no local imports.** This file is the single artifact such a caller is
   given. It must never pull in scripts/, which holds live Telegram credentials
   (tg_config.json, dad_assistant.local.json). Small helpers are therefore duplicated here
   with a pointer to their source of truth, and tests/test_screener.py asserts the copies
   still agree with the originals.

Searching is plain substring matching, not tokenized. A tokenizer (e.g. FTS5 trigram)
cannot match terms shorter than 3 characters, and the search units in this corpus are
2-character Chinese terms — 液冷, 毛利, 龍頭, 認證. Those would silently return nothing.
"""

import argparse
import csv
import datetime as dt
import json
import os
import sys

# Mirrors apply_batch.COLS. Column order is the CSV's contract; do not reorder.
COLS = [
    'Country', 'Timeframe', 'Sub-Sector', 'Industry', 'Tier', 'Company',
    'Capital/Market Cap', 'Core Business', 'Clients & Orders', 'Technical Moat',
    'Revenue Breakdown', 'Gross Margin Profile', 'Key Competitors', '12M Catalysts',
    'Key Investment Risks', 'CEO & Management', 'Core Patents & IP', '3-Year M&A',
    'CapEx & Expansion', 'Geopolitical Exposure', 'M&A Potential', 'Recent Insider Trading',
]
IDX = {name: i for i, name in enumerate(COLS)}

COUNTRY, TIMEFRAME, SUBSECTOR, INDUSTRY, TIER, COMPANY = 0, 1, 2, 3, 4, 5

# Columns --match searches. Sub-Sector, Industry and Company are included because they carry
# real content a searcher wants (`液冷` appears as a Sub-Sector theme). Country, Timeframe and
# Tier are excluded: they are low-cardinality facets with their own flags, so including them
# would make `--match 台灣` return every Taiwanese row.
PROSE_COLS = [SUBSECTOR, INDUSTRY, COMPANY] + list(range(6, len(COLS)))

# Compact default projection; --full returns all 22 columns.
DEFAULT_FIELDS = ['Company', 'Country', 'Industry', 'Tier', 'Capital/Market Cap',
                  'Core Business', '12M Catalysts', 'Key Investment Risks']

DEFAULT_CSV = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           '..', 'datasets', 'Global_100k_Investment_Database.csv')

# --- duplicated helpers (see module docstring) -------------------------------------------

# Source of truth: apply_batch.PLACEHOLDER_MARKERS / is_placeholder_cell / is_placeholder_row.
PLACEHOLDER_MARKERS = ('等待', '調查中', 'API抓取中', '深度調查')


def is_placeholder_cell(value):
    # The length cap keeps genuine narrative that merely mentions e.g. 反壟斷調查中 from matching.
    v = (value or '').strip()
    return v.startswith('等待') or (len(v) <= 20 and any(m in v for m in PLACEHOLDER_MARKERS))


def is_placeholder_row(row):
    return 'Global_Entity' in row[COMPANY] or 'placeholder' in row[COMPANY].lower()


def row_is_complete(row):
    return not any(is_placeholder_cell(c) for c in row)


# Source of truth: research_state.canonical_tier. Rule ORDER matters — 二線龍頭 contains 龍頭,
# so it must be tested first or second-tier companies fold into leaders. 龍體 is a recurring
# model typo for 龍頭.
TIER_KEYS = ('leader', 'second_leader', 'hidden_champion', 'potential',
             'supporting', 'minor_supplier', 'unknown')
TIER_LABELS = {
    'leader': '龍頭股', 'second_leader': '二線龍頭', 'hidden_champion': '隱形冠軍',
    'potential': '潛力股', 'supporting': '輔助公司', 'minor_supplier': '次要供應商',
    'unknown': '(未分類)',
}


def canonical_tier(raw):
    """Normalize noisy model-produced tier prose into maintenance cadence groups."""
    value = (raw or '').strip()
    if '二線龍頭' in value:
        return 'second_leader'
    if '龍頭' in value or '龍體' in value:
        return 'leader'
    if '隱形冠軍' in value:
        return 'hidden_champion'
    if '潛力' in value:
        return 'potential'
    if '輔助' in value:
        return 'supporting'
    if '次要供應' in value:
        return 'minor_supplier'
    return 'unknown'


# Source of truth: research_loop.COUNTRY_ALIASES. 'United States' and 'U.S.' are added here
# because the original map lacks them, so those rows canonicalize to themselves upstream.
COUNTRY_ALIASES = {
    'Taiwan': ['Taiwan', '台灣', '臺灣', '台湾'],
    'USA': ['USA', 'U.S.', 'US', 'United States of America', 'United States', '美國', '美国'],
    'Japan': ['Japan', '日本'],
    'South Korea': ['South Korea', 'Korea', '南韓', '韓國', '大韓民國'],
    'China': ['China', '中國', '中国'],
    'Israel': ['Israel', '以色列'],
    'France': ['France', '法國', '法国'],
    'Germany': ['Germany', '德國', '德国'],
    'Netherlands': ['Netherlands', '荷蘭', '荷兰'],
    'UK': ['UK', 'United Kingdom', '英國', '英国'],
    'Canada': ['Canada', '加拿大'],
    'Singapore': ['Singapore', '新加坡'],
    'Switzerland': ['Switzerland', '瑞士'],
    'Sweden': ['Sweden', '瑞典'],
    'India': ['India', '印度'],
    'Australia': ['Australia', '澳洲', '澳大利亞'],
    'Italy': ['Italy', '義大利', '意大利'],
    'Denmark': ['Denmark', '丹麥'],
    'Finland': ['Finland', '芬蘭'],
    'Norway': ['Norway', '挪威'],
    'Belgium': ['Belgium', '比利時'],
    'Austria': ['Austria', '奧地利'],
    'Spain': ['Spain', '西班牙'],
    'Ireland': ['Ireland', '愛爾蘭'],
}


def canonical_country(raw):
    """Longest-alias-first so 'United States' beats the 'US' substring."""
    s = (raw or '').strip()
    if not s:
        return ''
    best, best_len = '', 0
    for canon, aliases in COUNTRY_ALIASES.items():
        for a in aliases:
            if a.lower() in s.lower() and len(a) > best_len:
                best, best_len = canon, len(a)
    return best or s


# --- loading -----------------------------------------------------------------------------

def load_corpus(path=None):
    """Return (rows, meta). Rows are lists padded to len(COLS)."""
    path = os.path.abspath(path or DEFAULT_CSV)
    # Single fields reach ~1.9 KB; the default limit is far above that, but the corpus grows
    # and a truncated field would raise mid-scan rather than degrade.
    csv.field_size_limit(10_000_000)
    # utf-8-sig: the header is BOM-prefixed, so header[0] is '國家 (Country)' only after strip.
    with open(path, 'r', encoding='utf-8-sig', newline='') as f:
        reader = csv.reader(f)
        header = next(reader, [])
        rows = []
        for row in reader:
            if len(row) < 6 or not row[COMPANY].strip():
                continue
            if is_placeholder_row(row):
                continue
            rows.append((row + [''] * len(COLS))[:len(COLS)])
    meta = {
        'source': path,
        'source_mtime': dt.datetime.fromtimestamp(os.path.getmtime(path)).isoformat(timespec='seconds'),
        'rows': len(rows),
        'cols': len(header),
        'complete': sum(1 for r in rows if row_is_complete(r)),
        'header0': header[0] if header else '',
    }
    return rows, meta


# --- querying ----------------------------------------------------------------------------

def select(rows, country=None, tier=None, timeframe=None, industry=None, subsector=None,
           company=None, ticker=None, match=(), match_any=(), complete_only=False):
    """Filter rows. Facets are equality-after-normalization; text is substring."""
    want_country = canonical_country(country) if country else None
    # Accept both the English cadence key and the Chinese label.
    want_tier = None
    if tier:
        want_tier = tier if tier in TIER_KEYS else canonical_tier(tier)

    out = []
    for row in rows:
        if complete_only and not row_is_complete(row):
            continue
        if want_country and canonical_country(row[COUNTRY]) != want_country:
            continue
        if want_tier and canonical_tier(row[TIER]) != want_tier:
            continue
        if timeframe and timeframe.lower() not in row[TIMEFRAME].lower():
            continue
        # Industry is substring, never equality: 954 distinct values across the corpus mean
        # '半導體業' and '半導體' are separate labels for one industry, so equality silently
        # returns about a third of the true matches.
        if industry and industry.lower() not in row[INDUSTRY].lower():
            continue
        if subsector and subsector.lower() not in row[SUBSECTOR].lower():
            continue
        if company and company.lower() not in row[COMPANY].lower():
            continue
        if ticker and ticker.lower() not in row[COMPANY].lower():
            continue
        if match and not all(any(m.lower() in row[c].lower() for c in PROSE_COLS) for m in match):
            continue
        if match_any and not any(any(m.lower() in row[c].lower() for c in PROSE_COLS) for m in match_any):
            continue
        out.append(row)
    return out


def project(row, fields):
    d = {f: row[IDX[f]] for f in fields if f in IDX}
    d['_tier'] = canonical_tier(row[TIER])
    d['_country'] = canonical_country(row[COUNTRY])
    # _complete follows apply_batch's definition (placeholder markers only), so it agrees with
    # what the dashboard counts as a repair row. That definition ignores blank cells, so report
    # those separately — a row can be "complete" and still have nothing to say in six columns.
    d['_complete'] = row_is_complete(row)
    d['_empty_fields'] = sum(1 for c in row if not c.strip())
    return d


def facet_counts(rows, col):
    counts = {}
    for r in rows:
        key = r[col].strip()
        if key:
            counts[key] = counts.get(key, 0) + 1
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))


# --- output ------------------------------------------------------------------------------

def emit(payload, as_json):
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    meta = payload['_meta']
    print(f"# corpus {meta['rows']} rows ({meta['complete']} complete) · "
          f"updated {meta['source_mtime']} · matched {payload.get('matched', 0)}")
    for i, item in enumerate(payload.get('results', []), 1):
        print(f"\n[{i}] {item.get('Company', '?')}  "
              f"{item.get('_country', '')} · {TIER_LABELS.get(item.get('_tier'), '')}"
              f"{'' if item.get('_complete', True) else ' · INCOMPLETE'}")
        for k, v in item.items():
            if k.startswith('_') or k == 'Company' or not v:
                continue
            print(f"    {k}: {v}")


# --- self-test ---------------------------------------------------------------------------

def self_test(path=None):
    """Assert invariants, never fixtures — the loop appends companies daily, so any exact
    row count would turn this red overnight on its own."""
    failures, passed = [], []
    rows, meta = load_corpus(path)

    def check(label, ok, detail=''):
        (passed if ok else failures).append(label)
        print(f"  {'ok  ' if ok else 'FAIL'} {label}{'' if ok else '  <- ' + detail}")

    print(f"screen.py self-test · {meta['source']}")
    check('BOM stripped: header[0] == 國家 (Country)',
          meta['header0'] == '國家 (Country)', repr(meta['header0']))
    check('22 columns', meta['cols'] == len(COLS), f"got {meta['cols']}")
    check('row count above sanity floor (>1000)', meta['rows'] > 1000, f"got {meta['rows']}")
    check('complete rows are the majority', meta['complete'] > meta['rows'] * 0.8,
          f"{meta['complete']}/{meta['rows']}")

    # The case a trigram/tokenized index fails silently: a 2-character CJK term.
    for term in ('液冷', '毛利', '龍頭'):
        n = len(select(rows, match=[term]))
        check(f'2-char substring {term!r} returns > 0', n > 0, 'got 0')

    # Industry equality would under-count; substring must find strictly more.
    eq = sum(1 for r in rows if r[INDUSTRY].strip() == '半導體業')
    sub = len(select(rows, industry='半導體'))
    check('industry substring beats equality', sub > eq, f'substring {sub} vs equality {eq}')

    tiers = {canonical_tier(r[TIER]) for r in rows}
    check('all tiers normalize into known keys', tiers <= set(TIER_KEYS), str(tiers - set(TIER_KEYS)))

    print(f"\n{len(passed)} passed, {len(failures)} failed")
    return 1 if failures else 0


# --- cli ---------------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(description='Deterministic screener over the research corpus.')
    ap.add_argument('--csv', help='corpus path (default: datasets/Global_100k_Investment_Database.csv)')
    ap.add_argument('--self-test', action='store_true', help='assert corpus invariants and exit')
    ap.add_argument('--list-industries', action='store_true')
    ap.add_argument('--list-countries', action='store_true')
    ap.add_argument('--list-tiers', action='store_true')
    ap.add_argument('--list-subsectors', action='store_true')
    ap.add_argument('--min-count', type=int, default=1, help='hide facet values below this count')

    ap.add_argument('--country', help='canonical name or alias, Chinese accepted')
    ap.add_argument('--tier', help='English cadence key or Chinese label')
    ap.add_argument('--timeframe')
    ap.add_argument('--industry', help='SUBSTRING match — equality is unusable, see module docs')
    ap.add_argument('--subsector')
    ap.add_argument('--company')
    ap.add_argument('--ticker')
    ap.add_argument('--match', action='append', default=[], help='substring in prose columns (AND, repeatable)')
    ap.add_argument('--match-any', action='append', default=[], help='substring in prose columns (OR, repeatable)')
    ap.add_argument('--complete-only', action='store_true', help='drop rows with placeholder cells')
    ap.add_argument('--limit', type=int, default=20)
    ap.add_argument('--full', action='store_true', help='return all 22 columns')
    ap.add_argument('--fields', help='comma-separated column subset')
    ap.add_argument('--json', action='store_true')
    args = ap.parse_args(argv)

    if args.self_test:
        return self_test(args.csv)

    rows, meta = load_corpus(args.csv)
    meta['generated_at'] = dt.datetime.now().isoformat(timespec='seconds')

    listers = {'list_industries': INDUSTRY, 'list_countries': COUNTRY,
               'list_tiers': TIER, 'list_subsectors': SUBSECTOR}
    for flag, col in listers.items():
        if getattr(args, flag):
            pairs = [(v, n) for v, n in facet_counts(rows, col) if n >= args.min_count]
            payload = {'_meta': meta, 'facet': COLS[col], 'matched': len(pairs),
                       'values': [{'value': v, 'count': n} for v, n in pairs]}
            if args.json:
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            else:
                print(f"# {COLS[col]} · {len(pairs)} distinct · corpus {meta['rows']} rows "
                      f"· updated {meta['source_mtime']}")
                for v, n in pairs:
                    print(f"  {n:5d}  {v}")
            return 0

    hits = select(rows, country=args.country, tier=args.tier, timeframe=args.timeframe,
                  industry=args.industry, subsector=args.subsector, company=args.company,
                  ticker=args.ticker, match=args.match, match_any=args.match_any,
                  complete_only=args.complete_only)

    fields = COLS if args.full else (
        [f.strip() for f in args.fields.split(',')] if args.fields else DEFAULT_FIELDS)
    payload = {'_meta': meta, 'query': {k: v for k, v in vars(args).items() if v and k != 'csv'},
               'matched': len(hits),
               'results': [project(r, fields) for r in hits[:args.limit]]}
    emit(payload, args.json)
    return 0


if __name__ == '__main__':
    sys.exit(main())
