# Sector steering, and YouTube as conversation material

## Why this exists

The scraper had no sector-selection logic. Direction came from three things: one frozen sentence
of themes inside the nomination prompt, a country rotation of twelve entries (only two of which
named an industry at all), and a counter. Each discovery attempt took `counter % 12` as its
slice. Nothing consulted what the corpus already held, so the collection drifted lopsided —
483 semiconductor companies against three in satellites — and nothing could see it.

There was also a silent fault. The three work queues filtered `industry` with **equality**, while
`screen.select()` and the MCP tools have always matched it as a **substring**. Because the column
holds ~1,690 free-text labels for ~2,400 companies, equality found 59 of 503 semiconductor
companies and **0 of 103** defence companies. A slot focused on defence matched nothing, reported
"No matching candidates", and skipped — indistinguishable from a drained queue.

## What changed

**1. The filter agrees with the reader.** `research_state.focus_filters()` is now the single
source of that clause, shared by `research_queue`, `maintenance_screen_queue` and
`maintenance_update_queue`. Industry is a substring match via `INSTR` (not `LIKE`, so a term
containing `%` is not a wildcard). Country stays exact — it has a canonicaliser and a fixed alias
table, so equality is correct there.

**2. A standing focus that outlives the day.** Per-slot focus lives in `daily_slots` and is
reseeded every midnight, so it cannot express "keep hunting satellites until I say stop". The
standing focus lives in `workflow_meta` and is read at slot-claim time by
`research_loop.effective_focus()`. A focus written onto a slot still wins; the standing focus
fills any slot that has none. A count-scoped focus is decremented only after a batch actually
ran, so skips and failures never burn a budgeted batch.

**3. A panel that shows the shape.** `industry_focus.theme_coverage()` measures the corpus
against a curated theme vocabulary (`industry_themes` in config, seeded from the theme sentence
already in the nomination prompt). It reports the ~29% that match no theme, and the several
hundred that match more than one — so the Held column deliberately does not sum to the corpus.
Press **i** in the dashboard; it is also in `./dash --report`.

**4. Two ways for a decision to reach the scraper.**

## The hook, and why it is shaped this way

`botffet.py` records that an earlier revision accidentally shipped Bash/Read/Write/WebFetch to
the model with permissions bypassed, and that `SAFETY_FLAGS` plus `FORBIDDEN_FLAGS` now enforce
the boundary with tests pinning it. The reason given: the text it reads is untrusted, so the
model may only emit words about it.

Feeding in YouTube transcripts makes that reasoning stronger. A transcript is written by a
stranger. If the model could set research direction, a sentence inside a video could aim the
research budget.

So:

- **`/focus` is typed by a human.** It is parsed and executed on botffet's command path, in
  plain Python, with no model call. The model gains nothing; `SAFETY_FLAGS` is untouched and its
  tests pass unchanged. Works identically in the dashboard chat and on Telegram.

      /focus                  show the current standing focus
      /focus 低軌衛星          set it until cleared
      /focus 低軌衛星 3        set it for the next 3 batches
      /focus clear            clear it

  Every reply states what the sector actually matches, so a typo is caught when it is typed
  rather than hours later as a skipped slot.

- **The model may only propose.** It can end an answer with
  `FOCUS-PROPOSAL: <sector> | <reason>`. Plain Python lifts that line out, strips it from what
  the reader sees, and files a row in `focus_proposals`. **Nothing changes.** The suggestion
  appears in the dashboard and on the sector panel; one keypress accepts it, and only that
  keypress writes a focus. Repeating a suggestion refreshes it rather than filling the tray.

  The screener MCP server stays exactly what its docstring claims: read-only, opened `mode=ro`,
  with no write primitive in any schema. `tests/test_screener_mcp.py` asserts this.

## The video engine

