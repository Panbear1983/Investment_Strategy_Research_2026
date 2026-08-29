#!/bin/bash
set -euo pipefail

REPO_DIR="/Users/peter/GitHub/Investment_Strategy_Research_2026"

# The bot shells out to `claude` for the agentic answer path, and to `yt-dlp` for video
# metadata; both live outside a bare login PATH. The research loop wrapper exists for the same
# reason — a process started from the wrong shell loses them and fails only when someone asks a
# real question, which is the worst time to find out.
# /opt/homebrew/bin BEFORE the pip bin directory on purpose: a pip-installed yt-dlp from
# 2026-07-04 lingers there and returns HTTP 403 on every audio download, because YouTube
# changed and a six-week-old yt-dlp no longer works. Homebrew's is the maintained one.
export PATH="/opt/homebrew/bin:/Users/peter/.local/bin:/usr/local/bin:/usr/bin:/bin"

cd "$REPO_DIR"

# Only ONE process may poll a bot token: two pollers cause a 409 and one side silently starves.
# The script's own getMe preflight asserts the identity before claiming the poll.
# The interpreter is pinned for the same reason as the research loop — bare `python3` resolves
# to a different build depending on PATH order.
exec /Library/Frameworks/Python.framework/Versions/3.11/bin/python3 -u \
    scripts/botffet_bot.py >> "$REPO_DIR/scripts/botffet_bot.log" 2>&1
