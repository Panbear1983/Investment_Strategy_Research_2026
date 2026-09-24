"""Dropping a YouTube link into the chat, from Telegram or the dashboard.

The rules that matter here are about money and honesty: summarising is the first thing a chat
user can do that spends from the research loop's model budget, and a video with no captions must
be reported plainly rather than looking like a fault — and must not cost anyone a slot.
"""

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

import botffet
import video_intel as vi
from research_state import WorkflowState

LINK = 'https://youtu.be/iiU6ZzBG_IQ'
USER = '7512954760'


def _source(**kw):
    base = {'id': 1, 'video_id': 'iiU6ZzBG_IQ', 'title': 'A Video', 'channel': 'Fin Tek',
            'duration_seconds': 1260, 'status': 'fetched', 'transcript': 'text',
            'char_count': 4, 'url': LINK}
    base.update(kw)
    return base


class RoutingTests(unittest.TestCase):
    """A link goes to the video path; anything else does not."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.calls = []

        def fake_video(text, user, workflow=None, quota_db=None, cap=None):
            self.calls.append(text)
            return {'kind': 'video', 'text': 'ok'}

        patcher = mock.patch.object(botffet, '_video_command', side_effect=fake_video)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def _answer(self, text):
        return botffet.answer(text, user=USER, audit=False)

    def test_a_bare_link_is_enough(self):
        self.assertEqual(self._answer(LINK)['kind'], 'video')
        self.assertEqual(len(self.calls), 1)

    def test_a_link_inside_a_sentence_still_routes(self):
        self._answer(f'看看這個 {LINK} 有什麼')
        self.assertEqual(len(self.calls), 1)

    def test_the_explicit_command_works_too(self):
        self.assertEqual(self._answer(f'/video {LINK}')['kind'], 'video')

    def test_a_question_mentioning_a_video_in_words_does_not_route(self):
        self.assertEqual(self.calls, [], 'no link, so nothing should reach the video path')
        reply = self._answer('/screen japan')
        self.assertNotEqual(reply['kind'], 'video')
        self.assertEqual(self.calls, [])

    def test_a_non_youtube_link_does_not_route(self):
        self._answer('https://vimeo.com/12345')
        self.assertEqual(self.calls, [])


class NoTranscriptTests(unittest.TestCase):
    """A video without captions is a normal outcome, reported in Traditional Chinese."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = WorkflowState(os.path.join(self.tmp.name, 'workflow.sqlite3'))
        self.quota = os.path.join(self.tmp.name, 'chat.sqlite3')

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self):
        with mock.patch.object(vi, 'ensure_transcript',
                               side_effect=vi.NoTranscript('no caption track')):
            return botffet._video_command(LINK, USER, workflow=self.state, quota_db=self.quota)

    def test_it_says_the_video_has_no_subtitles(self):
        reply = self._run()
        self.assertEqual(reply['kind'], 'video_no_transcript')
        self.assertIn('沒有字幕', reply['text'])

    def test_it_does_not_read_like_a_crash(self):
        text = self._run()['text']
        for word in ('Traceback', 'Exception', 'Error', '失敗'):
            self.assertNotIn(word, text, word)

    def test_it_costs_nobody_a_slot(self):
        self._run()
        self.assertEqual(botffet.quota_used(botffet._video_quota_key(USER), self.quota), 0)

    def test_it_says_so_explicitly(self):
        self.assertIn('沒有動用', self._run()['text'])


class QuotaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = WorkflowState(os.path.join(self.tmp.name, 'workflow.sqlite3'))
        self.quota = os.path.join(self.tmp.name, 'chat.sqlite3')

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, cap=2):
        with mock.patch.object(vi, 'ensure_transcript', return_value=_source()), \
             mock.patch.object(vi, 'summarise_source',
                               return_value=([{'kind': 'summary', 'note': 's'}], 0)):
            return botffet._video_command(LINK, USER, workflow=self.state,
                                          quota_db=self.quota, cap=cap)

    def test_the_allowance_runs_out_and_says_so(self):
        self.assertEqual(self._run()['kind'], 'video')
        self.assertEqual(self._run()['kind'], 'video')
        third = self._run()
        self.assertEqual(third['kind'], 'video_quota')
        self.assertIn('額度已用完', third['text'])

    def test_it_explains_why_the_limit_exists(self):
        self._run(); self._run()
        self.assertIn('研究批次', self._run()['text'])

    def test_video_spend_is_separate_from_the_question_cap(self):
        self._run()
        self.assertEqual(botffet.quota_used(USER, self.quota), 0,
                         'a video must not consume one of the 30 daily questions')
        self.assertEqual(botffet.quota_used(botffet._video_quota_key(USER), self.quota), 1)

    def test_two_users_have_separate_allowances(self):
        self._run(cap=1)
        with mock.patch.object(vi, 'ensure_transcript', return_value=_source()), \
             mock.patch.object(vi, 'summarise_source', return_value=([], 0)):
            other = botffet._video_command(LINK, '7108285456', workflow=self.state,
                                           quota_db=self.quota, cap=1)
        self.assertEqual(other['kind'], 'video')


class ReuseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = WorkflowState(os.path.join(self.tmp.name, 'workflow.sqlite3'))
        self.quota = os.path.join(self.tmp.name, 'chat.sqlite3')
        source_id, _ = self.state.add_video('iiU6ZzBG_IQ', LINK)
        self.state.store_transcript(source_id, 'text', title='A Video', channel='Fin Tek')
        self.state.replace_video_notes(source_id, [{'kind': 'summary', 'note': 'stored'}])

    def tearDown(self):
        self.tmp.cleanup()

    def test_an_already_summarised_link_is_not_paid_for_again(self):
        with mock.patch.object(vi, 'summarise_source',
                               side_effect=AssertionError('must not summarise again')):
            reply = botffet._video_command(LINK, USER, workflow=self.state, quota_db=self.quota)
        self.assertIn('stored', reply['text'])
        self.assertIn('沒有再花費額度', reply['text'])
        self.assertEqual(botffet.quota_used(botffet._video_quota_key(USER), self.quota), 0)


class ReplyFormattingTests(unittest.TestCase):
    NOTES = [{'kind': 'summary', 'note': '重點摘要內容'},
             {'kind': 'company', 'company_name': 'Nvidia', 'ticker': 'NVDA', 'note': 'n'},
             {'kind': 'company', 'company_name': 'Spirit', 'ticker': '', 'note': 'n'},
             {'kind': 'sector', 'sector': '半導體', 'note': 'n'}]

    def test_it_leads_with_the_channel_and_title(self):
        text = botffet._format_video_reply(_source(), self.NOTES, 0, False)
        self.assertTrue(text.startswith('🎬 Fin Tek｜A Video'))
        self.assertIn('21 分鐘', text)

    def test_a_ticker_is_shown_when_known_and_omitted_when_not(self):
        text = botffet._format_video_reply(_source(), self.NOTES, 0, False)
        self.assertIn('Nvidia (NVDA)', text)
        self.assertIn('Spirit', text)
        self.assertNotIn('Spirit (', text)

    def test_dropped_claims_are_declared_not_hidden(self):
        text = botffet._format_video_reply(_source(), self.NOTES, 3, False)
        self.assertIn('3 項', text)
        self.assertIn('捨棄', text)

    def test_nothing_dropped_means_no_warning(self):
        self.assertNotIn('捨棄', botffet._format_video_reply(_source(), self.NOTES, 0, False))