`scripts/video_intel.py` is deliberately inert — it decides nothing, and a test asserts the
module never calls anything that writes a focus.

    python3 scripts/video_intel.py --add <url> [<url> ...]
    python3 scripts/video_intel.py --fetch
    python3 scripts/video_intel.py --summarise [--dry-run]
    python3 scripts/video_intel.py --status
    python3 scripts/video_intel.py --show <video-id>

Or press **y** in the dashboard: paste links, Fetch, Summarise, Read, then **Discuss with 爸菲特**,
which opens the chat with a question about that video already typed.

**Fetching costs nothing** — no API key, no quota. The transcript comes from YouTube's own
caption track via `youtube_transcript_api`, with `yt-dlp` as fallback and for metadata.

Two traps worth remembering, both found the hard way:

- Never use a wildcard in `--sub-langs`. `"en.*,zh.*"` matches ~100 auto-translated tracks and
  trips `HTTP 429` within seconds. Exact codes only.
- Prefer what was **said**. A popular video carries dozens of community translations, so asking
  for Chinese first returns a machine translation of an English talk — and quotes then get
  verified against text nobody spoke. `_pick_transcript()` uses the auto-generated track to
  identify the spoken language, prefers a human transcript in that same language, and treats
  Chinese and English only as fallbacks.

**Summarising** spends one Gemini call per ~12k-character chunk, through
`research_loop.llm_call`, so it lands in the same daily per-provider budget as the research loop
and shows up in the provider panel. `video_summary_daily_cap` stops a fifty-link paste eating
the research budget.

**The quote rule is the safeguard.** Every company or sector note must carry a verbatim span
from the transcript. A note whose quote is not literally present is dropped before it reaches
the database, and the count of dropped claims is reported. Quotes carry a timestamp, so each one
links back to the moment it was said.

爸菲特 reads these through two read-only tools, `video_search` and `video_brief`, whose
descriptions tell it to treat the material as third-party commentary and to name the channel.

## Config keys added

All optional, all read with `.get()` defaults, so `REQUIRED_CONFIG_KEYS` validation is unchanged.
Type-checked in `CONFIG_TYPES` because `industry_themes` is iterated.

| key | default | what it does |
| --- | --- | --- |
| `industry_themes` | 32-term list | the vocabulary coverage is measured against |
| `industry_thin_floor` | 10 | below this a theme is flagged starved |
| `industry_window_days` | 7 | the "worked recently" window |
| `video_summary_daily_cap` | 20 | most videos summarised in one day |

Kept identical in `scripts/config.json`, `~/.config/investment_research/config.json` and
`scripts/config.json.clean`, per `OPERATIONS.md`.

## Operating notes

- **Code changes need a loop restart.** The loop reloads *config* every pass, but not Python.
  The substring filter, the standing focus and the new diagnostics only take effect once the
  research loop is restarted.
- New tables (`focus_proposals`, `video_sources`, `video_notes`) are created by
  `init_schema()`'s `CREATE TABLE IF NOT EXISTS`, which the dashboard and the loop both run.
- A focus that matches nothing is no longer silent: the slot detail now says
  "Focus X matched none of the N pending companies" rather than the generic message.

## Discovery pacing (added after the sector work)

Discovery is what finds *new* companies, and it was throttled far below what the loop could
research: six attempts a day at twenty nominations each, against roughly 350–400 a day of
research capacity. That is why 2,270 companies were researched and one was pending.

Worse, `daily_empty_discovery_limit` was doing two jobs at once and counted **globally**: two
unproductive attempts anywhere stopped discovery for the rest of the day. Japan and South Korea
sit at positions 0 and 1 of `discovery_focus_rotation` and are both picked clean, so the first
two attempts of each day returned nothing and shut discovery down having used 2 of its 6.

Three changes:

1. **A per-slice ledger** in `loop_state.json` under `discovery_slices`. A slice that returns
   nothing new twice in a row rests for seven days, doubling on each further disappointment up
   to eight weeks, and waking immediately the moment it produces something. `discovery_focus()`
   walks the rotation skipping resting slices. If every slice is resting it takes the one whose
   rest ends soonest, rather than stalling. An absent ledger behaves exactly as before.
