"""爸菲特's voice: what gets spoken, and that a missing engine degrades rather than breaks."""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

import botffet_voice as v


class SpeakableTests(unittest.TestCase):
    def test_markup_links_urls_and_emoji_are_not_read_aloud(self):
        text = ('🎬 東森｜**重點** [連結](https://youtu.be/x) https://example.com/a?b=c\n'
                '```\ncode\n```\n| a | b |\n- 第一點\n1. 第二點\n輸入 /memory 看看')
        out = v.speakable(text, 500)
        for gone in ('🎬', '**', 'https://', 'code', '| a |', '- 第一', '1. 第二', '/memory'):
            self.assertNotIn(gone, out, gone)
        for kept in ('東森', '重點', '連結', '第一點', '第二點', '輸入 memory 看看'):
            self.assertIn(kept, out, kept)

    def test_long_text_is_cut_at_a_sentence_end(self):
        text = '第一句。第二句很長' + '很' * 40 + '。第三句。'
        out = v.speakable(text, 20)
        self.assertEqual(out, '第一句。')

    def test_text_without_sentence_ends_is_still_bounded(self):
        out = v.speakable('無' * 100, 30)
        self.assertEqual(len(out), 30)

    def test_short_text_is_untouched(self):
        self.assertEqual(v.speakable('老傢伙，我是爸菲特。', 500), '老傢伙，我是爸菲特。')

    def test_empty_text_gives_empty(self):
        self.assertEqual(v.speakable('', 500), '')
        self.assertEqual(v.speakable('🎬 ```x```', 500), '')


class SettingsTests(unittest.TestCase):
    def test_defaults_are_an_older_taiwanese_man(self):
        cfg = v.settings('/nonexistent/botffet_voice.json')
        self.assertEqual(cfg['voice'], 'zh-TW-YunJheNeural')
        self.assertTrue(cfg['rate'].startswith('-'), 'slower than the announcer default')
        self.assertTrue(cfg['pitch'].startswith('-'), 'lower than the announcer default')
        self.assertTrue(cfg['enabled'])

    def test_settings_file_overrides_only_known_keys_and_survives_garbage(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'v.json')
            with open(path, 'w') as f:
                json.dump({'voice': 'Grandpa (Chinese (Taiwan))', 'engine': 'say',
                           'max_chars': 999999, 'bogus': 1}, f)
            cfg = v.settings(path)
            self.assertEqual(cfg['engine'], 'say')
            self.assertEqual(cfg['voice'], 'Grandpa (Chinese (Taiwan))')
            self.assertLessEqual(cfg['max_chars'], v.HARD_MAX_CHARS)
            self.assertNotIn('bogus', cfg)
            with open(path, 'w') as f:
                f.write('{not json')
            self.assertEqual(v.settings(path)['voice'], v.DEFAULTS['voice'])


class DegradationTests(unittest.TestCase):
    def test_disabled_voice_reports_why_and_synthesize_raises_cleanly(self):
        cfg = dict(v.DEFAULTS, enabled=False)
        self.assertIn('switched off', v.unavailable_reason(cfg))
        with self.assertRaises(v.VoiceError):
            v.synthesize('你好', cfg)

    def test_voice_for_returns_none_when_nothing_is_listenable(self):
        self.assertIsNone(v.voice_for('🎬 ```only code```', dict(v.DEFAULTS)))

    def test_bot_handler_swallows_voice_failures(self):
        """The reply must survive a broken engine: _speak logs, tells Peter, and returns."""
        import asyncio
        import botffet_bot

        class Msg:
            chat_id = 'x'
            sent = []

            async def reply_voice(self, voice, **kwargs):
                self.sent.append(voice)

        def boom(_text, _cfg=None, report=None):
            if report is not None:
                report.update(attempts=3, errors=['edge try 1: NoAudioReceived: nothing'])
            raise v.VoiceError('no network')
        alerts = []

        async def alert(user, chat_id, reason):
            alerts.append((chat_id, reason))
        real, real_alert = botffet_bot.botffet_voice, botffet_bot._notify_voice_lost
        botffet_bot.botffet_voice = type('V', (), {'voice_for': staticmethod(boom)})
        botffet_bot._notify_voice_lost = alert
        try:
            with self.assertLogs('botffet_bot', level='WARNING') as logs:
                asyncio.run(botffet_bot._speak(Msg(), '你好', {'label': 'Dad'}))
            self.assertEqual(Msg.sent, [])
            self.assertIn('voice skipped', logs.output[0])
            self.assertIn('3 tries', logs.output[0])
            self.assertEqual(len(alerts), 1)
            # and an opted-out person gets no bubble at all, engine or not
            asyncio.run(botffet_bot._speak(Msg(), '你好', {'label': 'Dad', 'voice': False}))
            self.assertEqual(len(alerts), 1)
        finally:
            botffet_bot.botffet_voice = real
            botffet_bot._notify_voice_lost = real_alert

    def test_bot_handler_logs_every_bubble_it_sends(self):
        """Successes are logged too, and a fallback voice is called out as a warning."""
        import asyncio
        import botffet_bot

        class Msg:
            chat_id = 'x'
            sent = []

            async def reply_voice(self, voice, **kwargs):
                self.sent.append(voice.read())

        outcome = {'engine': 'edge', 'voice': 'zh-TW-YunJheNeural', 'attempts': 2,
                   'seconds': 4.2, 'errors': ['edge try 1: NoAudioReceived: nothing'],
                   'fallback': False}

        def ok(_text, _cfg=None, report=None):
            report.update(outcome)
            return b'ogg'
        real = botffet_bot.botffet_voice
        botffet_bot.botffet_voice = type('V', (), {'voice_for': staticmethod(ok)})
        try:
            with self.assertLogs('botffet_bot', level='INFO') as logs:
                asyncio.run(botffet_bot._speak(Msg(), '你好', {'label': 'Dad'}))
            self.assertEqual(Msg.sent, [b'ogg'])
            sent = [line for line in logs.output if 'voice sent' in line]
            self.assertEqual(len(sent), 1)
            self.assertIn('engine=edge attempts=2', sent[0])
            self.assertFalse([line for line in logs.output if 'fallback' in line])

            outcome.update(engine='say', voice='Grandpa (Chinese (Taiwan))', attempts=3,
                           fallback=True)
            with self.assertLogs('botffet_bot', level='INFO') as logs:
                asyncio.run(botffet_bot._speak(Msg(), '你好', {'label': 'Dad'}))
            self.assertTrue([line for line in logs.output
                             if 'WARNING' in line and 'voice via fallback say/Grandpa' in line])
            self.assertTrue([line for line in logs.output if 'engine=say attempts=3' in line])
        finally:
            botffet_bot.botffet_voice = real


