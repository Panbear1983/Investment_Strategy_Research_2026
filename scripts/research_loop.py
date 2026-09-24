# Autonomous batch-research loop: Gemini (with web search) -> validation gate -> apply_batch.
# Replaces the manual Gemini-web / hand-built update_batch_N.py cycle.
#
#   python3 research_loop.py --dry-run [N]   research N companies, print results, write nothing
#   python3 research_loop.py --once          run one batch and stop
#   python3 research_loop.py --run           run batches until queue/budget exhausted
#
# Repair rows (real tickers still carrying 等待/調查中 markers) are researched before any new
# companies are nominated.
#
# Backends (config.json "backend"):
#   "cli" (default) — headless Antigravity CLI (`agy -p`, config cli_command), which runs on the
#                     Gemini Pro subscription's OAuth quota; no API key. (Classic gemini-cli also
#                     works as cli_command but Google sunset its free Code Assist tier mid-2026.)
#   "api"           — REST generateContent with google_search grounding; needs GOOGLE_API_KEY
#                     env var or ~/.config/gemini/api_key (billed separately from the sub).
import argparse
import contextlib
import csv
import datetime
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from zoneinfo import ZoneInfo

# Single-instance guard: two --run loops racing on the same sqlite DB caused
# a real crash (ValueError: ticker already belongs to another company) and a
# silent multi-hour stall, both on 2026-07-23 — two `research_loop.py --run`
# processes had been started an hour apart in the same terminal and neither
# noticed the other. flock on this file is held for the life of the process;
# a second invocation fails fast instead of racing writes to the DB.
_LOCK_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.research_loop.lock')
_lock_fh = None  # kept open for the process lifetime — closing it releases the lock


