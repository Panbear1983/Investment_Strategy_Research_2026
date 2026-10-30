import io
import json
import os
import plistlib
import re
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

import botffet_voice
import daily_group_digest as dgd
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
            if method == 'sendVoice':
                return {'ok': True, 'result': {'message_id': result + 1}}
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


SUMMARY = '新公司受惠需求，但需留意風險。'
MODEL_REPLY = '[{"ticker":"NEW.TW","summary":"新公司受惠需求，但需留意風險。"}]'
_EMOJI = re.compile('[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0000FE0F]')


class DailyGroupDigestVoiceTests(unittest.TestCase):
    """The voice bubble: after the text, under the text, and never at the text's expense."""

    _telegram = staticmethod(DailyGroupDigestTests._telegram)
    _add_new_taiwan_research = DailyGroupDigestTests._add_new_taiwan_research

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = WorkflowState(os.path.join(self.tmp.name, 'workflow.sqlite3'))
        self.date = '2026-07-25'
        self.log = os.path.join(self.tmp.name, 'daily_group_digest.log')
        self._add_new_taiwan_research()

    def tearDown(self):
        self.tmp.cleanup()

    def _log_records(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log, encoding='utf-8') as handle:
            return [json.loads(line) for line in handle if line.strip()]

    def _run(self, telegram_request, render_voice, voice=True):
        return run_daily_group_digest(self.state, self.date, lambda prompt: MODEL_REPLY,
                                      telegram_request, voice=voice,
                                      render_voice=render_voice, log_path=self.log)

    def _ledger(self):
        return self.state.digest_delivery_status(self.date, [GROUP_RECIPIENT])[0]['status']

    def test_voice_goes_after_the_text_as_a_reply_to_it(self):
        calls, telegram_request = self._telegram(123)
        spoken = []

        result = self._run(telegram_request, lambda text, report: spoken.append(text) or b'OggS-fake')

        self.assertEqual(result['status'], 'sent')
        self.assertEqual(result['message_id'], 123)
        self.assertEqual(result['voice'], 'sent')
        self.assertEqual([method for method, _ in calls],
                         ['getMe', 'getChat', 'sendMessage', 'sendVoice'])
        voice_payload = calls[-1][1]
        self.assertEqual(voice_payload['reply_to_message_id'], 123)
        self.assertEqual(voice_payload['chat_id'], GROUP_RECIPIENT['chat_id'])
        self.assertEqual(voice_payload['voice'], b'OggS-fake')
        self.assertEqual(self._ledger(), 'sent')
        self.assertEqual(len(spoken), 1)
        self.assertTrue(spoken[0].startswith('每日投資研究更新，7月25日，1家。'))
        self.assertIn('第1家，New Taiwan。' + SUMMARY, spoken[0])
        self.assertNotIn('NEW.TW', spoken[0])
        records = self._log_records()
        self.assertEqual([r['status'] for r in records], ['voice_sent'])
        self.assertEqual(records[0]['message_id'], 124)
        self.assertEqual(records[0]['reply_to'], 123)

    def test_voice_error_keeps_the_digest_sent_and_logs_voice_failed(self):
        calls, telegram_request = self._telegram(123)

        def broken(text, report):
            raise botffet_voice.VoiceError('ffmpeg not found')

        result = self._run(telegram_request, broken)

        self.assertEqual(result['status'], 'sent')
        self.assertEqual(result['message_id'], 123)
        self.assertEqual(result['voice'], 'failed')
        self.assertNotIn('sendVoice', [method for method, _ in calls])
        self.assertEqual(self._ledger(), 'sent')
        records = self._log_records()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]['status'], 'voice_failed')
        self.assertEqual(records[0]['error_class'], 'VoiceError')
        self.assertIn('ffmpeg', records[0]['error'])

    def test_real_voice_module_failure_is_caught_too(self):
        """The default renderer (botffet_voice.voice_for) failing, e.g. ffmpeg missing."""
        calls, telegram_request = self._telegram(123)
        with mock.patch.object(botffet_voice, 'ffmpeg_path', return_value=None):
            result = self._run(telegram_request, None)

        self.assertEqual(result['status'], 'sent')
        self.assertEqual(result['voice'], 'failed')
        self.assertNotIn('sendVoice', [method for method, _ in calls])
        self.assertEqual(self._log_records()[0]['status'], 'voice_failed')
        self.assertIn('ffmpeg', self._log_records()[0]['error'])

    def test_telegram_rejecting_the_voice_keeps_the_digest_sent(self):
        calls, base = self._telegram(123)

        def telegram_request(method, payload):
            if method == 'sendVoice':
                calls.append((method, payload))
                return {'ok': False, 'description': 'Bad Request: message to reply not found'}
            return base(method, payload)

        result = self._run(telegram_request, lambda text, report: b'OggS-fake')

        self.assertEqual(result['status'], 'sent')
        self.assertEqual(result['voice'], 'failed')
        self.assertEqual(self._ledger(), 'sent')
        self.assertEqual(self._log_records()[0]['status'], 'voice_failed')

    def test_no_voice_when_text_send_fails(self):
        calls, base = self._telegram(123)

        def telegram_request(method, payload):
            if method == 'sendMessage':
                calls.append((method, payload))
                return {'ok': False}
            return base(method, payload)

        result = self._run(telegram_request, lambda text, report: b'OggS-fake')

        self.assertEqual(result['status'], 'failed')
        self.assertNotIn('sendVoice', [method for method, _ in calls])
        self.assertEqual(self._log_records(), [])

    def test_voice_off_by_default_for_callers(self):
        calls, telegram_request = self._telegram(123)
        result = self._run(telegram_request, lambda text, report: b'OggS-fake', voice=False)
        self.assertEqual(result['status'], 'sent')
        self.assertNotIn('voice', result)
        self.assertNotIn('sendVoice', [method for method, _ in calls])

    def _main(self, argv, env):
        """main() end to end with every outside edge faked: workflow, model, token, transport."""
        calls, telegram_request = self._telegram(123)
        rendered = []
        with mock.patch.object(dgd, 'WORKFLOW_PATH', self.state.path), \
                mock.patch.object(dgd, 'DIGEST_LOG_PATH', self.log), \
                mock.patch.object(dgd, '_load_token', return_value='TEST'), \
                mock.patch.object(dgd, '_telegram_request', return_value=telegram_request), \
                mock.patch.object(dgd, '_run_claude', return_value=MODEL_REPLY), \
                mock.patch.object(dgd, '_render_voice',
                                  side_effect=lambda text, report: rendered.append(text) or b'OggS'), \
                mock.patch.object(sys, 'argv', ['daily_group_digest.py', '--date', self.date, *argv]), \
                mock.patch.dict(os.environ, env, clear=False), \
                mock.patch('sys.stdout', new_callable=io.StringIO) as out:
            if 'ISR_DIGEST_VOICE' not in env:
                os.environ.pop('ISR_DIGEST_VOICE', None)
            dgd.main()
        return [method for method, _ in calls], rendered, json.loads(out.getvalue())

    def test_cli_sends_voice_by_default(self):
        methods, rendered, result = self._main(['--send'], {})
        self.assertEqual(methods, ['getMe', 'getChat', 'sendMessage', 'sendVoice'])
        self.assertEqual(len(rendered), 1)
        self.assertEqual(result['status'], 'sent')

    def test_cli_no_voice_flag_skips_voice(self):
        methods, rendered, result = self._main(['--send', '--no-voice'], {})
        self.assertEqual(methods, ['getMe', 'getChat', 'sendMessage'])
        self.assertEqual(rendered, [])
        self.assertEqual(result['status'], 'sent')

    def test_env_switch_skips_voice(self):
        methods, rendered, result = self._main(['--send'], {'ISR_DIGEST_VOICE': '0'})
        self.assertEqual(methods, ['getMe', 'getChat', 'sendMessage'])
        self.assertEqual(rendered, [])
        self.assertEqual(result['status'], 'sent')


