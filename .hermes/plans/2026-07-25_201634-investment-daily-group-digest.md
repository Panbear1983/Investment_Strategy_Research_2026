# Investment Daily Group Digest Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** At 20:00 Asia/Taipei each day, post one concise Traditional-Chinese digest to 財經推播Agent only when that local day has one to five eligible Taiwan/USA investment-research updates; newly discovered companies receive priority.

**Architecture:** Keep extraction, selection, model summarization, persistence, and Telegram transport separate. A read-only selector combines completed maintenance updates with completed deep-research results, deduplicates companies, and gives genuinely new workflow companies priority. A single-purpose digest runner claims a recipient/date ledger row, makes at most one Claude call only when candidates exist, snapshots the exact selected payload and rendered message, then sends through `@Hermes_Investment_Strategy_bot` to the verified group.

**Tech Stack:** Python 3.11, SQLite (`scripts/research_workflow.sqlite3`), existing Claude CLI/Sonnet path, Telegram Bot API, macOS launchd, `unittest`.

---

## Confirmed requirements

- Delivery time: **20:00 Asia/Taipei**, once per local calendar day.
- Destination: **財經推播Agent**, verified supergroup ID `-1003863698623`.
- Sender: **`@Hermes_Investment_Strategy_bot`** (`8601109385`), already transport-proven by group test message ID 198.
- Scope: companies whose persisted country is Taiwan or USA.
- Count: 0–5 companies. If there are no eligible updates, make **no Claude call and no Telegram call**.
- Content: one or two Traditional-Chinese model-written sentences/company; no `資料來源`; no investment advice; compact mobile format.
- Priority: a company newly discovered into the workflow (not an existing CSV company) and completed by deep research that day ranks before ordinary maintenance updates.
- Deduplication: one recipient/date payload may be sent at most once, even if the runner restarts.
- This is separate from the pending Financial_Reporting_Bot production migration. Do not modify its container, schedule, credentials, or deployment path.

## Current code facts

- `scripts/maintenance_posts.py:108-115` extracts only maintenance results, and `:246-262` is preview-only.
- `scripts/maintenance_posts.py:182-237` already provides bounded prompt, strict expected-ticker parser, and source-free compact renderer.
- `scripts/research_state.py:259-291` preserves a newly inserted company as `source='new'`; CSV-imported companies are `source='csv'`.
- `scripts/research_state.py:868-923` has a recipient/date preview ledger but needs delivery claims, no-op state, payload snapshot, and Telegram message metadata for an unattended sender.
- `scripts/research_loop.py:1396-1538` already owns research mutations and a singleton lock. The digest must not start a second `research_loop.py --run` process.

## Selection policy

For local date `D` in Asia/Taipei, use a half-open UTC interval `[D 00:00 Asia/Taipei, D+1 00:00 Asia/Taipei)`.

1. Query applied `research` batch items completed in that interval for Taiwan/USA companies.
2. Mark an item priority `new_company` only when its persisted `companies.source` is `new`; this means it originated outside the imported CSV, not merely that an existing company was refreshed.
3. Query applied material `maintenance` batch items in the same interval, using the existing evidence-backed extraction.
4. Deduplicate by canonical `company_id`; when the same company appears in both sources, keep the deep-research record and its higher priority.
5. Order `new_company` first, then newest completion time, then stable item ID. Take at most five.
6. If this produces zero candidates, atomically persist a `skipped_no_updates` ledger outcome and exit without provider or Telegram work.
7. Results completed after 20:00 belong to the next local day’s digest; never retroactively alter an already-sent daily snapshot.

## Tasks

### Task 1: Add a failing test for mixed daily candidate selection

**Objective:** Specify the 0–5 Taiwan/USA candidate contract before changing selectors.

**Files:**
- Modify: `tests/test_maintenance_posts.py`
- Modify: `scripts/maintenance_posts.py`

**Step 1: Write the failing tests.**