def _acquire_singleton_lock():
    global _lock_fh
    _lock_fh = open(_LOCK_PATH, 'w')
    try:
        fcntl.flock(_lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("Another research_loop.py is already running (lock held on "
              f"{_LOCK_PATH}). Exiting instead of racing it.")
        sys.exit(1)
    _lock_fh.write(str(os.getpid()))
    _lock_fh.flush()

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import apply_batch
from apply_batch import (COLS, CSV_PATH, REPO_DIR, extract_ticker,
                         is_placeholder_cell, is_placeholder_row, send_telegram)
from research_state import (DEEP_RESEARCHED, EXCLUDED, MAINT_RETRY,
                            MAINT_SCREEN_DUE, MAINT_UPDATE_DUE, RESEARCH_PENDING,
                            TICKER_REVIEW, VALIDATED, WorkflowState, normalize_source_urls)

SCRIPTS_DIR = os.path.join(REPO_DIR, 'scripts')
CONFIG_PATH = os.environ.get(
    'ISR_CONFIG_PATH',
    os.path.join(SCRIPTS_DIR, 'config.json')
)
STATE_PATH = os.path.join(SCRIPTS_DIR, 'loop_state.json')
# Global kill switch. The dashboard writes {"halted": true} here; the conductor and every
# long-running batch loop re-read it (like load_config) so a halt applies without a restart.
# The filename deliberately avoids "research_loop" so `pkill -f research_loop` can't match it.
CONTROL_PATH = os.path.join(SCRIPTS_DIR, 'loop_control.json')
MAINTENANCE_STATE_PATH = os.path.join(SCRIPTS_DIR, 'maintenance_state.json')
BATCHES_DIR = os.path.join(SCRIPTS_DIR, 'batches')
QUARANTINE_DIR = os.path.join(SCRIPTS_DIR, 'quarantine')
WORKFLOW_PATH = os.environ.get(
    'ISR_WORKFLOW_DB', os.path.join(SCRIPTS_DIR, 'research_workflow.sqlite3'))
_WORKFLOW = None

RESEARCH_KEYS = COLS[:21]  # model produces columns 0-20; insider col is fetched locally
# Fields that must be substantial Traditional-Chinese prose. Market cap (約 8 億台幣) and
# competitor lists (AMD、Intel) are legitimately short or non-CJK, so they are exempt.
NARRATIVE_KEYS = [k for k in COLS[7:21] if k != 'Key Competitors']
TIMEFRAMES = ('短期', '中期', '中長期', '長期')
CJK_RE = re.compile(r'[一-鿿]')

# News-sensitive columns the maintenance loop refreshes (a subset of RESEARCH_KEYS). A
# maintenance record carries ONLY these keys; apply_records merges non-empty fields into
# the existing row, so the rest of the foundational research is preserved untouched.
# Recent Insider Trading (col 21) refreshes for free via apply_records' get_insider_data.
MAINTENANCE_KEYS = ['12M Catalysts', 'Key Investment Risks', '3-Year M&A',
                    'Geopolitical Exposure', 'M&A Potential']
# Maintenance priority tiebreak within equal staleness: leaders before the long tail.
TIER_PRIORITY = {'龍頭股': 0, '潛力股': 1, '隱形冠軍': 2, '輔助公司': 3}

# Work-order priority carried over from the legacy sort_database.py
COUNTRY_PRIORITY = {'Taiwan': 1, 'USA': 2, 'Japan': 3, 'South Korea': 4, 'China': 5,
                    'Israel': 6, 'France': 7, 'Germany': 8, 'Netherlands': 9, 'UK': 10}

# Canonical English country name -> the surface forms the models emit (Chinese names,
# bilingual "美國（USA）", or a whole HQ paragraph led by the country). canonical_country
# keeps one label per country so the DB never carries e.g. both 'USA' and '美國'.
COUNTRY_ALIASES = {
    'Taiwan': ['Taiwan', '台灣', '臺灣', '台湾'], 'USA': ['USA', '美國', '美国'],
    'Japan': ['Japan', '日本'],
    'South Korea': ['South Korea', 'South韓', '南韓', '韓國', '大韓民國', 'Korea'],
    'China': ['China', '中國', '中国'], 'Israel': ['Israel', '以色列'],
    'France': ['France', '法國', '法国'], 'Germany': ['Germany', '德國', '德国'],
    'Netherlands': ['Netherlands', '荷蘭', '荷兰'],
    'UK': ['UK', 'United Kingdom', 'Britain', '英國', '英国'],
    'Switzerland': ['Switzerland', '瑞士'], 'Sweden': ['Sweden', '瑞典'],
    'Canada': ['Canada', '加拿大'], 'Denmark': ['Denmark', '丹麥', '丹麦'],
    'Singapore': ['Singapore', '新加坡'], 'Spain': ['Spain', '西班牙'],
    'Australia': ['Australia', '澳洲', '澳大利亞', '澳大利亚'], 'Brazil': ['Brazil', '巴西'],
    'Italy': ['Italy', '義大利', '意大利'], 'Argentina': ['Argentina', '阿根廷'],
}


def get_workflow():
    global _WORKFLOW
    if _WORKFLOW is None:
        _WORKFLOW = WorkflowState(WORKFLOW_PATH)
    return _WORKFLOW


def canonical_country(raw):
    """Map any Country surface form to one canonical English label. Picks the country
    whose alias appears earliest in the string, so a paragraph led by the primary
    country ('英國（與澳洲雙重上市…）') resolves to that country (UK), not a later mention.
    Unknown values are returned unchanged."""
    s = (raw or '').strip()
    if not s:
        return s
    best, best_idx = None, len(s) + 1
    for canon, aliases in COUNTRY_ALIASES.items():
        for a in aliases:
            i = s.find(a)
            if i != -1 and i < best_idx:
                best_idx, best = i, canon
    return best or s


# Keys the loop cannot run correctly without. Two categories, both learned the hard way on
# 2026-08-13 when a config swap replaced this file with a different schema:
#
#   crash-on-absent — read as cfg['key'] with no guard, so a missing key killed slots one by
#     one ('model' and 'batch_size' appear verbatim as skip reasons in daily_slots that day).
#     Loud, but per-slot and easy to mistake for a data problem.
#
#   silent-on-absent — read via cfg.get(key, default), where the default quietly DISABLES a
#     guarantee. These are the dangerous ones. 'claude_args' carries the tool boundary, so its
#     absence downgraded the research call to a fully-capable `claude -p` (canary-proven on
#     2026-07-27 to read arbitrary local files). 'schedule' absent means seed_daily_slots gets
#     [] and NO slots are created — the loop simply goes quiet the next day.
#
# Validating here converts both into one clear failure at startup instead of a slow puzzle.
REQUIRED_CONFIG_KEYS = (
    'batch_size', 'daily_call_budgets', 'delay_seconds', 'max_retries', 'model',
    'schedule', 'claude_args',
)

# Presence is not enough — TYPE matters. The 2026-08-13 replacement config kept
# `cli_extra_args` but changed it from a list to "". `cmd += ""` iterates an empty string and
# appends nothing, so the agy call silently lost `--mode plan` (its read-only planning mode)
# and `--print-timeout`. Worse, a NON-empty string would splat one character per argv element.
# Every key spliced into a command line or iterated must therefore be type-checked.
CONFIG_TYPES = {
    'batch_size': int, 'daily_call_budgets': dict, 'delay_seconds': (int, float),
    'max_retries': int, 'model': str, 'schedule': list, 'claude_args': list,
    'cli_extra_args': list, 'provider_fallback_order': list,
    'discovery_focus_rotation': list, 'maintenance_cadence_days': dict,
    'daily_discovery_attempt_budget': int, 'daily_empty_discovery_limit': int,
    # Sector-steering additions. industry_themes is iterated to build the coverage panel and
    # the focus dropdown, so it gets the same type guard as every other iterated key.
    'industry_themes': list, 'industry_thin_floor': int, 'industry_window_days': int,
    'video_summary_daily_cap': int, 'video_daily_cap_per_user': int,
    'video_chunk_chars': int, 'whisper_model_path': str, 'whisper_language': str,
    'whisper_realtime_factor': (int, float),
    'discovery_slice_strikes': int, 'discovery_slice_cooldown_days': int,
    'discovery_slice_cooldown_cap_days': int,
}

# Flags the claude research call must always carry, regardless of what config supplies.
# `--tools WebSearch` removes every other built-in tool (Bash/Read/Write/Edit/WebFetch/Task);
# `--setting-sources ''` stops the call inheriting ~/.claude/settings.json, which sets
# "defaultMode": "bypassPermissions". Config may ADD flags but can no longer REMOVE these:
# living only in config meant one file swap silently reopened the exposure.
MANDATORY_CLAUDE_FLAGS = (('--tools', 'WebSearch'), ('--setting-sources', ''))


class ConfigError(RuntimeError):
    """Config is unusable. Raised loudly rather than degrading into unsafe defaults."""


def validate_config(cfg, path=CONFIG_PATH):
    missing = [k for k in REQUIRED_CONFIG_KEYS if k not in cfg]
    if missing:
        raise ConfigError(
            f"{path} is missing required key(s): {', '.join(missing)}.\n"
            f"It has {len(cfg)} key(s): {', '.join(sorted(cfg))}.\n"
            "Refusing to run: absent keys would silently disable the tool boundary "
            "(claude_args) or daily scheduling (schedule). Restore a full config — "
            "scripts/config.json.clean is the known-good reference.")
    if not cfg.get('schedule'):
        raise ConfigError(f"{path}: 'schedule' is empty — no slots would ever be created.")
    wrong = []
    for key, want in CONFIG_TYPES.items():
        if key in cfg and not isinstance(cfg[key], want):
            names = getattr(want, '__name__', None) or '/'.join(t.__name__ for t in want)
            wrong.append(f"{key} must be {names}, got {type(cfg[key]).__name__} ({cfg[key]!r:.40})")
    if wrong:
        raise ConfigError(
            f"{path} has wrong-typed key(s):\n  " + "\n  ".join(wrong) +
            "\nRefusing to run: a string where a list belongs is spliced character-by-character "
            "into a command line, or silently contributes nothing.")
    return cfg


def enforce_claude_flags(claude_args):
    """Return claude_args with the mandatory tool-boundary flags guaranteed present.

    Config-supplied values win for anything else, so `--model` and additions still work; only
    the safety pair is non-negotiable. A config that tries to set them to something weaker is
    overridden rather than honoured.
    """
    args = list(claude_args or [])
    for flag, value in MANDATORY_CLAUDE_FLAGS:
        if flag in args:
            args[args.index(flag) + 1] = value      # normalise a weakened value
        else:
            args += [flag, value]
    return args


def load_config():
    try:
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            cfg = json.load(f)
    except FileNotFoundError:
        raise ConfigError(f"{CONFIG_PATH} does not exist.")
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{CONFIG_PATH} is not valid JSON: {exc}")
    validate_config(cfg, CONFIG_PATH)
    cfg['claude_args'] = enforce_claude_flags(cfg.get('claude_args'))
    return cfg


def load_control():
    """Kill-switch state. Absent or unreadable file == not halted, so a missing/corrupt
    control file can never wedge the loop into a permanent halt."""
    try:
        with open(CONTROL_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return {'halted': bool(data.get('halted', False)),
                'halted_at': data.get('halted_at', ''), 'by': data.get('by', '')}
    except (OSError, ValueError):
        return {'halted': False, 'halted_at': '', 'by': ''}


def halt_requested():
    return load_control()['halted']


def load_api_key(cfg):
    if cfg.get('backend', 'cli') == 'cli':
        return None  # gemini-cli uses its own OAuth login
    key = os.environ.get('GOOGLE_API_KEY', '').strip()
    if key:
        return key
    key_path = os.path.expanduser('~/.config/gemini/api_key')
    if os.path.exists(key_path):
        with open(key_path, 'r') as f:
            key = f.read().strip()
        if key:
            return key
    sys.exit("backend=api but no Gemini API key. Export GOOGLE_API_KEY or put the key in "
             "~/.config/gemini/api_key (create at https://aistudio.google.com/apikey), "
             "or set backend to 'cli' in config.json to use the gemini-cli OAuth login.")


# ---------------- state / budget ----------------

def load_state():
    today = datetime.date.today().isoformat()
    state = {
        'date': today,
        'calls': {'gemini': 0, 'agy_claude': 0, 'claude': 0, 'codex': 0},
        'discovery': {'attempts': 0, 'empty_results': 0},
        'discovery_cursor': 0,
        'discovery_slices': {},
        'batch_seq': 0,
    }
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, 'r', encoding='utf-8') as f:
            saved = json.load(f)
        state['batch_seq'] = saved.get('batch_seq', 0)
        # Restored OUTSIDE the date guard, like batch_seq. Keying the focus rotation off the
        # daily `attempts` counter meant it reset to index 0 every midnight: with
        # daily_empty_discovery_limit=2 the loop probed Japan then South Korea and stopped,
        # every single day, and the other ten slices became unreachable.
        cursor = saved.get('discovery_cursor', 0)
        state['discovery_cursor'] = cursor if isinstance(cursor, int) and cursor >= 0 else 0
        # Also outside the date guard: a slice is exhausted for weeks, not for a day. Without
        # this the ledger evaporated on every slot and no slice ever rested.
        slices = saved.get('discovery_slices')
        if isinstance(slices, dict):
            state['discovery_slices'] = slices
        last = saved.get('discovery_last_slice')
        if isinstance(last, str):
            state['discovery_last_slice'] = last
        # Date-stamped once-a-day markers. Outside the date guard: each is compared with a
        # date, so a stale value is simply "not today" and never wrong.
        for key in ('daily_digest_sent', 'direction_mined_out_notified'):
            if isinstance(saved.get(key), str):
                state[key] = saved[key]
        if saved.get('date') == today:
            calls = saved.get('calls', 0)
            if isinstance(calls, dict):
                state['calls'].update(calls)
            else:  # pre-dual-provider state file
                state['calls']['gemini'] = calls
            discovery = saved.get('discovery', {})
            if isinstance(discovery, dict):
                for field in ('attempts', 'empty_results'):
                    value = discovery.get(field, 0)
                    state['discovery'][field] = value if isinstance(value, int) and value >= 0 else 0
    return state


def save_state(state):
    with open(STATE_PATH, 'w', encoding='utf-8') as f:
        json.dump(state, f, indent=4)


def discovery_budget_available(cfg, state):
    """Discovery is optional growth work; cap it before invoking any provider.

    `daily_empty_discovery_limit` is now a CIRCUIT BREAKER for a broken provider, not a
    saturation guard — saturation is handled per slice by the cooldown ledger below. It used to
    be both, counted globally, and that combination stalled corpus growth daily: Japan and South
    Korea sit at positions 0 and 1 of the rotation and are both picked clean, so the first two
    attempts of a day returned nothing, tripped a limit of 2, and shut discovery down having
    used 2 of its 6 attempts. Raising the limit alone would just have paid for the same two dead
    slices; the ledger is what stops asking them.
    """
    discovery = state.setdefault('discovery', {'attempts': 0, 'empty_results': 0})
    attempt_limit = max(0, int(cfg.get('daily_discovery_attempt_budget', 24)))
    breaker = max(0, int(cfg.get('daily_empty_discovery_limit', 6)))
    return (discovery.get('attempts', 0) < attempt_limit
            and discovery.get('empty_results', 0) < breaker)


def _rotation_entry(entry):
    """One rotation entry as (country, industry). Accepts a bare country string."""
    if isinstance(entry, str):
        return entry, ''
    entry = list(entry) + ['', '']
    return (entry[0] or ''), (entry[1] or '')


def slice_key(country, industry):
    return f'{country}|{industry}'


def slice_ledger(state):
    """Per-slice memory of what discovery has already exhausted.

    Lives in loop_state.json beside the counters, so there is no schema migration and it is
    readable when diagnosing a stalled corpus. Absent ledger == today's behaviour.
    """
    return state.setdefault('discovery_slices', {})


def slice_cooldown_until(state, country, industry):
    entry = slice_ledger(state).get(slice_key(country, industry)) or {}
    return entry.get('cooldown_until') or ''


def discovery_focus(cfg, state, focus_country='', focus_industry='', today=None, plan=None):
    """Pick which slice of the world this discovery attempt should probe.

    An explicit slot focus always wins. Otherwise walk the rotation from the cursor, SKIPPING
    slices that are resting because recent attempts there found nothing new. A slice that has
    been picked clean used to be re-asked forever — Japan and South Korea returned 0 on every
    visit for days while still costing a call each time.

    If every slice is resting, take the one whose rest ends soonest rather than stalling: a
    stale slice is still a better use of an attempt than no attempt at all.
    """
    if focus_country or focus_industry:
        return focus_country, focus_industry
    today = today or datetime.date.today().isoformat()
    # The direction list (typed in the dashboard or the bot, in the workflow DB) outranks the
    # config rotation: hunt the thinnest listed industry that is not resting. An empty list,
    # or one whose every row is resting, falls through to the config rotation.
    if plan:
        from industry_focus import pick_direction
        pick = pick_direction(plan, slice_ledger(state), today)
        if pick is not None:
            return _take_slice(state, pick.get('country') or '', pick.get('industry') or '',
                               int(state.get('discovery_cursor', 0)))
    rotation = cfg.get('discovery_focus_rotation') or []
    if not rotation:
        state['discovery_last_slice'] = slice_key('', '')
        return '', ''
    # -1 because record_discovery_attempt() has already counted this attempt.
    start = max(0, int(state.get('discovery_cursor', 0)) - 1)
    resting = []
    for step in range(len(rotation)):
        country, industry = _rotation_entry(rotation[(start + step) % len(rotation)])
        until = slice_cooldown_until(state, country, industry)
        if not until or until <= today:
            return _take_slice(state, country, industry, start + step + 1)
        resting.append((until, start + step + 1, country, industry))
    resting.sort()
    _, cursor, country, industry = resting[0]
    return _take_slice(state, country, industry, cursor)


def _take_slice(state, country, industry, next_cursor):
    """Record which slice this attempt used, so its result can be credited to it.

    Mutates the state dict but deliberately does NOT persist it. record_discovery_attempt()
    has already saved just before this, and record_discovery_result() saves just after, so the
    only exposure is a process death mid-attempt costing one cursor advance. An earlier version
    did save here, which quietly turned discovery_focus() into a disk write — and an existing
    test that called it then overwrote the live loop_state.json, resetting the day's call
    counters and the batch sequence.
    """
    state['discovery_cursor'] = next_cursor
    state['discovery_last_slice'] = slice_key(country, industry)
    return country, industry


def record_discovery_attempt(state):
    discovery = state.setdefault('discovery', {'attempts': 0, 'empty_results': 0})
    discovery['attempts'] = int(discovery.get('attempts', 0)) + 1
    # attempts resets at midnight; the cursor must not, or the rotation never advances.
    state['discovery_cursor'] = int(state.get('discovery_cursor', 0)) + 1
    save_state(state)


def record_discovery_result(state, nominated, created=None, cfg=None, today=None):
    """Record whether a discovery attempt produced anything USEFUL.

    Success is new companies, not names returned. Measuring the raw nomination list meant a
    run that proposed 20 already-covered tickers — all silently discarded by dedupe — reset
    empty_results to 0. daily_empty_discovery_limit could therefore never trip, and the corpus
    sat flat for two days (2026-08-11/12) with nothing reporting the stall.

    `created` omitted keeps the old nomination-based behaviour for callers that cannot count.

    The result is also credited to the SLICE this attempt used, which is what lets an exhausted
    slice rest instead of being re-asked on every pass through the rotation.
    """
    discovery = state.setdefault('discovery', {'attempts': 0, 'empty_results': 0})
    produced = len(nominated or []) if created is None else created
    today = today or datetime.date.today().isoformat()
    key = state.get('discovery_last_slice')
    entry = slice_ledger(state).setdefault(key, {}) if key else None
    if produced:
        discovery['empty_results'] = 0
        if entry is not None:
            entry.update(empty_streak=0, cooldown_until='', last_productive=today)
    else:
        discovery['empty_results'] = int(discovery.get('empty_results', 0)) + 1
        if entry is not None:
            streak = int(entry.get('empty_streak', 0)) + 1
            entry['empty_streak'] = streak
            entry['last_empty'] = today
            # Rest a slice only after it has disappointed twice, so one bad provider answer
            # does not retire a productive area. Then back off geometrically: a slice that is
            # genuinely exhausted should cost less and less, without being abandoned forever —
            # new companies do list, and a country reopens.
            strikes = max(1, int((cfg or {}).get('discovery_slice_strikes', 2)))
            if streak >= strikes:
                base = max(1, int((cfg or {}).get('discovery_slice_cooldown_days', 7)))
                cap = max(base, int((cfg or {}).get('discovery_slice_cooldown_cap_days', 56)))
                days = min(cap, base * (2 ** (streak - strikes)))
                entry['cooldown_until'] = (datetime.date.fromisoformat(today)
                                           + datetime.timedelta(days=days)).isoformat()
    save_state(state)
    return produced


def _discovery_reason(stats):
    """Plain-language why an attempt yielded nothing, for the log and the Telegram alert."""
    returned = stats.get('returned')
    if not returned:
        return 'the provider returned an empty nomination list'
    bits = []
    for label, key in (('already covered', 'covered'), ('off-focus', 'off_focus'),
                       ('malformed', 'malformed')):
        if stats.get(key):
            bits.append(f"{stats[key]} {label}")
    detail = ', '.join(bits) or 'all dropped'
    return f'{returned} nominations returned, none usable ({detail})'


def load_maintenance_state():
    """ticker -> {'last_maintained': ISO date}. Absent ticker == never maintained."""
    if os.path.exists(MAINTENANCE_STATE_PATH):
        with open(MAINTENANCE_STATE_PATH, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}


def stamp_maintained(tickers):
    ms = load_maintenance_state()
    today = datetime.date.today().isoformat()
    for t in tickers:
        if t:
            ms[t] = {'last_maintained': today}
    with open(MAINTENANCE_STATE_PATH, 'w', encoding='utf-8') as f:
        json.dump(ms, f, ensure_ascii=False, indent=2)


def migrate_quarantine(workflow):
    """One-time salvage of legacy quota failures into the persistent queues."""
    if workflow.get_meta('quarantine_migrated_at'):
        return
    if not os.path.isdir(QUARANTINE_DIR):
        workflow.set_meta('quarantine_migrated_at', utc_iso())
        return
    latest_provider_limit = {}
    for name in sorted(os.listdir(QUARANTINE_DIR)):
        if not name.endswith('.json'):
            continue
        try:
            with open(os.path.join(QUARANTINE_DIR, name), encoding='utf-8') as f:
                data = json.load(f)
        except (OSError, ValueError):
            continue
        item = data.get('item') or {}
        if not item.get('Company'):
            continue
        problems = ' | '.join(str(p) for p in data.get('problems', []))
        low = problems.lower()
        ticker = extract_ticker(item['Company'])
        existing = workflow.company_by_ticker(ticker) if ticker else None
        quota_like = any(h in low for h in QUOTA_HINTS) or 'limit' in low
        if quota_like:
            provider_name = next((p for p in ('agy_claude', 'gemini', 'claude', 'codex')
                                  if p in low), None)
            if provider_name:
                latest_provider_limit[provider_name] = problems
            # A maintenance item carries current field values.  Recovered new-company
            # failures that already reached the CSV need no further deep-research retry.
            if item.get('values') and existing and existing['research_status'] == DEEP_RESEARCHED:
                with workflow.connect() as db:
                    db.execute("UPDATE companies SET maintenance_status=? WHERE id=?",
                               (MAINT_UPDATE_DUE, existing['id']))
            elif not existing or existing['research_status'] != DEEP_RESEARCHED:
                cid = workflow.enqueue_items([item], source='legacy_quota_retry')[0]
                workflow.requeue_research(cid, 'legacy_quota', problems, count_attempt=False)
        elif problems.startswith('UNLISTED:'):
            if existing and existing['research_status'] == DEEP_RESEARCHED:
                continue
            cid = workflow.upsert_company(item, source='legacy_quarantine')
            workflow.exclude(cid, problems)
        else:
            if existing and existing['research_status'] == DEEP_RESEARCHED:
                continue
            cid = workflow.upsert_company(item, source='legacy_quarantine')
            workflow.send_to_review(cid, 'legacy_validation', problems or 'legacy quarantine')
    for provider_name, detail in latest_provider_limit.items():
        exc = _quota_error(provider_name, detail)
        reset = exc.cooldown_until
        try:
            reset_future = datetime.datetime.fromisoformat(reset) > datetime.datetime.now(datetime.timezone.utc)
        except (TypeError, ValueError):
            reset_future = False
        if reset_future:
            workflow.mark_provider_failure(provider_name, exc.error_class, detail, reset)
    workflow.set_meta('quarantine_migrated_at', utc_iso())


def utc_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')


def recover_incomplete_batches(workflow):
    """Apply checkpointed results and requeue untouched work after an unclean stop."""
    for run, items in workflow.incomplete_batches():
        recovery_errors = []
        requeued_untouched = False
        validated = []
        for item in items:
            if item['status'] == VALIDATED and item.get('result_json'):
                try:
                    validated.append((item, json.loads(item['result_json'])))
                except ValueError:
                    pass
        if validated:
            results = [record for _, record in validated]
            apply_batch.apply_records(results, f"{run['label']} Recovery")
            for item, record in validated:
                cid = item['company_id']
                try:
                    if run['mode'] == 'maintenance':
                        workflow.mark_maintained(cid)
                        stamp_maintained([extract_ticker(record.get('Company', ''))])
                    else:
                        workflow.resolve_deep_research_result(
                            cid, extract_ticker(record.get('Company', '')), record.get('Company'))
                    workflow.checkpoint_item(run['id'], item['position'], 'applied',
                                             provider=item.get('provider'), result=record)
                except Exception as exc:
                    recovery_errors.append((item, str(exc)))
                    if cid:
                        workflow.send_to_review(cid, 'recovery', str(exc))
                    workflow.checkpoint_item(run['id'], item['position'], 'review',
                                             provider=item.get('provider'), result=record,
                                             error_class='recovery', error_text=str(exc))
        for item in items:
            if item['status'] in ('applied', VALIDATED, 'excluded', 'review'):
                continue
            cid = item['company_id']
            if not cid:
                continue
            requeued_untouched = True
            if run['mode'] == 'maintenance':
                workflow.requeue_maintenance(cid, 'interrupted', 'Recovered after process interruption')
            else:
                workflow.requeue_research(cid, 'interrupted',
                                          'Recovered after process interruption', count_attempt=False)
        final_status = 'partial' if recovery_errors or requeued_untouched else 'complete'
        workflow.finish_batch(run['id'], final_status)
        print(f"=== {run['label']} recovery done: applied "
              f"{len(validated) - len(recovery_errors)}, review {len(recovery_errors)} ===")


def refresh_provider_auth(workflow, cfg):
    checks = {
        'gemini': [cfg.get('cli_command', 'agy')],
        # Same binary and OAuth token as gemini, different upstream quota pool.
        'agy_claude': [cfg.get('cli_command', 'agy')],
        'claude': [cfg.get('claude_command', 'claude'), 'auth', 'status', '--json'],
        'codex': [cfg.get('codex_command', 'codex'), 'login', 'status'],
    }
    for provider, cmd in checks.items():
        if not shutil.which(cmd[0]):
            workflow.set_provider_health(provider, 'auth_error', f'{cmd[0]} executable not found')
            continue
        if provider in ('gemini', 'agy_claude'):
            workflow.set_provider_health(provider, 'healthy')
            continue
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=15,
                                    cwd=REPO_DIR, stdin=subprocess.DEVNULL)
            authenticated = result.returncode == 0
            if provider == 'claude' and authenticated:
                try:
                    authenticated = bool(json.loads(result.stdout).get('loggedIn'))
                except (ValueError, AttributeError):
                    authenticated = False
            if authenticated:
                workflow.set_provider_health(provider, 'healthy')
            else:
                detail = (result.stderr or result.stdout or 'authentication check failed').strip()
                workflow.set_provider_health(provider, 'auth_error', detail[:500])
        except Exception as exc:
            workflow.set_provider_health(provider, 'auth_error', str(exc))


