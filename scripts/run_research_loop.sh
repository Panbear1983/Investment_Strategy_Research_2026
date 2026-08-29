#!/bin/bash
set -euo pipefail

REPO_DIR="/Users/peter/GitHub/Investment_Strategy_Research_2026"

# launchd starts jobs with a minimal PATH. The loop shells out to claude/agy/codex,
# all of which live in ~/.local/bin, so it has to be exported explicitly.
export PATH="/Users/peter/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
# Config path override to avoid macOS quarantine on scripts/config.json
export ISR_CONFIG_PATH="/Users/peter/.config/investment_research/config.json"

cd "$REPO_DIR"

# caffeinate -i keeps the Mac awake through the overnight schedule. It runs python as
# a child and exits with its status, so launchd's KeepAlive still sees the real exit.
# The interpreter is pinned: bare `python3` resolves to 3.9.6 or 3.14.6 depending on
# PATH order, and the loop is only ever exercised on the 3.11 framework build.
# Explicitly pass ISR_CONFIG_PATH to python to ensure it's visible at import time.
# Always append to scripts/loop.log, whoever starts us. When Hermes launches this script its
# stdout is a pipe, so loop.log froze at 12:01 on 2026-08-13 while the loop was in fact running
# and failing — the file looked idle rather than broken, which is why a config regression went
# unnoticed for two hours. Redirecting here makes the log truthful regardless of the launcher.
exec /usr/bin/caffeinate -i \
    /usr/bin/env ISR_CONFIG_PATH="/Users/peter/.config/investment_research/config.json" \
    /Library/Frameworks/Python.framework/Versions/3.11/bin/python3 -u \
    scripts/research_loop.py --run >> "$REPO_DIR/scripts/loop.log" 2>&1