Add fixtures containing:
- one Taiwan `research` item for a company with `source='new'`;
- one USA maintenance item;
- one non-Taiwan/USA item;
- one non-material item with no changes;
- a sixth eligible item;
- a duplicate company represented by research and maintenance rows.

```python
def test_daily_group_candidates_prioritize_new_taiwan_usa_deep_research_and_cap_at_five(self):
    posts = extract_daily_group_digest_posts(self.state, '2026-07-25')
    self.assertEqual([post['ticker'] for post in posts], ['NEW.TW', 'US1', 'US2', 'TW2', 'TW3'])
    self.assertEqual(posts[0]['digest_kind'], 'new_company')
    self.assertEqual(len(posts), 5)

def test_daily_group_candidates_returns_empty_for_no_taiwan_usa_updates(self):
    self.assertEqual(extract_daily_group_digest_posts(self.state, '2026-07-25'), [])
```

**Step 2: Run RED.**

Run:
```bash
python3 -m unittest -v tests.test_maintenance_posts.MaintenancePostTests.test_daily_group_candidates_prioritize_new_taiwan_usa_deep_research_and_cap_at_five
```
Expected: FAIL because `extract_daily_group_digest_posts` does not exist.

**Step 3: Implement the smallest read-only selector.**

In `scripts/maintenance_posts.py`, add `extract_daily_group_digest_posts(workflow, digest_date, limit=5)` plus private SQL helpers. Reuse the existing Taipei date-window calculation rather than duplicating date math. Return the existing post shape plus only:

```python
{'digest_kind': 'new_company' | 'maintenance', 'priority': 0 | 1}
```

Do not send, mutate CSV, mutate SQLite, call a provider, or read Telegram configuration here.

**Step 4: Run GREEN.**

Run the two focused selector tests.

**Step 5: Commit.**

```bash
git add scripts/maintenance_posts.py tests/test_maintenance_posts.py
git commit -m "feat: select daily Taiwan and USA digest candidates"
```

### Task 2: Make the daily-delivery ledger claimable and snapshot-backed

**Objective:** Prevent duplicate delivery and preserve exactly what was selected/sent.

**Files:**
- Modify: `scripts/research_state.py:868-923`
- Modify: `tests/test_research_state.py`

**Step 1: Write one failing claim test.**

```python
def test_daily_digest_claim_is_atomic_and_refuses_a_second_same_date_recipient(self):
    first = self.state.claim_digest_delivery('2026-07-25', RECIPIENT, payload)
    second = self.state.claim_digest_delivery('2026-07-25', RECIPIENT, payload)
    self.assertTrue(first['claimed'])
    self.assertFalse(second['claimed'])
```

Add a no-candidate test asserting `record_digest_no_updates()` persists `skipped_no_updates` and a sender would not claim/send it.

**Step 2: Run RED.**

```bash
python3 -m unittest -v tests.test_research_state.ResearchStateTests.test_daily_digest_claim_is_atomic_and_refuses_a_second_same_date_recipient
```
Expected: FAIL because the claim API/schema fields do not exist.

**Step 3: Implement one narrow migration/API.**

Extend `maintenance_digest_deliveries` with nullable, backward-compatible fields:

```sql
payload_json TEXT,
payload_sha256 TEXT,
telegram_message_id TEXT,
error_class TEXT,
error_text TEXT
```

Permit statuses: `pending`, `claimed`, `sent`, `failed`, `skipped_no_updates`.

Implement these methods with one transaction each:
- `claim_digest_delivery(digest_date, recipient, payload)` — inserts/claims only if no prior sent/claimed/no-op row exists; stores canonical JSON and SHA-256.
- `record_digest_sent(...)` — records Telegram message ID only after Telegram success.
- `record_digest_failed(...)` — stores a bounded non-secret error class/text.
- `record_digest_no_updates(...)` — records the no-op outcome without a model call.

Never include bot tokens or raw Claude responses in the database.

**Step 4: Run GREEN and commit.**

```bash
python3 -m unittest -v tests.test_research_state

git add scripts/research_state.py tests/test_research_state.py
git commit -m "feat: persist daily digest delivery claims"
```

