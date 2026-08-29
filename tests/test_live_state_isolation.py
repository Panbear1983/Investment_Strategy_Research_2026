"""The suite must not write to anything the running system reads.

Two incidents motivate this file, both caused by a module-level path constant with no
environment override being reachable from a test:

- loop_state.json was rewritten mid-day, resetting the per-provider call counters and restarting
  the batch sequence from 1.
- 爸菲特's dashboard conversation was overwritten one test run at a time. history_append trims
  each chat to HISTORY_KEEP rows, so the count never moved while every real exchange was evicted.

tests/conftest.py redirects both constants for every test. These tests fail if that protection is
removed or bypassed, rather than waiting for the damage to be noticed by hand.
"""

import os
import sqlite3
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

import botffet
import research_loop as rl

SCRIPTS = os.path.join(os.path.dirname(__file__), '..', 'scripts')
LIVE_WORKFLOW_DB = os.path.realpath(os.path.join(SCRIPTS, 'research_workflow.sqlite3'))
LIVE_LOOP_STATE = os.path.realpath(os.path.join(SCRIPTS, 'loop_state.json'))


class RedirectionTests(unittest.TestCase):
    def test_the_chat_store_is_not_the_live_database(self):
        self.assertNotEqual(os.path.realpath(botffet.QUOTA_DB), LIVE_WORKFLOW_DB,
                            'a test writing here overwrites the dashboard conversation')

    def test_the_loop_state_is_not_the_live_file(self):
        self.assertNotEqual(os.path.realpath(rl.STATE_PATH), LIVE_LOOP_STATE,
                            'a test writing here resets the day of call counters')


class ChatWritesLandInTheTempStoreTests(unittest.TestCase):
    """Proves the redirect reaches the code path that actually caused the damage."""

    CORPUS_ROW_LIMIT = 1

    def test_a_recorded_turn_goes_to_the_temp_store_not_the_live_one(self):
        botffet.history_append('isolation-probe', 'user', 'hello')
        with sqlite3.connect(botffet.QUOTA_DB) as db:
            n = db.execute("SELECT COUNT(*) FROM chat_history WHERE chat_id='isolation-probe'"
                           ).fetchone()[0]
        self.assertEqual(n, 1, 'the turn did not land in the redirected store')

        live = sqlite3.connect(f'file:{LIVE_WORKFLOW_DB}?mode=ro', uri=True)
        try:
            leaked = live.execute(
                "SELECT COUNT(*) FROM chat_history WHERE chat_id='isolation-probe'").fetchone()[0]
        finally:
            live.close()
        self.assertEqual(leaked, 0, 'the turn leaked into the live database')

    def test_the_temp_store_starts_empty_for_each_test(self):
        # Per-test tmp_path means one test cannot see another's turns, which is what stopped
        # the suite accumulating a 60-turn fake conversation.
        with sqlite3.connect(botffet.QUOTA_DB) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS chat_history (
                id INTEGER PRIMARY KEY, chat_id TEXT, role TEXT, content TEXT, created_at TEXT)""")
            n = db.execute("SELECT COUNT(*) FROM chat_history WHERE chat_id='isolation-probe'"
                           ).fetchone()[0]
        self.assertEqual(n, 0)


class LoopStateWritesAreContainedTests(unittest.TestCase):
    def test_saving_state_does_not_touch_the_live_file(self):
        live_before = open(LIVE_LOOP_STATE, encoding='utf-8').read()
        rl.save_state({'date': '2020-01-01', 'calls': {'gemini': 999}, 'batch_seq': 0})
        self.assertEqual(open(LIVE_LOOP_STATE, encoding='utf-8').read(), live_before,
                         'save_state reached the live loop state')
        self.assertTrue(os.path.exists(rl.STATE_PATH))