def initialize_workflow(cfg=None):
    workflow = get_workflow()
    workflow.sync_csv(CSV_PATH, COLS, is_placeholder_row, is_placeholder_cell,
                      load_maintenance_state())
    migrate_quarantine(workflow)
    recover_incomplete_batches(workflow)
    if cfg:
        refresh_provider_auth(workflow, cfg)
    return workflow


# ---------------- queue ----------------

def scan_csv():
    """One pass over the CSV: repair queue + covered-company list."""
    repair, covered = [], []
    with open(CSV_PATH, 'r', encoding='utf-8-sig') as f:
        reader = csv.reader(f)
        next(reader)
        for row in reader:
            if is_placeholder_row(row):
                continue
            name = row[5].strip()
            covered.append(name)
            if any(is_placeholder_cell(c) for c in row):
                repair.append({'Company': name, 'Country': row[0], 'Industry': row[3]})
    # Stable sort: country priority first, CSV order within a country
    repair.sort(key=lambda it: COUNTRY_PRIORITY.get(it['Country'].strip(), 99))
    return repair, covered


class NominationFailed(RuntimeError):
    """The provider did not return a JSON array of nominations."""


def nominate_new(cfg, key, state, n, covered, provider='gemini',
                 focus_country='', focus_industry='', stats=None, show_tickers=None):
    tickers = sorted(t for t in (extract_ticker(c) for c in covered) if t)
    # Display-only. Dedupe below always runs against the full `tickers` set, so trimming
    # what the model SEES can never let a covered company through.
    listed = list(show_tickers) if show_tickers else tickers
    focus_bits = []
    if focus_country:
        focus_bits.append(f"國家必須是 {focus_country}")
    if focus_industry:
        focus_bits.append(f"產業必須是 {focus_industry}")
    focus_text = ("本批次有嚴格篩選條件：" + "，且".join(focus_bits)
                  + "。不得用不符合條件的公司補足數量。\n") if focus_bits else ''
    # The theme sentence follows the direction plan rather than a fixed tech list: a focused
    # attempt names its industry alone, an open one lists the whole curated vocabulary so the
    # model is not nudged back toward the sectors the corpus already holds most of.
    if focus_industry:
        theme_text = f"本批次主題：{focus_industry} 領域的全球供應鏈關鍵企業"
    else:
        from industry_focus import load_themes
        theme_text = ("延續資料庫主題（" + "、".join(load_themes(cfg))
                      + " 等全球供應鏈關鍵企業）")
    prompt = (
        "你是全球產業鏈投資研究員。請提名接下來最值得深度研究的 "
        f"{n} 家上市公司，{theme_text}，"
        "台灣與國際公司皆可，但不得與已收錄清單重複。"
        "提名時依國家優先順序：Taiwan → USA → Japan → South Korea → China → Israel → "
        "France → Germany → Netherlands → UK → 其他。\n"
        f"{focus_text}"
        "每家公司務必附上正確且真實存在的股票代號（公司名稱與代號必須相符）；"
        "若你對某公司的代號沒有把握，寧可不提名該公司，改提名你有把握的其他公司。\n"
        f"已收錄代號清單：{', '.join(listed)}\n\n"
    )
    # The gemini path enforces the shape at the CLI (NOMINATION_SCHEMA), which requires an
    # object root; the other providers are still asked for a bare array in words.
    schema = NOMINATION_SCHEMA if provider == 'gemini' else None
    element = ('{"Country": "國家英文名", "Company": "公司中文或英文名 (股票代號)", '
               '"Industry": "產業類別"}')
    prompt += (f'將結果放進 nominations 陣列，每個元素格式：{element}' if schema
               else f'僅輸出 JSON 陣列，每個元素格式：{element}')
    text = llm_call(cfg, key, state, prompt, provider, use_search=False, json_schema=schema)
    data = extract_json(text)
    if isinstance(data, dict):
        # Schema-enforced runs arrive wrapped, because a top-level array schema is rejected.
        data = data.get('nominations')
    if not isinstance(data, list):
        # A silent `return []` here made a BROKEN PROVIDER indistinguishable from a
        # SATURATED SLICE — both surfaced as "nominated 0" and neither alerted. Raising
        # lets build_queue exclude this provider and retry with the next one.
        raise NominationFailed(
            f'{provider} returned no JSON array of nominations '
            f'({len(text or "")} chars, starts {(text or "").strip()[:120]!r})')
    covered_tickers = set(tickers)
    items = []
    dropped_covered = dropped_focus = dropped_malformed = 0
    wanted_country = canonical_country(focus_country) if focus_country else ''
    for d in data:
        if not isinstance(d, dict) or not d.get('Company'):
            dropped_malformed += 1
            continue
        if extract_ticker(d['Company']) in covered_tickers:
            dropped_covered += 1
            continue
        country = canonical_country(d.get('Country', ''))
        if wanted_country and country != wanted_country:
            dropped_focus += 1
            continue
        items.append({'Company': d['Company'].strip(),
                      'Country': wanted_country or country,
                      'Industry': focus_industry or d.get('Industry', '').strip()})
    items.sort(key=lambda it: COUNTRY_PRIORITY.get(it['Country'], 99))
    if stats is not None:
        # Why a slice produced nothing is the whole diagnosis; the caller reports it.
        stats.update(returned=len(data), kept=len(items), covered=dropped_covered,
                     off_focus=dropped_focus, malformed=dropped_malformed)
    return items[:n]


def scan_completed():
    """Fully-researched rows carrying their current field values, for the maintenance
    queue. Incomplete rows are skipped — those belong to the repair queue."""
    completed = []
    with open(CSV_PATH, 'r', encoding='utf-8-sig') as f:
        reader = csv.reader(f)
        next(reader)
        for row in reader:
            if is_placeholder_row(row) or len(row) < 21:
                continue
            if any(is_placeholder_cell(c) for c in row):
                continue
            name = row[5].strip()
            ticker = extract_ticker(name)
            if not ticker:
                continue
            completed.append({
                'Company': name, 'ticker': ticker,
                'Country': canonical_country(row[0]), 'Tier': row[4].strip(),
                'values': {COLS[i]: row[i] for i in range(min(len(row), len(COLS)))},
            })
    return completed


def build_maintenance_queue(cfg, state, n):
    """Completed companies ranked by staleness (never-maintained first, then oldest),
    with Tier as the tiebreak so leaders refresh ahead of the long tail."""
    completed = scan_completed()
    ms = load_maintenance_state()
    completed.sort(key=lambda c: (ms.get(c['ticker'], {}).get('last_maintained', ''),
                                  TIER_PRIORITY.get(c['Tier'], 9)))
    return completed[:n], 'maintenance', len(completed)


# ---------------- gemini ----------------

def llm_call(cfg, key, state, prompt, provider='gemini', use_search=True, json_schema=None):
    budget = cfg['daily_call_budgets'].get(provider, 0)
    if state['calls'].get(provider, 0) >= budget:
        exc = BudgetExhausted(f"Daily {provider} call budget reached ({budget}).",
                              provider=provider, error_class='local_budget')
        get_workflow().mark_provider_failure(provider, 'local_budget', str(exc),
                                             _default_cooldown('local_budget'))
        raise exc
    try:
        if provider == 'claude':
            text = claude_call(cfg, state, prompt, use_search=use_search)
        elif provider == 'codex':
            text = codex_call(cfg, state, prompt)
        elif provider == 'agy_claude':
            # Claude served by the Antigravity CLI: Antigravity meters Claude and Gemini as
            # separate pools, so this is a fresh bucket when the Gemini bank is dry, and it
            # never touches Peter's own Claude subscription.
            text = gemini_call_cli(cfg, state, prompt, json_schema=json_schema,
                                   provider='agy_claude',
                                   model=cfg.get('agy_claude_model', DEFAULT_AGY_CLAUDE_MODEL))
        elif cfg.get('backend', 'cli') == 'cli':
            text = gemini_call_cli(cfg, state, prompt, json_schema=json_schema)
        else:
            text = gemini_call_api(cfg, key, state, prompt, use_search)
        get_workflow().mark_provider_success(provider)
        return text
    except BudgetExhausted as exc:
        get_workflow().mark_provider_failure(
            provider, exc.error_class, str(exc), exc.cooldown_until)
        raise