2. **`daily_empty_discovery_limit` is now only a circuit breaker** for a genuinely broken
   provider, raised from 2 to 6. Saturation is the ledger's job.
3. **The daily ceiling is 24, not 6**, which finally exceeds research throughput. Each attempt
   is one call, so this is about 24 calls out of a 300-call gemini budget.

Note the cursor arithmetic: `record_discovery_attempt()` increments the cursor and
`discovery_focus()` reads `cursor - 1` then advances past whatever it chose. Both halves are
needed — calling `discovery_focus()` twice without an attempt between legitimately re-asks the
same slice.

### Nominations no longer come back as prose

`cli_extra_args` runs the Antigravity CLI with `--mode plan`. In that mode a nomination call
sometimes wrote itself an `implementation_plan.md` and replied with prose about it, or tried to
execute a script and died on a denied permission prompt. Claude rescued the slot each time, but
the gemini call was wasted.

`agy --json-schema` removes the failure class. Two things had to be discovered by trying:

- The schema **root must be an object**. A top-level array schema is rejected outright —
  status ERROR, zero tokens, in 1.3 seconds. So nominations travel as
  `{"nominations": [...]}` and are unwrapped on arrival.
- The envelope's `response` string carries the model's own scratch keys (`toolAction`,
  `toolSummary`); `structured_output` is the schema-validated part, and is what is read.

A schema run is not in planning mode: `cli_extra_args` is replaced rather than extended for
those calls, with `cli_json_args` (default `--print-timeout 6m0s`) supplying the rest. Only the
gemini path uses this; Claude and Codex are still asked for a bare array in words.

Verified live: five valid Japanese semiconductor nominations, one call, 17 seconds, no plan
files.

### Config

| key | was | now |
| --- | --- | --- |
| `daily_discovery_attempt_budget` | 6 | 24 |
| `daily_empty_discovery_limit` | 2 (saturation + breaker) | 6 (breaker only) |
| `discovery_slice_strikes` | — | 2 |
| `discovery_slice_cooldown_days` | — | 7 |
| `discovery_slice_cooldown_cap_days` | — | 56 |
| `cli_json_args` | — | `["--print-timeout", "6m0s"]` (default, not stored) |

## YouTube links inside Telegram

Both allow-listed users can drop a YouTube link straight into
@Hermes_Investment_Strategy_bot and get it turned into notes, then discuss it there. A bare link
is enough — no command needed — and `/video <url>` does the same thing.

**Where the logic lives.** In `botffet.py`, on the command path, so Telegram, the dashboard chat
and the CLI behave identically — the repo's "no second brain" rule. Only the two-message
presentation lives in `botffet_bot.py`, because sending two messages is something only Telegram
can do.

**Fetching is separate from summarising, deliberately.** `video_intel.ensure_transcript()` costs
nothing; `summarise_source()` spends model calls. Splitting them is what lets the bot discover
"this video has no captions" and say so *before* claiming any of the user's daily allowance.
`process_video()` wraps both for the dashboard and the CLI.

**One video per request.** `fetch_pending()` and `summarise_pending()` walk the whole queue,
which is right for one person working through their own list and wrong for a shared chat: if two
people each drop a link, whoever triggers first would process both — the wrong person's video
against the wrong person's allowance. The chat path uses the single-video functions.

**The allowance.** Five videos per person per day (`video_daily_cap_per_user`), counted through
the existing `claim_quota()` under the key `<chat_id>:video`. That key suffix keeps video spend
separate from the 30-question chat cap, and reuses the claim-inside-the-UPDATE that makes either
hold under concurrency. `video_summary_daily_cap` remains the outer backstop.

This matters because summarising is **the first thing a chat user can do that spends money** —
every other command reads. Note that `access: read_only` in `telegram_users.json` is *not*
enforced by this bot; only `enabled` is. The per-person allowance is the control, not that field.

