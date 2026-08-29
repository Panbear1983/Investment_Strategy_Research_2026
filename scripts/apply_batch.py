# Generic batch applier for the 100k Global Investment Database.
# Importable (research_loop.py) or standalone: python3 apply_batch.py batch.json [--label "Batch X"] [--no-telegram]
import csv
import json
import os
import re
import ssl
import subprocess
import sys
import urllib.request

context = ssl._create_unverified_context()

REPO_DIR = os.environ.get(
    'ISR_REPO_DIR',
    '/Users/peter/GitHub/Investment_Strategy_Research_2026'
)
CSV_PATH = os.path.join(REPO_DIR, 'datasets', 'Global_100k_Investment_Database.csv')
PROGRESS_PATH = os.path.join(REPO_DIR, 'scripts', 'build_progress.json')
# The research loop's outbound status updates deliberately use the Orchestrator
# bot via `hermes send`.  The legacy investment token remains in tg_config.json
# for the separate two-way investment database bot; never reuse or remove it here.
ORCHESTRATOR_HERMES_HOME = os.environ.get(
    'ISR_ORCHESTRATOR_HERMES_HOME',
    '/Users/peter/Agents/hermes/hermeshome/profiles/orchestrator',
)
ORCHESTRATOR_TELEGRAM_TARGET = os.environ.get(
    'ISR_ORCHESTRATOR_TELEGRAM_TARGET',
    'telegram:7512954760',
)

# English record keys in CSV column order (0-21). Research records carry 0-20; col 21 is filled here.
COLS = [
    'Country', 'Timeframe', 'Sub-Sector', 'Industry', 'Tier', 'Company',
    'Capital/Market Cap', 'Core Business', 'Clients & Orders', 'Technical Moat',
    'Revenue Breakdown', 'Gross Margin Profile', 'Key Competitors', '12M Catalysts',
    'Key Investment Risks', 'CEO & Management', 'Core Patents & IP', '3-Year M&A',
    'CapEx & Expansion', 'Geopolitical Exposure', 'M&A Potential', 'Recent Insider Trading'
]

PLACEHOLDER_MARKERS = ('等待', '調查中', 'API抓取中', '深度調查')