QUOTA_HINTS = ('quota', '429', 'resource_exhausted', 'rate limit', 'usage limit',
               'session limit', 'weekly limit', "you've hit your")


def _reset_from_message(message):
    """Return a UTC ISO reset timestamp from the CLI messages we have observed."""
    text = message or ''
    tz = ZoneInfo('Asia/Taipei')
    now = datetime.datetime.now(tz)
    # Codex: "try again at Jul 25th, 2026 11:24 AM"
    m = re.search(r'(?:resets|try again at)\s+([A-Z][a-z]{2})\s+(\d{1,2})(?:st|nd|rd|th)?,?'
                  r'(?:\s+(\d{4}))?(?:\s+at)?\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)',
                  text, re.I)
    if m:
        month = datetime.datetime.strptime(m.group(1)[:3].title(), '%b').month
        year = int(m.group(3) or now.year)
        hour = int(m.group(4)) % 12 + (12 if m.group(6).lower() == 'pm' else 0)
        value = datetime.datetime(year, month, int(m.group(2)), hour,
                                  int(m.group(5) or 0), tzinfo=tz)
        if value < now and not m.group(3):
            value = value.replace(year=year + 1)
        return value.astimezone(datetime.timezone.utc).isoformat(timespec='seconds')
    # Antigravity: "Resets in 75h59m52s" / "Resets in 4h12m" — a relative duration. Before
    # this branch a 76-hour Gemini outage fell through to the 30-minute rate_limit default and
    # every gemini slot re-discovered it (3 agy calls + 180 s of backoff each time).
    m = re.search(r'resets?\s+in\s+(?:(\d+)h)?\s*(?:(\d+)m)?\s*(?:(\d+)s)?', text, re.I)
    if m and any(m.groups()):
        delta = datetime.timedelta(hours=int(m.group(1) or 0), minutes=int(m.group(2) or 0),
                                   seconds=int(m.group(3) or 0))
        return (datetime.datetime.now(datetime.timezone.utc) + delta).isoformat(timespec='seconds')
    # Claude: "resets 7am" (same day if future, otherwise next day).
    m = re.search(r'resets\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)', text, re.I)
    if m:
        hour = int(m.group(1)) % 12 + (12 if m.group(3).lower() == 'pm' else 0)
        value = now.replace(hour=hour, minute=int(m.group(2) or 0), second=0, microsecond=0)
        if value <= now:
            value += datetime.timedelta(days=1)
        return value.astimezone(datetime.timezone.utc).isoformat(timespec='seconds')
    return None


def _default_cooldown(error_class, message=''):
    explicit = _reset_from_message(message)
    if explicit:
        return explicit
    if error_class == 'local_budget':
        local = datetime.datetime.now(ZoneInfo('Asia/Taipei'))
        reset = (local + datetime.timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0)
        return reset.astimezone(datetime.timezone.utc).isoformat(timespec='seconds')
    hours = {'rate_limit': 0.5, 'session_limit': 2, 'weekly_limit': 24,
             'usage_limit': 24}.get(error_class, 1)
    return (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=hours)) \
        .isoformat(timespec='seconds')


def _quota_error(provider, message):
    low = (message or '').lower()
    if 'weekly limit' in low:
        kind = 'weekly_limit'
    elif 'session limit' in low:
        kind = 'session_limit'
    elif 'usage limit' in low or "you've hit your" in low or 'individual quota reached' in low:
        kind = 'usage_limit'
    else:
        kind = 'rate_limit'
    return BudgetExhausted(f'{provider} {kind.replace("_", " ")} hit: {message[:300]}',
                           provider=provider, error_class=kind,
                           cooldown_until=_default_cooldown(kind, message))


def _without_tools_arg(args):
    """claude_args minus any `--tools <list>` pair, so a no-tools run can set its own."""
    out, skip = [], False
    for arg in args:
        if skip:
            skip = False
        elif arg == '--tools':
            skip = True
        elif not arg.startswith('--tools='):
            out.append(arg)
    return out


def claude_call(cfg, state, prompt, use_search=True):
    # Claude Code headless on Peter's Claude subscription — a separate quota pool
    # from the Antigravity bank, scheduled into the overnight slots.
    args = list(cfg.get('claude_args', []))
    if not use_search:
        # Discovery is a knowledge task (20 names checked against the covered list). With
        # WebSearch on, sonnet verified every nominee online and hit the 600s timeout on
        # every Claude slot from 2026-09-05 — 3 × 600s burned before the fallback provider
        # even ran. With no tools the same prompt answers in seconds.
        args = _without_tools_arg(args) + ['--tools', '']
        prompt = CLI_NO_TOOLS_GUARD + prompt
    cmd = [cfg.get('claude_command', 'claude'), '-p', prompt] + args
    last_err = ''
    for attempt in range(3):
        try:
            res = subprocess.run(cmd, capture_output=True, text=True,
                                 timeout=600, cwd=REPO_DIR)
        except subprocess.TimeoutExpired:
            last_err = 'claude -p timed out after 600s'
            continue
        if res.returncode == 0 and res.stdout.strip():
            state['calls']['claude'] += 1
            save_state(state)
            return res.stdout
        last_err = (res.stderr or res.stdout or 'empty output').strip()[-400:]
        if any(h in last_err.lower() for h in QUOTA_HINTS):
            raise _quota_error('claude', last_err)
        time.sleep(15)
    raise RuntimeError(f"claude -p failed: {last_err}")


def codex_call(cfg, state, prompt):
    # Codex CLI headless on Peter's ChatGPT Pro — third independent quota pool.
    out_file = os.path.join(SCRIPTS_DIR, '.codex_last_msg.txt')
    cmd = [cfg.get('codex_command', 'codex'), 'exec', prompt,
           '--skip-git-repo-check', '--ephemeral', '-s', 'read-only',
           '-c', 'tools.web_search=true', '-o', out_file]
    if cfg.get('codex_model'):
        cmd += ['-m', cfg['codex_model']]
    last_err = ''
    for attempt in range(3):
        if os.path.exists(out_file):
            os.remove(out_file)
        try:
            res = subprocess.run(cmd, capture_output=True, text=True,
                                 timeout=600, cwd=REPO_DIR)
        except subprocess.TimeoutExpired:
            last_err = 'codex exec timed out after 600s'
            continue
        text = ''
        if os.path.exists(out_file):
            with open(out_file, 'r', encoding='utf-8') as f:
                text = f.read().strip()
        if res.returncode == 0 and (text or res.stdout.strip()):
            state['calls']['codex'] += 1
            save_state(state)
            return text or res.stdout
        last_err = (res.stderr or res.stdout or 'empty output').strip()[-400:]
        if any(h in last_err.lower() for h in QUOTA_HINTS):
            raise _quota_error('codex', last_err)
        time.sleep(15)
    raise RuntimeError(f"codex exec failed: {last_err}")


# The nomination call must return a JSON array. Asking politely was not enough: the Antigravity
# CLI runs with `--mode plan`, and in that mode it sometimes writes itself an
# implementation_plan.md and replies with prose about it, or tries to execute a script and dies
# on a denied permission prompt. Each of those wasted a gemini call and fell through to Claude.
#
# `agy --json-schema` removes the failure class instead of pleading against it, but the root
# MUST be an object — a top-level array schema is rejected outright (status ERROR, 0 tokens,
# verified 2026-08-27). So nominations travel wrapped and are unwrapped on arrival.
NOMINATION_SCHEMA = {
    'type': 'object',
    'properties': {
        'nominations': {
            'type': 'array',
            'items': {
                'type': 'object',
                'properties': {'Country': {'type': 'string'},
                               'Company': {'type': 'string'},
                               'Industry': {'type': 'string'}},
                'required': ['Country', 'Company', 'Industry'],
            },
        },
    },
    'required': ['nominations'],
}


DEFAULT_AGY_CLAUDE_MODEL = 'claude-sonnet-4-6'

# The 3.7/3.8 Flash models reach for tools that 3.5 never did. On a discovery prompt they write
# themselves a python one-liner to filter the covered-ticker list (or just "test python
# availability"); headless print mode cannot show the RunCommand confirmation, auto-denies it,
# and the run ends status=SUCCESS with an EMPTY response — so every discovery slot on
# 2026-09-03 fell through to Claude (timing out) and Codex (over quota) and produced nothing.
# Verified across 3.7 and 3.8, plain and --mode plan; plan mode does not prevent the attempt.
# Telling the model in the prompt that it has no tools is what stops it: with this preamble
# 3.8 returned 20 valid nominations on the same prompt. Schema runs get the strict form (no
# tools at all — the answer must come from knowledge); prose runs only forbid terminal
# commands and programs, so web lookups stay available to the research prompts.
CLI_NO_TOOLS_GUARD = (
    "重要：這是無人值守的批次作業，你沒有任何工具可用。不得執行指令、不得撰寫或執行程式、"
    "不得搜尋網頁或開啟瀏覽器，也不得撰寫實作計畫；請只憑你既有的知識，直接輸出結構化結果。"
    "已收錄清單只需憑閱讀比對即可，不需要用程式處理。\n"
)
CLI_NO_COMMANDS_GUARD = (
    "重要：這是無人值守的批次作業。不得執行終端指令、不得撰寫或執行程式、也不得撰寫實作計畫；"
    "沒有人會核准工具權限請求。請直接輸出要求的結果。\n"
)


def gemini_call_cli(cfg, state, prompt, json_schema=None, *, provider='gemini', model=None):
    """One headless Antigravity CLI run. `provider` names the counter/quota pool it bills to
    ('gemini' or 'agy_claude'); `model` overrides cfg['model'] (used for agy_claude)."""
    guard = CLI_NO_TOOLS_GUARD if json_schema is not None else CLI_NO_COMMANDS_GUARD
    cmd = [cfg.get('cli_command', 'agy'), '-p', guard + prompt]
    schema_file = None
    if json_schema is None:
        cmd += cfg.get('cli_extra_args', [])
    else:
        # A JSON-shaped call has no business being in planning mode, so cli_extra_args (which
        # carries --mode plan) is replaced rather than extended. The timeout is kept.
        schema_file = tempfile.NamedTemporaryFile(
            'w', suffix='.json', delete=False, encoding='utf-8')
        json.dump(json_schema, schema_file)
        schema_file.close()
        cmd += ['--json-schema', schema_file.name, '--output-format', 'json']
        cmd += cfg.get('cli_json_args', ['--print-timeout', '6m0s'])
    chosen_model = model or cfg.get('model')
    if chosen_model and chosen_model != 'auto':
        cmd += ['--model', chosen_model]
    env = {**os.environ, 'GEMINI_CLI_TRUST_WORKSPACE': 'true'}  # needed if cli_command is gemini-cli
    try:
        return _gemini_cli_attempts(cmd, env, state, json_schema, provider=provider)
    finally:
        if schema_file is not None:
            with contextlib.suppress(OSError):
                os.unlink(schema_file.name)


def _gemini_cli_attempts(cmd, env, state, json_schema, provider='gemini'):
    last_err = ''
    for attempt in range(3):
        try:
            res = subprocess.run(cmd, capture_output=True, text=True,
                                 timeout=600, cwd=REPO_DIR, env=env)
        except subprocess.TimeoutExpired:
            last_err = 'gemini-cli timed out after 600s'
            continue
        if res.returncode == 0 and res.stdout.strip():
            state['calls'][provider] = state['calls'].get(provider, 0) + 1
            save_state(state)
            if json_schema is None:
                return res.stdout
            envelope = _cli_envelope(res.stdout)
            if envelope is not None:
                return json.dumps(envelope, ensure_ascii=False)
            # A schema run that produced no structured output is a failure, not a text
            # answer — falling back to raw stdout would hand prose to a JSON parser again.
            last_err = f'no usable JSON in schema run: {_cli_schema_error(res.stdout)}'
            time.sleep(5)
            continue
        last_err = (res.stderr or res.stdout or 'empty output').strip()[-400:]
        if any(h in last_err.lower() for h in QUOTA_HINTS):
            if attempt < 2:
                wait = 60 * (attempt + 1)
                print(f"  {provider}-cli quota/rate hit, backing off {wait}s...")
                time.sleep(wait)
                continue
            raise _quota_error(provider, last_err)
        time.sleep(15)
    raise RuntimeError(f"gemini-cli failed: {last_err}")


