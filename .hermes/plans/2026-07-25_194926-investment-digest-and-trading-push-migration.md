# Investment Digest and Trading-Push Migration Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Deliver a concise, model-written Taiwan/USA maintenance digest and safely migrate the trading-day group push from `@Panbear_show_bot` to `@Hermes_Investment_Strategy_bot` without duplicate or misrouted Telegram messages.

**Architecture:** Keep research extraction and digest rendering pure/read-only in the Investment repository. Generate the model summary once at the explicit delivery boundary, validate it against the selected tickers, and persist an immutable delivery snapshot before sending. Treat the trading-day bot migration as a separate sender-ownership change: identify the actual owning service first, change only its credential/recipient configuration, then conduct controlled group tests with deduplication and rollback.

**Tech Stack:** Python 3.11, SQLite workflow state, Telegram Bot API, Claude CLI or explicitly approved cloud fallback, `unittest`, existing `curl --resolve api.telegram.org:443:149.154.166.110` DNS workaround.

---

## Current context and assumptions

- The maintenance digest implementation is in `/Users/peter/GitHub/Investment_Strategy_Research_2026/scripts/maintenance_posts.py` with tests in `tests/test_maintenance_posts.py`.
- It now selects the five most recent material updates by maintenance-loop completion time and defaults to `Taiwan`/`USA`; legacy tests can deliberately opt out with `countries=None`.
- The model boundary is intentionally pure: `model_digest_prompt()`, `parse_model_digest_summaries()`, and `render_model_daily_digest()` perform no provider call or Telegram send.
- The authorized one-time Claude test finished with **no Telegram send**: its response did not yield a valid summary for every requested ticker, so the safety gate stopped it and removed the temporary script. The raw response was not retained, so its exact format is unknown. Do not issue another provider call until parser diagnostics and tests are in place.
- The direct Investment sender in `/Users/peter/GitHub/Investment_Strategy_Research_2026/scripts/apply_batch.py:41-57` reads `scripts/tg_config.json`; its token and chat ID must never be printed, committed, or copied between projects.
- **Validated source-code ownership:** `/Users/peter/GitHub/Financial_Reporting_Bot/scheduler.py:58-145` runs Taiwan weekday morning/closing reports, invokes `twse_daily_report.py`, and loads its schedule from `bot_config.json`. `/Users/peter/GitHub/Financial_Reporting_Bot/twse_daily_report.py:294-318` is the canonical delivery path; it sends each enabled `delivery.channels` Telegram destination using the configured token environment variable and chat ID. `/Users/peter/GitHub/Financial_Reporting_Bot/config_tui.py:1320-1385` manages those channels and persists `bot_config.json` atomically with a `.bak` rollback. This validates Financial_Reporting_Bot as the migration owner. Runtime bot identity still requires a safe `getMe` preflight; secrets must not be read or printed.
- Target values supplied by Peter are: new bot `@Hermes_Investment_Strategy_bot` / `8601109385`, target group `財經推播Agent` / `3863698623`. These must be verified with Telegram `getMe`, `getChat`, and `getChatMember` before any production routing change.

## Non-negotiable safeguards

1. No local Ollama calls; use a cloud/CLI provider only.
2. One daily digest only; never per-batch notifications for this new digest.
3. The model is called at delivery time once, not when the dashboard/preview is opened.
4. If model output is incomplete/invalid, do not fabricate text or send a partial digest. Record the failure and retain the deterministic preview for diagnosis.
5. Do not change existing Hermes profile routes, gateway ownership, unrelated bot configs, or legacy services while migrating the one trading-day sender.
6. Do not enable a scheduler, restart a service, alter credentials, or send to the group until Peter explicitly approves the reviewed plan and the preflight checks pass.
7. Keep the existing sender available as rollback until a full trading-day cycle succeeds with exactly one group post from the new bot.

## Proposed workflow

1. Repair and test the non-sending model-response validation boundary, then perform one explicitly approved direct-chat test with non-secret proof: provider outcome, target, message ID, selected tickers, and character count.
2. Turn that tested boundary into a canonical, idempotent daily-delivery path with snapshotting and a controlled provider fallback policy.
3. Separately discover the concrete trading-day producer/scheduler, then wire only that producer to `@Hermes_Investment_Strategy_bot` and the `財經推播Agent` group.
4. Use a preflight → one marked group test → scheduled run observation → rollback-ready rollout sequence.

## Tasks

### Task 1: Record the one-time model digest result without changing automation

**Objective:** Record the completed no-send result and add non-secret diagnostics before considering another approved direct-chat test.

