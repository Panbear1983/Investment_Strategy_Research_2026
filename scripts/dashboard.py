#!/usr/bin/env python3
# Monitor and daily-schedule conductor TUI for the 100k investment research loop.
#
#   python3 scripts/dashboard.py            full-screen Textual dashboard
#   python3 scripts/dashboard.py --report   one-shot plain-text report (SSH/iPad friendly)
#
# The dashboard never writes research content. Schedule, provider, and review actions
# write orchestration state to the workflow SQLite database; the only other writes
# are dashboard_config.json and — via the roster editor (press g) — the Telegram
# roster (telegram_users.json) and Hermes gateway routes (~/.hermes/config.yaml),
# none of which the loop touches. The filename deliberately avoids "research_loop"
# so `pkill -f research_loop` can never match it.
import argparse
import csv
import datetime
import glob
import json
import os
import random
import re
import signal
import subprocess
import sys
from pathlib import Path

REPO_DIR = os.environ.get(
    'ISR_REPO_DIR',
    '/Users/peter/GitHub/Investment_Strategy_Research_2026'
)
SCRIPTS_DIR = os.path.join(REPO_DIR, 'scripts')
CSV_PATH = os.path.join(REPO_DIR, 'datasets', 'Global_100k_Investment_Database.csv')
# Same resolution as research_loop.py:63 — the dashboard must report on the config the loop
# actually reads. Hardcoding scripts/config.json meant that when the loop moved to
# ISR_CONFIG_PATH on 2026-08-13 the operator's diagnostic surface silently described a
# different file from the one in force.
LOOP_CONFIG_PATH = os.environ.get('ISR_CONFIG_PATH', os.path.join(SCRIPTS_DIR, 'config.json'))
STATE_PATH = os.path.join(SCRIPTS_DIR, 'loop_state.json')
CONTROL_PATH = os.path.join(SCRIPTS_DIR, 'loop_control.json')  # global kill switch
LOG_PATH = os.path.join(SCRIPTS_DIR, 'loop.log')
PROGRESS_PATH = os.path.join(SCRIPTS_DIR, 'build_progress.json')
BATCHES_DIR = os.path.join(SCRIPTS_DIR, 'batches')
QUARANTINE_DIR = os.path.join(SCRIPTS_DIR, 'quarantine')
DASH_CFG_PATH = os.path.join(SCRIPTS_DIR, 'dashboard_config.json')
TG_USERS_PATH = os.path.join(SCRIPTS_DIR, 'telegram_users.json')
HERMES_CONFIG_PATH = os.path.expanduser('~/.hermes/config.yaml')
WORKFLOW_PATH = os.environ.get(
    'ISR_WORKFLOW_DB', os.path.join(SCRIPTS_DIR, 'research_workflow.sqlite3'))

sys.path.insert(0, SCRIPTS_DIR)
try:
    from apply_batch import extract_ticker, is_placeholder_cell, is_placeholder_row
except Exception:
    # Fallback copies so the monitor stays alive even if apply_batch is mid-edit.
    PLACEHOLDER_MARKERS = ('等待', '調查中', 'API抓取中', '深度調查')

    def is_placeholder_cell(value):
        v = (value or '').strip()
        return v.startswith('等待') or (len(v) <= 20 and any(m in v for m in PLACEHOLDER_MARKERS))

    def is_placeholder_row(row):
        return 'Global_Entity' in row[5] or 'placeholder' in row[5].lower()

    def extract_ticker(company_name):
        m = re.search(r'\(([^)]+)\)\s*$', company_name or '')
        return m.group(1).strip() if m else ''

try:
    from research_state import WorkflowState
except Exception:
    WorkflowState = None

try:
    from maintenance_posts import daily_maintenance_digest_preview, extract_maintenance_posts
except Exception:
    daily_maintenance_digest_preview = None
    extract_maintenance_posts = None


try:
    import industry_focus
except Exception:            # the dashboard must open even if a helper is broken
    industry_focus = None

try:
    import video_intel
except Exception:
    video_intel = None


def workflow_state(create=False):
    if WorkflowState is None or (not create and not os.path.exists(WORKFLOW_PATH)):
        return None
    try:
        return WorkflowState(WORKFLOW_PATH)
    except Exception:
        return None


def maintenance_preview(workflow, limit=5):
    """Read-only dashboard preview sourced from the maintenance-post boundary."""
    if workflow is None or extract_maintenance_posts is None:
        return []
    return extract_maintenance_posts(workflow, limit=limit)


def daily_digest_preview(workflow, digest_date=None):
    """Read the local roster only to show a preview/status model; this never delivers."""
    if workflow is None or daily_maintenance_digest_preview is None:
        return {}
    try:
        from telegram_users import load_users
        recipients = [
            {'label': user.label, 'chat_id': user.chat_id}
            for user in load_users(Path(TG_USERS_PATH)).users if user.enabled
        ]
        return daily_maintenance_digest_preview(
            workflow, digest_date or datetime.date.today().isoformat(), recipients)
    except Exception:
        return {}

# The loop may gain a COUNTRY_PRIORITY sort; mirror it for the queue preview if present.
try:
    import research_loop as _rl
    COUNTRY_PRIORITY = getattr(_rl, 'COUNTRY_PRIORITY', None)
except Exception:
    COUNTRY_PRIORITY = None

PROVIDERS = ('gemini', 'agy_claude', 'claude', 'codex')
PROVIDER_STYLE = {'gemini': 'cyan', 'agy_claude': 'bright_magenta', 'claude': 'magenta',
                  'codex': 'bright_blue'}
DASH_DEFAULTS = {'marquee_cells_per_tick': 1, 'marquee_seconds': 8,
                 'poll_seconds': 5, 'history_rows': 10, 'language': 'en'}

# UI chrome strings. Research content (factoid text, field names) is inherently
# Traditional Chinese and is not affected by the language setting.
STRINGS = {
    'en': {
        'research': ' Research  ', 'capacity': ' Capacity  ',
        'done': '{n} done', 'in_prog': '{n} in progress', 'harvested': 'of {n} harvested',
        'cap_line': '{n:,} of 100,000 ({p:.1f}%)',
        'repair_phase': '  — repair phase, no nominations yet', 'today_delta': '  +{n} today',
        'scanning': 'scanning database…',
        'running': ' ● RUNNING ', 'stopped': ' ■ STOPPED ', 'halted': ' ⛔ HALTED ',
        'k_halt': 'HALT ALL', 'k_resume': 'RESUME', 'k_forcekill': 'Force Kill',
        'k_relaunch': 'Relaunch',
        'halt_done': '⛔ Research operation HALTED — daemon will claim no new slots',
        'resume_done': '▶ Research operation RESUMED — slots will run again',
        'forcekill_done': 'Force-killed loop daemon (pid {pid})',
        'forcekill_none': 'No running loop daemon to kill',
        'relaunch_done': 'Relaunched loop daemon (pid {pid})',
        'relaunch_busy': 'Loop already running (pid {pid})',
        'next': 'next ', 'in_m': ' in {m:.0f}m   ',
        'b_running': '▶ running', 'b_orphan': '✝ orphaned',
        'ok_failed': '  {ok} ok / {f} failed of {n}', 'researching': '  — researching {c}',
        'idle_next': 'idle — next: Batch {n} at ',
        'legend': '  ✓ applied   ▶ running   □ upcoming   – no batch (skipped/killed)',
        'no_sched': 'no slot schedule in config.json',
        'hist_title': 'Batch history',
        'hist_cols': ('Time', 'Batch', 'Source', 'Provider', 'Applied', 'Quar'),
        'applied_fmt': '{a} (upd {u}, new {c})',
        'todo': 'TODO\n', 'repair_pending': ' {n} repair rows pending\n',
        'batch_running': '\n Batch {n} in flight ({m}):\n',
        'coming_queue': '\n coming Batch {n} queue ({m}):\n', 'then': ' then: ',
        'queue_clear': ' repair queue clear — nominating new companies\n',
        'alerts': 'ALERTS\n', 'none': ' none',
        'a_dead': ('loop process NOT running — restart: cd repo && nohup caffeinate -i '
                   'python3 -u scripts/research_loop.py --run >> scripts/loop.log 2>&1 &'),
        'a_quar': '{n} quarantined record(s) await review in scripts/quarantine/',
        'a_review': '{n} workflow record(s) require manual review',
        'a_skip': 'slot skipped: {s}',
        'a_legacy': 'build_progress.json is in LEGACY format — stray legacy writer may be active',
        'a_stall': 'no batch applied in {h:.1f}h — loop may be stalled',
        'a_orphan': 'Batch {n} orphaned mid-flight (killed); its companies stay in queue',
        'marquee_wait': ' waiting for research content from the first finished batch…',
        's_title': 'Dashboard settings (dashboard_config.json — loop untouched)',
        's_speed': 'Marquee scroll speed (cells/tick):', 's_secs': 'Seconds per marquee message:',
        's_poll': 'Poll interval (seconds):', 's_rows': 'History rows:',
        's_lang': 'Language — en or zh:', 's_save': 'Save', 's_cancel': 'Cancel',
        'k_quit': 'Quit', 'k_settings': 'Settings', 'k_pause': 'Pause',
        'k_new': 'New batch', 'k_rescan': 'Rescan', 'k_lang': 'Lang',
        'k_providers': 'Providers', 'k_review': 'Review',
        'k_chat': 'Ask 爸菲特',
        'life': ('Lifecycle  deep {deep}  pending {pending}  running {running}  retry {retry}  '
                 'maint {maint}  review {review}  excluded {excluded}'),
        'src_repair': 'repair', 'src_new': 'new',
    },
    'zh': {
        'research': ' 研究進度  ', 'capacity': ' 收錄容量  ',
        'done': '{n} 完成', 'in_prog': '{n} 修復中', 'harvested': '共 {n} 已收錄',
        'cap_line': '{n:,} / 100,000（{p:.1f}%）',
        'repair_phase': '  — 修復階段，尚未提名新公司', 'today_delta': '  今日 +{n}',
        'scanning': '掃描資料庫中…',
        'running': ' ● 運行中 ', 'stopped': ' ■ 已停止 ', 'halted': ' ⛔ 已停機 ',
        'k_halt': '全部停機', 'k_resume': '恢復運行', 'k_forcekill': '強制終止',
        'k_relaunch': '重啟迴圈',
        'halt_done': '⛔ 研究作業已停機 — 不再領取新排程',
        'resume_done': '▶ 研究作業已恢復 — 排程將繼續執行',
        'forcekill_done': '已強制終止迴圈程序（pid {pid}）',
        'forcekill_none': '沒有運行中的迴圈程序可終止',
        'relaunch_done': '已重啟迴圈程序（pid {pid}）',
        'relaunch_busy': '迴圈已在運行（pid {pid}）',
        'next': '下一場 ', 'in_m': '，{m:.0f} 分鐘後   ',
        'b_running': '▶ 進行中', 'b_orphan': '✝ 中斷',
        'ok_failed': '  {ok} 成功 / {f} 失敗，共 {n}', 'researching': '  — 研究中 {c}',
        'idle_next': '待機 — 下一批 Batch {n} 於 ',
        'legend': '  ✓ 已寫入   ▶ 進行中   □ 待執行   – 無批次（跳過/中斷）',
        'no_sched': 'config.json 無排程',
        'hist_title': '批次紀錄',
        'hist_cols': ('時間', '批次', '來源', '供應商', '寫入', '隔離'),
        'applied_fmt': '{a}（更 {u}、新 {c}）',
        'todo': '待辦\n', 'repair_pending': ' {n} 筆待修復\n',
        'batch_running': '\n Batch {n} 進行中（{m}）：\n',
        'coming_queue': '\n 下一批 Batch {n} 佇列（{m}）：\n', 'then': ' 接著：',
        'queue_clear': ' 修復佇列已清空 — 進入新公司提名\n',
        'alerts': '警示\n', 'none': ' 無',
        'a_dead': ('迴圈程序未運行 — 重啟：cd repo && nohup caffeinate -i '
                   'python3 -u scripts/research_loop.py --run >> scripts/loop.log 2>&1 &'),
        'a_quar': '{n} 筆隔離紀錄待審（scripts/quarantine/）',
        'a_review': '{n} 筆流程紀錄需要人工審核',
        'a_skip': '時段跳過：{s}',
        'a_legacy': 'build_progress.json 為舊格式 — 疑似殘留舊寫入程序',
        'a_stall': '{h:.1f} 小時無批次寫入 — 迴圈可能卡住',
        'a_orphan': 'Batch {n} 中斷（被終止）；公司留在佇列',
        'marquee_wait': ' 等待第一個完成批次的研究內容…',
        's_title': '儀表板設定（dashboard_config.json — 不影響迴圈）',
        's_speed': '跑馬燈捲動速度（格/tick）：', 's_secs': '每則訊息秒數：',
        's_poll': '輪詢間隔（秒）：', 's_rows': '批次紀錄行數：',
        's_lang': '語言 — en 或 zh：', 's_save': '儲存', 's_cancel': '取消',
        'k_quit': '離開', 'k_settings': '設定', 'k_pause': '暫停',
        'k_new': '換批', 'k_rescan': '掃描', 'k_lang': '語言',
        'k_providers': '模型', 'k_review': '審核',
        'k_chat': '問爸菲特',
        'life': ('流程  深研 {deep}  待研 {pending}  進行 {running}  重試 {retry}  '
                 '維護 {maint}  審核 {review}  排除 {excluded}'),
        'src_repair': '修復', 'src_new': '新增',
    },
}


def tr(lang, key, **kw):
    s = STRINGS.get(lang, STRINGS['en']).get(key) or STRINGS['en'][key]
    return s.format(**kw) if kw else s


def read_json(path, default=None):
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return default


def load_dash_cfg():
    cfg = dict(DASH_DEFAULTS)
    cfg.update(read_json(DASH_CFG_PATH, {}) or {})
    return cfg


def save_dash_cfg(cfg):
    with open(DASH_CFG_PATH, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, indent=4)


# ---------------- collectors (all read-only) ----------------

def read_loop_config():
    return read_json(LOOP_CONFIG_PATH, {}) or {}


def read_loop_state():
    raw = read_json(STATE_PATH, {}) or {}
    calls = raw.get('calls', 0)
    if not isinstance(calls, dict):  # pre-multi-provider format
        calls = {'gemini': calls}
    if raw.get('date') != datetime.date.today().isoformat():
        calls = {}  # new day: show zeros until the loop's first call rewrites the file
    norm = {p: calls.get(p, 0) for p in PROVIDERS}
    return {'date': raw.get('date', ''), 'calls': norm,
            'batch_seq': raw.get('batch_seq', 0)}


def read_budgets(cfg):
    budgets = cfg.get('daily_call_budgets')
    if not isinstance(budgets, dict):
        budgets = {'gemini': cfg.get('daily_call_budget', 0)}
    return {p: budgets.get(p, 0) for p in PROVIDERS}


