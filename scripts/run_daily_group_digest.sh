#!/bin/bash
set -euo pipefail

REPO_DIR="/Users/peter/GitHub/Investment_Strategy_Research_2026"
export PATH="/Users/peter/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
# Config path override to avoid macOS quarantine on scripts/config.json
export ISR_CONFIG_PATH="/Users/peter/.config/investment_research/config.json"
cd "$REPO_DIR"
exec /usr/bin/python3 scripts/daily_group_digest.py --send