**Files:**
- Inspect: `/Users/peter/GitHub/Investment_Strategy_Research_2026/scripts/maintenance_posts.py`
- Inspect only: temporary test-process output and Telegram result metadata
- Do not create a permanent sender in this task.

**Step 1: Record the completed test result.**

Observed result: Claude did not return a valid parsed summary for every selected ticker. The code raised before `sendMessage`, so no Telegram message was sent; the temporary script deleted itself.

**Step 2: Verify no side effect exceeded scope.**

- Confirm no scheduler was enabled.
- Confirm no group message was sent.
- Confirm the temporary live-test file was deleted after the attempt.

**Step 3: Obtain Peter’s visual feedback only after a later successful direct-chat test.**

Acceptance criteria: Peter sees one compact Traditional-Chinese post, one or two sentences per company, no `資料來源`, and no non-Taiwan/USA company.

### Task 2: Add an immutable model-summary delivery snapshot

**Objective:** Make model-produced daily digest text auditable and idempotent without calling a model during a preview.

**Files:**
- Modify: `/Users/peter/GitHub/Investment_Strategy_Research_2026/scripts/research_state.py`
- Modify: `/Users/peter/GitHub/Investment_Strategy_Research_2026/scripts/maintenance_posts.py`
- Modify: `/Users/peter/GitHub/Investment_Strategy_Research_2026/tests/test_research_state.py`
- Modify: `/Users/peter/GitHub/Investment_Strategy_Research_2026/tests/test_maintenance_posts.py`

**Step 1: Write a failing state test for a recipient/date summary snapshot.**

```python
def test_digest_summary_snapshot_is_immutable_per_date_and_recipient(self):
    first = self.state.save_digest_summary(
        '2026-07-25', '7512954760', 'claude-sonnet', 'prompt-hash', '摘要')
    second = self.state.save_digest_summary(
        '2026-07-25', '7512954760', 'claude-sonnet', 'prompt-hash', '不同摘要')
    self.assertEqual(second['summary_text'], '摘要')
```

**Step 2: Run the focused test and verify RED.**

Run:
```bash
python3 -m unittest -v tests.test_research_state.WorkflowLifecycleTests.test_digest_summary_snapshot_is_immutable_per_date_and_recipient
```

Expected: FAIL because no snapshot persistence API/schema exists.

**Step 3: Add the smallest SQLite table/API.**

Store only: Taipei digest date, recipient ID, provider/model label, prompt hash, validated summary JSON/text, created timestamp, and failure status/detail. Add a unique `(digest_date, recipient_chat_id)` constraint so a retry reuses the original successful output.

**Step 4: Add a renderer-level test that a persisted snapshot is reused rather than prompting again.**

Inject a callable `summarize(prompt) -> response` into a narrow delivery-preparation function. Count calls in the test and assert exactly one call across two preparation attempts.

**Step 5: Implement the injectable delivery-preparation function.**

Keep it in `maintenance_posts.py` and make it:
1. select the Taiwan/USA top five;
2. build one prompt;
3. call the injected provider once only when no successful snapshot exists;
4. parse and require every requested ticker;
5. persist the validated output; and
6. return rendered text or a structured non-send failure.

**Step 6: Run focused tests and commit.**

```bash
python3 -m unittest -v tests.test_research_state tests.test_maintenance_posts
```

Commit only after tests pass:
```bash
git add scripts/research_state.py scripts/maintenance_posts.py tests/test_research_state.py tests/test_maintenance_posts.py
git commit -m "feat: snapshot model maintenance digests"
```

### Task 3: Define provider/failure policy for the model digest

**Objective:** Avoid surprise spend and prevent a quota failure from producing a malformed or duplicated message.

**Files:**
- Modify: `/Users/peter/GitHub/Investment_Strategy_Research_2026/scripts/maintenance_posts.py`
- Inspect: `/Users/peter/GitHub/Investment_Strategy_Research_2026/scripts/research_loop.py:511-720`
- Test: `/Users/peter/GitHub/Investment_Strategy_Research_2026/tests/test_maintenance_posts.py`

**Step 1: Write failing tests for the three outcomes.**

```python
def test_prepare_model_digest_does_not_send_or_snapshot_partial_json(self): ...
def test_prepare_model_digest_uses_existing_snapshot_without_provider_call(self): ...
def test_prepare_model_digest_returns_explicit_quota_failure(self): ...
def test_parse_model_digest_accepts_a_json_code_fence(self): ...
def test_parse_model_digest_rejects_missing_tickers_with_nonsecret_diagnostic(self): ...
```

**Step 2: Verify RED with the focused test command.**

```bash
python3 -m unittest -v tests.test_maintenance_posts.MaintenancePostTests
```

**Step 3: Implement the approved policy.**