### Task 3: Build a single-purpose daily digest runner

**Objective:** Turn a valid candidate list into one safe group message, or a recorded no-op.

**Files:**
- Create: `scripts/daily_group_digest.py`
- Modify: `tests/test_daily_group_digest.py`
- Reuse: `scripts/maintenance_posts.py`, `scripts/research_state.py`, `scripts/tg_config.json` (read token only; never print it)

**Step 1: Write failing tests using a temporary SQLite database and injected functions.**

```python
def test_runner_skips_provider_and_transport_when_no_candidates(self): ...
def test_runner_sends_one_snapshot_when_candidates_are_valid(self): ...
def test_runner_does_not_send_when_model_omits_one_expected_ticker(self): ...
def test_runner_does_not_resend_a_claimed_or_sent_digest(self): ...
def test_runner_rejects_non_group_or_wrong_sender_identity(self): ...
```

Inject `run_model`, `telegram_request`, `now`, and config paths. Tests must never invoke Claude or Telegram.

**Step 2: Run RED.**

```bash
python3 -m unittest -v tests.test_daily_group_digest
```
Expected: FAIL because runner module is absent.

**Step 3: Implement minimal runner boundaries.**

`daily_group_digest.py` must:
1. Resolve Taipei local date (allow `--date YYYY-MM-DD` only for controlled/manual runs).
2. Select 0–5 candidates through Task 1’s API.
3. On zero, persist `skipped_no_updates`, print a one-line no-op result, and exit 0.
4. Validate the configured Telegram token with `getMe` returns bot ID `8601109385`; validate `getChat` resolves `-1003863698623` and title `財經推播Agent` before any send.
5. Claim the recipient/date snapshot before calling Claude. If already claimed/sent/no-op, exit 0 without provider or transport calls.
6. Run exactly one `claude -p <prompt> --model sonnet` call only after a successful claim.
7. Require strict complete expected-ticker summaries; render `render_model_daily_digest()` with a group header stating the date and count, not a test label.
8. Enforce a 1,800-character maximum.
9. Send exactly one `sendMessage` only after all gates pass; persist message ID after API success.
10. On provider/parse/transport failure, record a bounded failure and send nothing. Do not silently fall back to a paid provider or retry automatically.

**Step 4: Run GREEN and full tests.**

```bash
python3 -m unittest -v tests.test_daily_group_digest
python3 -m unittest discover -s tests -p 'test_*.py' -q
python3 -m py_compile scripts/daily_group_digest.py scripts/maintenance_posts.py scripts/research_state.py
```

**Step 5: Commit.**

```bash
git add scripts/daily_group_digest.py scripts/maintenance_posts.py scripts/research_state.py tests/
git commit -m "feat: add idempotent daily investment group digest"
```

### Task 4: Validate runner behavior without spending or sending

**Objective:** Prove the runner’s real configuration gates without provider spend or a Telegram post.

**Files:**
- Create temporarily under an OS-safe `tempfile` directory: `hermes-verify-*.py`
- Inspect: `scripts/research_workflow.sqlite3`

**Step 1: Create and run a focused ad-hoc verifier.**

The verifier must use a temporary SQLite copy/fixture and injected no-op functions to prove:
- zero candidates causes zero model and Telegram calls;
- six candidates result in exactly five selected;
- a new-company deep-research item ranks first;
- group ID is `-1003863698623`;
- duplicate claim does not send twice.

**Step 2: Clean up and report it distinctly.**

Run it, remove it, and report it as **ad-hoc verification**, not suite green.

### Task 5: Install a dedicated 20:00 Asia/Taipei launcher

**Objective:** Schedule only the digest runner without creating a second research loop.

**Files:**
- Create: `deploy/com.panbear.investment-daily-group-digest.plist`
- Create: `scripts/run_daily_group_digest.sh`
- Modify: `OPERATIONS.md`
- Create outside repo during approved install: `~/Library/LaunchAgents/com.panbear.investment-daily-group-digest.plist`