def read_control():
    """Kill-switch state. Absent/corrupt file == not halted (mirrors research_loop.load_control)."""
    try:
        with open(CONTROL_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return {'halted': bool(data.get('halted', False))}
    except (OSError, ValueError):
        return {'halted': False}


def write_control(halted):
    """Flip the global kill switch. The daemon re-reads this every conductor tick, so the
    change applies without a restart."""
    data = {'halted': bool(halted),
            'halted_at': datetime.datetime.now().isoformat(timespec='seconds') if halted else '',
            'by': 'dashboard'}
    with open(CONTROL_PATH, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2)
    return data


def relaunch_loop():
    """Start a fresh detached research_loop --run daemon, mirroring the documented restart
    command (see the 'a_dead' alert). Returns the child PID."""
    log = open(LOG_PATH, 'a')
    # caffeinate -i keeps the Mac awake through the overnight schedule (matches the documented
    # restart command); start_new_session detaches like nohup so the daemon outlives the dashboard.
    proc = subprocess.Popen(
        ['caffeinate', '-i', 'python3', '-u',
         os.path.join(SCRIPTS_DIR, 'research_loop.py'), '--run'],
        cwd=REPO_DIR, stdout=log, stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL, start_new_session=True)
    return proc.pid


def loop_process():
    """Find the loop's python daemon. pgrep -f also matches unrelated processes
    (watcher shells) whose multi-line command text mentions research_loop.py,
    so verify each candidate PID's executable and args via ps."""
    try:
        res = subprocess.run(['pgrep', '-f', 'research_loop.py'],
                             capture_output=True, text=True, timeout=5)
        pids = res.stdout.split()
    except Exception:
        pids = []
    for pid_s in pids:
        if not pid_s.isdigit():
            continue
        try:
            comm = subprocess.run(['ps', '-o', 'comm=', '-p', pid_s],
                                  capture_output=True, text=True, timeout=5).stdout.strip()
            if 'python' not in os.path.basename(comm).lower():
                continue  # bash watchers, caffeinate wrapper, etc.
            args = subprocess.run(['ps', '-o', 'args=', '-p', pid_s],
                                  capture_output=True, text=True, timeout=5).stdout
            if '--run' not in args:
                continue  # --once / --dry-run helpers are not the daemon
            since = subprocess.run(['ps', '-o', 'lstart=', '-p', pid_s],
                                   capture_output=True, text=True, timeout=5).stdout.strip()
            return {'pid': int(pid_s), 'since': since}
        except Exception:
            continue
    return {'pid': None, 'since': ''}


def parse_schedule(cfg):
    """[(minutes, provider)] sorted; tolerates future [time, provider, task] entries."""
    sched = []
    for entry in cfg.get('schedule', []):
        hhmm, provider = entry[0], entry[1]
        h, m = map(int, hhmm.split(':'))
        sched.append((h * 60 + m, provider))
    return sorted(sched)


def next_slot(sched, now=None):
    if not sched:
        return None, None
    now = now or datetime.datetime.now()
    minutes_now = now.hour * 60 + now.minute
    for mins, provider in sched:
        if mins > minutes_now:
            return now.replace(hour=mins // 60, minute=mins % 60,
                               second=0, microsecond=0), provider
    mins, provider = sched[0]
    return ((now + datetime.timedelta(days=1))
            .replace(hour=mins // 60, minute=mins % 60, second=0, microsecond=0), provider)


_scan_cache = {'mtime': None, 'data': None}


def scan_db(force=False):
    """Full CSV pass, cached on file mtime. The only expensive collector."""
    try:
        mtime = os.path.getmtime(CSV_PATH)
    except OSError:
        return {'error': f'CSV missing: {CSV_PATH}'}
    if not force and _scan_cache['mtime'] == mtime and _scan_cache['data']:
        return _scan_cache['data']
    complete = harvested = 0
    repair, by_country = [], {}
    with open(CSV_PATH, 'r', encoding='utf-8-sig') as f:
        reader = csv.reader(f)
        next(reader, None)
        for row in reader:
            if len(row) < 6 or is_placeholder_row(row):
                continue
            harvested += 1
            if any(is_placeholder_cell(c) for c in row):
                repair.append((row[5].strip(), row[0].strip()))
                # Aggregate the breakdown on normalized names so 'USA' and '美國'
                # rows count as one country.
                cc = clean_country(row[0]) or row[0].strip()
                by_country[cc] = by_country.get(cc, 0) + 1
            else:
                complete += 1
    if COUNTRY_PRIORITY:
        try:
            prio = (COUNTRY_PRIORITY if isinstance(COUNTRY_PRIORITY, dict)
                    else {c: i for i, c in enumerate(COUNTRY_PRIORITY)})
            repair.sort(key=lambda rc: prio.get(rc[1], 99))
        except Exception:
            pass
    data = {'mtime': mtime, 'complete': complete, 'harvested': harvested,
            'repair': len(repair), 'next_up': repair[:40],
            'by_country': sorted(by_country.items(), key=lambda kv: -kv[1])[:6]}
    _scan_cache.update(mtime=mtime, data=data)
    return data


_coverage_cache = {'mtime': None, 'data': None}


def scan_coverage(force=False):
    """Theme coverage, cached on the workflow DB's mtime.

    The dashboard polls every 5 seconds and this walks the whole companies table, so it must
    not run on that cadence — same reasoning, and same cache shape, as scan_db above. The
    workflow DB is in WAL mode, so the -wal file is what actually moves on a write; check both.
    """
    if industry_focus is None:
        return None
    try:
        mtime = max(os.path.getmtime(p) for p in
                    (WORKFLOW_PATH, WORKFLOW_PATH + '-wal') if os.path.exists(p))
    except (OSError, ValueError):
        return None
    if not force and _coverage_cache['mtime'] == mtime and _coverage_cache['data']:
        return _coverage_cache['data']
    workflow = workflow_state()
    if workflow is None:
        return None
    cfg = read_loop_config()
    try:
        data = industry_focus.theme_coverage(
            workflow,
            window_days=int(cfg.get('industry_window_days',
                                    industry_focus.DEFAULT_WINDOW_DAYS)),
            themes=industry_focus.load_themes(cfg),
            thin_floor=int(cfg.get('industry_thin_floor',
                                   industry_focus.DEFAULT_THIN_FLOOR)))
    except Exception:
        # A coverage failure must never take the dashboard down; the panel just says so.
        return None
    _coverage_cache.update(mtime=mtime, data=data)
    return data


BATCH_LABEL_RE = re.compile(r'(?:API|Maintenance) Batch (\d+) \((\w+)(?:, (\w+))?\)')


def claimed_today(batches, now=None):
    """New tickers harvested today (sum of 'claimed' across today's archives)."""
    today = (now or datetime.datetime.now()).date()
    return sum(len(b['claimed']) for b in batches if b['when'].date() == today)


def read_batches(limit=20):
    out = []
    for path in glob.glob(os.path.join(BATCHES_DIR, 'batch_*.json')):
        d = read_json(path)
        if not d:
            continue
        m = BATCH_LABEL_RE.search(d.get('label', ''))
        summary = d.get('summary', {})
        mtime = os.path.getmtime(path)
        out.append({
            'seq': int(m.group(1)) if m else 0,
            'source': d.get('source', m.group(2) if m else '?'),
            'provider': d.get('provider') or (m.group(3) if m and m.group(3) else 'gemini'),
            'providers_used': d.get('providers_used', []),
            'applied': len(d.get('records', [])),
            'updated': [n for n in summary.get('updated', [])],
            'claimed': [n for n in summary.get('claimed', [])],
            'failed': d.get('failed', []),
            'requeued': d.get('requeued', []),
            'excluded': d.get('excluded', []),
            'mtime': mtime,
            'when': datetime.datetime.fromtimestamp(mtime),
        })
    out.sort(key=lambda b: -b['mtime'])
    return out[:limit]


LOG_HDR_RE = re.compile(
    r'^=== ((?:API|Maintenance) Batch (\d+) \(([^)]*)\)): (\d+) companies ===$', re.M)


def parse_log(tail_bytes=60000):
    info = {'current': None, 'skips': [], 'sleeping': None, 'throttle': False}
    try:
        with open(LOG_PATH, 'rb') as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - tail_bytes))
            text = f.read().decode('utf-8', 'replace')
    except OSError:
        return info
    info['skips'] = re.findall(r'^Slot skipped: (.+)$', text, re.M)[-3:]
    info['throttle'] = bool(re.search(r'quota/rate hit|backing off', text.splitlines()[-1] if text else ''))
    m_sleep = re.findall(r'^Next slot (\d\d:\d\d) \((\w+)(?:, \w+)?\); sleeping', text, re.M)
    if m_sleep:
        info['sleeping'] = m_sleep[-1]
    headers = list(LOG_HDR_RE.finditer(text))
    if headers:
        h = headers[-1]
        seq = int(h.group(2))
        segment = text[h.end():]
        if not re.search(rf'=== (?:API|Maintenance) Batch {seq} .* done:', segment):
            parts = [p.strip() for p in h.group(3).split(',')]
            researching = re.findall(r'^  researching (.+?) \.\.\.$', segment, re.M)
            actual = re.findall(r'^    ok \((\w+)\)$', segment, re.M)
            info['current'] = {
                'seq': seq,
                'source': parts[0] if parts else '?',
                'scheduled_provider': parts[1] if len(parts) > 1 else 'gemini',
                'provider': actual[-1] if actual else (parts[1] if len(parts) > 1 else 'gemini'),
                'size': int(h.group(4)),
                'ok': len(re.findall(r'^    ok$', segment, re.M)),
                'failed': len(re.findall(r'^    FAILED', segment, re.M)),
                'researching': researching[-1] if researching else '',
            }
    return info


def quarantine_count():
    try:
        return len([p for p in os.listdir(QUARANTINE_DIR) if p.endswith('.json')])
    except OSError:
        return 0


def rail_statuses(sched, batches, log_info, proc_alive, now=None):
    """Per-slot outcome for today's timetable."""
    now = now or datetime.datetime.now()
    today = now.date()
    pool = [b for b in batches if b['when'].date() == today]
    slots = []
    ordered = sorted(sched)
    for i, (mins, provider) in enumerate(ordered):
        t = datetime.datetime.combine(today, datetime.time(mins // 60, mins % 60))
        nxt = ordered[i + 1][0] if i + 1 < len(ordered) else mins + 150
        t_end = datetime.datetime.combine(today, datetime.time(min(nxt, 1439) // 60, min(nxt, 1439) % 60))
        slot = {'hhmm': f'{mins // 60:02d}:{mins % 60:02d}', 'provider': provider,
                'status': 'upcoming', 'detail': ''}
        if t <= now:
            match = next((b for b in pool
                          if b['provider'] == provider and t <= b['when'] < t_end), None)
            if match:
                pool.remove(match)
                slot['status'] = 'done'
                slot['detail'] = f"{match['applied']}" + (f"!{len(match['failed'])}" if match['failed'] else '')
            elif (log_info.get('current') and proc_alive
                  and log_info['current'].get('scheduled_provider', log_info['current']['provider']) == provider
                  and t_end > now):
                cur = log_info['current']
                slot['status'] = 'running'
                slot['detail'] = f"{cur['ok'] + cur['failed']}/{cur['size']}"
            else:
                slot['status'] = 'none'
        slots.append(slot)
    return slots


def conductor_rail(workflow, cfg, log_info, now=None):
    """Daily-slot rows formatted for both the TUI and plain-text dashboard."""
    now = now or datetime.datetime.now()
    day = now.date().isoformat()
    workflow.seed_daily_slots(day, cfg.get('schedule', []))
    rows = workflow.daily_slots(day)
    current = log_info.get('current') or {}
    out = []
    for row in rows:
        status = row['status']
        total = row.get('item_count') or cfg.get('batch_size', 20)
        processed = row.get('processed_count', 0)
        validated = row.get('validated_count', 0)
        applied = row.get('applied_count', 0)
        failed = row.get('failed_count', 0)
        scheduled = datetime.datetime.combine(
            now.date(), datetime.time.fromisoformat(row['slot_time']))
        if (status == 'pending' and row['enabled']
                and (now - scheduled).total_seconds() > 300):
            status = 'missed'
        if not row['enabled']:
            status, detail = 'disabled', 'OFF'
        elif status == 'pending':
            detail = f"0/{total}"
        elif status == 'running':
            done = max(processed, current.get('ok', 0) + current.get('failed', 0))
            total = current.get('size') or total
            detail = f'{done}/{total}'
        elif status in ('complete', 'partial'):
            if row.get('batch_status') == 'partial' and not row.get('batch_completed_at'):
                done = max(processed, applied, validated)
            else:
                done = applied
            detail = f'{done}/{total}' + (f' !{failed}' if failed else '')
        elif status == 'missed':
            detail = 'MISSED'
        elif status == 'skipped':
            detail = 'SKIPPED'
        else:
            detail = row.get('detail') or status.upper()
        focus_parts = []
        focus_detail_parts = []
        if row.get('country_focus'):
            focus_parts.append(row['country_focus'])
            focus_detail_parts.append(f"Country: {row['country_focus']}")
        if row.get('industry_focus'):
            focus_parts.append(row['industry_focus'])
            focus_detail_parts.append(f"Industry: {row['industry_focus']}")
        focus = ' • '.join(focus_parts)
        focus_detail = ' • '.join(focus_detail_parts)
        focused = bool(focus)
        marker = '★' if focused and row['enabled'] else ('☆' if focused else '')
        mode_label = 'MAINT' if row.get('mode') == 'maintenance' else 'DEEP'
        out.append({**row, 'hhmm': row['slot_time'], 'raw_status': row['status'], 'status': status,
                    'hhmm_display': f'{marker}{row["slot_time"]}' if marker else row['slot_time'],
                    'detail': detail, 'focus': focus, 'focus_detail': focus_detail,
                    'focused': focused, 'focus_marker': marker, 'mode_label': mode_label})
    return out


def focused_slot_summary(rail, now=None, standing=None):
    """Always-visible description of the focus rules in force.

    A standing focus leads, because it applies to every slot that has no focus of its own —
    including tomorrow's, which the per-slot rail cannot show. Per-slot overrides follow.
    """
    now = now or datetime.datetime.now()
    now_hhmm = now.strftime('%H:%M')
    standing_text = ''
    if standing:
        where = '/'.join(x for x in (standing.get('country'), standing.get('industry')) if x)
        scope = ('until cleared' if standing.get('batches_remaining') is None
                 else f"{standing['batches_remaining']} batches left")
        by = standing.get('set_by') or 'peter'
        standing_text = f"★ STANDING FOCUS  {where} ({scope}, set by {by})"
    active = [s for s in rail
              if s.get('focused') and s.get('enabled')
              and s.get('raw_status', s.get('status')) == 'pending'
              and s.get('slot_time', '') > now_hhmm]
    if not active:
        return standing_text or 'Focus: no standing focus, no focused future slots'
    entries = []
    for slot in active:
        mode = 'MAINT' if slot.get('mode') == 'maintenance' else 'DEEP'
        entries.append(
            f"{slot['slot_time']} {mode} — {slot.get('focus_detail') or slot.get('focus')}")
    slot_text = '★ SLOT FOCUS  ' + '  |  '.join(entries)
    return f'{standing_text}   ‖   {slot_text}' if standing_text else slot_text


def suppress_completed_log_batch(log_info, workflow):
    """A crash can leave the last log header open after SQLite was reconciled."""
    current = log_info.get('current')
    if not current or not workflow:
        return log_info
    persisted = workflow.batch_run(current['seq'])
    if not persisted or persisted['status'] in ('running', 'applying'):
        return log_info
    cleaned = dict(log_info)
    cleaned['current'] = None
    return cleaned


def build_snapshot():
    cfg = read_loop_config()
    sched = parse_schedule(cfg)
    proc = loop_process()
    log_info = parse_log()
    batches = read_batches()
    progress = read_json(PROGRESS_PATH, {}) or {}
    workflow = workflow_state(create=True)
    now = datetime.datetime.now()
    if workflow:
        log_info = suppress_completed_log_batch(log_info, workflow)
        rail = conductor_rail(workflow, cfg, log_info, now)
        upcoming = next((s for s in rail if s['enabled'] and s['status'] == 'pending'
                         and s['slot_time'] > now.strftime('%H:%M')), None)
        nxt_t = (now.replace(hour=int(upcoming['slot_time'][:2]),
                             minute=int(upcoming['slot_time'][3:]), second=0, microsecond=0)
                 if upcoming else None)
        nxt_p = upcoming['provider'] if upcoming else None
        if not upcoming and sched:
            first_mins, nxt_p = sched[0]
            nxt_t = ((now + datetime.timedelta(days=1)).replace(
                hour=first_mins // 60, minute=first_mins % 60, second=0, microsecond=0))
    else:
        rail = rail_statuses(sched, batches, log_info, proc['pid'] is not None, now)
        nxt_t, nxt_p = next_slot(sched, now)
    standing = workflow.standing_focus() if workflow else None
    focus_summary = focused_slot_summary(rail, now, standing)
    return {
        'coverage': scan_coverage(),
        'standing_focus': standing,
        'focus_proposals': workflow.focus_proposals() if workflow else [],
        'now': now,
        'cfg': cfg, 'sched': sched, 'proc': proc, 'log': log_info,
        'state': read_loop_state(), 'budgets': read_budgets(cfg),
        'batches': batches, 'next_slot': (nxt_t, nxt_p),
        'quarantine': quarantine_count(),
        'progress_file': progress,
        'rail': rail, 'focus_summary': focus_summary,
        'workflow': workflow.summary() if workflow else {},
        'providers': workflow.provider_states() if workflow else [],
        'review_items': workflow.review_items(20) if workflow else [],
        'maintenance_preview': maintenance_preview(workflow),
        'daily_digest_preview': daily_digest_preview(workflow),
    }


def build_alerts(snap, scan, lang='en'):
    alerts = []
    if snap['proc']['pid'] is None:
        alerts.append(('critical', tr(lang, 'a_dead')))
    if snap.get('workflow', {}).get('ticker_review'):
        alerts.append(('warning', tr(lang, 'a_review',
                                     n=snap['workflow']['ticker_review'])))
    elif snap['quarantine'] and not snap.get('workflow'):
        alerts.append(('warning', tr(lang, 'a_quar', n=snap['quarantine'])))
    for s in snap['log']['skips']:
        alerts.append(('warning', tr(lang, 'a_skip', s=s[:90])))
    status = snap['progress_file'].get('current_status', '')
    if status and 'Fully researched:' not in status:
        alerts.append(('warning', tr(lang, 'a_legacy')))
    if snap['batches'] and snap['proc']['pid'] is not None:
        age_h = (snap['now'] - snap['batches'][0]['when']).total_seconds() / 3600
        if age_h > 5:
            alerts.append(('warning', tr(lang, 'a_stall', h=age_h)))
    cur = snap['log'].get('current')
    if cur and snap['proc']['pid'] is None:
        alerts.append(('warning', tr(lang, 'a_orphan', n=cur['seq'])))
    return alerts


# The CSV's Country column mixes English, Chinese, and occasional free-text LLM
# output ("美國 (USA)。", full sentences). Normalize to one clean Chinese name
# for display; unknown-but-short values pass through, junk is dropped.
COUNTRY_CANON = {
    '台灣': '台灣', '臺灣': '台灣', 'Taiwan': '台灣',
    '美國': '美國', 'USA': '美國', 'United States': '美國',
    '日本': '日本', 'Japan': '日本',
    '南韓': '南韓', '韓國': '南韓', 'South Korea': '南韓', 'Korea': '南韓',
    '中國': '中國', 'China': '中國', '香港': '香港', 'Hong Kong': '香港',
    '以色列': '以色列', 'Israel': '以色列',
    '法國': '法國', 'France': '法國',
    '德國': '德國', 'Germany': '德國',
    '荷蘭': '荷蘭', 'Netherlands': '荷蘭',
    '英國': '英國', 'UK': '英國', 'United Kingdom': '英國',
    '瑞士': '瑞士', 'Switzerland': '瑞士',
    '新加坡': '新加坡', 'Singapore': '新加坡',
    '西班牙': '西班牙', 'Spain': '西班牙',
    '丹麥': '丹麥', 'Denmark': '丹麥',
    '瑞典': '瑞典', 'Sweden': '瑞典',
    '澳洲': '澳洲', 'Australia': '澳洲',
    '義大利': '義大利', 'Italy': '義大利',
    '加拿大': '加拿大', 'Canada': '加拿大',
    '印度': '印度', 'India': '印度',
    '愛爾蘭': '愛爾蘭', 'Ireland': '愛爾蘭',
}


def clean_country(value):
    v = (value or '').strip()
    prefix = re.split(r'[（(，。；／,;.]', v)[0].strip()
    for probe in (prefix, v):  # prefer the pre-punctuation prefix over parentheticals
        for kw, canon in COUNTRY_CANON.items():
            if kw in probe:
                return canon
    return prefix if 0 < len(prefix) <= 10 else ''


# 跑馬燈 content: research factoids sampled from finished batch archives.
# 催化劑 appears twice so news-like content shows up most often.
FACT_FIELDS = [
    ('12M Catalysts', '催化劑'),
    ('12M Catalysts', '催化劑'),
    ('Core Business', '核心業務'),
    ('Technical Moat', '護城河'),
    ('Key Investment Risks', '風險'),
    ('Gross Margin Profile', '毛利'),
    ('CapEx & Expansion', '擴產'),
    ('3-Year M&A', '併購'),
    ('CEO & Management', '經營層'),
]


def build_factoids(exclude_batch=None, max_batches=4, per_batch=8):
    """Random (company, field, snippet) factoids from finished batch archives."""
    paths = glob.glob(os.path.join(BATCHES_DIR, 'batch_*.json'))
    random.shuffle(paths)
    factoids = []
    for path in paths:
        d = read_json(path)
        records = (d or {}).get('records') or []
        if not records:
            continue
        m = BATCH_LABEL_RE.search(d.get('label', ''))
        seq = int(m.group(1)) if m else 0
        if exclude_batch is not None and seq == exclude_batch and len(paths) > 1:
            continue
        for rec in random.sample(records, min(per_batch, len(records))):
            candidates = [(k, lab) for k, lab in FACT_FIELDS if rec.get(k, '').strip()]
            if not candidates:
                continue
            key, label = random.choice(candidates)
            tag = '/'.join(x for x in (rec.get('Sub-Sector', ''), rec.get('Tier', '')) if x)
            factoids.append({
                'batch': seq,
                'company': rec.get('Company', '?'),
                'country': clean_country(rec.get('Country', '')),
                'tag': tag,
                'field': label,
                'text': re.sub(r'\s+', ' ', rec[key]).strip(),
            })
        if len({f['batch'] for f in factoids}) >= max_batches and len(factoids) >= 24:
            break
    random.shuffle(factoids)
    return factoids


# ---------------- plain-text report ----------------

def meter_line(done, mid, total, width=40):
    if total <= 0:
        return '░' * width
    d = max(1, round(width * done / total)) if done else 0
    m = max(1, round(width * mid / total)) if mid else 0
    d = min(d, width)
    m = min(m, width - d)
    return '█' * d + '▓' * m + '░' * max(0, width - d - m)


def read_roster():
    """Read-only view of the Telegram user roster. Never writes (monitor contract)."""
    data = read_json(TG_USERS_PATH, default={'users': []})
    rows = data.get('users', []) if isinstance(data, dict) else (data or [])
    out = []
    for u in rows:
        if isinstance(u, dict) and u.get('chat_id'):
            out.append({'label': str(u.get('label', '?')),
                        'chat_id': str(u.get('chat_id', '')),
                        'profile': str(u.get('profile', '')),
                        'enabled': bool(u.get('enabled', True))})
    return out


def gateway_status():
    """Read-only: count of telegram routes Hermes currently has applied. Never writes."""
    try:
        import yaml
        with open(HERMES_CONFIG_PATH, encoding='utf-8') as f:
            cfg = yaml.safe_load(f) or {}
        routes = (cfg.get('gateway') or {}).get('profile_routes') or []
        return sum(1 for r in routes if isinstance(r, dict) and r.get('platform') == 'telegram')
    except Exception:
        return None


def apply_roster_to_hermes(roster):
    """Merge the roster's telegram routes into Hermes gateway.profile_routes.
    Writes ONLY chat_id->profile routes — never a bot token or LLM key."""
    import telegram_users as tu
    from dad_assistant import _atomic_write_yaml, _read_yaml_mapping, hermes_home
    path = hermes_home() / 'config.yaml'
    cfg = _read_yaml_mapping(path)
    gateway = cfg.setdefault('gateway', {})
    if not isinstance(gateway, dict):
        raise RuntimeError('gateway config must be a mapping')
    existing = gateway.get('profile_routes') or []
    gateway['multiplex_profiles'] = True
    gateway['profile_routes'] = tu.build_profile_routes(roster, existing)
    _atomic_write_yaml(path, cfg)
    return len(roster.routes())


def render_report():
    snap = build_snapshot()
    scan = scan_db()
    lines = []
    p = snap['proc']
    nxt_t, nxt_p = snap['next_slot']
    lines.append(f"LOOP: {'RUNNING pid %s since %s' % (p['pid'], p['since']) if p['pid'] else 'STOPPED'}")
    if nxt_t:
        lines.append(f"next slot: {nxt_t:%H:%M} ({nxt_p}), in {max(0, (nxt_t - snap['now']).total_seconds() / 60):.0f} min")
    lines.append('calls today: ' + '  '.join(
        f"{pv} {snap['state']['calls'][pv]}/{snap['budgets'][pv]}" for pv in PROVIDERS))
    if snap['workflow']:
        w = snap['workflow']
        lines.append('lifecycle: ' +
                     f"deep {w['deep_researched']} | pending {w['pending']} | "
                     f"running {w['researching']} | retry {w['retry']} | "
                     f"maintenance {w['maintenance_due']} | review {w['ticker_review']} | "
                     f"excluded {w['excluded']}")
    if snap['providers']:
        lines.append('providers: ' + '  '.join(
            f"{p['provider']}={'ON' if p['enabled'] else 'OFF'}/{p['health']}"
            for p in snap['providers']))
    if snap['maintenance_preview']:
        newest = snap['maintenance_preview'][0]
        lines.append(f"maintenance preview: {len(snap['maintenance_preview'])} applied update(s); "
                     f"latest {newest['company_name']} (Batch {newest['batch_seq']})")
    digest = snap.get('daily_digest_preview', {})
    if digest:
        lines.append(f"daily digest preview: {'READY' if digest['posts'] else 'EMPTY'}; "
                     f"{len(digest['posts'])} material update(s); "
                     f"{len(digest['recipients'])} enabled recipient(s); PREVIEW-ONLY")
    if 'error' not in scan:
        lines.append('')
        lines.append(f"Research  {meter_line(scan['complete'], scan['repair'], scan['harvested'])} "
                     f"{scan['complete']} done | {scan['repair']} in progress | of {scan['harvested']} harvested")
        pct = scan['harvested'] / 1000
        if scan['repair']:
            phase = '— 修復階段，尚未提名新公司'
        else:
            phase = f"+{claimed_today(snap['batches'])} today"
        lines.append(f"Capacity  {meter_line(scan['harvested'], 0, 100000)} "
                     f"{scan['harvested']:,} of 100,000 ({pct:.1f}%) {phase}")
    cur = snap['log'].get('current')
    if cur:
        state = 'running' if p['pid'] else 'ORPHANED'
        lines.append(f"\nNOW: Batch {cur['seq']} ({cur['source']}, {cur['provider']}) {state} "
                     f"{cur['ok']}ok/{cur['failed']}fail of {cur['size']} — researching {cur['researching']}")
    lines.append('\nTODAY CONDUCTOR')
    lines.append(snap.get('focus_summary', 'Focus: no active future slots'))
    glyphs = {'pending': '□', 'running': '▶', 'complete': '✓', 'partial': '!',
              'disabled': '○', 'missed': '–', 'skipped': '×',
              'done': '✓', 'upcoming': '□', 'none': '–'}
    for s in snap['rail']:
        mode = s.get('mode_label') or (
            'MAINT' if s.get('mode') == 'maintenance' else 'DEEP')
        focus = f"  focus={s['focus']}" if s.get('focus') else ''
        lines.append(f"  {s.get('hhmm_display', s['hhmm']):<6}  {mode:<15}  {s['provider']:<7} "
                     f"{glyphs.get(s['status'], '?')} {s['detail']:<10}{focus}")
    lines.append('\nHISTORY (latest first)')
    for b in snap['batches'][:10]:
        lines.append(f"  {b['when']:%m-%d %H:%M}  Batch {b['seq']:<3} {b['source']:<7} {b['provider']:<7} "
                     f"applied {b['applied']:<3} (upd {len(b['updated'])}, new {len(b['claimed'])})"
                     + (f"  FAILED {len(b['failed'])}" if b['failed'] else ''))
    if 'error' not in scan:
        lines.append(f"\nTODO: {scan['repair']} repair rows pending"
                     + (', by country: ' + ', '.join(f'{c} {n}' for c, n in scan['by_country'])
                        if scan['by_country'] else ''))
        bsize = snap['cfg'].get('batch_size', 20)
        queue = [n for n, _ in scan['next_up'][:bsize]]
        if queue:
            running = bool(cur and p['pid'])
            label = (f"Batch {cur['seq']} in flight" if running
                     else f"coming Batch {snap['state']['batch_seq'] + 1} queue")
            lines.append(f"  {label} ({len(queue)}): " + ', '.join(queue))
        if scan['repair'] == 0:
            lines.append('  repair queue clear — loop is nominating new companies')
    alerts = build_alerts(snap, scan)
    coverage = snap.get('coverage')
    if coverage:
        total = len(coverage['themes'])
        pct = round(100 * coverage['unmapped'] / (coverage['researched'] or 1))
        lines.append(f"\nSECTOR COVERAGE ({coverage['researched']} researched; a company can "
                     f"match several themes, {pct}% match none)")
        lines.append(f"  {coverage['worked_themes']}/{total} themes worked in the last "
                     f"{coverage['window_days']} days · {coverage['thin_themes']} thin")
        top = coverage['themes'][:5]
        thin = [r for r in coverage['themes'] if r['thin']][-5:]
        lines.append('  heaviest: ' + ', '.join(f"{r['theme']} {r['held']}" for r in top))
        if thin:
            lines.append('  starved:  ' + ', '.join(f"{r['theme']} {r['held']}"
                                                    for r in reversed(thin)))
        focus = snap.get('standing_focus')
        if focus:
            where = '/'.join(x for x in (focus.get('country'), focus.get('industry')) if x)
            scope = ('until cleared' if focus.get('batches_remaining') is None
                     else f"{focus['batches_remaining']} batches left")
            lines.append(f"  STANDING FOCUS: {where} ({scope})")
        else:
            lines.append('  no standing focus — the scraper picks its own direction')
    lines.append('\nALERTS: ' + ('none' if not alerts else ''))
    for level, msg in alerts:
        lines.append(f"  [{level.upper()}] {msg}")
    roster = read_roster()
    if roster:
        enabled = sum(1 for u in roster if u['enabled'])
        applied = gateway_status()
        applied_str = f"{applied} applied to Hermes" if applied is not None else "Hermes routes unknown"
        lines.append(f"\nTELEGRAM ROSTER ({enabled}/{len(roster)} enabled · {applied_str})")
        for i, u in enumerate(roster, 1):
            mark = '●' if u['enabled'] else '○'
            lines.append(f"  {i}. {mark} {u['label']:<12} {u['chat_id']:<13} {u['profile']}")
    return '\n'.join(lines)


# ---------------- TUI ----------------

def run_tui(return_app=False):
    from rich.cells import cell_len
    from rich.table import Table
    from rich.text import Text
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Horizontal, Vertical
    from textual.screen import ModalScreen
    from textual.widgets import (Button, Checkbox, DataTable, Footer, Input,
                                 Label, RichLog, Select, Static)

    import time

    import botffet          # the one brain; Telegram and this TUI call the same answer()
    import telegram_users as tu

    STATUS_GLYPH = {'done': ('✓', 'green'), 'running': ('▶', 'yellow'),
                    'upcoming': ('□', 'dim'), 'none': ('–', 'dim red')}

    def cells_slice(text, offset, width):
        out, pos, taken = [], 0, 0
        for ch in text:
            w = cell_len(ch)
            if pos + w <= offset:
                pos += w
                continue
            if taken + w > width:
                break
            out.append(ch)
            taken += w
        return ''.join(out)

    class SettingsScreen(ModalScreen):
        CSS = """
        SettingsScreen { align: center middle; }
        #box { width: 56; height: auto; border: round $accent; padding: 1 2; background: $surface; }
        #box Input { margin-bottom: 1; }
        #buttons { height: auto; align-horizontal: right; }
        """

        def __init__(self, cfg):
            super().__init__()
            self.cfg_in = dict(cfg)

        def compose(self) -> ComposeResult:
            lang = self.cfg_in.get('language', 'en')
            with Vertical(id='box'):
                yield Label(tr(lang, 's_title'))
                yield Label(tr(lang, 's_speed'))
                yield Input(str(self.cfg_in['marquee_cells_per_tick']), id='in_speed')
                yield Label(tr(lang, 's_secs'))
                yield Input(str(self.cfg_in['marquee_seconds']), id='in_secs')
                yield Label(tr(lang, 's_poll'))
                yield Input(str(self.cfg_in['poll_seconds']), id='in_poll')
                yield Label(tr(lang, 's_rows'))
                yield Input(str(self.cfg_in['history_rows']), id='in_rows')
                yield Label(tr(lang, 's_lang'))
                yield Input(lang, id='in_lang')
                with Horizontal(id='buttons'):
                    yield Button(tr(lang, 's_save'), variant='primary', id='save')
                    yield Button(tr(lang, 's_cancel'), id='cancel')

        def on_button_pressed(self, event):
            if event.button.id != 'save':
                self.dismiss(None)
                return
            try:
                lang_in = self.query_one('#in_lang', Input).value.strip().lower()
                cfg = {
                    'marquee_cells_per_tick': max(1, int(self.query_one('#in_speed', Input).value)),
                    'marquee_seconds': max(2, int(self.query_one('#in_secs', Input).value)),
                    'poll_seconds': max(2, int(self.query_one('#in_poll', Input).value)),
                    'history_rows': max(3, int(self.query_one('#in_rows', Input).value)),
                    'language': lang_in if lang_in in STRINGS else self.cfg_in.get('language', 'en'),
                }
            except ValueError:
                self.dismiss(None)
                return
            self.dismiss(cfg)

    class ProviderScreen(ModalScreen):
        CSS = """
        ProviderScreen { align: center middle; }
        #pbox { width: 88; height: auto; border: round $accent; padding: 1 2; background: $surface; }
        #ptable { height: 8; border: round $primary; }
        #pbuttons { height: auto; }
        #pbuttons Button { margin-right: 1; }
        #pmsg { height: 3; border: round $secondary; padding: 0 1; }
        """

        def __init__(self, calls, budgets):
            super().__init__()
            self.calls, self.budgets = calls, budgets
            self.workflow = workflow_state(create=True)
            self.selected = None

        def compose(self) -> ComposeResult:
            with Vertical(id='pbox'):
                yield Label('Provider Control — select a row, then choose an action')
                yield DataTable(id='ptable', cursor_type='row', zebra_stripes=True)
                with Horizontal(id='pbuttons'):
                    yield Button('Enable / Disable', variant='primary', id='p-toggle')
                    yield Button('Clear Cooldown', id='p-clear')
                    yield Button('Close', id='p-close')
                yield Static(id='pmsg')

        def on_mount(self):
            self.query_one('#ptable', DataTable).add_columns(
                'Provider', 'Enabled', 'Next batch', 'Health', 'Local calls', 'Cooldown / error')
            self.refresh_rows()

        def refresh_rows(self):
            table = self.query_one('#ptable', DataTable)
            table.clear()
            if not self.workflow:
                self.query_one('#pmsg', Static).update('Workflow database unavailable')
                return
            for ps in self.workflow.provider_states():
                pv = ps['provider']
                detail = ps.get('cooldown_until') or ps.get('last_error') or ''
                table.add_row(pv, 'yes' if ps['enabled'] else 'no',
                              'FORCED' if ps.get('force_next') else '—', ps['health'],
                              f"{self.calls.get(pv, 0)}/{self.budgets.get(pv, 0)}",
                              detail[:42], key=pv)
            if self.selected:
                keys = [str(ps['provider']) for ps in self.workflow.provider_states()]
                if self.selected in keys:
                    table.move_cursor(row=keys.index(self.selected))
                    self._update_action_labels()

        def _select(self, row_key):
            new_selection = str(row_key.value)
            changed = new_selection != self.selected
            self.selected = new_selection
            self._update_action_labels()
            if changed:
                ps = self.workflow.provider_state(self.selected) if self.workflow else None
                self.query_one('#pmsg', Static).update(
                    f"Selected {self.selected.title()} — currently "
                    f"{'enabled' if ps and ps['enabled'] else 'disabled'}")

        def _update_action_labels(self):
            if not self.workflow or not self.selected:
                return
            ps = self.workflow.provider_state(self.selected)
            action = 'Disable' if ps and ps['enabled'] else 'Enable'
            self.query_one('#p-toggle', Button).label = f'{action} {self.selected.title()}'

        def on_data_table_row_selected(self, event):
            self._select(event.row_key)

        def on_data_table_row_highlighted(self, event):
            # A single mouse click or arrow-key move is enough to select a provider.
            self._select(event.row_key)

        def on_button_pressed(self, event):
            bid = event.button.id
            if bid == 'p-close':
                self.dismiss(True)
                return
            if not self.workflow or not self.selected:
                message = 'Select a provider row first'
                self.query_one('#pmsg', Static).update(message)
                self.notify(message, severity='warning')
                return
            ps = self.workflow.provider_state(self.selected)
            if bid == 'p-toggle':
                enabled = not bool(ps['enabled'])
                self.workflow.set_provider_enabled(self.selected, enabled)
                message = f"{self.selected.title()} {'enabled' if enabled else 'disabled'}"
            elif bid == 'p-clear':
                self.workflow.clear_cooldown(self.selected)
                message = f'{self.selected.title()} cooldown cleared'
            else:
                return
            self.refresh_rows()
            self.query_one('#pmsg', Static).update('✓ ' + message)
            self.notify(message, title='Provider Control')

    class ReviewScreen(ModalScreen):
        CSS = """
        ReviewScreen { align: center middle; }
        #vbox { width: 94%; height: 88%; border: round $accent; padding: 1 2; background: $surface; }
        #vtable { height: 1fr; border: round $primary; }
        #vform { height: 3; }
        #vform Input { width: 1fr; margin-right: 1; }
        #vbuttons { height: auto; }
        #vbuttons Button { margin-right: 1; }
        #vmsg { height: 3; }
        """

        def __init__(self):
            super().__init__()
            self.workflow = workflow_state(create=True)
            self.rows = []
            self.selected = None

        def compose(self) -> ComposeResult:
            with Vertical(id='vbox'):
                yield Label('Research Review — correct ticker and requeue, or exclude')
                yield DataTable(id='vtable', cursor_type='row', zebra_stripes=True)
                with Horizontal(id='vform'):
                    yield Input(id='v-company', placeholder='Company name')
                    yield Input(id='v-ticker', placeholder='Ticker, e.g. 2330.TW')
                with Horizontal(id='vbuttons'):
                    yield Button('Correct & Requeue', variant='primary', id='v-requeue')
                    yield Button('Exclude', variant='error', id='v-exclude')
                    yield Button('Close', id='v-close')
                yield Static(id='vmsg')

        def on_mount(self):
            self.query_one('#vtable', DataTable).add_columns(
                'Company', 'Status', 'Attempts', 'Problem')
            self.refresh_rows()

        def refresh_rows(self):
            table = self.query_one('#vtable', DataTable)
            table.clear()
            self.rows = self.workflow.review_items(100) if self.workflow else []
            for row in self.rows:
                table.add_row(row['company_name'], row['research_status'],
                              str(row['attempt_count']), (row['last_error'] or '')[:60],
                              key=str(row['id']))

        def on_data_table_row_selected(self, event):
            self.selected = int(event.row_key.value)
            row = next(r for r in self.rows if r['id'] == self.selected)
            self.query_one('#v-company', Input).value = re.sub(
                r'\s*\([^)]+\)\s*$', '', row['company_name']).strip()
            self.query_one('#v-ticker', Input).value = row['ticker'] or ''
            self.query_one('#vmsg', Static).update(row['last_error'] or '')

        def on_button_pressed(self, event):
            bid = event.button.id
            if bid == 'v-close':
                self.dismiss(True)
                return
            if not self.workflow or self.selected is None:
                self.query_one('#vmsg', Static).update('Select a company first')
                return
            try:
                if bid == 'v-requeue':
                    self.workflow.correct_ticker_and_requeue(
                        self.selected, self.query_one('#v-company', Input).value,
                        self.query_one('#v-ticker', Input).value)
                    self.query_one('#vmsg', Static).update('Correction saved and company requeued')
                elif bid == 'v-exclude':
                    self.workflow.exclude(self.selected, 'Excluded manually from dashboard')
                    self.query_one('#vmsg', Static).update('Company excluded')
                self.selected = None
                self.refresh_rows()
            except Exception as exc:
                self.query_one('#vmsg', Static).update(str(exc))

    class RosterScreen(ModalScreen):
        """In-app Telegram roster editor (opened with g). Writes telegram_users.json
        and, on Apply, Hermes gateway routes — never the loop's files."""
        CSS = """
        RosterScreen { align: center middle; }
        #rbox { width: 90%; max-width: 108; height: 90%; border: round $accent;
                padding: 1 2; background: $surface; }
        #rtable { height: 1fr; min-height: 5; border: round $primary; }
        .rrow { height: 3; }
        .rrow Label { width: 8; padding-top: 1; }
        .rrow Input, .rrow Select { width: 1fr; }
        #rbtns { height: auto; }
        #rbtns Button { margin-right: 1; }
        #rgw { height: auto; }
        #rgw Button { margin-right: 1; }
        #r-paircode { width: 22; }
        #rout { height: 5; border: round $secondary; }
        """
        COLUMNS = ('#', 'Label', 'Chat ID', 'Access', 'Profile', 'On')

        def __init__(self, users_path):
            super().__init__()
            self.users_path = users_path
            self.roster = tu.load_users(users_path)
            self.selected = None

        def compose(self) -> ComposeResult:
            with Vertical(id='rbox'):
                yield Label('Telegram Roster — add / edit / reorder, then Apply. Esc to close.')
                yield DataTable(id='rtable', cursor_type='row', zebra_stripes=True)
                with Horizontal(classes='rrow'):
                    yield Label('Label')
                    yield Input(id='r-label', placeholder='e.g. Dad')
                    yield Label('Chat ID')
                    yield Input(id='r-chat', placeholder='numeric ID')
                with Horizontal(classes='rrow'):
                    yield Label('Access')
                    yield Select([('read-only  (query only)', 'read_only'),
                                  ('read-write (edits a copy)', 'read_write')],
                                 value='read_only', id='r-access', allow_blank=False)
                    yield Label('Profile')
                    yield Input(id='r-profile', placeholder='(blank = tier default)')
                with Horizontal(classes='rrow'):
                    yield Label('Lang')
                    yield Select([('EN', 'english'), ('ZH', 'traditional_chinese'),
                                  ('Bi', 'bilingual')], value='bilingual',
                                 id='r-lang', allow_blank=False)
                    yield Checkbox('on', True, id='r-enabled')
                with Horizontal(id='rbtns'):
                    yield Button('Add/Update', variant='primary', id='r-upsert')
                    yield Button('New', id='r-clear')
                    yield Button('Remove', variant='error', id='r-remove')
                    yield Button('Up', id='r-up')
                    yield Button('Down', id='r-down')
                    yield Button('Apply to Hermes', variant='warning', id='r-apply')
                    yield Button('Close', id='r-close')
                with Horizontal(id='rgw'):
                    yield Input(placeholder='8-char pairing code', id='r-paircode')
                    yield Button('Approve Pairing', id='r-pair')
                    yield Button('Restart Gateway', id='r-restart')
                yield RichLog(id='rout', markup=True, wrap=True)

        def on_mount(self):
            self.query_one('#rtable', DataTable).add_columns(*self.COLUMNS)
            self._refresh()

        def _refresh(self):
            table = self.query_one('#rtable', DataTable)
            table.clear()
            for i, u in enumerate(self.roster.users):
                acc = 'rw' if u.access == 'read_write' else 'ro'
                table.add_row(str(i + 1), u.label, u.chat_id, acc, u.resolved_profile(),
                              'yes' if u.enabled else 'no', key=str(i))

        def _log(self, msg):
            self.query_one('#rout', RichLog).write(msg)

        def on_data_table_row_selected(self, event):
            try:
                self.selected = int(event.row_key.value)
            except (TypeError, ValueError):
                return
            u = self.roster.users[self.selected]
            self.query_one('#r-label', Input).value = u.label
            self.query_one('#r-chat', Input).value = u.chat_id
            self.query_one('#r-access', Select).value = u.access
            self.query_one('#r-profile', Input).value = u.profile
            self.query_one('#r-lang', Select).value = u.language
            self.query_one('#r-enabled', Checkbox).value = u.enabled

        def _form_user(self):
            u = tu.TelegramUser(
                label=self.query_one('#r-label', Input).value.strip(),
                chat_id=self.query_one('#r-chat', Input).value.strip(),
                access=str(self.query_one('#r-access', Select).value),
                profile=self.query_one('#r-profile', Input).value.strip(),  # blank = tier default
                language=str(self.query_one('#r-lang', Select).value),
                enabled=self.query_one('#r-enabled', Checkbox).value)
            u.validate()
            return u

        def _clear(self):
            self.selected = None
            for w in ('#r-label', '#r-chat', '#r-profile'):
                self.query_one(w, Input).value = ''
            self.query_one('#r-access', Select).value = 'read_only'
            self.query_one('#r-lang', Select).value = 'bilingual'
            self.query_one('#r-enabled', Checkbox).value = True

        def on_button_pressed(self, event):
            bid = event.button.id
            try:
                if bid == 'r-close':
                    self.dismiss(True)
                elif bid == 'r-clear':
                    self._clear()
                    self._log('form cleared — Add/Update will create a new user')
                elif bid == 'r-upsert':
                    u = self._form_user()
                    others = [x.chat_id for i, x in enumerate(self.roster.users) if i != self.selected]
                    if u.chat_id in others:
                        raise ValueError(f'chat_id {u.chat_id} already belongs to another user')
                    if self.selected is None:
                        self.roster.users.append(u)
                        self.selected = len(self.roster.users) - 1
                        verb = 'Added'
                    else:
                        self.roster.users[self.selected] = u
                        verb = 'Updated'
                    self.roster.validate()
                    tu.save_users(self.roster, self.users_path)
                    self._refresh()
                    self._log(f'[green]{verb}[/green] {u.label} ({u.chat_id})')
                elif bid == 'r-remove':
                    if self.selected is None:
                        raise ValueError('select a row first')
                    rem = self.roster.remove(self.selected)
                    self._clear()
                    tu.save_users(self.roster, self.users_path)
                    self._refresh()
                    self._log(f'[green]Removed[/green] {rem.label}')
                elif bid in ('r-up', 'r-down'):
                    if self.selected is None:
                        raise ValueError('select a row first')
                    self.selected = self.roster.move(self.selected, -1 if bid == 'r-up' else 1)
                    tu.save_users(self.roster, self.users_path)
                    self._refresh()
                elif bid == 'r-apply':
                    tu.save_users(self.roster, self.users_path)
                    n = apply_roster_to_hermes(self.roster)
                    self._log(f'[green]Applied[/green] {n} route(s) to Hermes')
                elif bid == 'r-pair':
                    if self.selected is None:
                        raise ValueError('select the user to pair first')
                    profile = self.roster.users[self.selected].profile
                    code = self.query_one('#r-paircode', Input).value.strip().upper()
                    if not re.fullmatch(r'[A-Z0-9]{8}', code):
                        raise ValueError('pairing code must be exactly 8 letters/numbers')
                    out = self._hermes(['hermes', '-p', profile, 'pairing',
                                        'approve', 'telegram', code])
                    self._log(f'[green]pairing approved[/green] ({profile}) {out}')
                elif bid == 'r-restart':
                    self._log(f'[green]gateway restart[/green] {self._hermes(["hermes", "gateway", "restart"])}')
            except Exception as exc:
                self._log(f'[red]{exc}[/red]')

        def _hermes(self, cmd):
            res = subprocess.run(cmd, check=False, capture_output=True, text=True,
                                 timeout=90, stdin=subprocess.DEVNULL)
            return (res.stdout or res.stderr or 'done').strip()[:200]

        def on_key(self, event):
            if event.key == 'escape':
                self.dismiss(True)

    class ChatScreen(ModalScreen):
        """爸菲特 / Wanna Botffet — ask the research corpus from inside the dashboard.

        A thin client of botffet.answer(): routing, retrieval, synthesis, quota and audit all
        live there, so this surface and Telegram cannot drift apart.

        The provider call is slow (12-18 s) and blocking, so it MUST run off the event loop —
        the dashboard's marquee (0.2 s), clock (1.0 s), poll (5 s) and kill switch all share
        it. Note RosterScreen._hermes does the opposite (a synchronous subprocess.run with
        timeout=90); that is a latent freeze, not a pattern to copy.
        """

        CSS = """
        ChatScreen { align: center middle; }
        #chatbox { width: 92%; height: 88%; border: round $accent; padding: 1 2;
                   background: $surface; }
        #chatlog { height: 1fr; border: round $primary 40%; }
        #chatrow { height: 3; margin-top: 1; }
        #chatinput { width: 1fr; }
        #chatstatus { height: 1; color: $text-muted; }
        """

        def __init__(self, user='local'):
            super().__init__()
            self.user = user
            self._gen = 0          # supersede stale replies; Worker.cancel cannot stop a thread

        def compose(self) -> ComposeResult:
            with Vertical(id='chatbox'):
                yield Label('爸菲特 / Wanna Botffet — /help, /screen, /brief, /reset · esc closes')
                yield RichLog(id='chatlog', wrap=True, markup=False, highlight=False)
                yield Static('', id='chatstatus')
                with Horizontal(id='chatrow'):
                    yield Input(placeholder='Ask about the corpus…', id='chatinput')
                    yield Button('Ask', variant='primary', id='chatsend')

        def on_mount(self):
            log = self.query_one('#chatlog', RichLog)
            rows = getattr(self.app, 'scan_data', None) or {}
            log.write(f"corpus: {rows.get('harvested', '?')} companies · "
                      f"commands are instant, questions take ~15s")
            self.query_one('#chatinput', Input).focus()

        def on_button_pressed(self, event):
            if event.button.id == 'chatsend':
                self._submit()

        def on_input_submitted(self, event):
            if event.input.id == 'chatinput':
                self._submit()

        def on_key(self, event):
            if event.key == 'escape':
                self.dismiss(None)

        def _submit(self):
            field = self.query_one('#chatinput', Input)
            question = field.value.strip()
            if not question:
                return
            field.value = ''
            log = self.query_one('#chatlog', RichLog)
            log.write(f"\n> {question}")
            self._gen += 1
            gen = self._gen
            self.query_one('#chatstatus', Static).update('… thinking')
            # Own worker group: never 'csvscan', or the 5-second scan and this cancel each
            # other. exclusive=True so a second question supersedes the first.
            self.run_worker(lambda: self._ask(question, gen), thread=True,
                            exclusive=True, group='babyfeit')

        def _ask(self, question, gen):
            started = time.time()

            def on_event(kind, detail):
                # Streamed from the provider as it reasons. This is what makes the assistant
                # supervised rather than merely fast: you see which queries it ran, so a wrong
                # answer is traceable to a wrong search instead of being unexplainable.
                if kind == 'tool':
                    args = {k: v for k, v in (detail.get('input') or {}).items() if v}
                    shown = ', '.join(f'{k}={v}' for k, v in list(args.items())[:3])
                    line = f"   ↳ {detail.get('name')}({shown[:88]})"
                elif kind == 'done':
                    line = f"   ↳ done · {detail.get('turns')} turns"
                else:
                    return
                self.app.call_from_thread(self._trace, gen, line)

            try:
                reply = botffet.answer(question, user=self.user, on_event=on_event)
                text = reply.get('text') or '(empty answer)'
                meta = f"{reply.get('kind')} · {time.time() - started:.1f}s"
                if reply.get('provider_calls'):
                    meta += f" · {reply['provider_calls']} model call(s)"
                if reply.get('turns'):
                    meta += f" · {reply['turns']} turns"
            except Exception as exc:
                text, meta = f'⚠️ {exc}', 'error'
            self.app.call_from_thread(self._apply, gen, text, meta)

        def _trace(self, gen, line):
            """Append one reasoning step. Same generation/mount guards as _apply."""
            if gen != self._gen or not self.is_mounted:
                return
            try:
                self.query_one('#chatlog', RichLog).write(line)
            except Exception:
                pass

        def _apply(self, gen, text, meta):
            # Drop a superseded or post-dismissal reply rather than writing into a dead widget.
            if gen != self._gen or not self.is_mounted:
                return
            try:
                self.query_one('#chatlog', RichLog).write(text)
                self.query_one('#chatstatus', Static).update(meta)
            except Exception:
                pass

    class IndustryScreen(ModalScreen):
        """Which sectors the corpus holds, and the control that aims the scraper at one.

        Counts are substring matches against a curated theme vocabulary, not distinct values of
        the industry column — that column holds ~1,690 free-text labels for ~2,400 companies, so
        a distinct count reports 1,690 and means nothing. A company can match several themes, and
        ~29% match none, so the Held column deliberately does not sum to the corpus. Both figures
        are shown rather than quietly dropped.

        Setting a focus here writes the STANDING focus, not a slot edit: update_daily_slot only
        accepts future pending slots of an already-seeded day, so slot edits can never express
        "keep hunting this until I say stop".
        """

        CSS = """
        IndustryScreen { align: center middle; }
        #indbox { width: 92%; height: 90%; border: round $accent; padding: 1 2;
                  background: $surface; }
        #indtable { height: 1fr; }
        #indfoot { height: auto; color: $text-muted; }
        #indmsg { height: auto; }
        .ind-buttons { height: 3; }
        #indinput { width: 1fr; }
        """

        WINDOWS = {'7': 7, '3': 30, '9': 90}

        def __init__(self, coverage=None):
            super().__init__()
            self.proposals = []
            self.coverage = coverage
            self.window_days = (coverage or {}).get('window_days',
                                                    industry_focus.DEFAULT_WINDOW_DAYS
                                                    if industry_focus else 7)
            self.selected = None

        def compose(self) -> ComposeResult:
            with Vertical(id='indbox'):
                yield Label('Sector coverage — 7/3/9 switch the window to 7, 30 or 90 days · esc closes')
                yield DataTable(id='indtable', cursor_type='row', zebra_stripes=True)
                yield Static('', id='indfoot')
                yield Static('', id='indprop')
                with Horizontal(classes='ind-buttons', id='indpropbuttons'):
                    yield Button('Accept suggestion', variant='success', id='ind-accept')
                    yield Button('Dismiss suggestion', id='ind-dismiss')
                with Horizontal(classes='ind-buttons'):
                    yield Input(placeholder='…or type a sector not in the list',
                                id='indinput')
                with Horizontal(classes='ind-buttons'):
                    yield Button('Set standing focus', variant='primary', id='ind-set')
                    yield Button('Set for next 3 batches', id='ind-set3')
                    yield Button('Clear standing focus', variant='warning', id='ind-clear')
                yield Static('', id='indmsg')

        def on_mount(self):
            self.query_one('#indtable', DataTable).add_columns(
                'Sector', 'Held', f'Worked {self.window_days}d', 'Pending', 'Last touched', '')
            self.refresh_rows()
            self.refresh_proposals()

        def refresh_proposals(self):
            """爸菲特's suggestions, which are advisory until a human accepts one here.

            The reason is rendered verbatim as text and never acted on: it was written by a
            model reading untrusted material, so it is evidence for Peter to weigh, not an
            instruction this screen follows.
            """
            workflow = workflow_state()
            self.proposals = workflow.focus_proposals() if workflow else []
            widget = self.query_one('#indprop', Static)
            buttons = self.query_one('#indpropbuttons', Horizontal)
            if not self.proposals:
                widget.update('')
                buttons.display = False
                return
            buttons.display = True
            text = Text()
            text.append(f'爸菲特 suggests ({len(self.proposals)} waiting):\n', 'bold cyan')
            for i, proposal in enumerate(self.proposals[:3]):
                where = '/'.join(x for x in (proposal['country'], proposal['industry']) if x)
                marker = '→ ' if i == 0 else '  '
                text.append(f'{marker}{where}', 'bold' if i == 0 else 'dim')
                if proposal['reason']:
                    text.append(f" — {proposal['reason']}", 'dim')
                text.append('\n')
            newest = self.proposals[0]
            self.query_one('#ind-accept', Button).label = f"Accept {newest['industry']}"
            widget.update(text)

        # -- data ----------------------------------------------------------------
        def _load(self, force=False):
            if industry_focus is None:
                return None
            cfg = read_loop_config()
            workflow = workflow_state()
            if workflow is None:
                return None
            try:
                return industry_focus.theme_coverage(
                    workflow, window_days=self.window_days,
                    themes=industry_focus.load_themes(cfg),
                    thin_floor=int(cfg.get('industry_thin_floor',
                                           industry_focus.DEFAULT_THIN_FLOOR)))
            except Exception:
                return None

        def refresh_rows(self):
            table = self.query_one('#indtable', DataTable)
            table.clear()
            if self.coverage is None or self.coverage.get('window_days') != self.window_days:
                self.coverage = self._load()
            coverage = self.coverage
            if not coverage:
                self.query_one('#indfoot', Static).update(
                    Text('Coverage unavailable — the workflow database could not be read.', 'red'))
                return
            widest = max((r['held'] for r in coverage['themes']), default=0) or 1
            for row in coverage['themes']:
                bar = meter_line(row['held'], 0, widest, width=18).split(' ')[0]
                style = 'yellow' if row['thin'] else ''
                table.add_row(
                    Text(row['theme'], style),
                    Text(str(row['held']), style),
                    Text(str(row['worked']), 'dim' if not row['worked'] else ''),
                    Text(str(row['pending']) if row['pending'] else '·', 'dim'),
                    Text((row['last_touched'] or '')[:10] or 'never', 'dim'),
                    Text(bar, 'cyan' if not row['thin'] else 'yellow'),
                    key=row['theme'])
            if coverage['themes']:
                self.selected = self.selected or coverage['themes'][0]['theme']
            self._render_foot()

        def _render_foot(self):
            coverage = self.coverage or {}
            researched = coverage.get('researched', 0)
            pct = round(100 * coverage.get('unmapped', 0) / researched) if researched else 0
            workflow = workflow_state()
            focus = workflow.standing_focus() if workflow else None
            slots = (industry_focus.slot_focus_summary(workflow)
                     if workflow and industry_focus else {'focused': 0, 'total': 0})
            text = Text()
            text.append(f"{researched} researched · ", 'dim')
            text.append(f"{coverage.get('unmapped', 0)} ({pct}%) match no theme", 'yellow')
            text.append(f" · {coverage.get('multi', 0)} match more than one, "
                        f"so Held does not sum to the corpus\n", 'dim')
            if focus:
                where = '/'.join(x for x in (focus.get('country'), focus.get('industry')) if x)
                scope = ('until cleared' if focus.get('batches_remaining') is None
                         else f"{focus['batches_remaining']} batches left")
                text.append(f"standing focus: {where} ({scope})", 'bold yellow')
                if focus.get('reason'):
                    text.append(f" — {focus['reason']}", 'dim')
            else:
                text.append('no standing focus — the scraper picks its own direction',
                            'dim')
            text.append(f"  ·  slots with their own focus today: "
                        f"{slots['focused']}/{slots['total']}", 'dim')
            self.query_one('#indfoot', Static).update(text)

        # -- interaction ---------------------------------------------------------
        def on_data_table_row_highlighted(self, event):
            if event.row_key is not None:
                self.selected = str(event.row_key.value)

        def on_data_table_row_selected(self, event):
            if event.row_key is not None:
                self.selected = str(event.row_key.value)

        def on_key(self, event):
            if event.key == 'escape':
                self.dismiss(None)
            elif event.key in self.WINDOWS and not self.query_one('#indinput', Input).has_focus:
                self.window_days = self.WINDOWS[event.key]
                self.coverage = None
                table = self.query_one('#indtable', DataTable)
                table.clear(columns=True)
                table.add_columns('Sector', 'Held', f'Worked {self.window_days}d',
                                  'Pending', 'Last touched', '')
                self.refresh_rows()

        def _chosen_sector(self):
            typed = self.query_one('#indinput', Input).value.strip()
            return typed or self.selected

        def _msg(self, text, style=''):
            self.query_one('#indmsg', Static).update(Text(text, style))

        def on_button_pressed(self, event):
            workflow = workflow_state()
            if workflow is None:
                self._msg('Workflow database unavailable.', 'red')
                return
            if event.button.id == 'ind-clear':
                cleared = workflow.clear_standing_focus()
                if cleared:
                    where = '/'.join(x for x in (cleared.get('country'),
                                                 cleared.get('industry')) if x)
                    self._msg(f'Cleared standing focus ({where}). The next slot picks its own '
                              f'direction again.', 'green')
                else:
                    self._msg('There was no standing focus to clear.', 'dim')
                self._render_foot()
                return
            if event.button.id in ('ind-accept', 'ind-dismiss'):
                if not self.proposals:
                    self._msg('No suggestion waiting.', 'dim')
                    return
                newest = self.proposals[0]
                if event.button.id == 'ind-dismiss':
                    workflow.resolve_focus_proposal(newest['id'], 'dismissed')
                    self._msg(f"Dismissed the suggestion for {newest['industry']}. "
                              f"Nothing changed.", 'dim')
                else:
                    focus = workflow.accept_focus_proposal(newest['id'])
                    where = '/'.join(x for x in (focus.get('country'),
                                                 focus.get('industry')) if x)
                    self._msg(f'Accepted — standing focus is now {where}, until cleared.',
                              'green')
                self.refresh_proposals()
                self._render_foot()
                return
            if event.button.id not in ('ind-set', 'ind-set3'):
                return
            sector = self._chosen_sector()
            if not sector:
                self._msg('Pick a sector in the table, or type one.', 'yellow')
                return
            # Echo what it will actually match BEFORE committing: a typo or a sector the corpus
            # has never heard of otherwise shows up much later as a silently skipped slot.
            match = industry_focus.focus_match_count(workflow, sector) if industry_focus else None
            if match is not None and not match['held'] and not match['pending']:
                self._msg(f"'{sector}' matches nothing already held. Setting it anyway — "
                          f"discovery will go looking for it, but no pending company will "
                          f"match. Press again to confirm.", 'yellow')
                if getattr(self, '_confirming', None) != sector:
                    self._confirming = sector
                    return
            self._confirming = None
            batches = 3 if event.button.id == 'ind-set3' else None
            try:
                focus = workflow.set_standing_focus(sector, batches=batches, set_by='peter',
                                                    reason='set from the sector panel')
            except ValueError as exc:
                self._msg(str(exc), 'red')
                return
            scope = 'until cleared' if batches is None else f'for the next {batches} batches'
            held = match['held'] if match else '?'
            self._msg(f"Standing focus set to {focus['industry']} {scope} — "
                      f"matches {held} companies already held.", 'green')
            self.query_one('#indinput', Input).value = ''
            self._render_foot()

    class VideoScreen(ModalScreen):
        """YouTube links in, reading material out — then discuss it with 爸菲特.

        This screen decides nothing about research direction. Fetching costs no quota at all;
        summarising spends one Gemini call per chunk and says so before it does. The sector
        decision is meant to come out of the conversation afterwards, which is why the useful
        button here is 'Discuss' rather than anything that writes a focus.

        Fetch and summarise are slow and blocking, so like ChatScreen they run OFF the event
        loop — the dashboard's marquee, clock, poll and kill switch all share it.
        """

        CSS = """
        VideoScreen { align: center middle; }
        #vidbox { width: 92%; height: 90%; border: round $accent; padding: 1 2;
                  background: $surface; }
        #vidtable { height: 40%; }
        #vidlog { height: 1fr; border: round $primary 40%; }
        #vidstatus { height: auto; color: $text-muted; }
        .vid-buttons { height: 3; }
        #vidinput { width: 1fr; }
        """

        def __init__(self):
            super().__init__()
            self.rows = []
            self.selected = None
            self._busy = False

        def compose(self) -> ComposeResult:
            with Vertical(id='vidbox'):
                yield Label('YouTube → notes → talk it over with 爸菲特 · esc closes · '
                            'videos without subtitles are transcribed locally')
                with Horizontal(classes='vid-buttons'):
                    yield Input(placeholder='paste one or more YouTube links…', id='vidinput')
                    yield Button('Add', variant='primary', id='vid-add')
                yield DataTable(id='vidtable', cursor_type='row', zebra_stripes=True)
                with Horizontal(classes='vid-buttons'):
                    yield Button('Fetch transcripts', id='vid-fetch')
                    yield Button('Summarise', variant='warning', id='vid-summarise')
                    yield Button('Read notes', id='vid-read')
                    yield Button('Discuss with 爸菲特', variant='success', id='vid-discuss')
                yield Static('', id='vidstatus')
                yield RichLog(id='vidlog', wrap=True, markup=False, highlight=False)

        def on_mount(self):
            self.query_one('#vidtable', DataTable).add_columns(
                'Video', 'Channel', 'Title', 'Status', 'Notes')
            self.refresh_rows()
            self.query_one('#vidinput', Input).focus()

        # -- data ----------------------------------------------------------------
        def refresh_rows(self):
            table = self.query_one('#vidtable', DataTable)
            table.clear()
            workflow = workflow_state()
            if workflow is None or video_intel is None:
                self._status('The video engine is unavailable.', 'red')
                return
            self.rows = workflow.videos(limit=50)
            counts = {}
            for source in self.rows:
                counts[source['id']] = len(workflow.video_notes(source_id=source['id'],
                                                                limit=200))
            palette = {'pending': 'dim', 'fetched': 'cyan',
                       'summarised': 'green', 'failed': 'red'}
            for source in self.rows:
                style = palette.get(source['status'], '')
                table.add_row(
                    Text(source['video_id'], style),
                    Text((source['channel'] or '—')[:20], 'dim'),
                    Text((source['title'] or source['url'])[:46]),
                    Text(source['status'], style),
                    Text(str(counts.get(source['id'], 0)) or '·', 'dim'),
                    key=str(source['id']))
            if self.rows and self.selected is None:
                self.selected = self.rows[0]['id']
            # 'failed' counts as waiting: fetch_pending retries those, and a video that failed
            # for want of subtitles can now succeed through the audio route.
            pending = sum(1 for r in self.rows if r['status'] in ('pending', 'failed'))
            fetched = sum(1 for r in self.rows if r['status'] == 'fetched')
            self._status(f"{len(self.rows)} video(s) · {pending} awaiting transcript · "
                         f"{fetched} fetched and ready to summarise")

        def _selected_row(self):
            return next((r for r in self.rows if r['id'] == self.selected), None)

        def _status(self, text, style='dim'):
            self.query_one('#vidstatus', Static).update(Text(text, style))

        def _log(self, line):
            self.query_one('#vidlog', RichLog).write(line)

        # -- interaction ---------------------------------------------------------
        def on_data_table_row_highlighted(self, event):
            if event.row_key is not None:
                self.selected = int(event.row_key.value)

        def on_data_table_row_selected(self, event):
            if event.row_key is not None:
                self.selected = int(event.row_key.value)

        def on_key(self, event):
            if event.key == 'escape':
                self.dismiss(None)

        def on_input_submitted(self, event):
            if event.input.id == 'vidinput':
                self._add()

        def _add(self):
            field = self.query_one('#vidinput', Input)
            blob = field.value.strip()
            if not blob:
                return
            workflow = workflow_state()
            added, known = video_intel.add_links(workflow, blob)
            field.value = ''
            if not added and not known:
                self._log('No YouTube links found in that text.')
            else:
                self._log(f"added {len(added)}, already known {len(known)}")
            self.refresh_rows()

        def on_button_pressed(self, event):
            bid = event.button.id
            if bid == 'vid-add':
                return self._add()
            if self._busy:
                self._status('Still working — one job at a time.', 'yellow')
                return
            if bid == 'vid-fetch':
                # Must agree with fetch_pending, which retries 'failed' rows too; counting only
                # 'pending' here left a captionless video stuck behind "Nothing waiting".
                pending = sum(1 for r in self.rows if r['status'] in ('pending', 'failed'))
                if not pending:
                    self._status('Nothing waiting for a transcript.', 'dim')
                    return
                # Still costs no model calls, but no longer always instant: a video whose
                # uploader disabled subtitles is transcribed locally at roughly nine times real
                # time, so a 30-minute one is about three minutes of this Mac's time.
                self._run_job('fetch',
                              f'Fetching {pending} transcript(s) — no model calls spent. '
                              f'Any without subtitles are transcribed locally, which takes '
                              f'a few minutes each…')
            elif bid == 'vid-summarise':
                fetched = sum(1 for r in self.rows if r['status'] == 'fetched')
                if not fetched:
                    self._status('Nothing fetched and waiting to be summarised.', 'dim')
                    return
                # State the cost before spending it: long videos are chunked, so this is a
                # floor, not an exact figure.
                self._run_job('summarise',
                              f'Summarising {fetched} video(s) — at least one Gemini call '
                              f'each, from the same daily budget as the research loop…')
            elif bid == 'vid-read':
                self._read()
            elif bid == 'vid-discuss':
                self._discuss()

        def _read(self):
            source = self._selected_row()
            if not source:
                self._status('Select a video first.', 'yellow')
                return
            workflow = workflow_state()
            log = self.query_one('#vidlog', RichLog)
            log.clear()
            log.write(f"{source['channel']} — {source['title'] or source['url']}")
            if source['error_text']:
                log.write(f"failed: {source['error_text']}")
            notes = workflow.video_notes(source_id=source['id'], limit=100)
            if not notes:
                log.write('(no notes yet — fetch, then summarise)')
                return
            for note in notes:
                if note['kind'] == 'summary':
                    log.write(f"\n{note['note']}")
                else:
                    label = note['company_name'] or note['sector']
                    log.write(f"\n[{note['kind']}] {label}: {note['note']}")
                    log.write(f"   “{note['quote'][:160]}”")
                    log.write(f"   {video_intel.watch_url(note['video_id'], note['start_seconds'])}")

        def _discuss(self):
            source = self._selected_row()
            if not source:
                self._status('Select a video first.', 'yellow')
                return
            if source['status'] != 'summarised':
                self._status('Summarise it first — there is nothing to discuss yet.', 'yellow')
                return
            title = source['title'] or source['video_id']
            # Hand the chat a question, not the transcript: 爸菲特 pulls what it needs through
            # the read-only video_search tool, so the corpus and the video stay distinguishable.
            self.dismiss({'discuss': f'這部影片「{title}」講了什麼？和資料庫已收錄的公司有什麼關聯？'})

        # -- the slow half, off the event loop ------------------------------------
        def _run_job(self, kind, message):
            self._busy = True
            self._status(message, 'yellow')
            self.run_worker(lambda: self._job(kind), thread=True,
                            exclusive=True, group='videowork')

        def _job(self, kind):
            try:
                workflow = workflow_state()
                if kind == 'fetch':
                    done, failed = video_intel.fetch_pending(workflow, limit=10)
                    lines = []
                    for source in done:
                        how = ('transcribed from audio'
                               if str(source.get('lang') or '').startswith('audio')
                               else 'subtitles')
                        lines.append(f"fetched {source['video_id']}  "
                                     f"{source.get('char_count', 0)} chars  ({how})")
                    for source in failed:
                        # A video that cannot be read is a normal outcome, not a fault.
                        label = ('no subtitles and no audio transcription available'
                                 if source.get('no_transcript') else source['error'])
                        lines.append(f"{'SKIPPED' if source.get('no_transcript') else 'FAILED'}"
                                     f"  {source['video_id']}  {label}")
                else:
                    done, failed = video_intel.summarise_pending(workflow, limit=5)
                    lines = []
                    for source in done:
                        kept = len([n for n in source['notes'] if n['kind'] != 'summary'])
                        lines.append(f"summarised {source['video_id']}  {kept} note(s)"
                                     + (f", {source['dropped']} dropped for an unverifiable "
                                        f"quote" if source['dropped'] else ''))
                    lines += [f"FAILED  {s['video_id']}  {s['error']}" for s in failed]
                summary = f"{len(done)} done, {len(failed)} failed"
            except Exception as exc:
                lines, summary = [f'⚠️ {exc}'], 'job failed'
            self.app.call_from_thread(self._job_done, lines, summary)

        def _job_done(self, lines, summary):
            self._busy = False
            if not self.is_mounted:
                return
            for line in lines or ['(nothing to do)']:
                self._log(line)
            self.refresh_rows()
            self._status(summary)

    class DashApp(App):
        TITLE = '100K Research Monitor'
        boot_lang = load_dash_cfg().get('language', 'en')
        BINDINGS = [
            Binding('q', 'quit', tr(boot_lang, 'k_quit')),
            Binding('c', 'chat', tr(boot_lang, 'k_chat')),
            Binding('y', 'videos', 'Videos'),
            Binding('i', 'industries', 'Sectors'),
            Binding('p', 'providers', tr(boot_lang, 'k_providers')),
            Binding('v', 'review', tr(boot_lang, 'k_review')),
            Binding('r', 'force_refresh', tr(boot_lang, 'k_rescan')),
            Binding('s', 'settings', tr(boot_lang, 'k_settings')),
            Binding('g', 'roster', 'Roster'),
            Binding('l', 'toggle_language', tr(boot_lang, 'k_lang')),
            Binding('space', 'pause_marquee', tr(boot_lang, 'k_pause')),
            Binding('n', 'marquee_refresh', tr(boot_lang, 'k_new')),
        ]
        CSS = """
        #marquee { height: 2; background: $primary 20%; color: $text; }
        #status, #nowline { height: 1; }
        #meters { height: 3; margin-top: 1; }
        #focus-summary { height: 2; margin-top: 1; padding: 0 1;
                         background: $warning 12%; color: $warning; }
        #railbox { height: auto; }
        #rail { width: 3fr; height: 24; border: round $primary; }
        #slot-controls { width: 2fr; height: auto; margin-left: 1; padding: 0 1;
                         border: round $accent; }
        #slot-info, #slot-msg { height: 2; }
        .slot-buttons { height: 3; width: 1fr; }
        .slot-buttons Button { min-width: 8; margin-right: 1; }
        #kill-switch { width: 1fr; }
        #focus-country, #focus-industry { width: 1fr; }
        #cols { height: 1fr; margin-top: 1; }
        #history { width: 3fr; }
        #side { width: 2fr; margin-left: 2; }
        #roster { height: auto; margin-top: 1; padding-top: 1; border-top: solid $accent; }
        """

        def __init__(self):
            super().__init__()
            self.dash_cfg = load_dash_cfg()
            self.snap = None
            self.scan_data = None
            self.m_paused = False
            self.force_scan = False
            self.factoids = []
            self.f_idx = 0
            self.f_offset = 0      # scroll position within an overflowing message
            self.f_elapsed = 0.0   # seconds the current message has been shown
            self.f_hold = 0.0      # pause after an overflowing message finishes scrolling
            self.latest_batch_mtime = None
            self.selected_slot_id = None
            wf = workflow_state()
            self.focus_countries, raw_industries = wf.focus_values() if wf else ([], [])
            # focus_values returns every distinct industry label — ~1,690 of them, including
            # whole sentences. That is unusable in a dropdown and, worse, invites picking a
            # one-off label that matches a single company. Offer the curated themes instead,
            # annotated with what each currently holds, and keep the raw labels behind them so
            # nothing that used to be selectable stops being selectable.
            # The option VALUE stays the bare sector — it is written straight into
            # industry_focus and matched as a substring, so an annotated value would match
            # nothing. Only the visible label carries the count.
            themes = industry_focus.load_themes(read_loop_config()) if industry_focus else []
            coverage = scan_coverage() or {}
            held = {r['theme']: r['held'] for r in coverage.get('themes', [])}
            self.focus_industries = [
                (f'{t}  ({held[t]} held)' if t in held else t, t) for t in themes]
            self.focus_industries += [(i, i) for i in raw_industries if i not in themes]

        def t(self, key, **kw):
            return tr(self.dash_cfg.get('language', 'en'), key, **kw)

        def src_label(self, source):
            if source == 'repair':
                return self.t('src_repair')
            if source == 'new':
                return self.t('src_new')
            return source

        def compose(self) -> ComposeResult:
            yield Static(id='marquee')
            yield Static(id='status')
            yield Static(id='nowline')
            yield Static(id='meters')
            yield Static(id='focus-summary')
            with Horizontal(id='railbox'):
                yield DataTable(id='rail', cursor_type='row', zebra_stripes=True)
                with Vertical(id='slot-controls'):
                    # Global kill switch (always active): primary toggle on its own full-width
                    # row, with the recovery actions beneath so nothing crowds a narrow panel.
                    with Horizontal(classes='slot-buttons'):
                        yield Button(
                            self.t('k_resume') if read_control()['halted'] else self.t('k_halt'),
                            variant='error', id='kill-switch')
                    with Horizontal(classes='slot-buttons'):
                        yield Button(self.t('k_forcekill'), variant='error', id='force-kill')
                        yield Button(self.t('k_relaunch'), id='relaunch-loop')
                    yield Static('Select a future slot', id='slot-info')
                    with Horizontal(classes='slot-buttons'):
                        yield Button('Gemini', id='slot-gemini')
                        yield Button('Claude', id='slot-claude')
                        yield Button('Codex', id='slot-codex')
                    with Horizontal(classes='slot-buttons'):
                        yield Button('Set Maintenance', id='slot-mode')
                        yield Button('Turn Off', variant='warning', id='slot-enabled')
                    with Horizontal(classes='slot-buttons'):
                        yield Select([(c, c) for c in self.focus_countries],
                                     allow_blank=True, prompt='Any country', id='focus-country')
                        yield Select(self.focus_industries,
                                     allow_blank=True, prompt='Any industry', id='focus-industry')
                    with Horizontal(classes='slot-buttons'):
                        yield Button('Apply Focus', variant='primary', id='slot-focus')
                        yield Button('Clear Focus', id='slot-clear-focus')
                    yield Static(id='slot-msg')
            with Horizontal(id='cols'):
                yield Static(id='history')
                with Vertical(id='side'):
                    yield Static(id='todo')
                    yield Static(id='alerts')
            yield Static(id='roster')
            yield Footer()

        def on_mount(self):
            self.query_one('#rail', DataTable).add_columns(
                'Time', 'Mode', 'Focus', 'Model', 'Progress')
            self.factoids = build_factoids()
            self.refresh_data()
            self.set_interval(0.2, self.tick_marquee)
            self.set_interval(self.dash_cfg['poll_seconds'], self.refresh_data)
            self.set_interval(1.0, self.render_status)

        # -- data --
        def refresh_data(self):
            self.snap = build_snapshot()
            newest = self.snap['batches'][0]['mtime'] if self.snap['batches'] else None
            if newest != self.latest_batch_mtime:
                self.latest_batch_mtime = newest
                self.factoids = build_factoids()
                self.next_factoid(reset=True)
            try:
                mtime = os.path.getmtime(CSV_PATH)
            except OSError:
                mtime = None
            if (self.force_scan or self.scan_data is None
                    or (mtime and self.scan_data.get('mtime') != mtime)):
                self.force_scan = False
                self.run_worker(self.scan_worker, thread=True, exclusive=True, group='csvscan')
            else:
                self.render_all()

        def scan_worker(self):
            data = scan_db()
            self.call_from_thread(self.apply_scan, data)

        def apply_scan(self, data):
            self.scan_data = data
            self.render_all()

        # -- rendering --
        def render_all(self):
            if not self.snap:
                return
            self.render_status()
            self.render_meters()
            self.render_focus_summary()
            self.render_rail()
            self.render_history()
            self.render_side()
            self.render_roster()

        def render_status(self):
            if not self.snap:
                return
            snap = self.snap
            p = snap['proc']
            halted = read_control()['halted']
            t = Text()
            if halted:
                # Daemon may still be alive (idle) — keep the pid, but flag the operation halted.
                t.append(self.t('halted'), 'bold yellow')
                if p['pid']:
                    t.append(f"pid {p['pid']}  ")
                else:
                    t.append(self.t('stopped'), 'bold red')
            elif p['pid']:
                t.append(self.t('running'), 'bold green')
                t.append(f"pid {p['pid']}  ")
            else:
                t.append(self.t('stopped'), 'bold red')
            nxt_t, nxt_p = snap['next_slot']
            if nxt_t:
                mins = max(0, (nxt_t - datetime.datetime.now()).total_seconds() / 60)
                t.append(self.t('next'))
                t.append(f'{nxt_t:%H:%M} {nxt_p}', PROVIDER_STYLE.get(nxt_p, ''))
                t.append(self.t('in_m', m=mins))
            for pv in PROVIDERS:
                t.append(f'{pv[0].upper()} ', PROVIDER_STYLE[pv])
                t.append(f"{snap['state']['calls'][pv]}/{snap['budgets'][pv]}  ")
            self.query_one('#status', Static).update(t)
            # Keep the global kill-switch controls in sync with live daemon/halt state.
            self.query_one('#kill-switch', Button).label = (
                self.t('k_resume') if halted else self.t('k_halt'))
            self.query_one('#force-kill', Button).disabled = not p['pid']
            self.query_one('#relaunch-loop', Button).disabled = bool(p['pid'])
            cur = snap['log'].get('current')
            nl = Text(' ')
            if cur:
                state = ((self.t('b_running'), 'yellow') if p['pid']
                         else (self.t('b_orphan'), 'red'))
                nl.append(f"Batch {cur['seq']} ({self.src_label(cur['source'])}, ")
                nl.append(cur['provider'], PROVIDER_STYLE.get(cur['provider'], ''))
                nl.append(') ')
                nl.append(*state)
                nl.append(self.t('ok_failed', ok=cur['ok'], f=cur['failed'], n=cur['size']))
                if cur['researching'] and p['pid']:
                    nl.append(self.t('researching', c=cur['researching']), 'italic')
            elif p['pid']:
                nxt_t2, nxt_p2 = snap['next_slot']
                if nxt_t2:
                    nl.append(self.t('idle_next', n=snap['state']['batch_seq'] + 1), 'dim')
                    nl.append(f'{nxt_t2:%H:%M} {nxt_p2}', PROVIDER_STYLE.get(nxt_p2, ''))
            self.query_one('#nowline', Static).update(nl)

        def render_meters(self):
            scan = self.scan_data
            t = Text()
            if not scan or 'error' in scan:
                t.append((scan or {}).get('error', self.t('scanning')), 'dim')
                self.query_one('#meters', Static).update(t)
                return
            width = 44
            total = max(scan['harvested'], 1)
            d = max(1, round(width * scan['complete'] / total)) if scan['complete'] else 0
            m = max(1, round(width * scan['repair'] / total)) if scan['repair'] else 0
            m = min(m, width - min(d, width))
            t.append(self.t('research'))
            t.append('█' * min(d, width), 'green')
            t.append('▓' * m, 'yellow')
            t.append('░' * max(0, width - d - m), 'dim')
            t.append('  ' + self.t('done', n=scan['complete']), 'green')
            t.append(' ▏')
            t.append(self.t('in_prog', n=scan['repair']), 'yellow')
            t.append(' ▏' + self.t('harvested', n=scan['harvested']) + '\n')
            h = max(1, round(width * scan['harvested'] / 100000))
            t.append(self.t('capacity'))
            t.append('█' * h, 'green')
            t.append('░' * (width - h), 'dim')
            t.append('  ' + self.t('cap_line', n=scan['harvested'], p=scan['harvested'] / 1000))
            if scan['repair']:
                t.append(self.t('repair_phase'), 'dim')
            elif self.snap:
                grown = claimed_today(self.snap['batches'])
                t.append(self.t('today_delta', n=grown), 'green' if grown else 'dim')
            w = (self.snap or {}).get('workflow', {})
            if w:
                t.append('\n')
                t.append(self.t('life', deep=w['deep_researched'], pending=w['pending'],
                                running=w['researching'], retry=w['retry'],
                                maint=w['maintenance_due'], review=w['ticker_review'],
                                excluded=w['excluded']), 'bold')
            self.query_one('#meters', Static).update(t)

        def render_rail(self):
            snap = self.snap
            table = self.query_one('#rail', DataTable)
            table.clear()
            status_styles = {
                'pending': 'dim', 'running': 'bold yellow', 'complete': 'green',
                'partial': 'yellow', 'disabled': 'dim red', 'missed': 'red',
                'skipped': 'dim red', 'done': 'green', 'upcoming': 'dim',
                'none': 'dim red',
            }
            selected_row = None
            for index, slot in enumerate(snap['rail']):
                slot_id = slot.get('id')
                row_key = str(slot_id if slot_id is not None else f'legacy-{index}')
                provider = slot.get('provider', '')
                enabled = bool(slot.get('enabled', True))
                time_style = ('bold yellow' if slot.get('focused') and enabled
                              else PROVIDER_STYLE.get(provider, ''))
                if not enabled:
                    time_style = 'dim red'
                mode = slot.get('mode', 'research')
                mode_label = slot.get('mode_label') or (
                    'MAINT' if mode == 'maintenance' else 'DEEP')
                mode_style = ('bold yellow' if mode == 'maintenance' else 'bold green')
                if not enabled:
                    mode_style = 'dim'
                focus = slot.get('focus') or 'All companies'
                table.add_row(
                    Text(slot.get('hhmm_display', slot['hhmm']), time_style),
                    Text(mode_label, mode_style),
                    Text(focus, '' if slot.get('focus') and enabled else 'dim'),
                    Text(provider.title(), PROVIDER_STYLE.get(provider, '')),
                    Text(slot.get('detail', ''), status_styles.get(slot['status'], '')),
                    key=row_key)
                if slot_id == self.selected_slot_id:
                    selected_row = index
            if selected_row is not None:
                table.move_cursor(row=selected_row)
            self.update_slot_controls()

        def render_focus_summary(self):
            summary = (self.snap or {}).get(
                'focus_summary', 'Focus: no standing focus, no focused future slots')
            style = 'bold yellow' if summary.startswith('★') else 'dim'
            self.query_one('#focus-summary', Static).update(Text(summary, style))

        def selected_slot(self):
            if self.selected_slot_id is None or not self.snap:
                return None
            return next((s for s in self.snap['rail']
                         if s.get('id') == self.selected_slot_id), None)

        def update_slot_controls(self):
            slot = self.selected_slot()
            buttons = ('#slot-gemini', '#slot-claude', '#slot-codex', '#slot-mode',
                       '#slot-enabled', '#slot-focus', '#slot-clear-focus')
            if not slot:
                self.query_one('#slot-info', Static).update('Select a future slot')
                for selector in buttons:
                    self.query_one(selector, Button).disabled = True
                maintenance_count = sum(1 for s in (self.snap or {}).get('rail', [])
                                        if s.get('enabled') and s.get('mode') == 'maintenance')
                self.query_one('#slot-msg', Static).update(
                    f'Enabled maintenance slots: {maintenance_count}/2'
                    + ('' if maintenance_count == 2 else '  ⚠ recommended: 2'))
                return
            editable = (slot.get('raw_status', slot['status']) == 'pending'
                        and slot['slot_time'] > self.snap['now'].strftime('%H:%M'))
            mode = slot.get('mode', 'research')
            focus = slot.get('focus_detail') or slot.get('focus') or 'all companies'
            self.query_one('#slot-info', Static).update(
                f"{slot['slot_time']}  {mode.upper()}  {slot['provider'].title()}\nFocus: {focus}")
            self.query_one('#slot-mode', Button).label = (
                'Set Deep' if mode == 'maintenance' else 'Set Maintenance')
            self.query_one('#slot-enabled', Button).label = (
                'Turn On' if not slot.get('enabled', True) else 'Turn Off')
            for selector in buttons:
                self.query_one(selector, Button).disabled = not editable
            country = slot.get('country_focus') or Select.BLANK
            industry = slot.get('industry_focus') or Select.BLANK
            # A slot can carry a focus that is not among the offered options — an old free-text
            # label, or one typed into /focus. Assigning an unknown value to a Select raises,
            # which would break the whole rail render, so fall back to blank.
            known = {v for _, v in self.focus_industries}
            if industry is not Select.BLANK and industry not in known:
                industry = Select.BLANK
            self.query_one('#focus-country', Select).value = country
            self.query_one('#focus-industry', Select).value = industry
            maintenance_count = sum(1 for s in self.snap['rail']
                                    if s.get('enabled') and s.get('mode') == 'maintenance')
            warning = (f'Enabled maintenance slots: {maintenance_count}/2'
                       + ('' if maintenance_count == 2 else '  ⚠ recommended: 2'))
            if not editable:
                warning += '\nThis slot is locked.'
            self.query_one('#slot-msg', Static).update(warning)

        def on_data_table_row_highlighted(self, event):
            if event.data_table.id != 'rail':
                return
            try:
                self.selected_slot_id = int(event.row_key.value)
            except (TypeError, ValueError):
                self.selected_slot_id = None
            self.update_slot_controls()

        def on_data_table_row_selected(self, event):
            self.on_data_table_row_highlighted(event)

        def edit_selected_slot(self, confirmation=None, **changes):
            slot = self.selected_slot()
            if not slot:
                self.notify('Select a schedule slot first', severity='warning')
                return
            wf = workflow_state(create=True)
            try:
                wf.update_daily_slot(slot['id'], **changes)
            except Exception as exc:
                self.query_one('#slot-msg', Static).update(str(exc))
                self.notify(str(exc), severity='warning')
                return
            self.refresh_data()
            if confirmation:
                self.query_one('#slot-msg', Static).update(confirmation)
                self.notify(confirmation)

        def handle_loop_control(self, bid):
            """Global kill switch: graceful halt/resume flag, plus a hard Force Kill and a
            Relaunch for a wedged or stopped daemon."""
            try:
                if bid == 'kill-switch':
                    halted = not read_control()['halted']
                    write_control(halted)
                    self.notify(self.t('halt_done' if halted else 'resume_done'),
                                severity='warning' if halted else 'information')
                elif bid == 'force-kill':
                    pid = loop_process()['pid']
                    if pid:
                        os.kill(pid, signal.SIGTERM)
                        self.notify(self.t('forcekill_done', pid=pid), severity='warning')
                    else:
                        self.notify(self.t('forcekill_none'), severity='warning')
                elif bid == 'relaunch-loop':
                    existing = loop_process()['pid']
                    if existing:
                        self.notify(self.t('relaunch_busy', pid=existing), severity='warning')
                    else:
                        # Explicitly starting the operation clears any stale halt flag, so the
                        # fresh daemon actually runs instead of coming up already halted.
                        write_control(False)
                        pid = relaunch_loop()
                        self.notify(self.t('relaunch_done', pid=pid))
            except Exception as exc:
                self.notify(str(exc), severity='error')
            self.refresh_data()

        def on_button_pressed(self, event):
            bid = event.button.id or ''
            if bid in ('kill-switch', 'force-kill', 'relaunch-loop'):
                self.handle_loop_control(bid)
                return
            if not bid.startswith('slot-'):
                return
            slot = self.selected_slot()
            if not slot:
                self.notify('Select a schedule slot first', severity='warning')
                return
            if bid in ('slot-gemini', 'slot-claude', 'slot-codex'):
                self.edit_selected_slot(provider=bid.removeprefix('slot-'))
            elif bid == 'slot-mode':
                mode = 'research' if slot.get('mode') == 'maintenance' else 'maintenance'
                self.edit_selected_slot(mode=mode)
            elif bid == 'slot-enabled':
                self.edit_selected_slot(enabled=not bool(slot.get('enabled', True)))
            elif bid == 'slot-clear-focus':
                self.edit_selected_slot(
                    confirmation='✓ Focus cleared; this slot covers all companies',
                    country_focus='', industry_focus='')
            elif bid == 'slot-focus':
                country_value = self.query_one('#focus-country', Select).value
                industry_value = self.query_one('#focus-industry', Select).value
                country = '' if country_value is Select.BLANK else str(country_value)
                industry = '' if industry_value is Select.BLANK else str(industry_value)
                parts = []
                if country:
                    parts.append(f'Country: {country}')
                if industry:
                    parts.append(f'Industry: {industry}')
                focus = ' • '.join(parts) or 'All companies'
                mode = 'Maintenance' if slot.get('mode') == 'maintenance' else 'Deep research'
                self.edit_selected_slot(
                    confirmation=f'✓ {mode} focus active: {focus}',
                    country_focus=country, industry_focus=industry)

        def render_history(self):
            table = Table(title=self.t('hist_title'), title_justify='left',
                          box=None, padding=(0, 1), title_style='bold')
            for col in self.t('hist_cols'):
                table.add_column(col)
            for b in self.snap['batches'][:self.dash_cfg['history_rows']]:
                provider_label = '→'.join(b.get('providers_used') or [b['provider']])
                table.add_row(
                    f"{b['when']:%m-%d %H:%M}", str(b['seq']), self.src_label(b['source']),
                    Text(provider_label, PROVIDER_STYLE.get(b['provider'], '')),
                    self.t('applied_fmt', a=b['applied'], u=len(b['updated']), c=len(b['claimed'])),
                    Text(str(len(b['failed'])), 'red' if b['failed'] else 'dim'))
            self.query_one('#history', Static).update(table)

        def render_side(self):
            scan = self.scan_data
            t = Text(self.t('todo'), 'bold')
            if scan and 'error' not in scan:
                if scan['repair']:
                    t.append(self.t('repair_pending', n=scan['repair']), 'yellow')
                    if scan['by_country']:
                        t.append(' ' + ', '.join(f'{c} {n}' for c, n in scan['by_country']) + '\n', 'dim')
                    snap = self.snap
                    bsize = snap['cfg'].get('batch_size', 20) if snap else 20
                    cur = snap['log'].get('current') if snap else None
                    running = bool(cur and snap['proc']['pid'])
                    queue = [n for n, _ in scan['next_up'][:bsize]]
                    if queue:
                        # While a batch runs, the CSV still lists its companies as
                        # pending, so the head of the queue IS the running batch.
                        if running:
                            t.append(self.t('batch_running', n=cur['seq'], m=len(queue)), 'bold yellow')
                        else:
                            seq = snap['state']['batch_seq'] + 1 if snap else '?'
                            t.append(self.t('coming_queue', n=seq, m=len(queue)), 'bold')
                        t.append(' ' + ', '.join(queue) + '\n')
                        upnext = [n for n, _ in scan['next_up'][bsize:bsize + 5]]
                        if running and upnext:
                            t.append(self.t('then') + ', '.join(upnext) + '…\n', 'dim')
                else:
                    t.append(self.t('queue_clear'), 'green')
            w = (self.snap or {}).get('workflow', {})
            if w:
                t.append('\nWorkflow queues\n', 'bold')
                t.append(f" pending {w['pending']}  retry {w['retry']}  "
                         f"maintenance {w['maintenance_due']}\n")
                t.append(f" review {w['ticker_review']}  excluded {w['excluded']}   "
                         "[v] inspect\n", 'yellow' if w['ticker_review'] else 'dim')
            coverage = (self.snap or {}).get('coverage')
            if coverage:
                t.append('\nSector coverage  [i] control\n', 'bold')
                total = len(coverage['themes'])
                researched = coverage['researched'] or 1
                pct = round(100 * coverage['unmapped'] / researched)
                t.append(f" {coverage['worked_themes']}/{total} worked in "
                         f"{coverage['window_days']}d   "
                         f"{coverage['thin_themes']} thin   {pct}% unmapped\n",
                         'yellow' if coverage['thin_themes'] else 'dim')
                thin = [r['theme'] for r in coverage['themes'] if r['thin']][-4:]
                if thin:
                    t.append(' starved: ' + ', '.join(reversed(thin)) + '\n', 'dim')
                focus = (self.snap or {}).get('standing_focus')
                if focus:
                    where = '/'.join(x for x in (focus.get('country'),
                                                 focus.get('industry')) if x)
                    scope = ('until cleared' if focus.get('batches_remaining') is None
                             else f"{focus['batches_remaining']} batches left")
                    t.append(f" focus: {where} ({scope})\n", 'bold yellow')
                else:
                    t.append(' no standing focus\n', 'dim')
                proposals = (self.snap or {}).get('focus_proposals') or []
                if proposals:
                    names = ', '.join(p['industry'] or p['country'] for p in proposals[:3])
                    t.append(f" 爸菲特 suggests: {names}  [i] to accept\n", 'cyan')
            providers = (self.snap or {}).get('providers', [])
            if providers:
                t.append('\nProviders  [p] control\n', 'bold')
                calls = (self.snap or {}).get('state', {}).get('calls', {})
                budgets = (self.snap or {}).get('budgets', {})
                for ps in providers:
                    pv = ps['provider']
                    state_text = 'ON' if ps['enabled'] else 'OFF'
                    style = PROVIDER_STYLE.get(pv, '') if ps['enabled'] else 'dim red'
                    t.append(f" {pv:<7} {state_text:<3} {ps['health']:<10} "
                             f"{calls.get(pv, 0)}/{budgets.get(pv, 0)}\n", style)
            self.query_one('#todo', Static).update(t)
            a = Text(self.t('alerts'), 'bold')
            alerts = build_alerts(self.snap, scan, self.dash_cfg.get('language', 'en'))
            if not alerts:
                a.append(self.t('none'), 'dim green')
            for level, msg in alerts:
                style = 'bold red' if level == 'critical' else 'yellow'
                a.append(f' [{level.upper()}] ', style)
                a.append(msg + '\n')
            self.query_one('#alerts', Static).update(a)

        def next_factoid(self, reset=False):
            self.f_offset = 0
            self.f_elapsed = 0.0
            self.f_hold = 0.0
            if reset:
                self.f_idx = 0
            else:
                self.f_idx += 1
                if self.f_idx >= len(self.factoids):  # wrap: fresh shuffle
                    self.factoids = build_factoids()
                    self.f_idx = 0

        def tick_marquee(self):
            bar = self.query_one('#marquee', Static)
            if not self.factoids:
                bar.update(Text(self.t('marquee_wait'), 'dim'))
                return
            width = max(20, self.size.width - 4)
            f = self.factoids[self.f_idx % len(self.factoids)]
            overflow = cell_len(f['text']) - width
            if not self.m_paused:
                if overflow <= 0:
                    self.f_elapsed += 0.2
                    if self.f_elapsed >= self.dash_cfg['marquee_seconds']:
                        self.next_factoid()
                elif self.f_offset < overflow:
                    self.f_offset = min(overflow,
                                        self.f_offset + self.dash_cfg['marquee_cells_per_tick'])
                else:
                    self.f_hold += 0.2
                    if self.f_hold >= 2.0:
                        self.next_factoid()
                f = self.factoids[self.f_idx % len(self.factoids)]
            out = Text(' ▌', 'bold')
            out.append(f['company'], 'bold')
            if f.get('country'):
                out.append(f" · {f['country']}", 'italic')
            out.append(f"  ▪ {f['field']}", 'bold yellow')
            if f['tag']:
                out.append(f"  [{f['tag']}]", 'dim')
            out.append(f"  Batch {f['batch']}", 'dim')
            out.append(f"  ({self.f_idx % len(self.factoids) + 1}/{len(self.factoids)})", 'dim')
            out.append('\n  ')
            out.append(cells_slice(f['text'], self.f_offset, width))
            bar.update(out)

        # -- actions --
        def render_roster(self):
            roster = read_roster()
            t = Text()
            if not roster:
                t.append('Telegram roster: empty — press g to add users', 'dim')
                self.query_one('#roster', Static).update(t)
                return
            enabled = sum(1 for u in roster if u['enabled'])
            applied = gateway_status()
            t.append('Telegram Roster  ', 'bold')
            t.append(f'{enabled}/{len(roster)} enabled', 'green' if enabled else 'dim')
            if applied is not None:
                t.append('  ·  ')
                t.append(f'{applied} route(s) applied',
                         'green' if applied == enabled else 'yellow')
                if applied != enabled:
                    t.append(' — press g to apply', 'dim yellow')
            t.append('   [g] edit', 'dim')
            for i, u in enumerate(roster, 1):
                t.append(f'\n {i}. ')
                t.append('●' if u['enabled'] else '○', 'green' if u['enabled'] else 'dim')
                t.append(f" {u['label']:<12} ", 'bold' if u['enabled'] else 'dim')
                t.append(f"{u['chat_id']:<13} ", 'dim')
                t.append(u['profile'])
            self.query_one('#roster', Static).update(t)

        def action_pause_marquee(self):
            self.m_paused = not self.m_paused

        def action_marquee_refresh(self):
            current = self.factoids[self.f_idx % len(self.factoids)]['batch'] if self.factoids else None
            self.factoids = build_factoids(exclude_batch=current)
            self.next_factoid(reset=True)

        def action_force_refresh(self):
            _scan_cache['mtime'] = None  # invalidate scan_db's module cache
            self.force_scan = True       # and bypass refresh_data's mtime guard
            self.refresh_data()

        def action_settings(self):
            def applied(cfg):
                if cfg:
                    self.dash_cfg.update(cfg)
                    save_dash_cfg(self.dash_cfg)
                    self.render_all()  # apply language/rows immediately
            self.push_screen(SettingsScreen(self.dash_cfg), applied)

        def action_toggle_language(self):
            cur = self.dash_cfg.get('language', 'en')
            self.dash_cfg['language'] = 'zh' if cur == 'en' else 'en'
            save_dash_cfg(self.dash_cfg)
            # Relaunch so class-level BINDINGS (footer hints) rebuild in the new
            # language — main() reruns run_tui on this result.
            self.exit(result='lang-toggle')

        def action_roster(self):
            # In-app modal editor — same process, no second dashboard. Refresh the
            # read-only roster panel when it closes.
            self.push_screen(RosterScreen(TG_USERS_PATH), lambda _=None: self.render_roster())

        def action_providers(self):
            if not self.snap:
                return
            self.push_screen(ProviderScreen(self.snap['state']['calls'], self.snap['budgets']),
                             lambda _=None: self.refresh_data())

        def action_review(self):
            self.push_screen(ReviewScreen(), lambda _=None: self.refresh_data())

        def action_videos(self):
            def closed(result=None):
                self.refresh_data()
                # 'Discuss' hands the question straight to the chat, so one keypress goes from
                # a video's notes to a conversation about them.
                if isinstance(result, dict) and result.get('discuss'):
                    self.push_screen(ChatScreen(), lambda _=None: self.refresh_data())
                    self.call_after_refresh(self._seed_chat, result['discuss'])
            self.push_screen(VideoScreen(), closed)

        def _seed_chat(self, question):
            try:
                field = self.screen.query_one('#chatinput', Input)
            except Exception:
                return
            field.value = question

        def action_industries(self):
            # Force a fresh coverage read on close: setting a focus changes what the
            # always-visible line should say, and the mtime cache may not have rolled yet.
            def closed(_=None):
                scan_coverage(force=True)
                self.refresh_data()
            self.push_screen(IndustryScreen((self.snap or {}).get('coverage')), closed)

        def action_chat(self):
            # Read-only surface: 爸菲特 answers questions, it never edits slots, roster or
            # research content (dashboard.py's standing invariant).
            self.push_screen(ChatScreen())

    if return_app:  # test hook: hand back the classes without running the loop
        return DashApp, RosterScreen

    return DashApp().run()


def main():
    ap = argparse.ArgumentParser(description='Read-only monitor for the research loop')
    ap.add_argument('--report', action='store_true', help='one-shot plain-text report')
    args = ap.parse_args()
    if args.report:
        print(render_report())
    else:
        while run_tui() == 'lang-toggle':
            pass


if __name__ == '__main__':
    main()