def send_telegram(text):
    """Send a loop-status update through the Orchestrator bot without exposing its token.

    `hermes send` is a one-shot transport: it does not start an agent loop or
    poll Telegram.  Failures are intentionally non-fatal so research work is
    never lost because a notification transport is temporarily unavailable.
    """
    if not text:
        return False
    env = os.environ.copy()
    env['HERMES_HOME'] = ORCHESTRATOR_HERMES_HOME
    cmd = ['hermes', 'send', '--quiet', '--to', ORCHESTRATOR_TELEGRAM_TARGET, text]
    try:
        res = subprocess.run(cmd, env=env, capture_output=True, text=True,
                             timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        # Print, don't raise: a notification must never lose research work. But it must also
        # never fail in total silence — Telegram is the only push channel, so an undelivered
        # alert previously meant the failure it was reporting became invisible too.
        print(f"  ⚠️  telegram send failed ({type(exc).__name__}): {exc}")
        return False
    if res.returncode != 0:
        print(f"  ⚠️  telegram send failed (exit {res.returncode}): "
              f"{(res.stderr or res.stdout or '').strip()[:200]}")
        return False
    return True


def extract_ticker(company_name):
    m = re.search(r'\(([^)]+)\)\s*$', company_name or '')
    return m.group(1).strip() if m else ''


def get_insider_data(ticker):
    # Quiver Quant insider stats; meaningful for plain US tickers only.
    if not re.fullmatch(r'[A-Z]{1,5}', ticker or ''):
        return "無資料 (美股限定)"
    url = f"https://www.quiverquant.com/insiders/{ticker}"
    req = urllib.request.Request(
        url,
        headers={'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'}
    )
    try:
        with urllib.request.urlopen(req, context=context, timeout=5) as response:
            html = response.read().decode('utf-8')
            match = re.search(r'const insiderGraphData = (\[.*?\]);', html)
            if match:
                data = json.loads(match.group(1).replace("'", '"'))
                res = []
                for q in data:
                    quarter = q.get('Quarter', '')
                    sentiment = q.get('Sentiment', 0)
                    if sentiment == 0:
                        val = "$0"
                    elif abs(sentiment) >= 1000000:
                        val = f"{'-' if sentiment < 0 else ''}${abs(sentiment)/1000000:.2f}M"
                    else:
                        val = f"{'-' if sentiment < 0 else ''}${abs(sentiment)/1000:.0f}K"
                    res.append(f"{quarter}: {val}")
                return ", ".join(res)
    except Exception:
        pass
    return "無資料 (美股限定)"


def is_placeholder_row(row):
    return 'Global_Entity' in row[5] or 'placeholder' in row[5].lower()


def is_placeholder_cell(value):
    # Placeholder cells are short status strings ('等待系統進行深度調查...', '調查中', '深度調查完成');
    # the length cap keeps genuine narrative that merely mentions e.g. 反壟斷調查中 from matching.
    v = (value or '').strip()
    return v.startswith('等待') or (len(v) <= 20 and any(m in v for m in PLACEHOLDER_MARKERS))


def row_is_complete(row):
    return not any(is_placeholder_cell(c) for c in row)


def recount_progress(status_label):
    real = complete = 0
    with open(CSV_PATH, 'r', encoding='utf-8-sig') as f:
        reader = csv.reader(f)
        next(reader)
        for row in reader:
            if not is_placeholder_row(row):
                real += 1
                if row_is_complete(row):
                    complete += 1
    progress = {
        "total_capacity": 100000,
        "real_tickers_harvested": real,
        "deep_research_completed": complete,
        "current_status": f"{status_label} (Fully researched: {complete}/100,000, tickers: {real}). System idle."
    }
    with open(PROGRESS_PATH, 'w', encoding='utf-8') as f:
        json.dump(progress, f, ensure_ascii=False, indent=4)
    return progress


# The exact bilingual header the CSV has always carried. The export must reproduce it
# byte-for-byte: dashboard.scan_db and screen.load_corpus both key off it (screen's self-test
# asserts header[0] == '國家 (Country)' after BOM stripping).
CSV_HEADER = [
    '國家 (Country)', '佈局期 (Timeframe)', '次產業板塊 (Sub-Sector)', '產業類別 (Industry)',
    '產業地位 (Tier)', '公司名稱與代號 (Company)', '資本額/市值 (Capital/Market Cap)',
    '核心業務與產品 (Core Business)', '供應鏈角色與關鍵客戶 (Clients & Orders)',
    '技術護城河優勢 (Technical Moat)', '主要營收占比 (Revenue Breakdown)',
    '毛利率表現 (Gross Margin Profile)', '主要競爭對手 (Key Competitors)',
    '未來1年關鍵催化劑 (12M Catalysts)', '主要投資風險 (Key Investment Risks)',
    '創辦人/CEO與管理層風格 (CEO & Management)', '核心專利與智財權佈局 (Core Patents & IP)',
    '過去三年併購與投資動態 (3-Year M&A)', '資本支出趨勢與擴產計畫 (CapEx & Expansion)',
    'ESG 與地緣政治曝險 (Geopolitical Exposure)', '潛在被併購價值或分拆可能 (M&A Potential)',
    '近期內部人交易 (Recent Insider Trading)',
]


# Module-level so a test can redirect it exactly as it redirects CSV_PATH. Without this,
# redirecting CSV_PATH alone left apply_records writing into the LIVE workflow database —
# a test run polluted two production records before this was added.
# Honour ISR_WORKFLOW_DB like research_loop.py:76 and dashboard.py:43 do. Without it a
# redirected/test run of the loop still wrote through this module into the PRODUCTION database
# and overwrote the production CSV — the module-level constant kept it redirectable by tests,
# but not by the env var the rest of the system uses.
WORKFLOW_DB_PATH = os.environ.get(
    'ISR_WORKFLOW_DB', os.path.join(REPO_DIR, 'scripts', 'research_workflow.sqlite3'))


def _workflow():
    """Lazy import so this module stays cheap and free of an import cycle."""
    from research_state import WorkflowState
    return WorkflowState(WORKFLOW_DB_PATH)


def export_csv(workflow, path=None):
    """Regenerate the CSV from canonical content. The CSV is now an EXPORT, not a store.

    Every existing reader (dashboard.scan_db, screen.load_corpus, dad_assistant,
    research_loop.scan_completed) keeps working unchanged against this file.
    """
    path = path or CSV_PATH
    records = workflow.research_records()
    temp_path = path + f'.tmp{os.getpid()}'
    with open(temp_path, 'w', newline='', encoding='utf-8-sig') as out:
        writer = csv.writer(out)
        writer.writerow(CSV_HEADER)
        for rec in records:
            writer.writerow([rec.get(col, '') for col in COLS])
    os.replace(temp_path, path)
    return len(records)


def apply_records(records, batch_label, fetch_insiders=True):
    """Write research records into the canonical store, then re-export the CSV.

    Until 2026-08-10 this wrote only to the CSV, where a new company could land solely by
    claiming a 'Global_Entity' placeholder row. Those ran out, so every new company fell into
    an `unmatched` list that nothing read — 342 companies were silently discarded over two
    weeks while the workflow DB marked them applied. An UPSERT has no capacity concept, so
    that failure mode cannot recur; `unmatched` now means a genuinely unusable record and is
    reported rather than dropped.

    Maintenance records are partial (MAINTENANCE_KEYS only), so non-empty fields MERGE over
    stored content rather than replacing the record.
    """
    from research_state import DEEP_RESEARCHED, RESEARCH_PENDING
    workflow = _workflow()

    insider_cache = {}
    if fetch_insiders:
        for rec in records:
            t = extract_ticker(rec.get('Company', ''))
            if t and t not in insider_cache:
                insider_cache[t] = get_insider_data(t)

    updated, claimed, unmatched = [], [], []
    for rec in records:
        name = (rec.get('Company') or '').strip()
        if not name:
            # Preserve the refusal (never invent an identity), but make the owner-facing
            # notification actionable instead of emitting a blank label after `: `.
            unmatched.append('<missing Company field>')
            continue
        ticker = extract_ticker(name)

        existing_id = workflow.research_company_id(name, ticker)
        stored = workflow.research_record(existing_id) if existing_id else None

        # Merge: a maintenance record carries only MAINTENANCE_KEYS, so empty fields must not
        # erase stored research. Same rule the CSV path used ("if val: row[i] = val").
        merged = dict(stored) if stored else {col: '' for col in COLS}
        for key in COLS[:21]:
            val = rec.get(key, '')
            if val:
                merged[key] = val
        # Keep the canonical name already on file, as the CSV path did.
        merged['Company'] = (stored or {}).get('Company') or name
        if ticker in insider_cache:
            merged[COLS[21]] = insider_cache[ticker]
        elif not merged.get(COLS[21]):
            merged[COLS[21]] = "無資料 (美股限定)"

        cid = existing_id or workflow.upsert_company(
            {'Company': name, 'Country': merged.get('Country', ''),
             'Industry': merged.get('Industry', ''), 'Tier': merged.get('Tier', '')},
            DEEP_RESEARCHED if row_is_complete([merged.get(c, '') for c in COLS])
            else RESEARCH_PENDING,
            source='new')
        workflow.upsert_research(cid, merged, COLS, source='apply',
                                 is_placeholder_cell=is_placeholder_cell)
        (updated if stored else claimed).append(merged['Company'])

    rows_written = export_csv(workflow)
    progress = recount_progress(f"{batch_label} Complete")

    if unmatched:
        # Never a silent return value again: an unusable record is an alert, because the
        # previous version of this list is exactly how 342 companies disappeared.
        print(f"  ⚠️  {len(unmatched)} record(s) could not be stored: {unmatched[:5]}")
        send_telegram(f"*{batch_label}* — {len(unmatched)} record(s) unusable and NOT stored: "
                      f"{', '.join(str(u) for u in unmatched[:5])}")

    return {
        'batch_label': batch_label,
        'updated': updated,
        'claimed': claimed,
        'unmatched': unmatched,
        'rows_exported': rows_written,
        'progress': progress,
    }


def main():
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    if not args:
        print("Usage: python3 apply_batch.py batch.json [--label 'Batch X'] [--no-telegram]")
        sys.exit(1)
    label = "Manual Batch"
    if '--label' in sys.argv:
        label = sys.argv[sys.argv.index('--label') + 1]
    with open(args[0], 'r', encoding='utf-8') as f:
        data = json.load(f)
    records = data if isinstance(data, list) else data.get('records', [])
    summary = apply_records(records, label)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if '--no-telegram' not in sys.argv:
        p = summary['progress']
        send_telegram(
            f"*{label} applied*\nUpdated: {len(summary['updated'])} | Claimed: {len(summary['claimed'])}\n"
            f"Fully researched: {p['deep_research_completed']} / tickers: {p['real_tickers_harvested']}"
        )


if __name__ == '__main__':
    main()
