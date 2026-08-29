import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

import apply_batch


class OrchestratorNotifierTests(unittest.TestCase):
    def test_status_notification_uses_orchestrator_one_shot_sender(self):
        with patch.object(apply_batch.subprocess, 'run') as run:
            apply_batch.send_telegram('✅ Research badge')

        command = run.call_args.args[0]
        kwargs = run.call_args.kwargs
        self.assertEqual(command, [
            'hermes', 'send', '--quiet', '--to',
            apply_batch.ORCHESTRATOR_TELEGRAM_TARGET,
            '✅ Research badge',
        ])
        self.assertEqual(kwargs['env']['HERMES_HOME'], apply_batch.ORCHESTRATOR_HERMES_HOME)
        self.assertEqual(kwargs['timeout'], 30)
        self.assertFalse(kwargs['check'])
        self.assertNotIn('curl', command)
        self.assertFalse(any('bot' in part for part in command))

    def test_empty_status_notification_does_not_start_sender(self):
        with patch.object(apply_batch.subprocess, 'run') as run:
            apply_batch.send_telegram('')

        run.assert_not_called()

    def test_transport_failure_does_not_stop_research(self):
        with patch.object(apply_batch.subprocess, 'run', side_effect=OSError('missing hermes')):
            apply_batch.send_telegram('✅ Research badge')


if __name__ == '__main__':
    unittest.main()
