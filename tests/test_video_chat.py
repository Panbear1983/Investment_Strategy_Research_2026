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