- Primary: one Claude Sonnet CLI call with no web-search tool.
- Do not silently switch to a paid API provider.
- On quota/invalid JSON: mark the prepared delivery as failed and do not send.
- Before deleting a failed temporary test artifact, retain only safe diagnostics: provider exit status, response character count, whether a JSON array/code fence was detected, expected ticker count, parsed ticker count, and missing/extra ticker symbols. Do not persist a raw model response or any secret.
- Normalize a single Markdown JSON fence (` ```json ... ``` `) before parsing, then require a JSON array with exactly the requested unique tickers. Do not attempt to extract arbitrary prose as JSON.
- A future retry must require a fresh explicit delivery attempt or an approved scheduler retry, but must not create a second message if a success snapshot already exists.

**Step 4: Re-run focused tests and commit.**

```bash
git add scripts/maintenance_posts.py tests/test_maintenance_posts.py
git commit -m "fix: guard model digest delivery failures"
```

### Task 4: Validate Financial_Reporting_Bot's live sender configuration

**Objective:** Complete runtime validation for the already-confirmed trading-day owner before changing its delivery channel.

**Files to inspect, no edits:**
- `/Users/peter/GitHub/Financial_Reporting_Bot/scheduler.py:58-145`
- `/Users/peter/GitHub/Financial_Reporting_Bot/twse_daily_report.py:294-318,1938`
- `/Users/peter/GitHub/Financial_Reporting_Bot/config_tui.py:200-213,1320-1385`
- the deployed `bot_config.json` and service environment, inspecting key names/structure only and never printing token values.

**Step 1: Record the validated execution path.**

The confirmed path is `scheduler.py` weekday morning/closing slot → `twse_daily_report.py` → `deliver_report()` → enabled Telegram `delivery.channels` (or the legacy environment fallback) → `_send_telegram()`.

**Step 2: Validate the running sender identity safely.**

Invoke `getMe` through the currently configured Financial_Reporting_Bot credential and record only its bot username/ID. Confirm it is `@Panbear_show_bot`; do not dump `TELEGRAM_BOT_TOKEN`, `.env`, or a service environment.

**Step 3: Record the active destination and service owner.**

Identify whether the current deployment uses an enabled `delivery.channels` entry or the `TELEGRAM_CHAT_ID` fallback; record only the target label/ID and launcher/service name. Confirm the existing daily report is the intended trading-day push.

**Step 4: Verify the built-in rollback point.**

Confirm `config_tui.py:200-213` will atomically write `bot_config.json` and preserve its `.bak` rollback before any channel switch.

**Step 5: Write a concise ownership record.**

Acceptance criteria: Financial_Reporting_Bot’s live scheduler, delivery channel mode, current bot identity, destination, and rollback file are all evidenced without exposing secrets.

### Task 5: Add sender configuration validation in Financial_Reporting_Bot

**Objective:** Make Financial_Reporting_Bot validate the new bot and group before it can send a production report.

**Files:**
- Modify: `/Users/peter/GitHub/Financial_Reporting_Bot/twse_daily_report.py`
- Modify: `/Users/peter/GitHub/Financial_Reporting_Bot/config_tui.py`
- Create/modify: `/Users/peter/GitHub/Financial_Reporting_Bot/tests/test_delivery.py` (or the repository’s existing delivery test module if one is discovered)

**Step 1: Write failing config-validation tests.**

Required cases:
- numeric group ID is accepted only when it is a Telegram group/supergroup;
- configured bot identity must equal `@Hermes_Investment_Strategy_bot`;
- the bot must be a member of group `3863698623` and have send permission;
- invalid/missing config fails closed before report generation/send;
- old `@Panbear_show_bot` config remains usable as an explicit rollback profile only.

**Step 2: Verify RED using the owner project’s canonical test command.**

**Step 3: Implement a single sender config object.**

Use environment/config indirection; never hard-code bot tokens or copy them into source. Keep old and new profiles mutually exclusive with an explicit `sender_profile` selection so both cannot post the same run.

**Step 4: Add a `--dry-run` path.**

It must render/report the selected bot profile, group ID, trading-day key, and message hash without calling Telegram.

**Step 5: Run focused tests, then commit within the actual owner repo.**

### Task 6: Telegram preflight for the new group bot

**Objective:** Validate Telegram access without publishing a trading report.

**Files:**
- No source change required unless Task 5 exposes a dedicated preflight command.

**Step 1: Run read-only Bot API checks using the new bot’s configured credential.**

- `getMe`: confirm bot identity is `@Hermes_Investment_Strategy_bot`.
- `getChat` for `3863698623`: confirm the expected group title/type.
- `getChatMember`: confirm the bot is present and permitted to post.

