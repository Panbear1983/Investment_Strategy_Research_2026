"""Keep the test suite off the live loop's and the chatbot's files.

Two module-level path constants point at real, live state and neither has an environment
override, so any test that reaches them writes production data:

- research_loop.STATE_PATH -> scripts/loop_state.json, the day's per-provider call counters and
  the batch sequence number. On 2026-08-27 a test run reset the counters from 151 to 26 and the
  batch sequence from 358 to 1, so the loop believed it had spent almost nothing and started
  renumbering batches from the beginning.
- botffet.QUOTA_DB -> scripts/research_workflow.sqlite3, which also holds 爸菲特's chat memory.
  tests/test_botffet.py calls answer() without a quota_db, so every run appended turns to the
  DASHBOARD's conversation (chat_id 'local') and, because history_append trims each chat to
  HISTORY_KEEP rows, evicted the real ones. The row count never moved, which is why it went
  unnoticed until all 60 stored exchanges were test queries.

Both are redirected for every test rather than fixed test by test, because the hazard is the
constant, not any one test's carelessness. Tests that pass an explicit path or quota_db are
unaffected.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

import botffet
import research_loop as rl


@pytest.fixture(autouse=True)
def _isolate_live_state(tmp_path, monkeypatch):
    monkeypatch.setattr(rl, 'STATE_PATH', str(tmp_path / 'loop_state.json'))
    # quota_connect() runs CREATE TABLE IF NOT EXISTS on open, so a fresh path needs no seeding.
    monkeypatch.setattr(botffet, 'QUOTA_DB', str(tmp_path / 'chat.sqlite3'))
    yield
