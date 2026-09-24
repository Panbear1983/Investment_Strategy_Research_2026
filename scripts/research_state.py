"""Persistent workflow state for the investment research scheduler.

The CSV remains the source of researched content.  This SQLite database stores
only orchestration state: known-company lifecycle, resumable batch manifests,
provider availability, maintenance screening, and review history.
"""
from __future__ import annotations

import csv
import datetime as dt
import hashlib
import json
import os
import re
import sqlite3
from contextlib import contextmanager
from urllib.parse import urlparse


PROVIDERS = ('gemini', 'agy_claude', 'claude', 'codex')
RESEARCH_PENDING = 'pending_deep_research'
RESEARCHING = 'researching'
VALIDATED = 'validated'
DEEP_RESEARCHED = 'deep_researched'
TICKER_REVIEW = 'needs_ticker_review'
EXCLUDED = 'excluded'

MAINT_NOT_DUE = 'not_due'
MAINT_SCREEN_DUE = 'screen_due'
MAINT_UPDATE_DUE = 'update_due'
MAINT_UPDATING = 'updating'
MAINT_RETRY = 'retry_wait'
MAX_SCREENING_SOURCE_URLS = 5
MAX_SCREENING_SOURCE_URL_LENGTH = 2048


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')


def parse_iso(value):
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed
    except (TypeError, ValueError):
        return None


def canonical_tier(raw: str) -> str:
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


def normalize_source_urls(source_urls):
    """Return a bounded, ordered list of unique HTTPS evidence URLs."""
    if source_urls is None:
        return []
    if not isinstance(source_urls, list) or len(source_urls) > MAX_SCREENING_SOURCE_URLS:
        raise ValueError('source URLs must be a list of at most five HTTPS URLs')
    normalized = []
    for value in source_urls:
        if not isinstance(value, str):
            raise ValueError('source URL must be a string')
        url = value.strip()
        parsed = urlparse(url)
        if (len(url) > MAX_SCREENING_SOURCE_URL_LENGTH or parsed.scheme != 'https'
                or not parsed.hostname):
            raise ValueError('source URL must be an HTTPS URL')
        if url not in normalized:
            normalized.append(url)
    return normalized


def focus_filters(country=None, industry=None):
    """SQL fragment and args for an optional country/industry focus, shared by every queue.

    Country stays EXACT after normalization: canonical_country and COUNTRY_ALIASES keep one
    label per country, so equality is right and a substring would over-match.

    Industry is a SUBSTRING match, deliberately. The column holds ~1,690 distinct free-text
    labels for ~2,400 companies: '半導體' and '半導體業' are two labels for one industry (screen.py
    self-tests that very pair), and hundreds of rows carry a whole descriptive phrase rather than
    a bare category. Equality meant a focus on 國防 matched 0 rows while the corpus holds 103, and
    半導體 matched 59 of 503 — so a focused slot reported "No matching candidates" and skipped,
    looking identical to a drained queue. screen.select() has always treated industry as a
    substring; this is what makes the work queues agree with the reader and the MCP tools.

    INSTR, not LIKE: a focus term containing % or _ would silently become a wildcard under LIKE.
    """
    clauses, args = [], []
    if country:
        clauses.append('LOWER(TRIM(country))=LOWER(?)')
        args.append(country.strip())
    if industry:
        clauses.append('INSTR(LOWER(industry), LOWER(?))>0')
        args.append(industry.strip())
    return ''.join(f' AND {c}' for c in clauses), args