**Timing.** A video is one model call per ~12k characters, so an hour-long video is around six
calls — far beyond the 200-second question timeout. `VIDEO_TIMEOUT` (15 minutes) applies to video
turns only, and the bot acknowledges the link immediately so a long wait does not read as a dead
bot. The existing per-chat `_in_flight` lock stops anyone queueing a second video behind it.

**Language.** Every message in this flow is Traditional Chinese, matching the summaries and
爸菲特's own voice. A video with no caption track is reported as its own plain message — it names
the reason, suggests trying a different video, and states explicitly that it did not consume one
of the day's five. It is a normal outcome, not a fault, and must never read like a crash. The
bot's older English messages (the allow-list refusal, the question timeout) are unchanged.

**Restarting the bot.** `scripts/run_botffet_bot.sh` now exists, matching the research loop's
wrapper: it pins the 3.11 interpreter, exports a PATH that includes `claude` and `yt-dlp`, and
appends to `scripts/botffet_bot.log`. Only one process may poll the token — stop the old one
first, or the second gets a 409 and one side silently starves. The bot is a separate long-running
process and picks up no code change until restarted.

## Videos with subtitles switched off

Some uploaders disable captions entirely — routine on Taiwanese TV finance channels, which is
exactly the content this engine exists for. `pnDmk4ARaVE` (東森精選新聞《寶傑怎麼說》, 33 minutes,
TSMC supply chain) failed this way on 2026-08-28. Verified directly: subtitles are disabled, not
missing, so it was never a bug on our side.

Those videos are now transcribed **locally on this Mac**, as the last resort inside
`fetch_transcript()` — caption API first, yt-dlp subtitles second, audio only when both find
nothing. It spends **zero** Gemini calls; it costs local time instead. No audio leaves the
machine.

`transcribe_audio()` returns the same `[(start_seconds, text)]` shape `parse_vtt()` does, which
is the point: `flatten()`, the char-offset index, the verbatim-quote check and the timestamped
links into the video all keep working unchanged on transcribed audio.

The reply says when text came from audio rather than captions, because transcription is very good
but not perfect on names, and a reader should know which kind of text they are reading.

### What had to be installed, and why

Three things, two of which were not anticipated:

| what | why |
| --- | --- |
| `whisper-cpp` 1.9.2 (brew) | the transcriber. Bottled, pulls in `ggml` and `sdl2-compat`. |
| `ggml-large-v3-turbo.bin` (1.6GB) | the model. Kept permanently in `~/.local/share/whisper-models/` — it is what does the listening. Downloaded once. |
| `deno` (brew) | yt-dlp warns that YouTube extraction without a JavaScript runtime is deprecated. Installed while chasing the 403 below; it was not the cause, but the warning is real and it removes it. |
| `yt-dlp` 2026.08.19 (brew) | **this was the actual fix.** |

**The 403.** Audio download failed with `HTTP Error 403: Forbidden` on every format, while format
*listing* worked fine. The cause was a pip-installed yt-dlp from 2026-07-04 — six weeks old, and
YouTube had changed. Homebrew's current build downloads without complaint.

That old pip copy still sits in `~/Library/Python/3.11/bin` and still returns 403.
`scripts/run_botffet_bot.sh` therefore puts `/opt/homebrew/bin` **first** on PATH. Anything else
invoking `yt-dlp` from a shell that finds the pip copy first will hit the same 403. Removing or
upgrading that copy would settle it permanently; it was left alone because other projects may
reference it.

### Audio is temporary, the model is not

The model is downloaded once and kept — re-downloading 1.6GB per video would be absurd. The audio
is a working file: ~60MB for a 33-minute video (16kHz mono WAV, measured), ~220MB for two hours,
removed by `tempfile.TemporaryDirectory` whatever happens, including on failure. A test asserts
the directory is gone even when the download throws.