**Step 2: Check for common blockers.**

- group ID may need the Telegram supergroup `-100...` form rather than the supplied positive identifier;
- Privacy Mode may be irrelevant for sending but can affect bot command/update behavior;
- DNS must use the existing `curl --resolve` workaround if normal name resolution fails.

**Step 3: Stop on any mismatch.**

Do not modify production config or send a test until Peter confirms the verified target identity/title.

### Task 7: Controlled migration test and rollback rehearsal

**Objective:** Prove the new sender can post once to the intended group without duplicating the normal trading-day report.

**Files:**
- Modify only the confirmed owner’s sender config and test harness from Tasks 4-5.

**Step 1: Save a timestamped configuration backup with file permissions preserved.**

Do not print or commit token contents.

**Step 2: Change only the sender profile to the new bot and target group.**

Leave schedule disabled for this test.

**Step 3: Send one clearly labeled test post.**

The message must say it is a test, include a non-production idempotency key, and contain no trading action recommendation. Capture only the Telegram message ID and group title/type.

**Step 4: Verify exactly-once behavior.**

Run the same test command again; it must detect the same idempotency key and refuse the duplicate.

**Step 5: Rehearse rollback.**

Restore the old profile configuration without sending. Verify dry-run resolves to `@Panbear_show_bot` and no process reload/restart has happened yet.

**Step 6: Ask Peter to confirm the new-bot group message visually.**

### Task 8: Staged production rollout

**Objective:** Move one real trading-day run with observability and no duplicate pushes.

**Files:**
- Modify: only the confirmed owner project’s production config and launcher/service definition if required.
- Modify: its runbook/README with bot ownership and rollback instructions.

**Step 1: Enable the new sender for one named trading-day run.**

Use a date-keyed delivery ledger/idempotency key shared by the report generator and sender.

**Step 2: Observe the run.**

Verify:
- group receives exactly one post from `@Hermes_Investment_Strategy_bot`;
- old bot posts nothing for that same run;
- logs record bot profile, target group, delivery key, and Telegram message ID but no token;
- report content matches the pre-send hash.

**Step 3: Keep old sender as rollback for one successful cycle.**

No deletion of the old sender/profile until Peter explicitly approves retirement after observing the next planned cycle.

**Step 4: Document operational ownership and rollback.**

Include the exact service command, config location, safe dry-run command, idempotency key format, and DNS workaround.

**Step 5: Run final verification and commit per repository.**

For Investment digest changes:
```bash
cd /Users/peter/GitHub/Investment_Strategy_Research_2026
python3 -m unittest discover -s tests -p 'test_*.py' -q
python3 -m py_compile scripts/maintenance_posts.py scripts/research_state.py scripts/dashboard.py
```

For the trading-day owner: use its discovered canonical test/lint command plus a dry-run that performs zero Telegram network calls.

## Risks, tradeoffs, and decisions needed

1. **Provider quota:** Claude had a session-limit response shortly before this plan. The default safe behavior is no send, not an automatic paid fallback. Peter must explicitly approve any alternate provider/model and spending limit.
2. **Group identifier ambiguity:** `3863698623` was previously inaccessible to the Investment bot. Adding the new bot may resolve membership, but Telegram supergroup IDs often use a negative `-100...` value. Preflight determines the authoritative value.
3. **Bot ID vs bot token:** `8601109385` identifies the bot/account, not its secret credential. The migration must use the token stored only in the correct owner’s secret/config location.
4. **Runtime configuration remains unverified:** source code confirms Financial_Reporting_Bot owns the trading-day path, but a safe `getMe`/configuration preflight must still prove its running credential, active channel mode, and target before migration.
5. **Live code reload:** A running process will keep old configuration/code until an explicitly approved restart. Plan the restart only after dry-run and group-test success.
6. **Message-length tradeoff:** One model sentence per company is the mobile-first default; permit two sentences only when the model needs to preserve a material catalyst-versus-risk distinction. Cap each summary at 120 Chinese characters and the full Telegram message below 1,800 characters.

## Completion criteria

- One approved model digest test is visually confirmed: five or fewer newest Taiwan/USA companies, all model summary text in Traditional Chinese, one or two sentences/company, no `資料來源`.
- Model-generated content is snapshot-backed and cannot create duplicate same-day sends.
- Financial_Reporting_Bot’s running scheduler and selected delivery channel are proven with safe runtime preflight evidence.
- The new bot is verified as the intended group member and completes a marked, exactly-once group test.
- One real trading-day cycle posts only from `@Hermes_Investment_Strategy_bot` to `財經推播Agent`; the old sender remains rollback-capable until Peter approves retirement.