class WorkflowState:
    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.init_schema()

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        conn.execute('PRAGMA busy_timeout=10000')
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def init_schema(self):
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS companies (
                    id INTEGER PRIMARY KEY,
                    ticker TEXT,
                    company_name TEXT NOT NULL,
                    country TEXT NOT NULL DEFAULT '',
                    industry TEXT NOT NULL DEFAULT '',
                    tier TEXT NOT NULL DEFAULT '',
                    research_status TEXT NOT NULL DEFAULT 'pending_deep_research',
                    maintenance_status TEXT NOT NULL DEFAULT 'not_due',
                    source TEXT NOT NULL DEFAULT 'new',
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at TEXT,
                    last_error_class TEXT,
                    last_error TEXT,
                    deep_researched_at TEXT,
                    last_screened_at TEXT,
                    last_maintained_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS companies_ticker_uq
                    ON companies(ticker) WHERE ticker IS NOT NULL AND ticker != '';
                CREATE INDEX IF NOT EXISTS companies_research_queue
                    ON companies(research_status, next_attempt_at, attempt_count, created_at);
                CREATE INDEX IF NOT EXISTS companies_maintenance_queue
                    ON companies(maintenance_status, last_screened_at, last_maintained_at);

                CREATE TABLE IF NOT EXISTS batch_runs (
                    id INTEGER PRIMARY KEY,
                    seq INTEGER NOT NULL UNIQUE,
                    label TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    source TEXT NOT NULL,
                    scheduled_provider TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'running',
                    created_at TEXT NOT NULL,
                    completed_at TEXT
                );
                CREATE TABLE IF NOT EXISTS daily_slots (
                    id INTEGER PRIMARY KEY,
                    slot_date TEXT NOT NULL,
                    slot_time TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    mode TEXT NOT NULL DEFAULT 'research',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    country_focus TEXT NOT NULL DEFAULT '',
                    industry_focus TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'pending',
                    batch_id INTEGER REFERENCES batch_runs(id),
                    detail TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(slot_date, slot_time)
                );
                CREATE INDEX IF NOT EXISTS daily_slots_due
                    ON daily_slots(slot_date, status, enabled, slot_time);
                CREATE TABLE IF NOT EXISTS batch_items (
                    id INTEGER PRIMARY KEY,
                    batch_id INTEGER NOT NULL REFERENCES batch_runs(id) ON DELETE CASCADE,
                    company_id INTEGER REFERENCES companies(id),
                    position INTEGER NOT NULL,
                    item_json TEXT NOT NULL,
                    provider TEXT,
                    status TEXT NOT NULL DEFAULT 'pending',
                    result_json TEXT,
                    raw_output TEXT,
                    error_class TEXT,
                    error_text TEXT,
                    updated_at TEXT NOT NULL,
                    UNIQUE(batch_id, position)
                );
                CREATE INDEX IF NOT EXISTS batch_items_status ON batch_items(status);

                CREATE TABLE IF NOT EXISTS provider_state (
                    provider TEXT PRIMARY KEY,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    health TEXT NOT NULL DEFAULT 'unknown',
                    cooldown_until TEXT,
                    last_error TEXT,
                    successes INTEGER NOT NULL DEFAULT 0,
                    failures INTEGER NOT NULL DEFAULT 0,
                    force_next INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS provider_events (
                    id INTEGER PRIMARY KEY,
                    provider TEXT NOT NULL,
                    success INTEGER NOT NULL,
                    error_class TEXT,
                    detail TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS provider_events_recent
                    ON provider_events(provider, id DESC);

                CREATE TABLE IF NOT EXISTS maintenance_screenings (
                    id INTEGER PRIMARY KEY,
                    company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
                    week_start TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    needs_update INTEGER NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    evidence_date TEXT,
                    source_urls_json TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL,
                    UNIQUE(company_id, week_start)
                );
                CREATE TABLE IF NOT EXISTS workflow_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                -- Canonical store for research CONTENT. Until 2026-08-10 the 22 research
                -- fields lived only in the CSV, which is a fixed-capacity sheet: a new company
                -- could enter only by claiming a 'Global_Entity' placeholder row, and once
                -- those ran out every newly researched company was silently discarded
                -- (342 lost over two weeks). This table has no capacity concept.
                -- record_json holds the whole 22-field record rather than 22 quoted columns
                -- ('Capital/Market Cap', 'M&A Potential'…); filtering stays in Python, where a
                -- full scan of the corpus costs ~10 ms.
                CREATE TABLE IF NOT EXISTS company_research (
                    company_id      INTEGER PRIMARY KEY
                                    REFERENCES companies(id) ON DELETE CASCADE,
                    ticker          TEXT,
                    company_name    TEXT NOT NULL,
                    record_json     TEXT NOT NULL,
                    complete        INTEGER NOT NULL DEFAULT 0,
                    source          TEXT NOT NULL DEFAULT 'apply',
                    source_batch_id INTEGER,
                    updated_at      TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS company_research_ticker
                    ON company_research(ticker);
                CREATE TABLE IF NOT EXISTS maintenance_digest_deliveries (
                    digest_date TEXT NOT NULL,
                    recipient_chat_id TEXT NOT NULL,
                    recipient_label TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending', 'claimed', 'sent', 'failed', 'skipped_no_updates')),
                    payload_json TEXT,
                    payload_sha256 TEXT,
                    telegram_message_id TEXT,
                    error_class TEXT,
                    error_text TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(digest_date, recipient_chat_id)
                );
                -- What 爸菲特 is allowed to do about research direction: suggest, never set.
                -- botffet's synthesis call reads untrusted text (corpus prose, and now YouTube
                -- transcripts written by strangers), so a sentence inside that text must never
                -- be able to aim the scraper. A proposal lands here and changes nothing until a
                -- human accepts it in the dashboard; the accept is what writes workflow_meta.
                CREATE TABLE IF NOT EXISTS focus_proposals (
                    id INTEGER PRIMARY KEY,
                    industry TEXT NOT NULL DEFAULT '',
                    country TEXT NOT NULL DEFAULT '',
                    reason TEXT NOT NULL DEFAULT '',
                    proposed_by TEXT NOT NULL DEFAULT 'botffet',
                    conversation TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending', 'accepted', 'dismissed')),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS focus_proposals_pending
                    ON focus_proposals(status, id DESC);

                -- The direction plan: which industries (optionally within one country) the
                -- discovery step hunts in, and how much of the effort each gets. The loop
                -- rotates through enabled rows by share; a row that stops producing rests on
                -- its own (research_loop.record_discovery_result). It steers DISCOVERY ONLY:
                -- maintenance and the retry queue are never filtered by it, which is the
                -- lesson of the 2026-09 fintech focus that starved both for five days.
                CREATE TABLE IF NOT EXISTS direction_plan (
                    id INTEGER PRIMARY KEY,
                    industry TEXT NOT NULL DEFAULT '',
                    country TEXT NOT NULL DEFAULT '',
                    share INTEGER NOT NULL DEFAULT 1 CHECK(share >= 1),
                    enabled INTEGER NOT NULL DEFAULT 1,
                    position INTEGER NOT NULL DEFAULT 0,
                    set_by TEXT NOT NULL DEFAULT 'peter',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(industry, country)
                );

                -- YouTube links turned into reading material. Deliberately inert: nothing here
                -- steers the scraper. It exists so a sector decision can come out of a
                -- CONVERSATION about what was said, with the transcript quotable underneath.
                CREATE TABLE IF NOT EXISTS video_sources (
                    id INTEGER PRIMARY KEY,
                    video_id TEXT NOT NULL UNIQUE,   -- the 11-char YouTube id; the dedupe key
                    url TEXT NOT NULL,
                    title TEXT NOT NULL DEFAULT '',
                    channel TEXT NOT NULL DEFAULT '',
                    published_at TEXT NOT NULL DEFAULT '',
                    duration_seconds INTEGER NOT NULL DEFAULT 0,
                    lang TEXT NOT NULL DEFAULT '',
                    transcript TEXT NOT NULL DEFAULT '',
                    -- [[char_offset, start_seconds], ...] so a verbatim quote can be traced
                    -- back to the moment it was said, and linked with &t=.
                    offsets_json TEXT NOT NULL DEFAULT '[]',
                    char_count INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending', 'fetched', 'summarised', 'failed')),
                    error_class TEXT,
                    error_text TEXT,
                    added_by TEXT NOT NULL DEFAULT 'local',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS video_sources_status
                    ON video_sources(status, id DESC);

                CREATE TABLE IF NOT EXISTS video_notes (
                    id INTEGER PRIMARY KEY,
                    source_id INTEGER NOT NULL
                        REFERENCES video_sources(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL
                        CHECK(kind IN ('summary', 'company', 'sector')),
                    company_name TEXT NOT NULL DEFAULT '',
                    ticker TEXT NOT NULL DEFAULT '',
                    sector TEXT NOT NULL DEFAULT '',
                    note TEXT NOT NULL DEFAULT '',
                    -- Verbatim span from the transcript. A note whose quote is not literally
                    -- present is dropped before it ever gets here, so every claim is checkable.
                    quote TEXT NOT NULL DEFAULT '',
                    start_seconds INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS video_notes_ticker ON video_notes(ticker);
                CREATE INDEX IF NOT EXISTS video_notes_sector ON video_notes(sector);
                CREATE INDEX IF NOT EXISTS video_notes_source ON video_notes(source_id, kind);
            """)
            video_columns = {row['name'] for row in db.execute(
                'PRAGMA table_info(video_sources)').fetchall()}
            if video_columns and 'offsets_json' not in video_columns:
                db.execute("ALTER TABLE video_sources "
                           "ADD COLUMN offsets_json TEXT NOT NULL DEFAULT '[]'")
            columns = {row['name'] for row in db.execute(
                'PRAGMA table_info(maintenance_screenings)').fetchall()}
            if 'source_urls_json' not in columns:
                db.execute("""ALTER TABLE maintenance_screenings
                    ADD COLUMN source_urls_json TEXT NOT NULL DEFAULT '[]'""")
            digest_columns = {row['name'] for row in db.execute(
                'PRAGMA table_info(maintenance_digest_deliveries)').fetchall()}
            if 'payload_json' not in digest_columns:
                db.executescript("""
                    ALTER TABLE maintenance_digest_deliveries
                        RENAME TO maintenance_digest_deliveries_legacy;
                    CREATE TABLE maintenance_digest_deliveries (
                        digest_date TEXT NOT NULL,
                        recipient_chat_id TEXT NOT NULL,
                        recipient_label TEXT NOT NULL,
                        status TEXT NOT NULL DEFAULT 'pending'
                            CHECK(status IN ('pending', 'claimed', 'sent', 'failed', 'skipped_no_updates')),
                        payload_json TEXT,
                        payload_sha256 TEXT,
                        telegram_message_id TEXT,
                        error_class TEXT,
                        error_text TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        PRIMARY KEY(digest_date, recipient_chat_id)
                    );
                    INSERT INTO maintenance_digest_deliveries
                        (digest_date, recipient_chat_id, recipient_label, status, created_at, updated_at)
                    SELECT digest_date, recipient_chat_id, recipient_label, status, created_at, updated_at
                    FROM maintenance_digest_deliveries_legacy;
                    DROP TABLE maintenance_digest_deliveries_legacy;
                """)
            now = utc_now()
            for provider in PROVIDERS:
                db.execute("""INSERT OR IGNORE INTO provider_state
                    (provider, updated_at) VALUES (?, ?)""", (provider, now))

    # ---- metadata / migration -------------------------------------------------

    def get_meta(self, key, default=None):
        with self.connect() as db:
            row = db.execute('SELECT value FROM workflow_meta WHERE key=?', (key,)).fetchone()
            return row['value'] if row else default

    def set_meta(self, key, value):
        with self.connect() as db:
            db.execute("""INSERT INTO workflow_meta(key, value) VALUES(?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value""", (key, str(value)))

    # ---- standing sector focus -------------------------------------------------
    # Per-slot focus lives in daily_slots and is reseeded every midnight, so it cannot express
    # "keep hunting satellites until I say otherwise". This is that standing instruction. It is
    # read at slot-claim time rather than baked into slots at seed time, so a focus set at 23:50
    # applies to tomorrow's slots too, and clearing it takes effect on the very next slot.
    # workflow_meta already exists for exactly this kind of single-row state — no new table.

    FOCUS_KEY = 'standing_focus'

    DEFAULT_FOCUS_BATCHES = 3

    def set_standing_focus(self, industry='', country='', batches=None,
                           set_by='peter', reason=''):
        """Set the standing focus for a bounded number of batches.

        batches=None means DEFAULT_FOCUS_BATCHES. A focus no longer runs "until cleared":
        the 2026-09-03 fintech focus was set that way and, once the sector was picked clean,
        starved discovery, the retry queue and maintenance for five days. Open-ended steering
        belongs in the direction plan, which rotates and rests on its own.
        """
        industry = (industry or '').strip()
        country = (country or '').strip()
        if not industry and not country:
            raise ValueError('a standing focus needs an industry, a country, or both')
        batches = int(self.DEFAULT_FOCUS_BATCHES if batches is None else batches)
        if batches < 1:
            raise ValueError('batches must be at least 1')
        record = {'industry': industry, 'country': country,
                  'batches_remaining': batches, 'set_by': set_by,
                  'reason': (reason or '').strip()[:500], 'set_at': utc_now()}
        self.set_meta(self.FOCUS_KEY, json.dumps(record, ensure_ascii=False))
        return record

    def standing_focus(self):
        """The active standing focus, or None. Never raises on a corrupt value."""
        raw = self.get_meta(self.FOCUS_KEY)
        if not raw:
            return None
        try:
            record = json.loads(raw)
        except (ValueError, TypeError):
            return None
        if not isinstance(record, dict):
            return None
        if not (record.get('industry') or record.get('country')):
            return None
        return record

    def clear_standing_focus(self):
        cleared = self.standing_focus()
        self.set_meta(self.FOCUS_KEY, '')
        return cleared

    def consume_standing_focus(self):
        """Count one batch against a count-scoped focus; clear it when it runs out.

        Called only after a batch actually ran with the focus applied, so a skipped or failed
        slot never burns a budgeted batch. An until-cleared focus is untouched.
        """
        record = self.standing_focus()
        if not record or record.get('batches_remaining') is None:
            return record
        remaining = int(record['batches_remaining']) - 1
        if remaining < 1:
            self.set_meta(self.FOCUS_KEY, '')
            return None
        record['batches_remaining'] = remaining
        self.set_meta(self.FOCUS_KEY, json.dumps(record, ensure_ascii=False))
        return record

    # ---- video sources (conversation material, not instructions) ----------------

    def add_video(self, video_id, url, added_by='local'):
        """Register a link. Re-adding one already known is a no-op that returns its row id."""
        video_id = (video_id or '').strip()
        if not video_id:
            raise ValueError('a video id is required')
        now = utc_now()
        with self.connect() as db:
            row = db.execute('SELECT id FROM video_sources WHERE video_id=?',
                             (video_id,)).fetchone()
            if row:
                return row['id'], False
            cur = db.execute("""INSERT INTO video_sources
                (video_id, url, added_by, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)""", (video_id, url, added_by, now, now))
            return cur.lastrowid, True

    def store_transcript(self, source_id, transcript, lang='', offsets=None, **meta):
        allowed = ('title', 'channel', 'published_at', 'duration_seconds')
        sets = ['transcript=?', 'char_count=?', 'lang=?', 'offsets_json=?', "status='fetched'",
                'error_class=NULL', 'error_text=NULL', 'updated_at=?']
        args = [transcript, len(transcript or ''), lang,
                json.dumps(offsets or [], separators=(',', ':')), utc_now()]
        for field in allowed:
            if meta.get(field) not in (None, ''):
                sets.insert(-1, f'{field}=?')
                args.insert(-1, meta[field])
        args.append(source_id)
        with self.connect() as db:
            db.execute(f"UPDATE video_sources SET {', '.join(sets)} WHERE id=?", args)

    def fail_video(self, source_id, error_class, error_text):
        with self.connect() as db:
            db.execute("""UPDATE video_sources SET status='failed', error_class=?,
                error_text=?, updated_at=? WHERE id=?""",
                (error_class, str(error_text)[:1000], utc_now(), source_id))

    def video(self, source_id=None, video_id=None):
        sql = 'SELECT * FROM video_sources WHERE ' + ('id=?' if source_id else 'video_id=?')
        with self.connect() as db:
            row = db.execute(sql, (source_id or video_id,)).fetchone()
        return dict(row) if row else None

    def videos(self, status=None, limit=50):
        sql = 'SELECT * FROM video_sources'
        args = []
        if status:
            sql += ' WHERE status=?'
            args.append(status)
        sql += ' ORDER BY id DESC LIMIT ?'
        args.append(limit)
        with self.connect() as db:
            return [dict(r) for r in db.execute(sql, args)]

    def replace_video_notes(self, source_id, notes):
        """Write a video's notes, replacing any earlier pass. Re-summarising is idempotent."""
        now = utc_now()
        with self.connect() as db:
            db.execute('DELETE FROM video_notes WHERE source_id=?', (source_id,))
            for note in notes:
                db.execute("""INSERT INTO video_notes
                    (source_id, kind, company_name, ticker, sector, note, quote,
                     start_seconds, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (source_id, note.get('kind', 'summary'),
                     note.get('company_name', ''), note.get('ticker', ''),
                     note.get('sector', ''), note.get('note', ''), note.get('quote', ''),
                     int(note.get('start_seconds') or 0), now))
            db.execute("""UPDATE video_sources SET status='summarised', updated_at=?
                WHERE id=?""", (now, source_id))

    def video_notes(self, source_id=None, kind=None, ticker=None, sector=None,
                    text=None, limit=30):
        clauses, args = [], []
        if source_id:
            clauses.append('n.source_id=?'); args.append(source_id)
        if kind:
            clauses.append('n.kind=?'); args.append(kind)
        if ticker:
            clauses.append('UPPER(n.ticker)=UPPER(?)'); args.append(ticker.strip())
        if sector:
            clauses.append('INSTR(LOWER(n.sector), LOWER(?))>0'); args.append(sector.strip())
        if text:
            clauses.append("""(INSTR(LOWER(n.note), LOWER(?))>0
                               OR INSTR(LOWER(n.quote), LOWER(?))>0
                               OR INSTR(LOWER(n.company_name), LOWER(?))>0)""")
            args += [text.strip()] * 3
        where = (' WHERE ' + ' AND '.join(clauses)) if clauses else ''
        args.append(limit)
        with self.connect() as db:
            rows = db.execute(f"""SELECT n.*, v.video_id, v.title, v.channel, v.published_at,
                v.url FROM video_notes n JOIN video_sources v ON v.id=n.source_id{where}
                ORDER BY n.id DESC LIMIT ?""", args).fetchall()
        return [dict(r) for r in rows]

    def video_summary(self):
        with self.connect() as db:
            rows = db.execute("""SELECT status, COUNT(*) n FROM video_sources
                GROUP BY status""").fetchall()
            notes = db.execute('SELECT COUNT(*) n FROM video_notes').fetchone()['n']
        counts = {r['status']: r['n'] for r in rows}
        counts['notes'] = notes
        counts['total'] = sum(v for k, v in counts.items() if k != 'notes')
        return counts

    # ---- focus proposals (advisory; a human accepts them) ----------------------

    def propose_focus(self, industry='', country='', reason='', proposed_by='botffet',
                      conversation=''):
        """Record a suggestion. Deliberately does not touch the standing focus."""
        industry = (industry or '').strip()
        country = (country or '').strip()
        if not industry and not country:
            raise ValueError('a proposal needs an industry, a country, or both')
        now = utc_now()
        with self.connect() as db:
            # One pending proposal per target: a chat that keeps recommending satellites should
            # refresh its reason, not fill the tray with duplicates to dismiss one by one.
            existing = db.execute("""SELECT id FROM focus_proposals
                WHERE status='pending' AND industry=? AND country=?""",
                (industry, country)).fetchone()
            if existing:
                db.execute("""UPDATE focus_proposals SET reason=?, proposed_by=?,
                    conversation=?, updated_at=? WHERE id=?""",
                    (reason[:1000], proposed_by, conversation, now, existing['id']))
                return existing['id']
            cur = db.execute("""INSERT INTO focus_proposals
                (industry, country, reason, proposed_by, conversation, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (industry, country, reason[:1000], proposed_by, conversation, now, now))
            return cur.lastrowid

    def focus_proposals(self, status='pending', limit=20):
        with self.connect() as db:
            rows = db.execute("""SELECT * FROM focus_proposals WHERE status=?
                ORDER BY id DESC LIMIT ?""", (status, limit)).fetchall()
        return [dict(r) for r in rows]

    def resolve_focus_proposal(self, proposal_id, status):
        if status not in ('accepted', 'dismissed'):
            raise ValueError("status must be 'accepted' or 'dismissed'")
        with self.connect() as db:
            row = db.execute('SELECT * FROM focus_proposals WHERE id=?',
                             (proposal_id,)).fetchone()
            if not row:
                return None
            db.execute('UPDATE focus_proposals SET status=?, updated_at=? WHERE id=?',
                       (status, utc_now(), proposal_id))
            return dict(row)

    def accept_focus_proposal(self, proposal_id, share=1):
        """Accept a proposal AND add it to the direction plan, in that order.

        This is the only place a proposal becomes an instruction, and it is only ever reached
        from a human keypress in the dashboard. It used to set the standing focus; since
        2026-09-08 an accepted sector joins the rotation instead of becoming a hard filter
        over every slot.
        """
        row = self.resolve_focus_proposal(proposal_id, 'accepted')
        if not row:
            return None
        return self.add_direction(row['industry'], row['country'], share,
                                  set_by=f"accepted from {row['proposed_by']}")

    # ---- direction plan -------------------------------------------------------
    # The rotating list of industries discovery hunts in. Unlike the standing focus this is
    # not a filter: it only decides what one nomination prompt asks for, one row per attempt,
    # in proportion to `share`. An empty plan means the config's discovery_focus_rotation.

    def direction_plan(self, enabled_only=False):
        sql = 'SELECT * FROM direction_plan'
        if enabled_only:
            sql += ' WHERE enabled=1'
        sql += ' ORDER BY position, id'
        with self.connect() as db:
            return [dict(r) for r in db.execute(sql).fetchall()]

    @staticmethod
    def _direction_row(db, plan_id):
        row = db.execute('SELECT * FROM direction_plan WHERE id=?', (plan_id,)).fetchone()
        return dict(row) if row else None

    def add_direction(self, industry='', country='', share=1, set_by='peter'):
        """Add a row, or re-enable an identical one. Both fields empty is allowed: that is
        a free-choice row where the model picks the industry itself."""
        industry = (industry or '').strip()
        country = (country or '').strip()
        share = max(1, int(share or 1))
        now = utc_now()
        with self.connect() as db:
            row = db.execute('SELECT * FROM direction_plan WHERE industry=? AND country=?',
                             (industry, country)).fetchone()
            if row:
                db.execute('UPDATE direction_plan SET enabled=1, updated_at=? WHERE id=?',
                           (now, row['id']))
                return self._direction_row(db, row['id'])
            position = db.execute('SELECT COALESCE(MAX(position), -1) + 1 p '
                                  'FROM direction_plan').fetchone()['p']
            cur = db.execute("""INSERT INTO direction_plan
                (industry, country, share, enabled, position, set_by, created_at, updated_at)
                VALUES (?, ?, ?, 1, ?, ?, ?, ?)""",
                (industry, country, share, position, set_by, now, now))
            return self._direction_row(db, cur.lastrowid)

    def remove_direction(self, plan_id):
        with self.connect() as db:
            row = self._direction_row(db, plan_id)
            if row:
                db.execute('DELETE FROM direction_plan WHERE id=?', (plan_id,))
            return row

    def set_direction_share(self, plan_id, share):
        share = max(1, int(share))
        with self.connect() as db:
            db.execute('UPDATE direction_plan SET share=?, updated_at=? WHERE id=?',
                       (share, utc_now(), plan_id))
            return self._direction_row(db, plan_id)

    def set_direction_enabled(self, plan_id, enabled):
        with self.connect() as db:
            db.execute('UPDATE direction_plan SET enabled=?, updated_at=? WHERE id=?',
                       (1 if enabled else 0, utc_now(), plan_id))
            return self._direction_row(db, plan_id)

    def move_direction(self, plan_id, delta):
        """Swap places with the neighbour `delta` rows away (-1 = up, +1 = down)."""
        rows = self.direction_plan()
        index = next((i for i, r in enumerate(rows) if r['id'] == plan_id), None)
        if index is None:
            return None
        target = index + int(delta)
        if target < 0 or target >= len(rows):
            return rows[index]
        rows[index], rows[target] = rows[target], rows[index]
        now = utc_now()
        with self.connect() as db:
            for position, row in enumerate(rows):
                db.execute('UPDATE direction_plan SET position=?, updated_at=? WHERE id=?',
                           (position, now, row['id']))
            return self._direction_row(db, plan_id)

    HISTORY_KEY = 'direction_history'

    def set_direction_list(self, entries, set_by='peter'):
        """Replace the plan with `entries`: (industry, country) pairs in priority order.

        This is what the typing box does. Rows already present keep their id and position
        history; rows not in the new list are deleted; positions follow the typed order, which
        is the tie-break when two rows hold the same number of companies. Returns the plan.
        """
        wanted, seen = [], set()
        for industry, country in entries:
            key = ((industry or '').strip(), (country or '').strip())
            if key in seen:
                continue
            seen.add(key)
            wanted.append(key)
        now = utc_now()
        with self.connect() as db:
            existing = {(r['industry'], r['country']): dict(r)
                        for r in db.execute('SELECT * FROM direction_plan').fetchall()}
            for key, row in existing.items():
                if key not in seen:
                    db.execute('DELETE FROM direction_plan WHERE id=?', (row['id'],))
            for position, (industry, country) in enumerate(wanted):
                row = existing.get((industry, country))
                if row:
                    db.execute("""UPDATE direction_plan SET position=?, enabled=1, updated_at=?
                        WHERE id=?""", (position, now, row['id']))
                else:
                    db.execute("""INSERT INTO direction_plan
                        (industry, country, share, enabled, position, set_by, created_at,
                         updated_at) VALUES (?, ?, 1, 1, ?, ?, ?, ?)""",
                        (industry, country, position, set_by, now, now))
        if wanted:
            self._remember_direction(wanted, now)
        return self.direction_plan()

    def clear_direction_list(self):
        rows = self.direction_plan()
        with self.connect() as db:
            db.execute('DELETE FROM direction_plan')
        return rows

    def _remember_direction(self, wanted, when, keep=8):
        history = self.direction_history()
        entry = {'set_at': when, 'items': [{'industry': i, 'country': c} for i, c in wanted]}
        if history and history[0].get('items') == entry['items']:
            return
        self.set_meta(self.HISTORY_KEY, json.dumps([entry] + history[:keep - 1],
                                                   ensure_ascii=False))

    def direction_history(self):
        """Previous lists, newest first — so the panel can show what was already mined."""
        raw = self.get_meta(self.HISTORY_KEY)
        try:
            history = json.loads(raw) if raw else []
        except (ValueError, TypeError):
            return []
        return history if isinstance(history, list) else []

    def direction_status(self):
        """Every plan row with `held` — researched companies matching it — which is what the
        need-first picker orders by. One query per row; the plan is a handful of rows."""
        rows = self.direction_plan()
        with self.connect() as db:
            for row in rows:
                clause, args = focus_filters(row['country'] or None, row['industry'] or None)
                row['held'] = db.execute(
                    f'SELECT COUNT(*) n FROM companies WHERE research_status=?{clause}',
                    (DEEP_RESEARCHED, *args)).fetchone()['n']
        return rows

    def seed_direction_plan(self, entries, set_by='seed'):
        """Fill an EMPTY plan from (industry, country, share) tuples. A plan with any rows,
        even disabled ones, is left alone so a seed can never overwrite Peter's edits."""
        if self.direction_plan():
            return 0
        for industry, country, share in entries:
            self.add_direction(industry, country, share, set_by=set_by)
        return len(entries)

    def upsert_company(self, item, research_status=RESEARCH_PENDING, source='new',
                       maintenance_status=None):
        now = utc_now()
        ticker = (item.get('ticker') or extract_ticker(item.get('Company', ''))).strip()
        name = item.get('Company', '').strip()
        if not name:
            raise ValueError('Company is required')
        with self.connect() as db:
            row = None
            if ticker:
                row = db.execute('SELECT * FROM companies WHERE ticker=?', (ticker,)).fetchone()
            if row is None:
                row = db.execute('SELECT * FROM companies WHERE company_name=?', (name,)).fetchone()
            if row:
                values = [ticker or row['ticker'], name, item.get('Country', row['country']),
                          item.get('Industry', row['industry']), item.get('Tier', row['tier']),
                          now, row['id']]
                db.execute("""UPDATE companies SET ticker=?, company_name=?, country=?, industry=?, tier=?,
                    updated_at=? WHERE id=?""", values)
                if research_status == DEEP_RESEARCHED and row['research_status'] != DEEP_RESEARCHED:
                    db.execute("""UPDATE companies SET research_status=?, deep_researched_at=?,
                        maintenance_status=?, updated_at=? WHERE id=?""",
                        (DEEP_RESEARCHED, now, maintenance_status or MAINT_NOT_DUE, now, row['id']))
                return row['id']
            cur = db.execute("""INSERT INTO companies
                (ticker, company_name, country, industry, tier, research_status,
                 maintenance_status, source, created_at, updated_at, deep_researched_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (ticker or None, name, item.get('Country', ''), item.get('Industry', ''),
                 item.get('Tier', ''), research_status,
                 maintenance_status or MAINT_NOT_DUE, source, now, now,
                 now if research_status == DEEP_RESEARCHED else None))
            return cur.lastrowid

    def sync_csv(self, csv_path, cols, is_placeholder_row, is_placeholder_cell,
                 maintenance_state=None):
        """Idempotently import real CSV rows without treating empty capacity as companies."""
        maintenance_state = maintenance_state or {}
        seen = 0
        with open(csv_path, 'r', encoding='utf-8-sig') as f:
            reader = csv.reader(f)
            next(reader, None)
            for row in reader:
                if len(row) < 6 or is_placeholder_row(row):
                    continue
                name = row[5].strip()
                ticker = extract_ticker(name)
                complete = not any(is_placeholder_cell(c) for c in row)
                item = {'Company': name, 'ticker': ticker, 'Country': row[0],
                        'Industry': row[3], 'Tier': row[4]}
                cid = self.upsert_company(
                    item, DEEP_RESEARCHED if complete else RESEARCH_PENDING,
                    source='csv', maintenance_status=MAINT_NOT_DUE)
                stamp = maintenance_state.get(ticker, {}).get('last_maintained')
                if stamp:
                    with self.connect() as db:
                        db.execute("""UPDATE companies SET last_maintained_at=?, updated_at=?
                            WHERE id=?""", (stamp, utc_now(), cid))
                seen += 1
        self.set_meta('csv_migrated_at', utc_now())
        return seen

    # ---- company queues -------------------------------------------------------

    def company_by_ticker(self, ticker):
        with self.connect() as db:
            return db.execute('SELECT * FROM companies WHERE ticker=?', (ticker,)).fetchone()

    def enqueue_items(self, items, source='new'):
        ids = []
        for item in items:
            cid = self.upsert_company(item, RESEARCH_PENDING, source)
            with self.connect() as db:
                row = db.execute('SELECT research_status FROM companies WHERE id=?', (cid,)).fetchone()
                if row['research_status'] not in (DEEP_RESEARCHED, EXCLUDED, TICKER_REVIEW):
                    db.execute("""UPDATE companies SET research_status=?, source=?, updated_at=?
                        WHERE id=?""", (RESEARCH_PENDING, source, utc_now(), cid))
            ids.append(cid)
        return ids

    # ---- canonical research content -----------------------------------------------------

    def upsert_research(self, company_id, record, cols, source='apply', batch_id=None,
                        is_placeholder_cell=None):
        """Store one company's 22-field research record. Idempotent, no capacity limit.

        This is the write that the CSV path could not perform once its placeholder rows ran
        out. An UPSERT cannot silently drop a company, which is the whole point.
        """
        name = (record.get('Company') or '').strip()
        if not name:
            raise ValueError('research record has no Company')
        clean = {k: (record.get(k) or '') for k in cols}
        clean['Company'] = name
        complete = 1
        if is_placeholder_cell is not None:
            complete = 0 if any(is_placeholder_cell(v) for v in clean.values()) else 1
        with self.connect() as db:
            db.execute("""INSERT INTO company_research
                (company_id, ticker, company_name, record_json, complete, source,
                 source_batch_id, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(company_id) DO UPDATE SET
                    ticker=excluded.ticker, company_name=excluded.company_name,
                    record_json=excluded.record_json, complete=excluded.complete,
                    source=excluded.source, source_batch_id=excluded.source_batch_id,
                    updated_at=excluded.updated_at""",
                (company_id, extract_ticker(name), name,
                 json.dumps(clean, ensure_ascii=False), complete, source, batch_id, utc_now()))
        return company_id

    def research_company_id(self, name, ticker=None):
        """Find an existing company by exact name, else by ticker. None if unknown.

        Ticker is checked second so a renamed company still lands on its existing row rather
        than creating a duplicate.
        """
        with self.connect() as db:
            row = db.execute('SELECT id FROM companies WHERE company_name=?',
                             (name,)).fetchone()
            if row:
                return row['id']
            if ticker:
                row = db.execute("""SELECT id FROM companies WHERE ticker=? AND ticker!=''
                                    ORDER BY id LIMIT 1""", (ticker,)).fetchone()
                if row:
                    return row['id']
        return None

    def research_record(self, company_id):
        """Stored 22-field record for one company, or None."""
        if not company_id:
            return None
        with self.connect() as db:
            row = db.execute('SELECT record_json FROM company_research WHERE company_id=?',
                             (company_id,)).fetchone()
        if not row:
            return None
        try:
            return json.loads(row['record_json'])
        except ValueError:
            return None

    def research_records(self, complete_only=False):
        """Every stored research record, oldest-first by company id — the export order."""
        sql = 'SELECT record_json FROM company_research'
        if complete_only:
            sql += ' WHERE complete=1'
        sql += ' ORDER BY company_id'
        with self.connect() as db:
            rows = db.execute(sql).fetchall()
        out = []
        for r in rows:
            try:
                out.append(json.loads(r['record_json']))
            except ValueError:
                continue
        return out

    def research_content_count(self):
        with self.connect() as db:
            return db.execute('SELECT COUNT(*) c FROM company_research').fetchone()['c']

    def research_queue(self, limit, retry_cap=10, country=None, industry=None):
        """Next companies to research: hand-picked ones first (source 'manual' — Peter typed
        them, 2026-09-09), then retries up to retry_cap, then discovered names by age."""
        now = utc_now()
        focus_sql, filter_args = focus_filters(country, industry)
        with self.connect() as db:
            eligible = """research_status=? AND
                (next_attempt_at IS NULL OR next_attempt_at<=?)"""
            manual = db.execute(f"""SELECT * FROM companies WHERE {eligible}{focus_sql}
                AND source='manual' ORDER BY created_at, id LIMIT ?""",
                (RESEARCH_PENDING, now, *filter_args, limit)).fetchall()
            retry = db.execute(f"""SELECT * FROM companies WHERE {eligible}{focus_sql}
                AND source!='manual' AND (attempt_count>0 OR last_error_class IS NOT NULL)
                ORDER BY next_attempt_at, updated_at, id LIMIT ?""",
                (RESEARCH_PENDING, now, *filter_args,
                 max(0, min(retry_cap, limit - len(manual))))).fetchall()
            remaining = max(0, limit - len(manual) - len(retry))
            ordinary = db.execute(f"""SELECT * FROM companies WHERE {eligible}{focus_sql}
                AND source!='manual' AND attempt_count=0 AND last_error_class IS NULL
                ORDER BY created_at, id LIMIT ?""",
                (RESEARCH_PENDING, now, *filter_args, remaining)).fetchall()
        return [dict(r) for r in manual + retry + ordinary]

    # ---- hand-picked companies ----------------------------------------------------------

    @staticmethod
    def parse_company_request(text):
        """'台積電 (2330.TW)' -> ('台積電 (2330.TW)', '2330.TW'); a bare 'nvda' or '2330.tw'
        -> ('NVDA (NVDA)', 'NVDA') and the researcher fills in the name. No usable ticker ->
        (None, '')."""
        text = (text or '').strip().lstrip('@').strip()
        if not text:
            return None, ''
        ticker = extract_ticker(text)
        if ticker:
            return text, ticker.strip()
        if re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.\-]{0,11}', text) and ' ' not in text:
            ticker = text.upper()
            return f'{ticker} ({ticker})', ticker
        return None, ''

    def request_company(self, text, set_by='peter'):
        """Put one company Peter named into the pipeline. Returns what happened:
        'queued' (new, front of the research queue), 'promoted' (was pending, now first),
        'refresh' (already researched: marked due for the next maintenance slot),
        'running' (a batch has it right now), or 'invalid' (no ticker to go on)."""
        name, ticker = self.parse_company_request(text)
        if not name:
            return {'action': 'invalid', 'text': text, 'company': '', 'ticker': ''}
        now = utc_now()
        with self.connect() as db:
            row = db.execute('SELECT * FROM companies WHERE ticker=?', (ticker,)).fetchone()
            if row is None:
                cur = db.execute("""INSERT INTO companies
                    (ticker, company_name, research_status, maintenance_status, source,
                     created_at, updated_at) VALUES (?, ?, ?, ?, 'manual', ?, ?)""",
                    (ticker, name, RESEARCH_PENDING, MAINT_NOT_DUE, now, now))
                return {'action': 'queued', 'id': cur.lastrowid, 'company': name,
                        'ticker': ticker}
            result = {'id': row['id'], 'company': row['company_name'], 'ticker': ticker}
            status = row['research_status']
            if status == DEEP_RESEARCHED:
                # Straight to the update queue, ahead of the never-maintained rows it is
                # ordered with (last_maintained_at NULL sorts first).
                db.execute("""UPDATE companies SET maintenance_status=?, last_maintained_at=NULL,
                    next_attempt_at=NULL, updated_at=? WHERE id=?""",
                    (MAINT_UPDATE_DUE, now, row['id']))
                return {**result, 'action': 'refresh'}
            if status == RESEARCHING:
                return {**result, 'action': 'running'}
            db.execute("""UPDATE companies SET research_status=?, source='manual',
                attempt_count=0, next_attempt_at=NULL, last_error_class=NULL, last_error=NULL,
                updated_at=? WHERE id=?""", (RESEARCH_PENDING, now, row['id']))
            return {**result, 'action': 'promoted' if status == RESEARCH_PENDING else 'queued'}

    def requested_companies(self):
        """Hand-picked companies still waiting, oldest first — for the dashboard strip."""
        with self.connect() as db:
            rows = db.execute("""SELECT * FROM companies WHERE research_status=? AND
                source='manual' ORDER BY created_at, id""", (RESEARCH_PENDING,)).fetchall()
        return [dict(r) for r in rows]

    def covered_tickers(self, country=None, industry=None):
        """Tickers already in the corpus, optionally restricted to one country and/or one
        industry (substring, like every other industry match here).

        Used to trim the nomination prompt: listing all covered tickers costs ~30k
        characters of every discovery call, almost all of it irrelevant once the attempt is
        steered at a single slice. Callers must still dedupe against the FULL covered set —
        this only decides what the model is shown.
        """
        sql = ("SELECT ticker FROM companies "
               "WHERE ticker IS NOT NULL AND TRIM(ticker)!=''")
        clause, args = focus_filters(country, industry)
        sql += clause
        with self.connect() as db:
            return sorted({r['ticker'].strip() for r in db.execute(sql, args)
                           if (r['ticker'] or '').strip()})

    def focus_values(self):
        """Values offered by the dashboard's optional country/industry selectors."""
        with self.connect() as db:
            countries = [r['value'] for r in db.execute("""SELECT DISTINCT TRIM(country) value
                FROM companies WHERE TRIM(country)!='' ORDER BY value COLLATE NOCASE""")]
            industries = [r['value'] for r in db.execute("""SELECT DISTINCT TRIM(industry) value
                FROM companies WHERE TRIM(industry)!='' ORDER BY value COLLATE NOCASE""")]
        return countries, industries

    # ---- daily batch conductor -----------------------------------------------

    def seed_daily_slots(self, slot_date, schedule):
        """Create that day's editable schedule without overwriting manual choices."""
        now = utc_now()
        with self.connect() as db:
            for entry in schedule:
                if len(entry) < 2:
                    continue
                hhmm, provider = str(entry[0]), str(entry[1])
                mode = str(entry[2]) if len(entry) > 2 else 'research'
                if provider not in PROVIDERS or mode not in ('research', 'maintenance'):
                    continue
                if not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', hhmm):
                    continue
                db.execute("""INSERT OR IGNORE INTO daily_slots
                    (slot_date, slot_time, provider, mode, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)""",
                    (slot_date, hhmm, provider, mode, now, now))
        return self.daily_slots(slot_date)

    def daily_slots(self, slot_date):
        with self.connect() as db:
            rows = db.execute("""SELECT ds.*, br.seq batch_seq,
                br.status batch_status, br.completed_at batch_completed_at,
                COUNT(bi.id) item_count,
                COALESCE(SUM(CASE WHEN bi.status IN
                    ('validated','applied','review','requeued','excluded') THEN 1 ELSE 0 END), 0)
                    processed_count,
                COALESCE(SUM(CASE WHEN bi.status='validated' THEN 1 ELSE 0 END), 0)
                    validated_count,
                COALESCE(SUM(CASE WHEN bi.status='applied' THEN 1 ELSE 0 END), 0) applied_count,
                COALESCE(SUM(CASE WHEN bi.status IN
                    ('review','requeued','excluded') THEN 1 ELSE 0 END), 0) failed_count
                FROM daily_slots ds
                LEFT JOIN batch_runs br ON br.id=ds.batch_id
                LEFT JOIN batch_items bi ON bi.batch_id=ds.batch_id
                WHERE ds.slot_date=? GROUP BY ds.id ORDER BY ds.slot_time""",
                (slot_date,)).fetchall()
        return [dict(r) for r in rows]

    def daily_slot(self, slot_id):
        with self.connect() as db:
            row = db.execute('SELECT * FROM daily_slots WHERE id=?', (slot_id,)).fetchone()
        return dict(row) if row else None

    def update_daily_slot(self, slot_id, *, provider=None, mode=None, enabled=None,
                          country_focus=None, industry_focus=None, now=None):
        """Edit only a future, unclaimed slot. None leaves a field unchanged."""
        now = now or dt.datetime.now()
        with self.connect() as db:
            row = db.execute('SELECT * FROM daily_slots WHERE id=?', (slot_id,)).fetchone()
            if not row:
                raise ValueError('Schedule slot not found')
            scheduled = dt.datetime.combine(dt.date.fromisoformat(row['slot_date']),
                                            dt.time.fromisoformat(row['slot_time']))
            if row['status'] != 'pending' or scheduled <= now:
                raise ValueError('Only future, unprocessed slots can be edited')
            updates, args = [], []
            if provider is not None:
                if provider not in PROVIDERS:
                    raise ValueError('Unknown provider')
                updates.append('provider=?'); args.append(provider)
            if mode is not None:
                if mode not in ('research', 'maintenance'):
                    raise ValueError('Unknown batch mode')
                updates.append('mode=?'); args.append(mode)
            if enabled is not None:
                updates.append('enabled=?'); args.append(1 if enabled else 0)
            if country_focus is not None:
                updates.append('country_focus=?'); args.append(country_focus.strip())
            if industry_focus is not None:
                updates.append('industry_focus=?'); args.append(industry_focus.strip())
            if not updates:
                return dict(row)
            updates.append('updated_at=?'); args.append(utc_now()); args.append(slot_id)
            db.execute(f"UPDATE daily_slots SET {', '.join(updates)} WHERE id=?", args)
            changed = db.execute('SELECT * FROM daily_slots WHERE id=?', (slot_id,)).fetchone()
        return dict(changed)

    def claim_due_slot(self, now=None, grace_minutes=5):
        """Claim the newest due slot and mark older/late enabled slots as missed."""
        now = now or dt.datetime.now()
        day, hhmm = now.date().isoformat(), now.strftime('%H:%M')
        with self.connect() as db:
            due = db.execute("""SELECT * FROM daily_slots WHERE slot_date=? AND enabled=1
                AND status='pending' AND slot_time<=? ORDER BY slot_time DESC""",
                (day, hhmm)).fetchall()
            if not due:
                return None
            candidate = due[0]
            older_ids = [r['id'] for r in due[1:]]
            if older_ids:
                marks = ','.join('?' for _ in older_ids)
                db.execute(f"""UPDATE daily_slots SET status='missed',
                    detail='Previous batch overran this time', updated_at=?
                    WHERE id IN ({marks})""", (utc_now(), *older_ids))
            scheduled = dt.datetime.combine(now.date(), dt.time.fromisoformat(candidate['slot_time']))
            if (now - scheduled).total_seconds() > grace_minutes * 60:
                db.execute("""UPDATE daily_slots SET status='missed',
                    detail='Slot was not claimed within its start window', updated_at=? WHERE id=?""",
                    (utc_now(), candidate['id']))
                return None
            changed = db.execute("""UPDATE daily_slots SET status='running', updated_at=?
                WHERE id=? AND status='pending' AND enabled=1""",
                (utc_now(), candidate['id']))
            if not changed.rowcount:
                return None
            row = db.execute('SELECT * FROM daily_slots WHERE id=?',
                             (candidate['id'],)).fetchone()
        return dict(row)

    def next_daily_slot(self, slot_date, after_hhmm='00:00'):
        with self.connect() as db:
            row = db.execute("""SELECT * FROM daily_slots WHERE slot_date=? AND enabled=1
                AND status='pending' AND slot_time>? ORDER BY slot_time LIMIT 1""",
                (slot_date, after_hhmm)).fetchone()
        return dict(row) if row else None

    def miss_due_slots(self, now=None, detail='Previous batch overran this time'):
        now = now or dt.datetime.now()
        with self.connect() as db:
            changed = db.execute("""UPDATE daily_slots SET status='missed', detail=?, updated_at=?
                WHERE slot_date=? AND enabled=1 AND status='pending' AND slot_time<=?""",
                (detail[:1000], utc_now(), now.date().isoformat(), now.strftime('%H:%M')))
        return changed.rowcount

    def finish_daily_slot(self, slot_id, status, detail='', batch_id=None):
        if status not in ('complete', 'partial', 'skipped', 'missed'):
            raise ValueError('Invalid slot outcome')
        with self.connect() as db:
            db.execute("""UPDATE daily_slots SET status=?, detail=?,
                batch_id=COALESCE(?, batch_id), updated_at=? WHERE id=?""",
                (status, detail[:1000], batch_id, utc_now(), slot_id))

    def mark_researching(self, company_id):
        with self.connect() as db:
            db.execute('UPDATE companies SET research_status=?, updated_at=? WHERE id=?',
                       (RESEARCHING, utc_now(), company_id))

    def mark_deep_researched(self, company_id, ticker=None, company_name=None):
        now = utc_now()
        with self.connect() as db:
            if ticker:
                collision = db.execute('SELECT id, company_name FROM companies WHERE ticker=? AND id!=?',
                                       (ticker, company_id)).fetchone()
                if collision:
                    detail = (f"Duplicate candidate merged into {collision['company_name']} "
                              f"(workflow company {collision['id']})")
                    db.execute("""UPDATE companies SET research_status=?,
                        last_error_class=?, last_error=?, updated_at=? WHERE id=?""",
                        (EXCLUDED, 'merged', detail, now, company_id))
                    db.execute("""UPDATE companies SET research_status=?,
                        deep_researched_at=COALESCE(deep_researched_at, ?), updated_at=? WHERE id=?""",
                        (DEEP_RESEARCHED, now, now, collision['id']))
                    return
            db.execute("""UPDATE companies SET ticker=COALESCE(?, ticker),
                company_name=COALESCE(?, company_name), research_status=?, maintenance_status=?,
                attempt_count=0, next_attempt_at=NULL, last_error_class=NULL, last_error=NULL,
                deep_researched_at=COALESCE(deep_researched_at, ?), updated_at=? WHERE id=?""",
                (ticker, company_name, DEEP_RESEARCHED, MAINT_NOT_DUE, now, now, company_id))

    def resolve_deep_research_result(self, company_id, ticker=None, company_name=None):
        """Finalize a result without crashing when a model corrects it to a known ticker.

        The CSV applier already merges a corrected result into the canonical ticker row.
        Mirror that outcome here by excluding the duplicate candidate while leaving the
        canonical company deep-researched.  Normal results finalize as before.
        """
        now = utc_now()
        with self.connect() as db:
            source = db.execute('SELECT * FROM companies WHERE id=?', (company_id,)).fetchone()
            if not source:
                raise ValueError(f'company {company_id} not found')
            collision = None
            if ticker:
                collision = db.execute('SELECT * FROM companies WHERE ticker=? AND id!=?',
                                       (ticker, company_id)).fetchone()
            if collision:
                detail = (f"Duplicate candidate merged into {collision['company_name']} "
                          f"(workflow company {collision['id']})")
                db.execute("""UPDATE companies SET research_status=?,
                    last_error_class='duplicate_ticker', last_error=?, next_attempt_at=NULL,
                    updated_at=? WHERE id=?""",
                    (EXCLUDED, detail, now, company_id))
                return {'company_id': collision['id'], 'merged': True,
                        'duplicate_company_id': company_id, 'detail': detail}
            db.execute("""UPDATE companies SET ticker=COALESCE(?, ticker),
                company_name=COALESCE(?, company_name), research_status=?, maintenance_status=?,
                attempt_count=0, next_attempt_at=NULL, last_error_class=NULL, last_error=NULL,
                deep_researched_at=COALESCE(deep_researched_at, ?), updated_at=? WHERE id=?""",
                (ticker, company_name, DEEP_RESEARCHED, MAINT_NOT_DUE, now, now, company_id))
            return {'company_id': company_id, 'merged': False,
                    'duplicate_company_id': None, 'detail': ''}

    def requeue_research(self, company_id, error_class, error_text, count_attempt=True,
                         delay_hours=0):
        next_at = None
        if delay_hours:
            next_at = (dt.datetime.now(dt.timezone.utc)
                       + dt.timedelta(hours=delay_hours)).isoformat(timespec='seconds')
        with self.connect() as db:
            db.execute("""UPDATE companies SET research_status=?,
                attempt_count=attempt_count+?, next_attempt_at=?, last_error_class=?,
                last_error=?, updated_at=? WHERE id=?""",
                (RESEARCH_PENDING, 1 if count_attempt else 0, next_at,
                 error_class, error_text[:1000], utc_now(), company_id))

    def send_to_review(self, company_id, error_class, error_text, attempt_count=None):
        with self.connect() as db:
            db.execute("""UPDATE companies SET research_status=?, last_error_class=?,
                last_error=?, attempt_count=COALESCE(?, attempt_count), updated_at=? WHERE id=?""",
                (TICKER_REVIEW, error_class, error_text[:1000], attempt_count,
                 utc_now(), company_id))

    def exclude(self, company_id, reason):
        with self.connect() as db:
            db.execute("""UPDATE companies SET research_status=?, last_error_class='excluded',
                last_error=?, updated_at=? WHERE id=?""",
                (EXCLUDED, reason[:1000], utc_now(), company_id))

    def correct_ticker_and_requeue(self, company_id, company_name, ticker):
        ticker = ticker.strip().upper()
        if not re.fullmatch(r'[A-Z0-9.^-]{1,16}(?:\.[A-Z0-9]{1,6})?', ticker):
            raise ValueError('Ticker format is invalid')
        with self.connect() as db:
            collision = db.execute('SELECT company_name FROM companies WHERE ticker=? AND id!=?',
                                   (ticker, company_id)).fetchone()
            if collision:
                raise ValueError(f'Ticker already belongs to {collision["company_name"]}')
            label = re.sub(r'\s*\([^)]+\)\s*$', '', company_name).strip()
            label = f'{label} ({ticker})'
            db.execute("""UPDATE companies SET ticker=?, company_name=?, research_status=?,
                attempt_count=0, next_attempt_at=NULL, last_error_class=NULL, last_error=NULL,
                updated_at=? WHERE id=?""",
                (ticker, label, RESEARCH_PENDING, utc_now(), company_id))

    # ---- resumable batch manifests -------------------------------------------

    def create_batch(self, seq, label, mode, source, provider, items, slot_id=None):
        now = utc_now()
        with self.connect() as db:
            cur = db.execute("""INSERT OR REPLACE INTO batch_runs
                (id, seq, label, mode, source, scheduled_provider, status, created_at)
                VALUES ((SELECT id FROM batch_runs WHERE seq=?), ?, ?, ?, ?, ?, 'running', ?)""",
                (seq, seq, label, mode, source, provider, now))
            row = db.execute('SELECT id FROM batch_runs WHERE seq=?', (seq,)).fetchone()
            batch_id = row['id']
            db.execute('DELETE FROM batch_items WHERE batch_id=?', (batch_id,))
            for pos, item in enumerate(items):
                cid = item.get('_company_id')
                persisted_item = dict(item)
                if mode == 'maintenance' and cid:
                    screening = db.execute("""SELECT id, reason, evidence_date,
                        source_urls_json, created_at FROM maintenance_screenings
                        WHERE company_id=? ORDER BY created_at DESC, id DESC LIMIT 1""",
                                           (cid,)).fetchone()
                    if screening:
                        persisted_item['_maintenance_screening'] = {
                            'id': screening['id'],
                            'reason': screening['reason'],
                            'evidence_date': screening['evidence_date'],
                            'source_urls': normalize_source_urls(
                                json.loads(screening['source_urls_json'])),
                            'screened_at': screening['created_at'],
                        }
                db.execute("""INSERT INTO batch_items
                    (batch_id, company_id, position, item_json, status, updated_at)
                    VALUES (?, ?, ?, ?, 'pending', ?)""",
                    (batch_id, cid, pos, json.dumps(persisted_item, ensure_ascii=False), now))
            if slot_id is not None:
                db.execute("""UPDATE daily_slots SET batch_id=?, status='running', updated_at=?
                    WHERE id=?""", (batch_id, now, slot_id))
            return batch_id

    def checkpoint_item(self, batch_id, position, status, provider=None, result=None,
                        raw='', error_class=None, error_text=None):
        with self.connect() as db:
            db.execute("""UPDATE batch_items SET status=?, provider=?, result_json=?, raw_output=?,
                error_class=?, error_text=?, updated_at=? WHERE batch_id=? AND position=?""",
                (status, provider,
                 json.dumps(result, ensure_ascii=False) if result is not None else None,
                 raw[-20000:] if raw else None, error_class,
                 (error_text or '')[:2000] or None, utc_now(), batch_id, position))

    def finish_batch(self, batch_id, status='complete'):
        with self.connect() as db:
            db.execute('UPDATE batch_runs SET status=?, completed_at=? WHERE id=?',
                       (status, utc_now(), batch_id))
            slot_status = 'complete' if status == 'complete' else 'partial'
            db.execute("""UPDATE daily_slots SET status=?, updated_at=? WHERE batch_id=?
                AND status='running'""", (slot_status, utc_now(), batch_id))

    def batch_run(self, seq):
        with self.connect() as db:
            row = db.execute('SELECT * FROM batch_runs WHERE seq=?', (seq,)).fetchone()
        return dict(row) if row else None

    def incomplete_batches(self):
        with self.connect() as db:
            runs = db.execute("""SELECT * FROM batch_runs WHERE status IN ('running','applying')
                ORDER BY seq""").fetchall()
            result = []
            for run in runs:
                items = db.execute('SELECT * FROM batch_items WHERE batch_id=? ORDER BY position',
                                   (run['id'],)).fetchall()
                result.append((dict(run), [dict(i) for i in items]))
            return result

    # ---- providers ------------------------------------------------------------

    def provider_states(self):
        now = dt.datetime.now(dt.timezone.utc)
        with self.connect() as db:
            rows = db.execute('SELECT * FROM provider_state ORDER BY provider').fetchall()
        out = []
        for row in rows:
            d = dict(row)
            until = parse_iso(d.get('cooldown_until'))
            if d['health'] == 'cooldown' and until and until <= now:
                self.clear_cooldown(d['provider'])
                d['health'], d['cooldown_until'] = 'unknown', None
            out.append(d)
        return out

    def provider_state(self, provider):
        return next((p for p in self.provider_states() if p['provider'] == provider), None)

    def set_provider_enabled(self, provider, enabled):
        with self.connect() as db:
            db.execute("""UPDATE provider_state SET enabled=?, updated_at=? WHERE provider=?""",
                       (1 if enabled else 0, utc_now(), provider))

    def set_provider_health(self, provider, health, detail=None):
        current = self.provider_state(provider)
        if current and current['health'] == 'cooldown' and health == 'healthy':
            return
        with self.connect() as db:
            db.execute("""UPDATE provider_state SET health=?, last_error=?, updated_at=?
                WHERE provider=?""", (health, detail, utc_now(), provider))

    def force_provider_next(self, provider):
        with self.connect() as db:
            db.execute('UPDATE provider_state SET force_next=0')
            db.execute('UPDATE provider_state SET force_next=1, updated_at=? WHERE provider=?',
                       (utc_now(), provider))

    def consume_forced_provider(self):
        with self.connect() as db:
            row = db.execute("""SELECT provider FROM provider_state
                WHERE force_next=1 AND enabled=1 LIMIT 1""").fetchone()
            if row:
                db.execute('UPDATE provider_state SET force_next=0 WHERE provider=?',
                           (row['provider'],))
                return row['provider']
        return None

    def mark_provider_success(self, provider):
        now = utc_now()
        with self.connect() as db:
            db.execute("""UPDATE provider_state SET health='healthy', cooldown_until=NULL,
                last_error=NULL, successes=successes+1, updated_at=? WHERE provider=?""",
                (now, provider))
            db.execute("""INSERT INTO provider_events(provider, success, created_at)
                VALUES (?, 1, ?)""", (provider, now))

    def mark_provider_failure(self, provider, error_class, detail, cooldown_until=None):
        now = utc_now()
        health = 'cooldown' if cooldown_until else ('auth_error' if error_class == 'auth' else 'degraded')
        with self.connect() as db:
            db.execute("""UPDATE provider_state SET health=?, cooldown_until=?, last_error=?,
                failures=failures+1, updated_at=? WHERE provider=?""",
                (health, cooldown_until, detail[:1000], now, provider))
            db.execute("""INSERT INTO provider_events
                (provider, success, error_class, detail, created_at) VALUES (?, 0, ?, ?, ?)""",
                (provider, error_class, detail[:1000], now))

    def clear_cooldown(self, provider):
        with self.connect() as db:
            db.execute("""UPDATE provider_state SET health='unknown', cooldown_until=NULL,
                updated_at=? WHERE provider=?""", (utc_now(), provider))

    def recent_success_rate(self, provider, n=20):
        with self.connect() as db:
            rows = db.execute("""SELECT success FROM provider_events WHERE provider=?
                ORDER BY id DESC LIMIT ?""", (provider, n)).fetchall()
        return (sum(r['success'] for r in rows) / len(rows)) if rows else 0.5

    # ---- maintenance ----------------------------------------------------------

    def begin_maintenance_cycle(self, week_start, weekly_capacity, cadence_days,
                                today=None, stuck_updating_hours=12):
        """Mark completed companies due once per cycle, tiering only above capacity.

        `today` is the real current date, NOT week_start. Deriving it from week_start
        froze the tiered-cadence delta to Monday, so nothing could come due mid-week: on
        2026-08-26 the 228 rows screened 08-18 measured as 6 days stale instead of 8 and
        stayed invisible, skipping 26 consecutive slots with "No matching candidates".
        """
        today = today or dt.date.today()
        # Rows abandoned mid-update are skipped by the due-check below, so without a reaper
        # they are invisible to BOTH the screen and update queues forever (four Taiwan rows
        # sat in 'updating' from 2026-08-23 with attempt_count=0 and no error).
        self.reap_stuck_updating(stuck_updating_hours)
        with self.connect() as db:
            total = db.execute('SELECT COUNT(*) n FROM companies WHERE research_status=?',
                               (DEEP_RESEARCHED,)).fetchone()['n']
            rows = db.execute('SELECT * FROM companies WHERE research_status=?',
                              (DEEP_RESEARCHED,)).fetchall()
            for row in rows:
                if row['maintenance_status'] in (MAINT_UPDATE_DUE, MAINT_UPDATING, MAINT_RETRY):
                    continue
                last = (row['last_screened_at'] or '')[:10]
                due = not last or last < week_start
                if total > weekly_capacity and last:
                    group = canonical_tier(row['tier'])
                    days = cadence_days.get(group, cadence_days.get('unknown', 28))
                    try:
                        due = (today - dt.date.fromisoformat(last)).days >= days
                    except ValueError:
                        due = True
                if due:
                    db.execute("""UPDATE companies SET maintenance_status=?, updated_at=?
                        WHERE id=?""", (MAINT_SCREEN_DUE, utc_now(), row['id']))
        self.set_meta('maintenance_cycle_week', week_start)
        return total

    def reap_stuck_updating(self, max_age_hours=12):
        """Return rows abandoned mid-update to the update queue.

        mark_maintenance_updating() flips a row to 'updating' before the LLM call; if the
        process dies (or a batch aborts) between there and mark_maintained/requeue, the row
        keeps that status. begin_maintenance_cycle deliberately skips 'updating' rows, so
        nothing else can ever rescue them. Returns the number of rows reclaimed.
        """
        if not max_age_hours:
            return 0
        cutoff = (dt.datetime.now(dt.timezone.utc)
                  - dt.timedelta(hours=max_age_hours)).isoformat(timespec='seconds')
        with self.connect() as db:
            cur = db.execute("""UPDATE companies SET maintenance_status=?, updated_at=?
                WHERE maintenance_status=? AND updated_at<?""",
                (MAINT_UPDATE_DUE, utc_now(), MAINT_UPDATING, cutoff))
            return cur.rowcount or 0

    def maintenance_screen_queue(self, limit, country=None, industry=None):
        focus_sql, filter_args = focus_filters(country, industry)
        with self.connect() as db:
            rows = db.execute(f"""SELECT * FROM companies WHERE research_status=?
                AND maintenance_status=?{focus_sql}""",
                (DEEP_RESEARCHED, MAINT_SCREEN_DUE, *filter_args)).fetchall()
        # SQLite cannot call canonical_tier; stable Python sort handles noisy tiers.
        order = {'leader': 0, 'second_leader': 1, 'potential': 2,
                 'hidden_champion': 3, 'supporting': 4, 'minor_supplier': 5, 'unknown': 6}
        data = [dict(r) for r in rows]
        data.sort(key=lambda r: (order[canonical_tier(r['tier'])], r['last_screened_at'] or '', r['id']))
        return data[:limit]

    def record_screening(self, company_id, week_start, provider, needs_update,
                         reason='', evidence_date=None, source_urls=None):
        now = utc_now()
        source_urls_json = json.dumps(normalize_source_urls(source_urls), ensure_ascii=False)
        with self.connect() as db:
            db.execute("""INSERT INTO maintenance_screenings
                (company_id, week_start, provider, needs_update, reason, evidence_date,
                 source_urls_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(company_id, week_start) DO UPDATE SET
                    provider=excluded.provider, needs_update=excluded.needs_update,
                    reason=excluded.reason, evidence_date=excluded.evidence_date,
                    source_urls_json=excluded.source_urls_json,
                    created_at=excluded.created_at""",
                (company_id, week_start, provider, 1 if needs_update else 0,
                 reason[:1000], evidence_date, source_urls_json, now))
            db.execute("""UPDATE companies SET last_screened_at=?, maintenance_status=?,
                updated_at=? WHERE id=?""",
                (now, MAINT_UPDATE_DUE if needs_update else MAINT_NOT_DUE, now, company_id))

    def maintenance_update_queue(self, limit, country=None, industry=None):
        now = utc_now()
        focus_sql, filter_args = focus_filters(country, industry)
        with self.connect() as db:
            rows = db.execute(f"""SELECT * FROM companies WHERE research_status=? AND
                (maintenance_status=? OR (maintenance_status=? AND
                 (next_attempt_at IS NULL OR next_attempt_at<=?))){focus_sql}
                ORDER BY COALESCE(last_maintained_at,''), updated_at, id LIMIT ?""",
                (DEEP_RESEARCHED, MAINT_UPDATE_DUE, MAINT_RETRY, now,
                 *filter_args, limit)).fetchall()
        return [dict(r) for r in rows]

    def mark_maintenance_updating(self, company_id):
        with self.connect() as db:
            db.execute('UPDATE companies SET maintenance_status=?, updated_at=? WHERE id=?',
                       (MAINT_UPDATING, utc_now(), company_id))

    def mark_maintained(self, company_id):
        now = utc_now()
        with self.connect() as db:
            db.execute("""UPDATE companies SET maintenance_status=?, last_maintained_at=?,
                attempt_count=0, next_attempt_at=NULL, last_error_class=NULL, last_error=NULL,
                updated_at=? WHERE id=?""", (MAINT_NOT_DUE, now, now, company_id))

    def requeue_maintenance(self, company_id, error_class, error_text, delay_hours=0):
        next_at = None
        if delay_hours:
            next_at = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=delay_hours)).isoformat(timespec='seconds')
        with self.connect() as db:
            db.execute("""UPDATE companies SET maintenance_status=?, next_attempt_at=?,
                last_error_class=?, last_error=?, updated_at=? WHERE id=?""",
                (MAINT_RETRY, next_at, error_class, error_text[:1000], utc_now(), company_id))

    # ---- preview-only maintenance digest delivery ledger ---------------------

    @staticmethod
    def _digest_recipients(recipients):
        normalized, seen = [], set()
        for recipient in recipients:
            label = str(recipient.get('label', '')).strip()
            chat_id = str(recipient.get('chat_id', '')).strip()
            if not label or not chat_id:
                raise ValueError('digest recipients require label and chat_id')
            if chat_id in seen:
                raise ValueError('digest recipient chat IDs must be unique')
            seen.add(chat_id)
            normalized.append({'label': label, 'chat_id': chat_id})
        return sorted(normalized, key=lambda row: (row['label'].casefold(), row['chat_id']))

    @staticmethod
    def _digest_payload(payload):
        text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        return text, hashlib.sha256(text.encode('utf-8')).hexdigest()

    def claim_digest_delivery(self, digest_date, recipient, payload):
        """Atomically reserve a recipient/date digest before provider or transport work."""
        row = self._digest_recipients([recipient])[0]
        payload_json, payload_sha256 = self._digest_payload(payload)
        now = utc_now()
        with self.connect() as db:
            updated = db.execute("""UPDATE maintenance_digest_deliveries
                SET recipient_label=?, status='claimed', payload_json=?, payload_sha256=?,
                    telegram_message_id=NULL, error_class=NULL, error_text=NULL, updated_at=?
                WHERE digest_date=? AND recipient_chat_id=? AND status='pending'""",
                (row['label'], payload_json, payload_sha256, now, digest_date, row['chat_id'])).rowcount
            if not updated:
                inserted = db.execute("""INSERT OR IGNORE INTO maintenance_digest_deliveries
                    (digest_date, recipient_chat_id, recipient_label, status, payload_json,
                     payload_sha256, created_at, updated_at)
                    VALUES (?, ?, ?, 'claimed', ?, ?, ?, ?)""",
                    (digest_date, row['chat_id'], row['label'], payload_json, payload_sha256,
                     now, now)).rowcount
                updated = inserted
            stored = db.execute("""SELECT status, payload_sha256 FROM maintenance_digest_deliveries
                WHERE digest_date=? AND recipient_chat_id=?""",
                (digest_date, row['chat_id'])).fetchone()
        return {'claimed': bool(updated), 'status': stored['status'],
                'payload_sha256': stored['payload_sha256']}

    def record_digest_no_updates(self, digest_date, recipient):
        """Persist a terminal no-op without spending a provider call or contacting Telegram."""
        row = self._digest_recipients([recipient])[0]
        now = utc_now()
        with self.connect() as db:
            db.execute("""UPDATE maintenance_digest_deliveries
                SET recipient_label=?, status='skipped_no_updates', payload_json=NULL,
                    payload_sha256=NULL, telegram_message_id=NULL, error_class=NULL,
                    error_text=NULL, updated_at=?
                WHERE digest_date=? AND recipient_chat_id=? AND status='pending'""",
                (row['label'], now, digest_date, row['chat_id']))
            db.execute("""INSERT OR IGNORE INTO maintenance_digest_deliveries
                (digest_date, recipient_chat_id, recipient_label, status, created_at, updated_at)
                VALUES (?, ?, ?, 'skipped_no_updates', ?, ?)""",
                (digest_date, row['chat_id'], row['label'], now, now))

    def record_digest_sent(self, digest_date, recipient, message_id):
        row = self._digest_recipients([recipient])[0]
        with self.connect() as db:
            db.execute("""UPDATE maintenance_digest_deliveries
                SET status='sent', telegram_message_id=?, error_class=NULL, error_text=NULL,
                    updated_at=? WHERE digest_date=? AND recipient_chat_id=? AND status='claimed'""",
                (str(message_id), utc_now(), digest_date, row['chat_id']))

    def record_digest_failed(self, digest_date, recipient, error_class, error_text):
        row = self._digest_recipients([recipient])[0]
        with self.connect() as db:
            db.execute("""UPDATE maintenance_digest_deliveries
                SET status='failed', error_class=?, error_text=?, updated_at=?
                WHERE digest_date=? AND recipient_chat_id=? AND status='claimed'""",
                (str(error_class)[:100], str(error_text)[:1000], utc_now(),
                 digest_date, row['chat_id']))

    def register_digest_recipients(self, digest_date, recipients):
        """Create idempotent pending entries for a future delivery gate; never sends."""
        rows = self._digest_recipients(recipients)
        now, inserted = utc_now(), 0
        with self.connect() as db:
            for recipient in rows:
                cur = db.execute("""INSERT OR IGNORE INTO maintenance_digest_deliveries
                    (digest_date, recipient_chat_id, recipient_label, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?)""",
                    (digest_date, recipient['chat_id'], recipient['label'], now, now))
                inserted += cur.rowcount
        return inserted

    def record_digest_delivery_state(self, digest_date, recipient, status):
        """Persist one preview/status state for one digest-recipient identity pair."""
        if status not in ('pending', 'claimed', 'sent', 'failed', 'skipped_no_updates'):
            raise ValueError('invalid digest delivery status')
        row = self._digest_recipients([recipient])[0]
        now = utc_now()
        with self.connect() as db:
            db.execute("""INSERT INTO maintenance_digest_deliveries
                (digest_date, recipient_chat_id, recipient_label, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(digest_date, recipient_chat_id) DO UPDATE SET
                    recipient_label=excluded.recipient_label,
                    status=excluded.status,
                    updated_at=excluded.updated_at""",
                       (digest_date, row['chat_id'], row['label'], status, now, now))

    def digest_delivery_status(self, digest_date, recipients):
        """Read delivery state without creating rows or touching any sender boundary."""
        rows = self._digest_recipients(recipients)
        with self.connect() as db:
            stored = {
                row['recipient_chat_id']: row['status']
                for row in db.execute("""SELECT recipient_chat_id, status
                    FROM maintenance_digest_deliveries WHERE digest_date=?""", (digest_date,))
            }
        return [{**recipient, 'status': stored.get(recipient['chat_id'], 'pending')}
                for recipient in rows]

    # ---- dashboard ------------------------------------------------------------

    def summary(self):
        with self.connect() as db:
            rows = db.execute("""SELECT research_status, maintenance_status, COUNT(*) n
                FROM companies GROUP BY research_status, maintenance_status""").fetchall()
            failures = db.execute("""SELECT COUNT(*) n FROM companies
                WHERE last_error_class IS NOT NULL""").fetchone()['n']
        out = {'deep_researched': 0, 'pending': 0, 'researching': 0, 'retry': 0,
               'maintenance_due': 0, 'ticker_review': 0, 'excluded': 0, 'failures': failures}
        for row in rows:
            n, rs, ms = row['n'], row['research_status'], row['maintenance_status']
            if rs == DEEP_RESEARCHED:
                out['deep_researched'] += n
            elif rs == RESEARCHING:
                out['researching'] += n
            elif rs == RESEARCH_PENDING:
                out['pending'] += n
                if row['research_status'] == RESEARCH_PENDING:
                    pass
            elif rs == TICKER_REVIEW:
                out['ticker_review'] += n
            elif rs == EXCLUDED:
                out['excluded'] += n
            if ms in (MAINT_SCREEN_DUE, MAINT_UPDATE_DUE, MAINT_RETRY, MAINT_UPDATING):
                out['maintenance_due'] += n
        with self.connect() as db:
            out['retry'] = db.execute("""SELECT COUNT(*) n FROM companies
                WHERE research_status=? AND (attempt_count>0 OR last_error_class IS NOT NULL)""",
                (RESEARCH_PENDING,)).fetchone()['n']
        return out

    def review_items(self, limit=50):
        with self.connect() as db:
            rows = db.execute("""SELECT * FROM companies WHERE research_status IN (?, ?)
                OR last_error_class IS NOT NULL ORDER BY updated_at DESC LIMIT ?""",
                (TICKER_REVIEW, EXCLUDED, limit)).fetchall()
        return [dict(r) for r in rows]


def extract_ticker(company_name: str) -> str:
    match = re.search(r'\(([^)]+)\)\s*$', company_name or '')
    return match.group(1).strip().upper() if match else ''
