#!/bin/bash
# Health check for the autonomous research loop.
#
# Answers three questions the loop cannot answer about itself:
#   1. Is exactly one loop running (never zero, never two)?
#   2. Is it actually working, or idling through slots it cannot fill?
#   3. Are the queues able to refill, or is it about to stall silently?
#
# Read-only. Safe to run at any time, including mid-batch.

set -uo pipefail
cd "$(dirname "$0")"

DB=research_workflow.sqlite3
TODAY=$(date +%F)

echo "=== process ==="
PID=$(pgrep -f 'research_loop.py --run' | head -1)
if [ -z "$PID" ]; then
    echo "  DAEMON   : NOT RUNNING  <-- launchd should respawn within 60s (ThrottleInterval)"
else
    echo "  DAEMON   : pid $PID up $(ps -o etime= -p "$PID" | tr -d ' ')"
fi

# The flock — not the PID file — is the real singleton guard. A held lock means no
# second loop can race writes to the DB (the 2026-07-23 ticker-collision failure).
python3 - <<'PY'
import fcntl, os
p = '.research_loop.lock'
if not os.path.exists(p):
    print("  LOCK     : missing"); raise SystemExit
fh = open(p)
try:
    fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    fcntl.flock(fh, fcntl.LOCK_UN)
    print("  LOCK     : FREE  <-- no loop holds it; a second instance could start")
except OSError:
    print(f"  LOCK     : held by pid {open(p).read().strip()}  (no concurrent run possible)")
PY

echo "  HALTED   : $(python3 -c "import json;print(json.load(open('loop_control.json'))['halted'])" 2>/dev/null || echo '?')"

# A log that stopped advancing is the tell that the loop is wedged rather than idle.
if [ -f loop.log ]; then
    AGE=$(( ($(date +%s) - $(stat -f %m loop.log)) / 60 ))
    echo "  LAST LOG : $(date -r loop.log '+%H:%M:%S')  (${AGE} min ago)"
    [ "$AGE" -gt 90 ] && echo "             ^ WARNING: slots are 72 min apart; >90 min means wedged"
fi

echo
echo "=== slots today ($TODAY) ==="
sqlite3 -header -column "$DB" \
    "select status, count(*) n from daily_slots where slot_date='$TODAY' group by 1 order by n desc;"
SKIPPED=$(sqlite3 "$DB" "select count(*) from daily_slots where slot_date='$TODAY' and status='skipped';")
[ "$SKIPPED" -ge 3 ] && echo "  WARNING: $SKIPPED skipped slots today — check the queue section below"

echo
echo "=== last 5 batches ==="
grep "done: applied" loop.log 2>/dev/null | tail -5 | sed 's/^=== /  /;s/ ===$//'

echo
echo "=== queues (can the loop refill itself?) ==="
sqlite3 "$DB" "select '  deep_researched : '||count(*) from companies where research_status='deep_researched';"
sqlite3 "$DB" "select '  research queue  : '||count(*) from companies where research_status='pending_deep_research';"
sqlite3 "$DB" "select '  screen due      : '||count(*) from companies where maintenance_status='screen_due';"
sqlite3 "$DB" "select '  update due      : '||count(*) from companies where maintenance_status='update_due';"
sqlite3 "$DB" "select '  in flight       : '||count(*) from companies where maintenance_status='updating' or research_status='researching';"
# Both queues at zero is the exact 2026-08-25/26 stall: every slot skips, silently.
EMPTY=$(sqlite3 "$DB" "select (select count(*) from companies where research_status='pending_deep_research')
                            + (select count(*) from companies where maintenance_status in ('screen_due','update_due','retry_wait'));")
[ "$EMPTY" -eq 0 ] && echo "  WARNING: both queues empty — the next slot will skip unless discovery refills"

echo
echo "=== discovery (new-company growth) ==="
python3 -c "
import json
s = json.load(open('loop_state.json'))
d = s.get('discovery', {})
print(f\"  date {s.get('date')}  attempts {d.get('attempts')}  empty_results {d.get('empty_results')}  cursor {s.get('discovery_cursor')}\")
" 2>/dev/null
grep "discovery \[" loop.log 2>/dev/null | tail -3 | sed 's/^ */  /'

echo
echo "=== providers ==="
sqlite3 -column "$DB" \
    "select '  '||provider, health, 'ok='||successes, 'fail='||failures,
            coalesce('cooldown until '||cooldown_until,'') from provider_state;"
