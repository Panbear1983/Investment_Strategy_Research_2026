# Operations

## Config (read this before editing any config)

- The loop reads `ISR_CONFIG_PATH`, falling back to `scripts/config.json`. Both that file and
  `~/.config/investment_research/config.json` are kept identical and valid; the env-var copy
  exists because the repo copy sits under `~/Desktop` and hit a macOS `PermissionError`.
- **`scripts/config.json.clean` is the known-good reference (36 keys since 2026-08-30).** Restore from it.
- Four providers since 2026-08-30: `gemini` (agy, Gemini Flash), `agy_claude` (the same `agy`
  binary with `--model` = `agy_claude_model`, default `claude-sonnet-4-6` — Antigravity meters
  Claude and Gemini as separate pools, so this is a fresh bucket when the Gemini bank is dry),
  `claude` (Peter's own subscription), `codex`. `agy_claude` has NO schedule slots: it is reached
  only through `provider_fallback_order`, budget 60 calls/day, and its 5-hour Antigravity window
  is shared with any Claude use inside the Antigravity IDE. Do not give it slots without Peter's ask.
- `_reset_from_message` understands Antigravity's relative form (`Resets in 75h59m52s`) since
  2026-08-30. Before that a multi-day Gemini outage was cooled down for 30 minutes and re-probed
  every slot (3 agy calls + 180 s of backoff each time).
- `load_config` now REFUSES to start on a config missing any of `REQUIRED_CONFIG_KEYS`
  (`batch_size, daily_call_budgets, delay_seconds, max_retries, model, schedule, claude_args`)
  or with an empty `schedule`. In-loop reloads never crash the daemon: on a bad or unreadable
  file it alerts once and keeps running on the last good config.
- **The claude tool boundary is enforced in code** (`MANDATORY_CLAUDE_FLAGS` /
  `enforce_claude_flags`), not config. Config may add flags but cannot remove or weaken
  `--tools WebSearch` / `--setting-sources ''`.
- Why all of the above exists — 2026-08-13: a config swap replaced 18 operational keys with a
  different schema (a staging/redesign draft; its paths like `datasets/workflow.db` do not
  exist). The loop kept running: slots died one by one on `KeyError` — `'model'` and
  `'batch_size'` appear verbatim as skip reasons in `daily_slots` — while `claude_args` and
  `schedule`, which fail via `.get()` defaults, silently dropped the tool boundary and would
  have created **zero slots** the next day.

## Process and logs

- Research loop state is owned by `scripts/research_loop.py`; do not start a second `--run` instance.
- Singleton lock: `scripts/.research_loop.lock`.
- The loop is **hand-started or Hermes-started**, not launchd-supervised. Restart with
  `nohup /bin/bash scripts/run_research_loop.sh >/dev/null 2>&1 &` — the wrapper sets
  `ISR_CONFIG_PATH`, pins the 3.11 interpreter, and appends to `scripts/loop.log`.
- **`loop.log` is only truthful when started via that wrapper.** When Hermes launches the loop
  directly its stdout is a pipe, so `loop.log` freezes and the loop looks idle while it is in
  fact running — and failing. On 2026-08-13 that hid a config regression for two hours. If
  `loop.log` is stale but a process exists, trust the **database** (`daily_slots`,
  `batch_runs`), not the file.
- **launchd supervision is BLOCKED by macOS TCC — do not load the staged job.** A LaunchAgent
  does not inherit Terminal's Desktop grant, so any job whose program lives under `~/Desktop`
  is denied before it can execute. Proven by `com.panbear.investment-daily-group-digest`, which
  fired at 20:00 on 2026-07-26 and died with exit 126:
  `/bin/bash: .../scripts/run_daily_group_digest.sh: Operation not permitted`.
  The same failure hits `com.panbear.improve` (silently stopped 2026-07-20) and would hit the
  research loop identically. Jobs outside `~/Desktop` (`cmux-relay`, `daily`) exit 0.
  The real fix is to move the repo out of `~/Desktop`, or grant the launchd context Full Disk
  Access — not to retry the bootstrap.
- Staged but deliberately NOT loaded: `deploy/com.panbear.investment-research-loop.plist` and
  `scripts/run_research_loop.sh` (plus a copy in `~/Library/LaunchAgents/`). They are validated
  and correct; they are blocked only by the TCC issue above. Do not `launchctl bootstrap` them
  until the repo lives outside `~/Desktop`.
- The pid recorded in `.research_loop.lock` is unreliable: a refused second instance truncates the file before it fails `flock`. The flock is authoritative; use `launchctl list` or `pgrep` to identify the live process.
- The dashboard may halt the loop through its persisted control state; process existence is not proof that work is enabled.
- Investment Telegram routes are owned by the orchestrator Hermes gateway. **Routes must be
  written to `~/.hermes/profiles/orchestrator/config.yaml`** — the running gateway
  (`ai.hermes.gateway-orchestrator`) never reads the root `~/.hermes/config.yaml`, yet the
  dashboard's "Apply routes to Hermes" and `dad_assistant` both write there, so route edits
  made through them silently have no effect.
- Scheduled research-batch notifications use bot **8989213802** through the orchestrator
  profile selected by `ORCHESTRATOR_HERMES_HOME`. `hermes send` reads that profile's active
  `TELEGRAM_BOT_TOKEN` for every one-shot delivery, so no research-loop restart is required
  after a token change. The destination remains Peter's configured Telegram chat.
- Bot 8601109385 (`@Hermes_Investment_Strategy_bot`) is reserved for the separate investment
  database/daily-digest path and currently has **zero** `getUpdates` consumers — notifier sends
  only call `sendMessage`, which does not contend. Starting `scripts/antigravity_telegram_bot.py`
  would make it the sole poller; running two pollers on one token causes the 409 that retired
  `PanbearClaw_bot`.
- The roster (`scripts/telegram_users.json`) routes users to profile `researcher`, which mounts
  `dad_reports:rw` and would fail `setup_reader_profile.assert_strict_readonly`. Read-only
  research users belong on `investment-research-reader`.
- Do not repurpose or delete `dad-investment` or `investment-research-reader` until the observation/retirement decision is recorded.
- Provider/model changes require a no-spend static audit first. Do not restart the loop merely to test an edit.

## Sector steering (see docs/sector_focus_and_video.md)

- The loop reloads **config** every pass but never reloads **code**. The industry-filter fix, the
  standing focus and the focus-miss diagnostic all take effect only after a restart.
- Config gained four optional keys (`industry_themes`, `industry_thin_floor`,
  `industry_window_days`, `video_summary_daily_cap`), so the known-good reference is now 26 keys,
  not 22. All three copies were updated together.
- The industry filter on all three work queues is now a SUBSTRING match, matching `screen.select`.
  Equality found 0 of 103 defence companies, so a focused slot skipped in silence. Country
  deliberately stays exact.
- The standing focus lives in `workflow_meta` under `standing_focus`. Clear it with `/focus clear`
  in the chat, or from the sector panel (`i`).
- 爸菲特 can only PROPOSE a direction; proposals land in `focus_proposals` and change nothing until
  accepted in the dashboard. `screener_mcp.py` remains read-only and its tests assert that — do
  not add a write tool there.
- `screener_mcp.py` now honours `ISR_WORKFLOW_DB` like every other script, so it and the loop
  always read the same database.
- Video summarising spends from the gemini daily budget. Fetching transcripts costs nothing.

## Discovery pacing

- Discovery's daily ceiling is now 24 attempts (was 6), which finally exceeds research capacity.
- `daily_empty_discovery_limit` changed MEANING: it is now only a broken-provider circuit
  breaker (6), not a saturation guard. Saturation is handled per slice.
- `loop_state.json` gained `discovery_slices`: a slice that finds nothing twice rests 7 days,
  doubling to a 56-day cap, waking the moment it produces. Read it when diagnosing a flat corpus.
- Gemini nomination calls now run with `agy --json-schema` and NOT in `--mode plan`. The schema
  root must be an object — a top-level array schema is rejected with status ERROR and 0 tokens.
- Claude and Codex nominations are unchanged (asked for a bare array in words).

## 爸菲特 Telegram bot

- Restart with `nohup /bin/bash scripts/run_botffet_bot.sh >/dev/null 2>&1 &`. The wrapper pins
  the 3.11 interpreter and exports a PATH containing `claude` and `yt-dlp`; a bot started from a
  bare shell loses those and only fails when someone asks a real question.
- Only ONE process may poll bot 8601109385. Stop the old one before starting a new one.
- The bot picks up no code change until restarted. It ran unrestarted from 2026-08-10 to 08-28.
- Telegram users can now spend model budget by dropping a YouTube link (5 per person per day,
  `video_daily_cap_per_user`). `access: read_only` in the roster is NOT enforced by this bot —
  only `enabled` is. The per-person allowance is the real control.
- Memory is three tiers per chat_id, all in `research_workflow.sqlite3` (see
  docs/sector_focus_and_video.md, "Long-term memory"): `chat_history` (6-turn window) +
  `chat_summary` (rolling notes) as before, plus `chat_profile` (standing facts, ≤40 lines,
  revised in the same compaction call — `previous` column holds the version before) and
  `chat_archive` (every turn, never trimmed, searchable by the model via the read-only
  `memory_search` MCP tool, scoped by the `ISR_CHAT_ID` env var botffet sets when spawning
  the server). `/reset` clears window + summary only; `/reset all` clears everything; `/memory`
  shows what is held. Per-turn context is still fixed-size — the archive is fetched on demand.
- Tests no longer touch live state: `tests/conftest.py` redirects `research_loop.STATE_PATH` and
  `botffet.QUOTA_DB`, and `tests/test_live_state_isolation.py` fails if that protection is removed.
  Before this, running the suite overwrote the dashboard's 爸菲特 conversation and the loop's
  daily call counters.