def _cli_payload(stdout):
    """The whole `--output-format json` envelope as a dict, or None if it is not one."""
    try:
        data = json.loads(stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return None
    return data if isinstance(data, dict) else None


def _cli_envelope(stdout):
    """The schema-validated object from an `--output-format json` run, or None.

    `structured_output` is the validated part and is preferred. But on roughly 5% of runs it
    came back absent while `response` still carried the object — the model answered, the
    envelope just did not promote it. Treating that as a failure wasted a gemini call and fell
    through to another provider, so the response string is salvaged rather than discarded. It
    carries the model's own scratch keys (toolAction, toolSummary) alongside the real ones,
    which is harmless: callers read the key they asked for.
    """
    data = _cli_payload(stdout)
    if data is None or data.get('status') == 'ERROR':
        return None
    structured = data.get('structured_output')
    if isinstance(structured, dict) and structured:
        return structured
    raw = data.get('response')
    if isinstance(raw, str) and raw.strip():
        salvaged = extract_json(raw)
        if isinstance(salvaged, dict) and salvaged:
            return salvaged
    return None


def _cli_schema_error(stdout):
    """A diagnostic worth reading. The old message printed the tail of the envelope, which was
    the echoed schema every time — identical for every failure and useless."""
    data = _cli_payload(stdout)
    if data is None:
        return f'not a JSON envelope: {stdout.strip()[:200]}'
    response = str(data.get('response') or '')
    return (f"status={data.get('status')} structured_output="
            f"{type(data.get('structured_output')).__name__} "
            f"response={len(response)} chars: {response[:200]!r}")


def gemini_call_api(cfg, key, state, prompt, use_search=True):
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{cfg['model']}:generateContent?key={key}")
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.3, "maxOutputTokens": 8192},
    }
    if use_search:
        body["tools"] = [{"google_search": {}}]
    req = urllib.request.Request(
        url, data=json.dumps(body).encode('utf-8'),
        headers={'Content-Type': 'application/json'}, method='POST')
    last_err = None
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                payload = json.load(resp)
            state['calls']['gemini'] += 1
            save_state(state)
            parts = payload.get('candidates', [{}])[0].get('content', {}).get('parts', [])
            return ''.join(p.get('text', '') for p in parts)
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code in (429, 500, 503):
                wait = 30 * (attempt + 1)
                print(f"  HTTP {e.code}, backing off {wait}s...")
                time.sleep(wait)
                continue
            detail = e.read().decode('utf-8', 'replace')[:300]
            raise RuntimeError(f"Gemini HTTP {e.code}: {detail}") from e
        except (urllib.error.URLError, TimeoutError) as e:
            last_err = e
            time.sleep(15 * (attempt + 1))
    if isinstance(last_err, urllib.error.HTTPError) and last_err.code == 429:
        raise _quota_error('gemini', f'HTTP 429: {last_err}')
    raise RuntimeError(f"Gemini call failed after retries: {last_err}")


class BudgetExhausted(Exception):
    def __init__(self, message, provider=None, error_class='usage_limit', cooldown_until=None):
        super().__init__(message)
        self.provider = provider
        self.error_class = error_class
        self.cooldown_until = cooldown_until


def provider_eligible(cfg, state, provider, excluded=()):
    if provider in excluded:
        return False
    ps = get_workflow().provider_state(provider)
    if not ps or not ps['enabled'] or ps['health'] in ('cooldown', 'auth_error'):
        return False
    budget = cfg.get('daily_call_budgets', {}).get(provider, 0)
    return state.get('calls', {}).get(provider, 0) < budget


def select_provider(cfg, state, scheduled, excluded=(), consume_force=False):
    """Schedule-first provider selection with transparent capacity-based fallback."""
    wf = get_workflow()
    if consume_force:
        forced = wf.consume_forced_provider()
        if forced and provider_eligible(cfg, state, forced, excluded):
            return forced
    budget = cfg.get('daily_call_budgets', {})
    threshold = float(cfg.get('provider_low_budget_threshold', 0.10))
    scheduled_budget = budget.get(scheduled, 0)
    scheduled_left = scheduled_budget - state.get('calls', {}).get(scheduled, 0)
    if (provider_eligible(cfg, state, scheduled, excluded)
            and scheduled_budget and scheduled_left / scheduled_budget > threshold):
        return scheduled
    order = cfg.get('provider_fallback_order', ['gemini', 'agy_claude', 'claude', 'codex'])
    candidates = []
    for provider in order:
        if not provider_eligible(cfg, state, provider, excluded):
            continue
        total = max(1, budget.get(provider, 0))
        remaining = max(0, total - state.get('calls', {}).get(provider, 0)) / total
        candidates.append((remaining, wf.recent_success_rate(provider), -order.index(provider), provider))
    return max(candidates)[-1] if candidates else None


def extract_json(text):
    for candidate in (text,
                      *re.findall(r'```(?:json)?\s*(.*?)```', text, re.DOTALL)):
        candidate = candidate.strip()
        try:
            return json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            pass
    # last resort: outermost brace/bracket slice
    for open_ch, close_ch in (('{', '}'), ('[', ']')):
        start, end = text.find(open_ch), text.rfind(close_ch)
        if 0 <= start < end:
            try:
                return json.loads(text[start:end + 1])
            except (json.JSONDecodeError, ValueError):
                pass
    return None


# ---------------- company-name helpers ----------------

def company_name_only(company):
    """Strip a trailing ' (ticker)' so only the company name remains."""
    return re.sub(r'\s*\([^)]+\)\s*$', '', company or '').strip()


def _cjk_core(company):
    """The Traditional-Chinese characters of a name, ignoring English and ticker."""
    return ''.join(CJK_RE.findall(company_name_only(company)))


def same_company(requested, returned):
    """True if two Company strings name the same firm, ignoring ticker and any
    English/Chinese suffix differences. Used to accept a researcher's ticker
    correction (name preserved) while rejecting drift to a different company."""
    req_cjk, got_cjk = _cjk_core(requested), _cjk_core(returned)
    if len(req_cjk) >= 2 and len(got_cjk) >= 2:
        return req_cjk in got_cjk or got_cjk in req_cjk
    a, b = company_name_only(requested).lower(), company_name_only(returned).lower()
    return bool(a and b and (a in b or b in a))


# ---------------- research + validation ----------------

def research_prompt(item, critique=None):
    template = ',\n'.join(f'  "{k}": "..."' for k in RESEARCH_KEYS)
    p = (
        "你是資深全球供應鏈與投資研究員。請以繁體中文對下列公司進行深度投資研究，"
        "風格比照專業投資資料庫：具體、資訊密集、每欄一到三句完整敘述，必須反映 2025-2026 最新事實。"
        "請先使用網路搜尋工具查證最新資料再作答。\n\n"
        f"公司：{item['Company']}\n"
        f"國家：{item.get('Country', '未知')}\n"
        f"產業提示：{item.get('Industry', '')}\n\n"
        "僅輸出一個 JSON 物件（不要任何其他文字），鍵名與順序如下：\n"
        "{\n" + template + "\n}\n\n"
        "規則：\n"
        f"1. Company 必須是「公司名 (股票代號)」格式，代號與上方指定公司一致。\n"
        f"2. Timeframe 只能是：{'、'.join(TIMEFRAMES)}。\n"
        "3. Tier 只能是：龍頭股、潛力股、輔助公司、隱形冠軍。\n"
        "4. Sub-Sector 用資料庫既有板塊詞彙（例：AI伺服器、矽光子、先進封裝、次奈米製程、低軌衛星、"
        "電動車、先進機器人、生技新藥；不適用時可自訂簡短板塊名）。\n"
        "5. 不得輸出「等待」「調查中」「不明」等佔位語；查不到的欄位請給出有依據的估計並註明「估」。\n"
        "6. Capital/Market Cap 給出幣別與數量級（例：約 3.2兆美元）。\n"
        "7. 若查證後發現指定股票代號與公司名稱不符：以「公司名稱」為準，將 Company 欄位改用你"
        "查證到的正確代號，直接輸出更正後的完整 JSON，切勿提出問題或只輸出說明文字。\n"
        "8. 若該公司確實未公開上市、或查無真實股票代號，請「僅」輸出這一個 JSON 物件"
        "（不要任何其他文字）：{\"unlisted\": \"公司名稱，及未上市或無法查證的簡短原因\"}。"
    )
    if critique:
        p += "\n\n上一次輸出未通過驗證，問題如下，請修正後重新輸出完整 JSON：\n- " + "\n- ".join(critique)
    return p


def validate_record(rec, item):
    problems = []
    if not isinstance(rec, dict):
        return ["output is not a JSON object"]
    if 'unlisted' in rec and 'Company' not in rec:
        # Researcher confirmed (via web search) the nominated company is not publicly
        # listed / has no real ticker. Terminal — quarantine cleanly, do not retry.
        return [f"UNLISTED: {str(rec.get('unlisted'))[:200]}"]
    for k in RESEARCH_KEYS:
        v = rec.get(k)
        if not isinstance(v, str) or not v.strip():
            problems.append(f"欄位 {k} 缺漏或為空")
    if problems:
        return problems
    for k in RESEARCH_KEYS:
        if is_placeholder_cell(rec[k]):
            problems.append(f"欄位 {k} 為佔位語而非實際內容")
    want_ticker = extract_ticker(item['Company'])
    got_ticker = extract_ticker(rec['Company'])
    if want_ticker and want_ticker != got_ticker:
        # The researcher (which has web search) corrected the nominated ticker. The
        # nominator runs without search and is the unreliable source, so accept the
        # fix when the company NAME is preserved; reject only a drift to a different
        # company. apply_records upserts by ticker/name, so no duplicate is created.
        if not (got_ticker and same_company(item['Company'], rec['Company'])):
            problems.append(f"Company 代號不符：要求 {want_ticker}，得到 {got_ticker or '無'}")
    if rec['Timeframe'] not in TIMEFRAMES:
        rec['Timeframe'] = '中長期'  # soft-normalize instead of failing
    rec['Country'] = canonical_country(rec.get('Country', ''))  # one canonical label per country
    for k in NARRATIVE_KEYS:
        v = rec[k].strip()
        if len(v) < 10 and not v.startswith('無'):  # 無大型併購。 etc. is a legitimate short answer
            problems.append(f"欄位 {k} 過短（{len(v)} 字）")
        elif not CJK_RE.search(v):
            problems.append(f"欄位 {k} 非繁體中文敘述")
    return problems


def maintenance_prompt(item, critique=None):
    """News-refresh prompt: update ONLY MAINTENANCE_KEYS for an already-researched
    company, based on its current values + the latest ~4-8 weeks of news."""
    cur = item.get('values', {})
    template = ',\n'.join(f'  "{k}": "..."' for k in MAINTENANCE_KEYS)
    current_block = '\n'.join(f"【{k}（現有）】{cur.get(k, '')}" for k in MAINTENANCE_KEYS)
    p = (
        "你是資深投資研究員。下列公司已在資料庫中，現在只需『維護更新』以下少數欄位，"
        "反映最近 4-8 週的最新消息。請先使用網路搜尋工具查證最新事實再作答。\n\n"
        f"公司：{item['Company']}\n"
        f"國家：{item.get('Country', '未知')}\n\n"
        "以下是這些欄位的現有內容，請在其基礎上更新：加入有實質、有時間依據的最新變化；"
        "若近期無重大變化，仍以繁體中文完整重寫一到三句、保留原有重點。\n"
        f"{current_block}\n\n"
        "僅輸出一個 JSON 物件（不要任何其他文字），且『只』包含下列鍵：\n"
        "{\n" + template + "\n}\n\n"
        "規則：\n"
        "1. 每欄一到三句完整繁體中文敘述，具體、資訊密集。\n"
        "2. 不得輸出「等待」「調查中」「不明」等佔位語。\n"
        "3. 不要輸出上列以外的任何欄位（例如 Company、Core Business 等一律不要）。"
    )
    if critique:
        p += "\n\n上一次輸出未通過驗證，問題如下，請修正後重新輸出：\n- " + "\n- ".join(critique)
    return p


def validate_maintenance(rec, item):
    """Validate a maintenance record (only MAINTENANCE_KEYS) and strip any extra columns
    the model may have added, so apply_records can only ever merge the news fields."""
    if not isinstance(rec, dict):
        return ["output is not a JSON object"]
    problems = []
    for k in MAINTENANCE_KEYS:
        v = rec.get(k)
        if not isinstance(v, str) or not v.strip():
            problems.append(f"欄位 {k} 缺漏或為空")
    if problems:
        return problems
    for k in MAINTENANCE_KEYS:
        v = rec[k].strip()
        if is_placeholder_cell(v):
            problems.append(f"欄位 {k} 為佔位語")
        elif len(v) < 10 and not v.startswith('無'):
            problems.append(f"欄位 {k} 過短（{len(v)} 字）")
        elif not CJK_RE.search(v):
            problems.append(f"欄位 {k} 非繁體中文敘述")
    if problems:
        return problems
    # Reduce the record to MAINTENANCE_KEYS only, keyed for apply_records to match the row.
    kept = {k: rec[k] for k in MAINTENANCE_KEYS}
    kept['Company'] = item['Company']
    rec.clear()
    rec.update(kept)
    return []