class BotWiringTests(unittest.TestCase):
    """The Telegram-only half: acknowledge first, and allow far longer for a video."""

    def test_a_video_gets_its_own_longer_timeout(self):
        import botffet_bot
        self.assertGreater(botffet_bot.VIDEO_TIMEOUT, botffet_bot.ANSWER_TIMEOUT)
        self.assertGreaterEqual(botffet_bot.VIDEO_TIMEOUT, 600)

    def test_the_acknowledgement_is_in_chinese_and_states_what_is_left(self):
        import botffet_bot
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(botffet, 'QUOTA_DB', os.path.join(tmp, 'q.sqlite3')):
                ack = botffet_bot._video_ack(USER)
        self.assertIn('收到連結', ack)
        self.assertIn('還可以處理', ack)

    def test_the_acknowledgement_survives_an_unreadable_quota_store(self):
        import botffet_bot
        with mock.patch.object(botffet, 'quota_used', side_effect=OSError('boom')):
            self.assertIn('收到連結', botffet_bot._video_ack(USER))


class VideoQuotaQueryTests(unittest.TestCase):
    """Asking how many videos are left is answered from the counter: no model, no claim."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.quota = os.path.join(self.tmp.name, 'chat.sqlite3')
        self.video_calls = []

        def fake_video(text, user, workflow=None, quota_db=None, cap=None):
            self.video_calls.append(text)
            return {'kind': 'video', 'text': 'ok'}

        patcher = mock.patch.object(botffet, '_video_command', side_effect=fake_video)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def _spend(self, n, cap=5):
        for _ in range(n):
            botffet.claim_quota(botffet._video_quota_key(USER), cap, self.quota)

    def _answer(self, text):
        def model(prompt):
            raise AssertionError('the model must not be called for an allowance question')
        return botffet.answer(text, user=USER, audit=False, quota_db=self.quota,
                              provider=model)

    def test_the_report_states_used_and_left(self):
        self._spend(2)
        reply = botffet.video_quota_report(USER, quota_db=self.quota, cap=5)
        self.assertEqual(reply['kind'], 'quota')
        self.assertIn('已處理 2 部', reply['text'])
        self.assertIn('還可以處理 3 部', reply['text'])
        self.assertIn('每天 5 部', reply['text'])

    def test_asking_never_spends_a_slot(self):
        botffet.video_quota_report(USER, quota_db=self.quota, cap=5)
        self.assertEqual(botffet.quota_used(botffet._video_quota_key(USER), self.quota), 0)
        self.assertEqual(botffet.quota_used(USER, self.quota), 0)

    def test_an_exhausted_allowance_says_when_it_resets(self):
        self._spend(5)
        reply = botffet.video_quota_report(USER, quota_db=self.quota, cap=5)
        self.assertIn('額度已用完', reply['text'])
        self.assertIn('午夜重置', reply['text'])

    def test_the_command_and_its_chinese_aliases(self):
        for cmd in ('/quota', '/額度', '/影片額度', '/video'):
            reply = self._answer(cmd)
            self.assertEqual(reply['kind'], 'quota', cmd)
            self.assertEqual(reply['provider_calls'], 0, cmd)
        self.assertEqual(self.video_calls, [], 'a bare /video reports, it does not process')

    def test_a_plain_chinese_question_is_answered_from_the_counter(self):
        for q in ('今天還能處理幾部影片？', '一天有幾個yt轉換文字的機會？', '影片額度還剩多少',
                  '我還可以處理幾部影片', 'how many videos do I have left today?',
                  '今天YouTube轉檔的數量還有嗎？', '影片轉檔還有幾次'):
            self.assertTrue(botffet.is_video_quota_question(q), q)
            reply = self._answer(q)
            self.assertEqual(reply['kind'], 'quota', q)
            self.assertEqual(reply['provider_calls'], 0, q)
        self.assertEqual(self.video_calls, [])

    def test_a_question_about_a_video_itself_is_not_hijacked(self):
        for q in ('這部影片講了什麼？', '影片裡提到哪些投資機會？', '幫我看看這部影片的重點',
                  'analytics companies with a limit order feature',
                  f'{LINK} 今天還能處理幾部影片'):
            self.assertFalse(botffet.is_video_quota_question(q), q)

    def test_a_roster_entry_can_carry_its_own_allowance(self):
        import json
        roster = os.path.join(self.tmp.name, 'telegram_users.json')
        with open(roster, 'w', encoding='utf-8') as f:
            json.dump({'users': [{'label': 'Dad', 'chat_id': '7108285456', 'enabled': True,
                                  'video_cap': 9},
                                 {'label': 'Peter', 'chat_id': USER, 'enabled': True}]}, f)
        with mock.patch.object(botffet, 'ROSTER_PATH', roster), \
             mock.patch.object(botffet, 'rl_config',
                               return_value={'video_daily_cap_per_user': 5}):
            self.assertEqual(botffet._video_cap('7108285456'), 9)
            self.assertEqual(botffet._video_cap(USER), 5, 'no override falls back to config')
            self.assertEqual(botffet._video_cap(), 5)
            dad = botffet.video_quota_report('7108285456', quota_db=self.quota)
            self.assertIn('每天 9 部', dad['text'])

    def test_an_unreadable_roster_means_no_override(self):
        with mock.patch.object(botffet, 'ROSTER_PATH', '/nonexistent/roster.json'), \
             mock.patch.object(botffet, 'rl_config',
                               return_value={'video_daily_cap_per_user': 5}):
            self.assertEqual(botffet._video_cap(USER), 5)

    def test_the_acknowledgement_uses_the_configured_cap(self):
        import botffet_bot
        with mock.patch.object(botffet, 'rl_config',
                               return_value={'video_daily_cap_per_user': 7}), \
             mock.patch.object(botffet, 'ROSTER_PATH', '/nonexistent/roster.json'), \
             mock.patch.object(botffet, 'QUOTA_DB', self.quota):
            self.assertIn('還可以處理 7 部', botffet_bot._video_ack(USER))


class _FlakyMessage:
    """A Telegram message whose reply_text fails `failures` times with a network error."""

    def __init__(self, failures):
        self.chat_id = USER
        self.failures = failures
        self.sent = []
        self.calls = 0

    async def reply_text(self, chunk):
        from telegram.error import NetworkError
        self.calls += 1
        if self.failures:
            self.failures -= 1
            raise NetworkError('httpx.ConnectError')
        self.sent.append(chunk)


class ReplyDeliveryTests(unittest.TestCase):
    """Dad's 70-minute video on 2026-09-03 was summarised and then lost to one ConnectError
    on the final send. The reply must be retried, and a lost one must be loud in the log."""

    def _deliver(self, message, body='重點來了'):
        import asyncio
        import botffet_bot
        with mock.patch('asyncio.sleep', new=mock.AsyncMock()) as nap:
            ok = asyncio.run(botffet_bot._deliver(message, body, 'video'))
        return ok, nap

    def test_one_momentary_failure_is_retried_and_the_text_arrives(self):
        message = _FlakyMessage(failures=1)
        ok, nap = self._deliver(message)
        self.assertTrue(ok)
        self.assertEqual(message.calls, 2)
        self.assertEqual(message.sent, ['重點來了'])
        nap.assert_awaited_once_with(3)

    def test_a_dead_link_gives_up_after_four_tries_and_says_so(self):
        import botffet_bot
        message = _FlakyMessage(failures=99)
        with self.assertLogs(botffet_bot.log, level='ERROR') as captured:
            ok, nap = self._deliver(message)
        self.assertFalse(ok)
        self.assertEqual(message.calls, 4)
        self.assertEqual(nap.await_count, 3)
        self.assertTrue(any('reply LOST' in line for line in captured.output))

    def test_a_non_network_error_is_not_retried(self):
        import asyncio
        import botffet_bot

        class Broken(_FlakyMessage):
            async def reply_text(self, chunk):
                self.calls += 1
                raise ValueError('bad request')

        message = Broken(failures=0)
        with self.assertLogs(botffet_bot.log, level='ERROR'):
            self.assertFalse(asyncio.run(botffet_bot._deliver(message, 'x')))
        self.assertEqual(message.calls, 1)

    def test_a_long_answer_is_chunked_and_every_chunk_is_sent(self):
        import botffet_bot
        message = _FlakyMessage(failures=0)
        ok, _ = self._deliver(message, '字' * (botffet_bot.TELEGRAM_CHUNK + 10))
        self.assertTrue(ok)
        self.assertEqual(len(message.sent), 2)


class VideoFailureAlertTests(unittest.TestCase):
    """An unsuccessful conversion is reported to Peter through the Orchestrator bot —
    not in the sender's chat, and never for Peter's own chat."""

    def test_failure_replies_carry_their_own_kind(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = WorkflowState(os.path.join(tmp, 'w.sqlite3'))
            with mock.patch.object(vi, 'ensure_transcript', side_effect=RuntimeError('yt-dlp died')):
                fetch = botffet._video_command(LINK, USER, workflow=state,
                                               quota_db=os.path.join(tmp, 'q.sqlite3'), cap=5)
            with mock.patch.object(vi, 'ensure_transcript', return_value=_source()), \
                 mock.patch.object(vi, 'summarise_source', side_effect=RuntimeError('gemini empty')):
                summ = botffet._video_command(LINK, USER, workflow=state,
                                              quota_db=os.path.join(tmp, 'q.sqlite3'), cap=5)
        self.assertEqual(fetch['kind'], 'video_error')
        self.assertIn('yt-dlp died', fetch['text'])
        self.assertEqual(summ['kind'], 'video_error')
        self.assertIn('gemini empty', summ['text'])

    def test_only_failures_produce_a_reason(self):
        import botffet_bot
        self.assertIsNone(botffet_bot._failure_reason('video', '🎬 ok', True))
        self.assertIsNone(botffet_bot._failure_reason('video_quota', '📊 額度已用完', True))
        self.assertIsNone(botffet_bot._failure_reason('video_error', '⚠️ x', False))
        self.assertEqual(botffet_bot._failure_reason('video_error', '⚠️ 取得逐字稿時失敗：boom\n更多', True),
                         '⚠️ 取得逐字稿時失敗：boom')
        self.assertTrue(botffet_bot._failure_reason('video_no_transcript', '📄 沒有字幕', True))

    def test_the_report_names_the_person_link_and_reason(self):
        import botffet_bot
        report = botffet_bot._video_failure_report({'label': 'Dad'}, '7108285456',
                                                   f'幫我看 {LINK}', '取得逐字稿時失敗：boom')
        self.assertIn('爸菲特影片轉換失敗', report)
        self.assertIn('Dad（7108285456）', report)
        self.assertIn('iiU6ZzBG_IQ', report)
        self.assertIn('boom', report)

    def test_dads_failure_goes_to_the_operator_but_peters_does_not(self):
        import asyncio
        import apply_batch
        import botffet_bot
        with mock.patch.object(apply_batch, 'send_telegram', return_value=True) as send:
            asyncio.run(botffet_bot._notify_video_failure({'label': 'Dad'}, '7108285456',
                                                          LINK, '取得逐字稿時失敗'))
            self.assertEqual(send.call_count, 1)
            self.assertIn('Dad', send.call_args.args[0])
            asyncio.run(botffet_bot._notify_video_failure({'label': 'Peter'}, USER, LINK, 'x'))
            self.assertEqual(send.call_count, 1, "Peter already sees his own failure")

    def test_a_broken_transport_never_raises(self):
        import asyncio
        import apply_batch
        import botffet_bot
        with mock.patch.object(apply_batch, 'send_telegram', side_effect=OSError('no hermes')):
            asyncio.run(botffet_bot._notify_video_failure({'label': 'Dad'}, '7108285456',
                                                          LINK, 'x'))


class ProviderFallbackTests(unittest.TestCase):
    """Gemini first by design; Claude only once Gemini is dry.

    Claude's real ceiling is the Max plan's rolling window, shared with Peter's own sessions at
    the keyboard. Antigravity is a separate subscription that competes with nothing else, so
    video work belongs there until it runs out.
    """

    SOURCE = {'id': 1, 'video_id': 'abcdefghijk', 'transcript': '台積電產能吃緊',
              'offsets_json': '[]', 'title': 't', 'channel': 'c', 'duration_seconds': 60}
    CFG = {'daily_call_budgets': {'gemini': 1, 'claude': 1},
           'provider_fallback_order': ['gemini', 'claude'], 'video_chunk_chars': 40000}

    def test_it_stays_on_gemini_when_gemini_has_room(self):
        seen = []

        def fake(cfg, key, state, prompt, provider='gemini', use_search=True, **kw):
            seen.append(provider)
            return '{"summary": "s", "companies": [], "sectors": []}'

        with mock.patch.object(vi.rl, 'llm_call', side_effect=fake):
            vi.summarise_video(self.CFG, {}, self.SOURCE, None)
        self.assertEqual(seen, ['gemini'])

    def test_it_falls_through_to_claude_when_gemini_is_exhausted(self):
        seen = []

        def fake(cfg, key, state, prompt, provider='gemini', use_search=True, **kw):
            seen.append(provider)
            if provider == 'gemini':
                raise vi.rl.BudgetExhausted('gemini dry', provider='gemini',
                                            error_class='local_budget')
            return '{"summary": "s", "companies": [], "sectors": []}'

        with mock.patch.object(vi.rl, 'llm_call', side_effect=fake):
            vi.summarise_video(self.CFG, {}, self.SOURCE, None)
        self.assertEqual(seen, ['gemini', 'claude'])
        self.assertEqual(vi.summarise_video.last_provider, 'claude')

    def test_the_provider_that_did_the_work_is_recorded(self):
        with mock.patch.object(vi.rl, 'llm_call',
                               return_value='{"summary": "s", "companies": [], "sectors": []}'):
            vi.summarise_video(self.CFG, {}, self.SOURCE, None)
        self.assertEqual(vi.summarise_video.last_provider, 'gemini')


class AudioReplyTests(unittest.TestCase):
    """A reader should know whether they are reading captions or machine-heard speech."""

    NOTES = [{'kind': 'summary', 'note': '重點'}]

    def _source(self, lang):
        return {'video_id': 'abcdefghijk', 'title': 'T', 'channel': 'C',
                'duration_seconds': 1954, 'lang': lang}

    def test_a_transcribed_video_says_so(self):
        text = botffet._format_video_reply(self._source('audio:auto'), self.NOTES, 0, False)
        self.assertIn('語音辨識', text)
        self.assertIn('可能略有出入', text)

    def test_a_subtitled_video_does_not(self):
        text = botffet._format_video_reply(self._source('zh-TW'), self.NOTES, 0, False)
        self.assertNotIn('語音辨識', text)

    def test_a_missing_language_is_treated_as_captions(self):
        source = self._source('zh-TW')
        source['lang'] = None
        self.assertNotIn('語音辨識', botffet._format_video_reply(source, self.NOTES, 0, False))


class TimeoutTests(unittest.TestCase):
    def test_the_video_timeout_allows_for_a_long_transcription(self):
        import botffet_bot
        # A two-hour video is roughly half an hour of local transcription.
        self.assertGreaterEqual(botffet_bot.VIDEO_TIMEOUT, 1800)
        self.assertGreater(botffet_bot.VIDEO_TIMEOUT, botffet_bot.ANSWER_TIMEOUT)

    def test_the_acknowledgement_warns_that_audio_takes_minutes(self):
        import botffet_bot
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(botffet, 'QUOTA_DB', os.path.join(tmp, 'q.sqlite3')):
                ack = botffet_bot._video_ack(USER)
        self.assertIn('語音辨識', ack)