class SpeakableDigestTests(unittest.TestCase):
    POSTS = [
        {'ticker': 'VRSK', 'company_name': 'Verisk Analytics (VRSK)'},
        {'ticker': '2330.TW', 'company_name': '台積電（2330.TW）'},
        {'ticker': 'FICO', 'company_name': 'Fair Isaac Corporation (FICO)'},
    ]
    SUMMARIES = {
        'VRSK': '📊 Verisk推出詐欺偵測｜也與Qantev合作；但收購案仍待FTC審查。',
        '2330.TW': '✅ 先進製程需求強勁，地緣風險需留意。',
        'FICO': 'FICO重組裁員約15%，預期釋放綜效；歐盟AI法案增加海外合…',
    }

    def test_header_names_and_no_tickers_emoji_or_separator(self):
        text = dgd.speakable_digest(self.POSTS, self.SUMMARIES, '2026-10-26')
        self.assertTrue(text.startswith('每日投資研究更新，10月26日，3家。'))
        self.assertIn('第1家，Verisk Analytics。', text)
        self.assertIn('第2家，台積電。', text)
        self.assertIn('第3家，Fair Isaac Corporation。', text)
        self.assertIsNone(_EMOJI.search(text))
        for gone in ('｜', '(VRSK)', '2330.TW', '1.', '…'):
            self.assertNotIn(gone, text)

    def test_truncated_model_summary_ends_on_a_whole_clause(self):
        text = dgd.speakable_digest(self.POSTS, self.SUMMARIES, '2026-10-26')
        self.assertTrue(text.endswith('預期釋放綜效。'), text)

    def test_cap_cuts_at_a_sentence_and_points_to_the_text(self):
        posts = [{'ticker': f'T{i}', 'company_name': f'公司{i} (T{i})'} for i in range(1, 6)]
        summaries = {f'T{i}': '這是一句很長的摘要，' * 9 + '結束。' for i in range(1, 6)}
        full = dgd.speakable_digest(posts, summaries, '2026-10-26', max_chars=10_000)
        capped = dgd.speakable_digest(posts, summaries, '2026-10-26', max_chars=300)
        self.assertGreater(len(full), 300)
        self.assertLessEqual(len(capped), 300)
        self.assertTrue(capped.endswith('其餘請看文字。'))
        body = capped[:-len('其餘請看文字。')].rstrip()
        self.assertTrue(body.endswith('。'), body[-20:])
        self.assertTrue(full.startswith(body))

    def test_default_cap_holds_for_a_full_five_company_digest(self):
        posts = [{'ticker': f'T{i}', 'company_name': f'Company {i} (T{i})'} for i in range(1, 6)]
        summaries = {f'T{i}': '摘' * 118 + '。' for i in range(1, 6)}
        text = dgd.speakable_digest(posts, summaries, '2026-10-26')
        self.assertLessEqual(len(text), dgd.VOICE_MAX_CHARS)
        self.assertTrue(text.endswith('其餘請看文字。'))

    def test_cap_never_announces_a_company_and_then_skips_it(self):
        """Regression from the 2026-10-26 live render: a sentence-level cut kept
        "第4家，Fair Isaac Corporation。" with none of its summary, then 其餘請看文字."""
        posts = [{'ticker': f'T{i}', 'company_name': f'Company {i} (T{i})'} for i in range(1, 6)]
        summaries = {f'T{i}': '第一句說明需求。第二句說明風險與曝險。' * 4 for i in range(1, 6)}
        for cap in range(120, 600, 7):
            text = dgd.speakable_digest(posts, summaries, '2026-10-26', max_chars=cap)
            self.assertLessEqual(len(text), cap)
            for line in text.split('\n')[1:]:
                if line.startswith('第'):
                    self.assertTrue(line.endswith('曝險。'), (cap, line))

    def test_cap_falls_back_to_a_sentence_when_one_company_is_too_long(self):
        posts = [{'ticker': 'T1', 'company_name': 'Company 1 (T1)'}]
        summaries = {'T1': '這是第一句。' * 40}
        text = dgd.speakable_digest(posts, summaries, '2026-10-26', max_chars=100)
        self.assertLessEqual(len(text), 100)
        self.assertIn('第1家，Company 1。這是第一句。', text)
        self.assertTrue(text.endswith('。\n其餘請看文字。'))

    def test_voice_enabled_switches(self):
        self.assertTrue(dgd.voice_enabled(False, {}))
        self.assertTrue(dgd.voice_enabled(False, {'ISR_DIGEST_VOICE': '1'}))
        self.assertFalse(dgd.voice_enabled(True, {}))
        for off in ('0', 'false', 'OFF', 'no'):
            self.assertFalse(dgd.voice_enabled(False, {'ISR_DIGEST_VOICE': off}))