### Cost: chunk size is the real lever

The Antigravity budget counts **calls, not tokens**. Chunking was 12,000 characters, which was
needlessly cautious. Measured on the two stored transcripts: **7 calls at 12k, 3 at 40k**, with
no quality loss and fewer chunk boundaries for `dedupe_notes()` to clean up after. A 33-minute
video drops from 3 calls to 1. Tunable via `video_chunk_chars`.

### Gemini first, Claude only when dry

`summarise_video()` catches `BudgetExhausted` and finishes the video on the next provider in
`provider_fallback_order`, recording which one did the work. Gemini leads deliberately:
Antigravity is a separate subscription that competes with nothing else, whereas Claude's real
ceiling is the Max plan's rolling window — shared with the chatbot, the research loop's Claude
slots, and Peter's own Claude Code sessions. Three separate tallies draw on that one
subscription, so `loop_state.json`'s Claude count understates the true usage.

Since 2026-08-30 there is a tier between them: `agy_claude`, Claude Sonnet served by the same
Antigravity CLI (`agy --model claude-sonnet-4-6`). Antigravity meters Claude and Gemini as
separate pools, so when the Gemini bank is dry this is a fresh bucket that costs Peter's own
Claude subscription nothing. It is fallback-only (no slots, 60 calls/day) because Antigravity's
Claude window is reported to drain far faster than Gemini Flash and is shared with Claude use in
the Antigravity IDE. The research loop's ranking reaches it before `claude` while it has budget
and is not cooling down; `summarise_video()` inherits the same order.

## Long-term memory

The chat already had a working memory: the last six turns verbatim, and a rolling summary that
older turns are folded into. It kept per-turn context flat, but it also forgot. The summary is
rewritten on every compaction and told to stay under 200 words, so a fact from March is gone by
June; and `chat_history` is trimmed to 60 rows, so nothing older could be looked up either. For
a bot one person talks to for years, that is a conversation partner with no past.

Two tiers were added. Neither grows what a turn sends.

**A profile of standing facts** (`chat_profile`). The compaction call now returns two sections
instead of one: the rolling notes as before, and the complete revised list of facts that should
still be true months from now — holdings, risk appetite, how they like answers, what they follow,
decisions they said they made. The model is shown the existing list and asked to merge, correct
and drop, so a contradiction in a new turn replaces the stale fact rather than sitting beside it.
Plain Python splits the reply on the `### Durable facts` heading. It is capped at 40 lines and
2,000 characters by `profile_put()` whatever the model returned, and it only ever moves from one
non-empty list to another: a reply that lost the section, or came back with "(none)", leaves the
profile untouched. The previous version is kept in the `previous` column. Cost: zero extra
calls — it rides the compaction call that was already being made.

**An archive of every turn** (`chat_archive`). `history_append()` writes each turn twice: the
trimmed working copy and an untrimmed one. The archive is never sent to the model. Instead the
screener MCP server gained `memory_search`, a substring search over it that returns a window of
text around each hit with the date and who said it. Whose archive it reads is decided by the
`ISR_CHAT_ID` environment variable `agentic_provider()` sets when it spawns the server — not by a
tool argument, so the model has no parameter through which to ask for another person's history.
Spawned without it (a stray launch), the tool answers nothing and says why. The prompt tells the
model the tool exists and when to reach for it ("the company we talked about last time").

Existing history was backfilled: on first open after the upgrade, each chat's surviving
`chat_history` rows become the first pages of its archive, once. Dad's four exchanges from
August 10–11 are in there.

**`/reset` changed meaning.** It now clears the window and the summary only — a fresh thread,
not a stranger. `/reset all` also drops the profile and the archive. `/memory` shows what is
held, in Traditional Chinese, without a model call.

Per-turn context is therefore: persona + profile (≤2,000 chars) + summary (≤2,500) + six turns
(≤1,500 each) + the question. Fixed, however long the relationship runs; anything older is one
tool call away.
