#!/usr/bin/env bash
# Launch the investment research dashboard and daily-slot conductor.
#
#   ./launch_dashboard.sh            full-screen Textual dashboard
#   ./launch_dashboard.sh --report   one-shot plain-text report (SSH/iPad friendly)
#
# Any arguments are passed straight through to dashboard.py.
set -euo pipefail

# Resolve this script's own directory so it works from anywhere.
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

exec python3 "${REPO_DIR}/scripts/dashboard.py" "$@"