def maintenance_screen_prompt(items, since_date):
    companies = '\n'.join(
        f"- {item['Company']}｜{item.get('Country', '')}｜{item.get('Industry', '')}"
        for item in items)
    return (
        "你是投資研究維護編輯。請使用網路搜尋，快速檢查下列上市公司自 "
        f"{since_date} 起是否出現會實質改變投資資料庫的消息。只考慮：12M Catalysts、"
        "Key Investment Risks、3-Year M&A、Geopolitical Exposure、M&A Potential。"
        "一般股價波動、重複舊聞、無實質內容的評論都標記為不需更新。\n\n"
        f"{companies}\n\n"
        "只輸出 JSON 陣列，每家公司恰好一筆："
        '[{"Company":"原公司名稱 (代號)","needs_update":true,'
        '"reason":"具體變化摘要或無重大變化","evidence_date":"YYYY-MM-DD或空字串",'
        '"source_urls":["https://example.com/evidence"]}]'
    )


def parse_maintenance_screen(raw, items):
    data = extract_json(raw)
    if not isinstance(data, list):
        raise ValueError('maintenance screen output is not a JSON array')
    expected = {extract_ticker(i['Company']): i for i in items}
    found = {}
    for result in data:
        if not isinstance(result, dict):
            continue
        ticker = extract_ticker(result.get('Company', ''))
        if ticker not in expected or ticker in found:
            continue
        needs = result.get('needs_update')
        if not isinstance(needs, bool):
            continue
        found[ticker] = {
            'needs_update': needs,
            'reason': str(result.get('reason', '')).strip(),
            'evidence_date': str(result.get('evidence_date', '')).strip() or None,
            'source_urls': normalize_source_urls(result.get('source_urls')),
        }
    missing = [t for t in expected if t not in found]
    if missing:
        raise ValueError('maintenance screen missing tickers: ' + ', '.join(missing[:5]))
    return found


def maintenance_week_start(now=None):
    now = now or datetime.datetime.now(ZoneInfo('Asia/Taipei'))
    return (now.date() - datetime.timedelta(days=now.weekday())).isoformat()


def prepare_maintenance_slot(cfg, key, state, scheduled_provider, workflow,
                             allow_forced_provider=True, focus_country='',
                             focus_industry=''):
    """Screen due companies in efficient groups, then return up to 20 flagged updates."""
    group_size = int(cfg.get('maintenance_screen_group_size', 20))
    groups = int(cfg.get('maintenance_screen_groups_per_slot', 4))
    today_slots = workflow.daily_slots(datetime.date.today().isoformat())
    enabled_maintenance = sum(1 for s in today_slots
                              if s['enabled'] and s['mode'] == 'maintenance')
    if not today_slots:
        enabled_maintenance = sum(1 for e in cfg.get('schedule', [])
                                  if len(e) > 2 and e[2] == 'maintenance')
    maintenance_slots = enabled_maintenance * 7
    weekly_capacity = max(group_size * groups, group_size * groups * maintenance_slots)
    cadence = cfg.get('maintenance_cadence_days', {
        'leader': 7, 'second_leader': 7, 'potential': 14,
        'hidden_champion': 14, 'supporting': 28,
        'minor_supplier': 28, 'unknown': 28,
    })
    week = maintenance_week_start()
    # Real local date, not `week`: the cadence delta must advance day by day or nothing
    # can ever come due between Mondays. maintenance_week_start() resolves the same tz.
    workflow.begin_maintenance_cycle(
        week, weekly_capacity, cadence,
        today=datetime.datetime.now(ZoneInfo('Asia/Taipei')).date())
    due = workflow.maintenance_screen_queue(
        group_size * groups, focus_country, focus_industry)
    since = (datetime.date.fromisoformat(week) - datetime.timedelta(days=7)).isoformat()
    for start in range(0, len(due), group_size):
        # Global kill switch: stop before the next group. Unscreened groups simply stay due.
        if halt_requested():
            print('  ⛔ halt requested — maintenance screening stopped; remaining groups stay due.')
            break
        rows = due[start:start + group_size]
        items = [{'Company': r['company_name'], 'Country': r['country'],
                  'Industry': r['industry'], '_company_id': r['id']} for r in rows]
        excluded = set()
        while True:
            provider = select_provider(cfg, state, scheduled_provider, excluded,
                                       consume_force=(allow_forced_provider and start == 0
                                                      and not excluded))
            if not provider:
                print('  maintenance screening paused — no healthy provider')
                break
            try:
                raw = llm_call(cfg, key, state, maintenance_screen_prompt(items, since), provider)
                results = parse_maintenance_screen(raw, items)
                for item in items:
                    ticker = extract_ticker(item['Company'])
                    result = results[ticker]
                    workflow.record_screening(item['_company_id'], week, provider, **result)
                print(f"  screened {len(items)} companies via {provider}")
                break
            except BudgetExhausted as exc:
                excluded.add(provider)
                print(f"  {provider} screening throttled; selecting fallback: {exc}")
            except Exception as exc:
                get_workflow().mark_provider_failure(provider, 'screening_error', str(exc))
                excluded.add(provider)
                print(f"  {provider} screening failed; selecting fallback: {exc}")
    # Over-fetch far past batch_size: the queue orders never-maintained-first, and DB rows
    # whose ticker no longer exists in the CSV ("csv-orphans") can never be maintained, never
    # get last_maintained_at, and therefore permanently sort to the head. Fetching only
    # batch_size rows let ~50 orphans blind whole slots to 73 real due companies
    # (2026-07-27 20:54/22:06 "No matching candidates" skips).
    rows = workflow.maintenance_update_queue(500, focus_country, focus_industry)
    current = {c['ticker']: c for c in scan_completed()}
    items, dropped = select_maintainable(rows, current, cfg.get('batch_size', 20))
    if dropped:
        sample = ', '.join(t for t in dropped[:5])
        print(f"  skipped {len(dropped)} csv-orphan(s) in maintenance queue "
              f"(in DB but absent from CSV): {sample}{'…' if len(dropped) > 5 else ''}")
    all_rows = workflow.maintenance_update_queue(1000000, focus_country, focus_industry)
    remaining = sum(1 for r in all_rows if r['ticker'] in current)
    return items, 'maintenance', remaining


def select_maintainable(rows, current, limit):
    """First `limit` due rows that still exist in the CSV's completed set.

    `current` maps ticker -> CSV row dict (scan_completed()). Rows failing the join are
    returned in `dropped` so the caller can REPORT them — the previous silent drop is what
    made the orphan-clog skips undiagnosable from loop.log.
    """
    items, dropped = [], []
    for row in rows:
        source = current.get(row['ticker'])
        if source is None:
            dropped.append(row['ticker'] or f"id={row['id']}")
            continue
        source = dict(source)
        source['_company_id'] = row['id']
        items.append(source)
        if len(items) >= limit:
            break
    return items, dropped


def research_company(cfg, key, state, item, provider='gemini', mode='research'):
    critique = None
    raw = ''
    prompt_fn = maintenance_prompt if mode == 'maintenance' else research_prompt
    validate_fn = validate_maintenance if mode == 'maintenance' else validate_record
    for _ in range(cfg['max_retries'] + 1):
        raw = llm_call(cfg, key, state, prompt_fn(item, critique), provider)
        rec = extract_json(raw)
        problems = validate_fn(rec, item)
        if not problems:
            if mode != 'maintenance':
                want, got = extract_ticker(item['Company']), extract_ticker(rec['Company'])
                if want and got and want != got:
                    print(f"    ticker corrected: {want} -> {got} ({rec['Company']})")
            return rec, [], raw
        if problems and problems[0].startswith('UNLISTED:'):
            return None, problems, raw  # terminal: no valid ticker exists, don't retry
        critique = problems
        time.sleep(cfg['delay_seconds'])
    return None, critique or ["no JSON extracted"], raw


def quarantine(item, problems, raw):
    ts = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
    safe = re.sub(r'[^A-Za-z0-9._-]', '_', extract_ticker(item['Company']) or item['Company'])[:40]
    path = os.path.join(QUARANTINE_DIR, f"{ts}_{safe}.json")
    with open(path, 'w', encoding='utf-8') as f:
        json.dump({'item': item, 'problems': problems, 'raw': raw}, f, ensure_ascii=False, indent=2)
    return path


# ---------------- batch loop ----------------