class TelegramTransportTests(unittest.TestCase):
    """The real request helper, with urllib's urlopen replaced so nothing leaves the machine."""

    class _Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.close()

    def _capture(self, method, payload):
        seen = []

        def fake_urlopen(request, timeout=None):
            seen.append((request, timeout))
            return self._Response(b'{"ok": true, "result": {"message_id": 7}}')

        with mock.patch('urllib.request.urlopen', side_effect=fake_urlopen):
            reply = dgd._telegram_request('TOKEN')(method, payload)
        self.assertEqual(reply['result']['message_id'], 7)
        self.assertEqual(len(seen), 1)
        return seen[0]

    def test_text_calls_stay_json(self):
        request, timeout = self._capture('sendMessage', {'chat_id': '-1', 'text': '你好'})
        self.assertEqual(request.get_header('Content-type'), 'application/json')
        self.assertEqual(json.loads(request.data.decode('utf-8')), {'chat_id': '-1', 'text': '你好'})
        self.assertEqual(timeout, 20)

    def test_voice_upload_is_multipart_with_reply(self):
        request, timeout = self._capture('sendVoice', {
            'chat_id': '-1003863698623', 'reply_to_message_id': 468, 'voice': b'OggS\x00\x01'})
        self.assertTrue(request.full_url.endswith('/sendVoice'))
        content_type = request.get_header('Content-type')
        self.assertTrue(content_type.startswith('multipart/form-data; boundary='))
        body = request.data
        self.assertIn(b'name="reply_to_message_id"\r\n\r\n468\r\n', body)
        self.assertIn(b'name="chat_id"\r\n\r\n-1003863698623\r\n', body)
        self.assertIn(b'name="voice"; filename="digest.ogg"\r\nContent-Type: audio/ogg\r\n\r\nOggS\x00\x01\r\n',
                      body)
        self.assertTrue(body.endswith(('--' + content_type.split('boundary=')[1] + '--\r\n').encode()))
        self.assertEqual(timeout, 120)


if __name__ == '__main__':
    unittest.main()
