# Investment Strategy Research 2026

A self-running research pipeline that keeps a 100,000-company investment database
up to date, using multiple AI models in rotation and delivering results over
Telegram.

## What it does

The database (`datasets/Global_100k_Investment_Database.csv`) tracks companies
worldwide — mostly Taiwan and US listings — classified by investment theme
(e.g. AI server supply chain, advanced packaging, silicon photonics, edge AI,
EV ecosystem, energy storage) and by investment horizon (short/3–6mo,
mid/1–3yr, long/3–5yr). Each entry is tagged as a lead player, a supporting
company, or a secondary supplier within its theme, with capital size, what the
company does, and its specific role in the supply chain or order book. Sample
research output (in Traditional Chinese) lives under `docs/`.

Companies start as placeholders and get filled in over time by an autonomous
research loop that runs around the clock.

## How the research loop runs

`scripts/research_loop.py`, supervised by launchd as
`com.panbear.investment-research-loop`, runs continuously and wakes on a fixed
daily schedule of 20 slots roughly 72 minutes apart (`scripts/config.json` →
`schedule`). Each slot processes a batch of up to 20 companies
(`batch_size`). Slots alternate between two modes:

- **Maintenance** slots (8 of the 20) re-check and refresh companies already
  in the database.
- **Deep research** slots (the remaining 12) research new or still-incomplete
  companies.

Each slot calls out to one of four AI providers, in a fallback chain so a
quota limit or outage on one doesn't stall the loop: **Gemini** (via the
Antigravity CLI, on subscription), **Claude via Antigravity** (a separate
quota bucket from the Gemini one), **Claude** (direct, Peter's own
subscription), and **Codex**. Each provider has its own daily call budget, and
the loop reloads its configuration on every pass so limits and schedule
changes take effect without a restart.

## Delivery

- A daily group digest and a conversational Telegram bot (`scripts/botffet_bot.py`)
  summarize research progress and answer questions about the database, for
  Peter and one other family member.
- The bot can also summarize a shared YouTube link into text.
- A terminal dashboard (`scripts/dashboard.py`, launch via `./dash`) shows
  live loop status, provider quota usage, and lets Peter pause/resume
  individual pieces of the pipeline.

## Running it / operations

See `OPERATIONS.md` for configuration, the provider fallback chain, process
supervision, logging pitfalls, and known operational gotchas. Tests are under
`tests/` (pytest).