class RetryAndFallbackTests(unittest.TestCase):
    """An Edge hiccup (2026-09-16: 36 s, then "No audio was received") must cost a retry,
    not the bubble; a dead Edge must still produce a bubble from the offline voice."""

    def setUp(self):
        self.calls = {'edge': 0, 'say': 0}
        self.originals = {name: getattr(v, name) for name in (
            '_pause', '_to_ogg', 'unavailable_reason', '_say_available', '_edge', '_say')}
        v._pause = lambda seconds: None
        v.unavailable_reason = lambda cfg=None: None
        v._say_available = lambda: True

        def to_ogg(src, dst):
            with open(src, 'rb') as f, open(dst, 'wb') as g:
                g.write(b'OGG:' + f.read())

        def say(text, cfg, out):
            self.calls['say'] += 1
            with open(out, 'wb') as f:
                f.write(f'say:{cfg["voice"]}'.encode('utf-8'))
        v._to_ogg = to_ogg
        v._say = say

    def tearDown(self):
        for name, fn in self.originals.items():
            setattr(v, name, fn)

    def _edge_fails(self, times, make_exc):
        def edge(text, cfg, out):
            self.calls['edge'] += 1
            if self.calls['edge'] <= times:
                raise make_exc()
            with open(out, 'wb') as f:
                f.write(b'edge')
        v._edge = edge

    def test_edge_hiccup_is_retried_and_edge_still_speaks(self):
        from edge_tts.exceptions import NoAudioReceived
        self._edge_fails(2, lambda: NoAudioReceived('No audio was received.'))
        info = {}
        data = v.synthesize('你好', dict(v.DEFAULTS), info)
        self.assertEqual(data, b'OGG:edge')
        self.assertEqual((info['engine'], info['attempts'], info['fallback']),
                         ('edge', 3, False))
        self.assertEqual(len(info['errors']), 2)
        self.assertEqual(self.calls['say'], 0)

    def test_edge_down_falls_back_to_the_offline_grandpa_voice(self):
        from edge_tts.exceptions import NoAudioReceived
        self._edge_fails(99, lambda: NoAudioReceived('No audio was received.'))
        info = {}
        data = v.synthesize('你好', dict(v.DEFAULTS), info)
        self.assertEqual(data, b'OGG:say:Grandpa (Chinese (Taiwan))')
        self.assertEqual(self.calls['edge'], 3)
        self.assertTrue(info['fallback'])
        self.assertEqual((info['engine'], info['voice']), ('say', 'Grandpa (Chinese (Taiwan))'))
        self.assertEqual(len(info['errors']), 3)

    def test_fallback_off_means_a_clear_error_after_the_retries(self):
        from edge_tts.exceptions import WebSocketError
        self._edge_fails(99, lambda: WebSocketError('closed'))
        info = {}
        with self.assertRaises(v.VoiceError) as caught:
            v.synthesize('你好', dict(v.DEFAULTS, fallback_engine=''), info)
        self.assertIn('after 3 attempt', str(caught.exception))
        self.assertEqual(self.calls['say'], 0)
        self.assertEqual(info['attempts'], 3)

    def test_bad_parameters_are_not_retried(self):
        self._edge_fails(99, lambda: ValueError('Invalid pitch'))
        with self.assertRaises(v.VoiceError):
            v.synthesize('你好', dict(v.DEFAULTS, fallback_engine=''), {})
        self.assertEqual(self.calls['edge'], 1)

    def test_retries_setting_is_honoured(self):
        from edge_tts.exceptions import NoAudioReceived
        self._edge_fails(99, lambda: NoAudioReceived('nothing'))
        info = {}
        v.synthesize('你好', dict(v.DEFAULTS, retries=0), info)
        self.assertEqual(self.calls['edge'], 1)
        self.assertTrue(info['fallback'])

    def test_report_is_filled_even_when_voice_is_off(self):
        v.unavailable_reason = self.originals['unavailable_reason']   # the real check
        info = {}
        with self.assertRaises(v.VoiceError):
            v.synthesize('你好', dict(v.DEFAULTS, enabled=False), info)
        self.assertEqual(info['attempts'], 0)
        self.assertIn('seconds', info)


if __name__ == '__main__':
    unittest.main()