def run_batch(cfg, key, state, items, source, provider='gemini', mode='research', workflow=None,
              slot_id=None):
    state['batch_seq'] += 1
    seq = state['batch_seq']
    save_state(state)
    model = {'gemini': cfg['model'],
             'agy_claude': f"{cfg.get('agy_claude_model', DEFAULT_AGY_CLAUDE_MODEL)} (via agy)",
             'claude': 'Claude (headless)',
             'codex': 'Codex (headless)'}[provider]
    label = f"{'Maintenance' if mode == 'maintenance' else 'API'} Batch {seq} ({source}, {provider})"
    print(f"\n=== {label}: {len(items)} companies ===")
    batch_id = (workflow.create_batch(seq, label, mode, source, provider, items, slot_id)
                if workflow else None)
    # No per-batch Telegram message any more (2026-09-08, Peter): the day's batches are
    # folded into one digest by research_digest / maybe_send_daily_digest. The log keeps
    # the per-batch record.

    records, failed, requeued, excluded_items = [], [], [], []
    providers_used = []
    provider_blocked = False
    halted = False
    for position, item in enumerate(items):
        # Global kill switch: abort at the company boundary. Companies already researched
        # (< position) keep their checkpoints; this one and the rest are requeued untouched.
        if halt_requested():
            halted = True
            for rest_pos in range(position, len(items)):
                rest = items[rest_pos]
                rest_cid = rest.get('_company_id')
                if workflow and rest_cid:
                    if mode == 'maintenance':
                        workflow.requeue_maintenance(rest_cid, 'halted',
                                                     'Research operation halted via kill switch')
                    else:
                        workflow.requeue_research(rest_cid, 'halted',
                                                  'Research operation halted via kill switch',
                                                  count_attempt=False)
                    workflow.checkpoint_item(batch_id, rest_pos, 'requeued',
                                             error_class='halted',
                                             error_text='Research operation halted via kill switch')
                requeued.append(rest['Company'])
            print(f"  ⛔ halt requested — requeued {len(items) - position} untouched companies")
            break
        print(f"  researching {item['Company']} ...")
        cid = item.get('_company_id')
        if workflow and cid:
            if mode == 'maintenance':
                workflow.mark_maintenance_updating(cid)
            else:
                workflow.mark_researching(cid)
        tried = set()
        rec, problems, raw, used_provider = None, ['no healthy provider'], '', None
        while True:
            chosen = select_provider(cfg, state, provider, tried,
                                     consume_force=(slot_id is None and position == 0 and not tried)
                                     ) if workflow else provider
            if not chosen:
                provider_blocked = True
                break
            used_provider = chosen
            if chosen not in providers_used:
                providers_used.append(chosen)
            try:
                # One content attempt per batch. Genuine validation failures are
                # requeued with backoff and use a different provider next time.
                one_try_cfg = dict(cfg)
                one_try_cfg['max_retries'] = 0
                rec, problems, raw = research_company(
                    one_try_cfg, key, state, item, chosen, mode)
                break
            except BudgetExhausted as exc:
                tried.add(chosen)
                print(f"    {chosen} throttled — switching provider ({exc})")
                continue
            except Exception as exc:
                get_workflow().mark_provider_failure(chosen, 'transient', str(exc))
                tried.add(chosen)
                problems, raw = [str(exc)], ''
                print(f"    {chosen} failed — switching provider ({exc})")
                continue
        # A model response may parse structurally but omit its identity field. Never let that
        # bypass the workflow checkpoint and reach apply_batch as an anonymous record: treat it
        # like any other validation failure so the known queue item is requeued with backoff.
        if rec and not str(rec.get('Company') or '').strip():
            rec = None
            problems = ['missing Company field in model result']
        if rec:
            records.append((position, item, rec, used_provider, raw))
            if workflow:
                workflow.checkpoint_item(batch_id, position, VALIDATED, used_provider, rec, raw)
            print(f"    ok ({used_provider})")
        else:
            detail = '; '.join(problems or ['unknown failure'])
            terminal_unlisted = detail.startswith('UNLISTED:')
            ticker_problem = ('代號' in detail or '欄位 Company' in detail
                              or 'ticker' in detail.lower())
            if workflow and cid:
                if provider_blocked:
                    if mode == 'maintenance':
                        workflow.requeue_maintenance(cid, 'provider_unavailable', detail)
                    else:
                        workflow.requeue_research(cid, 'provider_unavailable', detail,
                                                  count_attempt=False)
                    workflow.checkpoint_item(batch_id, position, 'requeued', used_provider,
                                             error_class='provider_unavailable', error_text=detail)
                    requeued.append(item['Company'])
                elif terminal_unlisted:
                    workflow.exclude(cid, detail)
                    workflow.checkpoint_item(batch_id, position, 'excluded', used_provider,
                                             raw=raw, error_class='unlisted', error_text=detail)
                    excluded_items.append(item['Company'])
                elif ticker_problem:
                    workflow.send_to_review(cid, 'ticker', detail)
                    workflow.checkpoint_item(batch_id, position, 'review', used_provider,
                                             raw=raw, error_class='ticker', error_text=detail)
                    failed.append(item['Company'])
                elif mode == 'maintenance':
                    workflow.requeue_maintenance(cid, 'validation', detail, delay_hours=1)
                    workflow.checkpoint_item(batch_id, position, 'requeued', used_provider,
                                             raw=raw, error_class='validation', error_text=detail)
                    requeued.append(item['Company'])
                else:
                    row = workflow.company_by_ticker(extract_ticker(item['Company']))
                    attempts = (row['attempt_count'] if row else 0) + 1
                    if attempts >= 3:
                        workflow.send_to_review(cid, 'validation', detail, attempts)
                        workflow.checkpoint_item(batch_id, position, 'review', used_provider,
                                                 raw=raw, error_class='validation', error_text=detail)
                        failed.append(item['Company'])
                    else:
                        delay = (1, 6, 24)[min(attempts - 1, 2)]
                        workflow.requeue_research(cid, 'validation', detail,
                                                  count_attempt=True, delay_hours=delay)
                        workflow.checkpoint_item(batch_id, position, 'requeued', used_provider,
                                                 raw=raw, error_class='validation', error_text=detail)
                        requeued.append(item['Company'])
            else:
                failed.append(item['Company'])
                qp = quarantine(item, problems, raw)
                print(f"    FAILED -> {os.path.basename(qp)} ({detail[:120]})")
            if workflow:
                print(f"    {('REVIEW' if item['Company'] in failed else 'REQUEUED')} ({detail[:120]})")
        if provider_blocked:
            # No provider can continue this slot. Persist every untouched item.
            for rest_pos in range(position + 1, len(items)):
                rest = items[rest_pos]
                rest_cid = rest.get('_company_id')
                if workflow and rest_cid:
                    if mode == 'maintenance':
                        workflow.requeue_maintenance(rest_cid, 'provider_unavailable',
                                                     'No healthy provider remained in this slot')
                    else:
                        workflow.requeue_research(rest_cid, 'provider_unavailable',
                                                  'No healthy provider remained in this slot',
                                                  count_attempt=False)
                    workflow.checkpoint_item(batch_id, rest_pos, 'requeued',
                                             error_class='provider_unavailable',
                                             error_text='No healthy provider remained in this slot')
                requeued.append(rest['Company'])
            break
        time.sleep(cfg['delay_seconds'])

    summary = None
    if records:
        applied_records = [entry[2] for entry in records]
        summary = apply_batch.apply_records(applied_records, label)
        if mode == 'maintenance':
            stamp_maintained([extract_ticker(r.get('Company', '')) for r in applied_records])
        if workflow:
            for position, item, record, used_provider, raw in records:
                cid = item.get('_company_id')
                try:
                    if mode == 'maintenance':
                        workflow.mark_maintained(cid)
                    else:
                        resolution = workflow.resolve_deep_research_result(
                            cid, extract_ticker(record.get('Company', '')), record.get('Company'))
                        if resolution['merged']:
                            print(f"    merged duplicate: {resolution['detail']}")
                    workflow.checkpoint_item(batch_id, position, 'applied', used_provider, record, raw)
                except Exception as e:
                    print(f"    ERROR checkpointing {item.get('Company', cid)}: {e}")
                    failed.append(item.get('Company', cid))
                    workflow.checkpoint_item(batch_id, position, 'review', used_provider,
                                             raw=raw, error_class='checkpoint_crash', error_text=str(e))

    batch_path = os.path.join(
        BATCHES_DIR, f"batch_api_{seq:03d}_{datetime.date.today().isoformat()}.json")
    with open(batch_path, 'w', encoding='utf-8') as f:
        json.dump({'label': label, 'source': source, 'provider': provider,
                   'providers_used': providers_used,
                   'items': [{k: v for k, v in item.items() if not k.startswith('_')}
                             for item in items],
                   'requested': [item['Company'] for item in items if item.get('_requested')],
                   'records': [entry[2] for entry in records], 'failed': failed,
                   'requeued': requeued, 'excluded': excluded_items,
                   'summary': ({k: v for k, v in summary.items() if k != 'progress'}
                               if summary else {})},
                  f, ensure_ascii=False, indent=2)
    if workflow:
        workflow.finish_batch(batch_id, 'complete' if not (failed or requeued) else 'partial')

    # A halt is still worth an immediate message; a completed batch is not — it goes into the
    # daily digest (2026-09-08).
    if halted:
        send_telegram(f"⛔ *{label} HALTED* (kill switch) — applied {len(records)} of "
                      f"{len(items)} before stopping.")
    print(f"=== {label} done: applied {len(records)}, review {len(failed)}, "
          f"requeued {len(requeued)}, excluded {len(excluded_items)} ===")
    return len(records)


def _workflow_item(row):
    return {'Company': row['company_name'], 'Country': row['country'],
            'Industry': row['industry'], 'Tier': row['tier'],
            '_company_id': row['id'],
            '_requested': (row['source'] if 'source' in row.keys() else '') == 'manual'}


def build_queue(cfg, key, state, batch_size, provider='gemini', mode='research', workflow=None,
                focus_country='', focus_industry=''):
    if mode == 'maintenance':
        if workflow:
            rows = workflow.maintenance_update_queue(
                batch_size, focus_country, focus_industry)
            current = {c['ticker']: c for c in scan_completed()}
            items = []
            for row in rows:
                item = current.get(row['ticker'])
                if item:
                    item = dict(item)
                    item['_company_id'] = row['id']
                    items.append(item)
            return items, 'maintenance', len(workflow.maintenance_update_queue(
                1000000, focus_country, focus_industry))
        return build_maintenance_queue(cfg, state, batch_size)
    repair, covered = scan_csv()
    if not workflow and repair:
        return repair[:batch_size], 'repair', len(repair)
    if not workflow:
        items = nominate_new(cfg, key, state, batch_size, covered, provider,
                             focus_country, focus_industry)
        return items, 'new', len(items)

    workflow.enqueue_items(repair, source='repair')
    retry_cap = cfg.get('retry_slots_per_batch', 10)
    rows = workflow.research_queue(batch_size, retry_cap, focus_country, focus_industry)
    # Top up rather than wait for empty (2026-09-09): a short queue — a hand-picked company,
    # a couple of retries — used to swallow the whole slot, one item researched and no hunt.
    # The hunt now runs whenever the batch has room, still within the daily attempt budget.
    already = len(rows)
    if already < batch_size and discovery_budget_available(cfg, state):
        chosen = select_provider(cfg, state, provider)
        if chosen:
            record_discovery_attempt(state)
            # Rotate the slice each attempt. Coverage is heavily skewed (Taiwan 538, USA 424,
            # Japan 182 …), so an undirected nomination keeps re-proposing the same saturated
            # names; steering at an under-covered slice is what makes a new attempt productive.
            plan = workflow.direction_status()
            if not isinstance(plan, list):
                plan = None
            _warn_if_direction_mined_out(cfg, state, plan)
            slice_country, slice_industry = discovery_focus(cfg, state,
                                                            focus_country, focus_industry,
                                                            plan=plan)
            where = '/'.join(x for x in (slice_country, slice_industry) if x) or 'global'
            # Show only the slice's own tickers: a 30k-character global list is noise once
            # the attempt is steered at one country or industry.
            listed = (workflow.covered_tickers(slice_country, slice_industry)
                      if (slice_country or slice_industry) else None)
            # Fall back across providers exactly as maintenance screening does. Discovery
            # used to give up on the first failure, so one provider returning prose instead
            # of JSON silently cost the whole slot — and looked identical to a saturated
            # slice. mark_provider_failure alone does NOT reroute: it sets health to
            # 'degraded', which provider_eligible still accepts, so the scheduled provider
            # would just be picked again. The exclusion set is what actually moves us on.
            stats, excluded, nominated = {}, set(), None
            while chosen:
                try:
                    nominated = nominate_new(cfg, key, state, batch_size, covered, chosen,
                                             slice_country, slice_industry, stats=stats,
                                             show_tickers=listed)
                    break
                except BudgetExhausted as exc:
                    print(f"  {chosen} discovery throttled; selecting fallback: {exc}")
                except NominationFailed as exc:
                    workflow.mark_provider_failure(chosen, 'nomination_error', str(exc))
                    print(f"  {chosen} discovery returned no usable JSON; "
                          f"selecting fallback: {exc}")
                except Exception as exc:
                    workflow.mark_provider_failure(chosen, 'nomination_error', str(exc))
                    print(f"  {chosen} discovery failed; selecting fallback: {exc}")
                excluded.add(chosen)
                chosen = select_provider(cfg, state, provider, excluded)

            if nominated is None:
                # A fault, not a saturated slice: deliberately does NOT count against
                # daily_empty_discovery_limit, which exists to stop pointless spend on a
                # slice that has nothing left, not to punish a broken provider.
                print(f"  ⚠️  discovery [{where}] produced nothing — every provider failed")
                send_telegram(f"*Discovery failed* — no provider could nominate companies "
                              f"for {where}. Corpus growth is stalled until this clears.")
            else:
                workflow.enqueue_items(nominated, source='new')
                # Count AFTER enqueue: duplicates are dropped there, so this is the only
                # honest measure of whether the attempt produced work.
                rows = workflow.research_queue(batch_size, retry_cap,
                                               focus_country, focus_industry)
                produced = record_discovery_result(state, nominated,
                                                   created=max(0, len(rows) - already),
                                                   cfg=cfg)
                print(f"  discovery [{where}]: nominated {len(nominated or [])}, "
                      f"{produced} new to research")
                # Gated on `produced` alone. Gating on `nominated` too meant the commonest
                # dead end — the provider returns 20 names and every one is already covered,
                # so nominated == 0 — printed nothing and alerted nobody. This warning had
                # fired zero times in the entire log while slots were being skipped.
                if not produced:
                    resting = slice_cooldown_until(state, slice_country, slice_industry)
                    note = (f' — resting until {resting}' if resting else '')
                    print(f"  ⚠️  discovery unproductive [{where}] — "
                          f"{_discovery_reason(stats)}{note}")
                    if state.get('discovery', {}).get('empty_results') == 1:
                        send_telegram(
                            f"*Discovery unproductive* — {chosen} found nothing new for "
                            f"{where}: {_discovery_reason(stats)}. "
                            f"New-company growth has stalled.")
        rows = workflow.research_queue(batch_size, retry_cap, focus_country, focus_industry)
    items = [_workflow_item(row) for row in rows]
    sources = {row['source'] for row in rows}
    source = sources.pop() if len(sources) == 1 else ('new' if not sources else 'mixed')
    return items, source, len(workflow.research_queue(
        1000000, retry_cap, focus_country, focus_industry))


def maybe_send_daily_digest(cfg, state, now=None, sender=None):
    """Send yesterday's digest once local time passes cfg['daily_digest_time'] (HH:MM,
    default 00:05; empty disables). Called from the idle path only, so a batch running
    across midnight finishes first and lands in the day its file is stamped with.

    First run: the marker is missing, so it is set to the day that is already due WITHOUT
    sending — otherwise every restart would replay an old day. Returns the day sent, or None.
    """
    at = str(cfg.get('daily_digest_time', '00:05') or '').strip()
    if not at:
        return None
    now = now or datetime.datetime.now()
    passed = now.strftime('%H:%M') >= at
    due = (now.date() - datetime.timedelta(days=1 if passed else 2)).isoformat()
    sent = state.get('daily_digest_sent')
    if not isinstance(sent, str):
        state['daily_digest_sent'] = due
        save_state(state)
        return None
    if sent >= due:
        return None
    from research_digest import send_digest
    pieces = send_digest(due, sender or send_telegram)
    state['daily_digest_sent'] = due
    save_state(state)
    print(f"📋 daily digest for {due} sent ({len(pieces)} message(s))")
    return due


def _warn_if_direction_mined_out(cfg, state, plan):
    """Once a day, say when every listed industry is resting: the list needs retyping.

    Without this the fallback to the config rotation is silent, and the log would read like a
    plan that was never set. The loop keeps working either way.
    """
    if not plan or not any(r.get('enabled', 1) for r in plan):
        return
    from industry_focus import pick_direction
    today = datetime.date.today().isoformat()
    if pick_direction(plan, slice_ledger(state), today) is not None:
        return
    if state.get('direction_mined_out_notified') == today:
        return
    state['direction_mined_out_notified'] = today
    save_state(state)
    print('  ⚠️  direction list mined out — every listed industry is resting; '
          'falling back to the config rotation until the list is retyped')
    send_telegram('*Direction list mined out* — every industry on the list came back empty '
                  'twice and is resting. Discovery is on the config rotation until you type a '
                  'new list (dashboard, or /focus in the bot).')


