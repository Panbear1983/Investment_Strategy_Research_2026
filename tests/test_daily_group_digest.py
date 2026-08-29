import os
import plistlib
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

from daily_group_digest import GROUP_RECIPIENT, run_daily_group_digest
from research_state import DEEP_RESEARCHED, WorkflowState


class DailyGroupDigestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = WorkflowState(os.path.join(self.tmp.name, 'workflow.sqlite3'))
        self.date = '2026-07-25'

    def tearDown(self):
        self.tmp.cleanup()

    def _add_new_taiwan_research(self):
        company_id = self.state.upsert_company(
            {'Company': 'New Taiwan (NEW.TW)', 'ticker': 'NEW.TW', 'Country': 'Taiwan'},
            DEEP_RESEARCHED, source='new')
        batch_id = self.state.create_batch(
            1, 'Research Batch', 'research', 'new', 'claude',
            [{'Company': 'New Taiwan (NEW.TW)', '_company_id': company_id}])
        self.state.checkpoint_item(
            batch_id, 0, 'applied', 'claude',
            {'Company': 'New Taiwan (NEW.TW)', '12M Catalysts': 'new catalyst'})
        with self.state.connect() as db:
            db.execute('UPDATE batch_items SET updated_at=? WHERE batch_id=?',
                       ('2026-07-25T10:00:00+00:00', batch_id))

    @staticmethod
    def _telegram(result):
        calls = []

        def request(method, payload):
            calls.append((method, payload))
            if method == 'getMe':
                return {'ok': True, 'result': {'id': 8601109385, 'username': 'Hermes_Investment_Strategy_bot'}}
            if method == 'getChat':
                return {'ok': True, 'result': {'id': -1003863698623, 'type': 'supergroup', 'title': '財經推播Agent'}}
            if method == 'sendMessage':
                return {'ok': True, 'result': {'message_id': result}}
            raise AssertionError(method)

        return calls, request

    def test_runner_skips_provider_and_transport_when_no_candidates(self):
        model_calls = []
        telegram_calls, telegram_request = self._telegram(1)

        result = run_daily_group_digest(
            self.state, self.date, lambda prompt: model_calls.append(prompt), telegram_request)

        self.assertEqual(result['status'], 'skipped_no_updates')
        self.assertEqual(model_calls, [])
        self.assertEqual(telegram_calls, [])
        self.assertEqual(
            self.state.digest_delivery_status(self.date, [GROUP_RECIPIENT])[0]['status'],
            'skipped_no_updates')

    def test_runner_sends_one_snapshot_when_candidates_are_valid(self):
        self._add_new_taiwan_research()
        model_calls = []
        telegram_calls, telegram_request = self._telegram(123)

        result = run_daily_group_digest(
            self.state, self.date,
            lambda prompt: model_calls.append(prompt) or '[{"ticker":"NEW.TW","summary":"新公司受惠需求，但需留意風險。"}]',
            telegram_request)

        self.assertEqual(result['status'], 'sent')
        self.assertEqual(result['message_id'], 123)
        self.assertEqual(len(model_calls), 1)
        self.assertEqual([method for method, _ in telegram_calls], ['getMe', 'getChat', 'sendMessage'])
        self.assertEqual(
            self.state.digest_delivery_status(self.date, [GROUP_RECIPIENT])[0]['status'], 'sent')

    def test_runner_does_not_send_when_model_omits_expected_ticker(self):
        self._add_new_taiwan_research()
        telegram_calls, telegram_request = self._telegram(1)

        result = run_daily_group_digest(
            self.state, self.date, lambda prompt: '[]', telegram_request)

        self.assertEqual(result['status'], 'failed')
        self.assertNotIn('sendMessage', [method for method, _ in telegram_calls])
        self.assertEqual(
            self.state.digest_delivery_status(self.date, [GROUP_RECIPIENT])[0]['status'], 'failed')

    def test_runner_does_not_resend_a_claimed_or_sent_digest(self):
        self._add_new_taiwan_research()
        telegram_calls, telegram_request = self._telegram(123)
        model = lambda prompt: '[{"ticker":"NEW.TW","summary":"新公司受惠需求，但需留意風險。"}]'

        first = run_daily_group_digest(self.state, self.date, model, telegram_request)
        second = run_daily_group_digest(self.state, self.date, model, telegram_request)

        self.assertEqual(first['status'], 'sent')
        self.assertEqual(second['status'], 'already_sent')
        self.assertEqual([method for method, _ in telegram_calls].count('sendMessage'), 1)

    def test_launch_agent_runs_only_the_daily_digest_at_eight_pm(self):
        repo = os.path.join(os.path.dirname(__file__), '..')
        plist_path = os.path.join(repo, 'deploy', 'com.panbear.investment-daily-group-digest.plist')
        with open(plist_path, 'rb') as handle:
            plist = plistlib.load(handle)

        self.assertEqual(plist['Label'], 'com.panbear.investment-daily-group-digest')
        self.assertEqual(plist['StartCalendarInterval'], {'Hour': 20, 'Minute': 0})
        self.assertFalse(plist['RunAtLoad'])
        self.assertNotIn('research_loop.py', ' '.join(plist['ProgramArguments']))
        self.assertIn('run_daily_group_digest.sh', ' '.join(plist['ProgramArguments']))


if __name__ == '__main__':
    unittest.main()
