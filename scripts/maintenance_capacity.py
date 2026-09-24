#!/usr/bin/env python3
"""How many maintenance slots a day the corpus needs, and how to spread them.

Two maintenance slots were hard-coded as "recommended" while the corpus grew past 3,000
companies; by 2026-09-08 that was half of what the refresh cadence demands and 2,193 companies
sat overdue. The recommendation is now computed from the tier mix, the cadence in config and
the observed share of checks that turn into a full update. Pure functions; the workflow reader
is the only thing that touches the database, read-only.
"""

import datetime
import math

DEFAULT_CADENCE = {'leader': 7, 'second_leader': 7, 'potential': 14, 'hidden_champion': 14,
                   'supporting': 28, 'minor_supplier': 28, 'unknown': 28}
DEFAULT_FLAG_RATE = 0.2   # share of screened companies that need a full update (Aug 2026: 19%)


def daily_screen_need(tier_counts, cadence=None):
    """Companies that must be checked per day for every tier to meet its cadence."""
    cadence = {**DEFAULT_CADENCE, **(cadence or {})}
    return sum(count / max(1, int(cadence.get(tier, cadence['unknown'])))
               for tier, count in tier_counts.items())


def recommend_slots(tier_counts, cadence=None, flag_rate=DEFAULT_FLAG_RATE,
                    screen_per_slot=80, updates_per_slot=20, max_slots=20):
    """Slots per day so that both the checks and the resulting updates keep pace.

    A slot screens `screen_per_slot` companies and then fully updates at most
    `updates_per_slot` of the ones flagged; whichever of the two runs out first sets the number.
    """
    need = daily_screen_need(tier_counts, cadence)
    if need <= 0:
        return 1
    by_screening = math.ceil(need / max(1, screen_per_slot))
    by_updates = math.ceil(need * max(0.0, flag_rate) / max(1, updates_per_slot))
    return max(1, min(max_slots, max(by_screening, by_updates)))


def recommend_from_workflow(workflow, cfg=None, now=None):
    """The recommendation for the live corpus, plus the inputs so the panel can explain it."""
    from research_state import DEEP_RESEARCHED, canonical_tier
    cfg = cfg or {}
    now = now or datetime.datetime.now(datetime.timezone.utc)
    since = (now - datetime.timedelta(days=28)).isoformat()
    with workflow.connect() as db:
        rows = db.execute('SELECT tier, COUNT(*) n FROM companies WHERE research_status=? '
                          'GROUP BY tier', (DEEP_RESEARCHED,)).fetchall()
        screened = db.execute('SELECT COUNT(*) n, COALESCE(SUM(needs_update), 0) f '
                              'FROM maintenance_screenings WHERE created_at>=?',
                              (since,)).fetchone()
    tier_counts = {}
    for row in rows:
        key = canonical_tier(row['tier'])
        tier_counts[key] = tier_counts.get(key, 0) + row['n']
    flag_rate = (screened['f'] / screened['n']) if screened['n'] >= 50 else DEFAULT_FLAG_RATE
    group = int(cfg.get('maintenance_screen_group_size', 20))
    groups = int(cfg.get('maintenance_screen_groups_per_slot', 4))
    schedule_len = len(cfg.get('schedule') or []) or 20
    recommended = recommend_slots(tier_counts, cfg.get('maintenance_cadence_days'), flag_rate,
                                  screen_per_slot=group * groups,
                                  updates_per_slot=int(cfg.get('batch_size', 20)),
                                  max_slots=schedule_len)
    return {'recommended': recommended, 'daily_need': round(daily_screen_need(
        tier_counts, cfg.get('maintenance_cadence_days'))), 'flag_rate': round(flag_rate, 2),
        'tier_counts': tier_counts, 'screen_per_slot': group * groups}


def maintenance_count(schedule):
    return sum(1 for e in schedule or [] if len(e) > 2 and e[2] == 'maintenance')


def _mode(entry):
    return entry[2] if len(entry) > 2 else 'research'


def with_maintenance_count(schedule, n):
    """A copy of `schedule` with exactly `n` maintenance slots.

    Existing maintenance slots are kept. Adding places each new one in the widest gap between
    maintenance slots (around the clock), choosing among the gap's middle three positions the
    provider that has the fewest maintenance slots so far — Claude researches a company in
    three to four minutes against Gemini's one to two, so a run of Claude maintenance slots
    would overrun the 72-minute spacing. Ties go to Gemini for the same reason. Removing drops
    the slot crowded closest to its neighbours. So 10:06 and 13:42 stay where Peter put them
    and the extra slots spread out across the day and across providers.
    """
    entries = [list(e) for e in schedule or []]
    total = len(entries)
    if total == 0:
        return entries
    n = max(0, min(total, int(n)))
    marked = [i for i, e in enumerate(entries) if _mode(e) == 'maintenance']

    def circular_gap_after(i, chosen):
        nxt = min((j for j in chosen if j > i), default=None)
        return (nxt - i) if nxt is not None else (total - i + min(chosen))

    def provider_load():
        load = {}
        for i in marked:
            load[entries[i][1]] = load.get(entries[i][1], 0) + 1
        return load

    while len(marked) < n:
        if not marked:
            marked.append(total // 2)
            continue
        best, best_gap = None, -1
        for i in sorted(marked):
            gap = circular_gap_after(i, marked)
            if gap > best_gap:
                best, best_gap = i, gap
        mid = (best + best_gap // 2) % total
        candidates = [c % total for c in (mid, mid - 1, mid + 1)
                      if c % total not in marked and best_gap >= 2
                      and 0 < (c - best) % total < best_gap]
        if not candidates:  # nothing fits in any gap: take any free slot
            marked.append(next(i for i in range(total) if i not in marked))
            continue
        load = provider_load()
        candidates.sort(key=lambda c: (load.get(entries[c][1], 0),
                                       0 if entries[c][1] == 'gemini' else 1,
                                       abs(((c - mid + total // 2) % total) - total // 2)))
        marked.append(candidates[0])
    while len(marked) > n:
        marked.sort()

        def crowding(i):
            prev = max((j for j in marked if j < i), default=None)
            nxt = min((j for j in marked if j > i), default=None)
            before = (i - prev) if prev is not None else (i + total - max(marked))
            after = (nxt - i) if nxt is not None else (total - i + min(marked))
            return min(before, after)
        marked.remove(min(marked, key=lambda i: (crowding(i), -i)))
    for i, e in enumerate(entries):
        entries[i] = [e[0], e[1], 'maintenance'] if i in marked else [e[0], e[1]]
    return entries