def effective_focus(workflow, slot):
    """(country, industry, origin) this slot should run with.

    A focus written onto the slot itself always wins — that is the per-slot override the
    dashboard has always offered. Otherwise the standing focus applies. It is read HERE, at
    claim time, rather than baked into slots by seed_daily_slots: that is what lets a focus set
    tonight apply to tomorrow's slots, and lets clearing it take effect on the very next slot
    instead of the next day.
    """
    country = (slot.get('country_focus') or '').strip()
    industry = (slot.get('industry_focus') or '').strip()
    if country or industry:
        return country, industry, 'slot'
    standing = workflow.standing_focus() if workflow else None
    if standing:
        return ((standing.get('country') or '').strip(),
                (standing.get('industry') or '').strip(), 'standing')
    return '', '', 'none'


def parse_schedule(cfg):
    sched = []
    for entry in cfg['schedule']:
        hhmm, provider = entry[0], entry[1]
        mode = entry[2] if len(entry) > 2 else 'research'
        h, m = map(int, hhmm.split(':'))
        sched.append((h * 60 + m, provider, mode))
    return sorted(sched)


def next_slot(sched):
    now = datetime.datetime.now()
    minutes_now = now.hour * 60 + now.minute
    for mins, provider, mode in sched:
        if mins > minutes_now:
            return now.replace(hour=mins // 60, minute=mins % 60,
                               second=0, microsecond=0), provider, mode
    mins, provider, mode = sched[0]
    return ((now + datetime.timedelta(days=1))
            .replace(hour=mins // 60, minute=mins % 60, second=0, microsecond=0), provider, mode)


def main():
    ap = argparse.ArgumentParser(description="Autonomous Gemini research loop")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument('--dry-run', nargs='?', const=2, type=int, metavar='N',
                      help='research N companies (default 2), print, write nothing')
    mode.add_argument('--once', action='store_true', help='run one batch')
    mode.add_argument('--run', action='store_true', help='run until queue/budget exhausted')
    ap.add_argument('--batch-size', type=int, help='override config batch_size')
    ap.add_argument('--provider', choices=['gemini', 'agy_claude', 'claude', 'codex'], default='gemini',
                    help='provider for --dry-run/--once (the schedule decides during --run)')
    ap.add_argument('--mode', choices=['research', 'maintenance'], default='research',
                    help='research = grow/repair; maintenance = news-refresh completed rows')
    args = ap.parse_args()

    if args.dry_run is None:      # --once / --run actually write to the DB
        _acquire_singleton_lock()

    cfg = load_config()
    if args.batch_size:
        cfg['batch_size'] = args.batch_size
    key = load_api_key(cfg)
    state = load_state()
    os.makedirs(BATCHES_DIR, exist_ok=True)
    os.makedirs(QUARANTINE_DIR, exist_ok=True)
    workflow = None if args.dry_run is not None else initialize_workflow(cfg)

    if args.dry_run is not None:
        items, source, remaining = build_queue(cfg, key, state, args.dry_run,
                                               args.provider, args.mode)
        print(f"Queue source: {source} ({remaining} pending). "
              f"Dry-running {len(items)} via {args.provider} ({args.mode}):")
        for item in items:
            rec, problems, raw = research_company(cfg, key, state, item, args.provider, args.mode)
            print(f"\n--- {item['Company']} ---")
            if rec:
                print(json.dumps(rec, ensure_ascii=False, indent=2))
            else:
                print(f"VALIDATION FAILED: {problems}\nraw (first 500): {raw[:500]}")
        print(f"\nCalls used today: {state['calls']} / budgets {cfg['daily_call_budgets']}. "
              "Nothing written.")
        return

    if args.once:
        try:
            if args.mode == 'maintenance':
                items, source, remaining = prepare_maintenance_slot(
                    cfg, key, state, args.provider, workflow)
            else:
                items, source, remaining = build_queue(
                    cfg, key, state, cfg['batch_size'], args.provider, args.mode, workflow)
            if items:
                print(f"Queue: {remaining} pending ({source}).")
                run_batch(cfg, key, state, items, source, args.provider, args.mode, workflow)
            else:
                print("Queue empty — nothing to research.")
        except BudgetExhausted as e:
            print(str(e))
            send_telegram(f"*Research loop stopped* — {e}")
        return

    # --run: the workflow database's daily slots are the conductor.  The config
    # schedule only seeds a new date; dashboard edits then apply without a restart.
    throttle_notified = set()
    announced_next = None
    halt_notified = False
    config_broken_since = None
    while True:
        # Reload so config edits apply without a restart — but never let a bad or briefly
        # unreadable file take the daemon down or, worse, downgrade it to unsafe defaults.
        # Both happened on 2026-08-13: a PermissionError on the config killed the loop, and the
        # replacement file that "fixed" it silently dropped the tool boundary and the schedule.
        # Keep running on the last-good config instead, and say so once.
        try:
            cfg = load_config()
            config_broken_since = None
        except (ConfigError, OSError) as exc:
            if 'config_reload_failed' not in throttle_notified:
                throttle_notified.add('config_reload_failed')
                print(f"⚠️  config reload failed — continuing on the last good config: {exc}")
                send_telegram(f"*Config reload failed* — the research loop is continuing on its "
                              f"last good config. Fix required: {str(exc)[:300]}")
            config_broken_since = config_broken_since or datetime.datetime.now()
        now = datetime.datetime.now()
        # Global kill switch: when halted, the daemon stays alive but claims no slots. An
        # in-flight batch already aborts itself at the next company boundary (see run_batch).
        if halt_requested():
            if not halt_notified:
                print("⛔ Research operation HALTED via kill switch — claiming no slots until resumed.")
                send_telegram("⛔ *Research operation HALTED* — kill switch engaged; no new slots "
                              "will run until resumed from the dashboard.")
                halt_notified = True
                announced_next = None
            time.sleep(15)
            continue
        if halt_notified:
            print("▶ Research operation RESUMED — kill switch cleared.")
            send_telegram("▶ *Research operation RESUMED* — kill switch cleared; slots will run again.")
            halt_notified = False
        day = now.date().isoformat()
        workflow.seed_daily_slots(day, cfg.get('schedule', []))
        slot = workflow.claim_due_slot(now)
        if not slot:
            try:
                maybe_send_daily_digest(cfg, load_state(), now)
            except Exception as exc:
                print(f"  ⚠️  daily digest failed: {exc}")
            upcoming = workflow.next_daily_slot(day, now.strftime('%H:%M'))
            marker = ((upcoming['slot_time'], upcoming['provider'], upcoming['mode'])
                      if upcoming else None)
            if marker and marker != announced_next:
                target = now.replace(hour=int(upcoming['slot_time'][:2]),
                                     minute=int(upcoming['slot_time'][3:]),
                                     second=0, microsecond=0)
                wait = max(0, (target - now).total_seconds())
                print(f"Next slot {upcoming['slot_time']} ({upcoming['provider']}, "
                      f"{upcoming['mode']}); sleeping {wait/60:.0f} min.")
                announced_next = marker
            time.sleep(15)
            continue
        announced_next = None
        provider, mode = slot['provider'], slot['mode']
        focus_country, focus_industry, focus_origin = effective_focus(workflow, slot)
        if focus_origin == 'standing':
            print(f"Standing focus applies: "
                  f"{'/'.join(x for x in (focus_country, focus_industry) if x)}")
        state = load_state()  # rolls date + per-provider counters at midnight
        try:
            if mode == 'maintenance':
                items, source, remaining = prepare_maintenance_slot(
                    cfg, key, state, provider, workflow, allow_forced_provider=False,
                    focus_country=focus_country, focus_industry=focus_industry)
            else:
                items, source, remaining = build_queue(
                    cfg, key, state, cfg['batch_size'], provider, mode, workflow,
                    focus_country, focus_industry)
            if not items:
                focus = '/'.join(x for x in (focus_country, focus_industry) if x)
                detail = f"No matching candidates{f' for {focus}' if focus else ''}"
                # A focus that matches nothing reads identically to a drained queue, both in
                # loop.log and in daily_slots.detail — which is exactly the confusion that hid
                # the exact-match industry filter for so long (a slot focused on 國防 matched 0
                # of 103 held companies and skipped in silence). Name the difference instead.
                if focus and mode != 'maintenance':
                    unfocused = len(workflow.research_queue(1000000))
                    if unfocused:
                        detail = (f"Focus {focus} matched none of the {unfocused} pending "
                                  f"companies — the queue is not empty, the focus is too narrow")

                # A focus that matches nothing must not idle the slot. 2026-09-03..08 a fintech
                # focus with no batch budget skipped research slots ("matched none of the 2
                # pending") and every maintenance slot ("No matching candidates") while 2,193
                # companies sat overdue for their routine refresh. Run the slot unfocused
                # instead and say so; the focus is still consumed after run_batch, so a
                # budgeted focus that never matches cannot linger forever either.
                if focus:
                    print(f"  focus {focus} matched nothing for this slot — running it unfocused.")
                    focus_country = focus_industry = ''
                    if mode == 'maintenance':
                        items, source, remaining = prepare_maintenance_slot(
                            cfg, key, state, provider, workflow, allow_forced_provider=False)
                    else:
                        items, source, remaining = build_queue(
                            cfg, key, state, cfg['batch_size'], provider, mode, workflow)
                    if not items:
                        detail += ' — the unfocused fallback found nothing either'

                if not items and mode != 'maintenance' and not workflow.research_queue(1):
                    print(f"Queue empty ({mode}), auto-falling back to maintenance mode.")
                    # Notify once per day, not once per slot. With 18 research slots against a
                    # drained queue this fired ~16 times a day, which buried real alerts and
                    # made a working fallback look like a fault. loop.log still records each.
                    # The key is date-stamped because throttle_notified is created once
                    # outside the while loop and never cleared — an undated marker would
                    # silence this permanently rather than daily.
                    fallback_key = f'auto_fallback:{datetime.date.today().isoformat()}'
                    if fallback_key not in throttle_notified:
                        throttle_notified.add(fallback_key)
                        send_telegram(
                            "*Deep research queue empty* — research slots are pivoting to "
                            "maintenance for now. Further pivots today will not be repeated here.")
                    mode = 'maintenance'
                    items, source, remaining = prepare_maintenance_slot(
                        cfg, key, state, provider, workflow, allow_forced_provider=False,
                        focus_country=focus_country, focus_industry=focus_industry)

                if not items:
                    workflow.finish_daily_slot(slot['id'], 'skipped', detail)
                    print(f"Queue empty ({mode}) — {detail}.")
                    # This is BOTH queues dry — the slot produces nothing at all. The old
                    # guard tested `mode`, which the fallback above had already reassigned to
                    # 'maintenance', so it could never fire on the path that actually needs
                    # it: 26 consecutive slots were skipped in silence on 2026-08-25/26.
                    idle_key = f'loop_idle:{datetime.date.today().isoformat()}'
                    if idle_key not in throttle_notified:
                        throttle_notified.add(idle_key)
                        send_telegram(
                            f"*Research loop idle* — {slot['slot_time']} produced nothing: "
                            f"{detail}. "
                            f"Further idle slots today will not be repeated here.")
                    continue
            print(f"Queue: {remaining} pending ({source}).")
            run_batch(cfg, key, state, items, source, provider, mode, workflow,
                      slot_id=slot['id'])
            # Only a batch that actually ran burns a budgeted focus; skips and failures below
            # never reach here, so "next 3 batches" means three real batches.
            if focus_origin == 'standing':
                left = workflow.consume_standing_focus()
                if left is None:
                    print('Standing focus finished its batch budget and was cleared.')
            throttle_notified.discard(provider)
        except BudgetExhausted as e:
            workflow.finish_daily_slot(slot['id'], 'skipped', str(e))
            print(f"Slot skipped: {e}")
            if provider not in throttle_notified:
                send_telegram(f"*{provider} slot skipped* — {e} "
                              f"Its next scheduled slot will retry; other provider unaffected.")
                throttle_notified.add(provider)
        except Exception as exc:
            workflow.finish_daily_slot(slot['id'], 'skipped', str(exc))
            print(f"Slot skipped: {exc}")
            send_telegram(f"*Scheduled slot failed* — {slot['slot_time']} {mode}: {exc}")
        finally:
            # A batch never creates a late catch-up queue. Any times it crossed are
            # explicitly missed, while future times remain eligible.
            workflow.miss_due_slots(datetime.datetime.now())
        time.sleep(1)


if __name__ == '__main__':
    main()