**Step 1: Write a failing/static validation check.**

Add a test or validator which parses the plist and asserts:
- `StartCalendarInterval` is Hour `20`, Minute `0`;
- it invokes only `scripts/run_daily_group_digest.sh`;
- it does not invoke `research_loop.py --run`;
- standard output/error logs are under the Investment repository’s `scripts/` log directory.

**Step 2: Implement launcher files.**

`run_daily_group_digest.sh` must set an explicit safe `PATH`, `cd` to the repository, and execute only:

```bash
exec /usr/bin/python3 scripts/daily_group_digest.py
```

The plist must use the repo’s canonical paths, set `RunAtLoad` false, and not embed tokens or chat IDs.

Before installation, resolve the actual `claude` executable and verify the LaunchAgent environment can invoke it without printing credentials. If it cannot, stop and report the concrete environment gap; do not substitute an API provider.

**Step 3: Install only after explicit go-ahead for scheduler activation.**

Use `launchctl bootstrap`/`kickstart` as appropriate, then verify loaded state, calendar interval, log paths, and last-exit state. Do not force an extra production run merely to increment `runs`.

**Step 4: Commit application-side files.**

```bash
git add deploy/com.panbear.investment-daily-group-digest.plist scripts/run_daily_group_digest.sh OPERATIONS.md
git commit -m "feat: schedule daily investment group digest"
```

### Task 6: One controlled first 20:00 production observation

**Objective:** Confirm end-to-end behavior in the real scheduled window.

**Files:**
- Inspect: `scripts/daily_group_digest.log`, `scripts/daily_group_digest.error.log`
- Inspect: `scripts/research_workflow.sqlite3`

**Step 1: Before 20:00, confirm singleton and recipient state.**

- Check that only one digest launcher is loaded.
- Confirm there is no existing `sent`/`claimed` record for that date/group.
- Do not alter research-loop state or create a second loop.

**Step 2: Observe the scheduled result.**

Expected outcomes:
- Eligible updates exist: one group post, max five, with new companies first; ledger has `sent`, message ID, and payload SHA-256.
- No eligible updates exist: no group post, no Claude invocation, ledger has `skipped_no_updates`.
- Provider/Telegram failure: no group post and bounded `failed` ledger state.

**Step 3: Verify idempotency.**

Invoke the runner in safe status-only/dry-run mode (not a second send) and confirm it recognizes the existing sent/no-op state.

**Step 4: Commit only if all intended source changes are clean and reviewed.**

## Risks and decisions

1. **Post-20:00 scrapes:** they intentionally roll into the next day’s digest. This avoids a second same-day push.
2. **Model quota/output failure:** no fallback provider and no Telegram message; diagnostics stay non-secret. A manual retry command can be designed later, but is not automatic in this first version.
3. **New-company priority:** use persisted `companies.source == 'new'`, not CSV file timing. This accurately distinguishes newly discovered workflow records from existing CSV companies.
4. **Group bot:** this routine uses the verified Investment bot directly. It must not wait for or modify the separate Financial_Reporting_Bot deployment.
5. **Duplicate prevention:** the SQLite claim happens before model/transport work; a crash after claim becomes an explicit failed state instead of a blind duplicate retry.
6. **Cost control:** zero updates costs zero provider calls. Nonzero updates cost at most one Claude call per calendar day/group digest attempt.

## Acceptance criteria

- At 20:00 Asia/Taipei, the job evaluates that local day exactly once.
- Zero Taiwan/USA updates results in no model invocation and no Telegram push.
- One to five eligible updates produces exactly one group post, no longer than 1,800 characters.
- Newly discovered, deep-researched Taiwan/USA companies appear before maintenance-only updates.
- Existing CSV companies refreshed through maintenance are included only when they have material persisted changes.
- The exact selected payload and sent message ID are stored; a restart cannot duplicate a successful post.
- The first live scheduled observation proves either successful one-message delivery or a documented no-op/failure state.
