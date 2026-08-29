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
        """The reply must survive a broken engine: _speak logs and returns."""
        import asyncio
        import botffet_bot

        class Msg:
            chat_id = 'x'
            sent = []

            async def reply_voice(self, voice):
                self.sent.append(voice)

        def boom(_text):
            raise v.VoiceError('no network')
        real = botffet_bot.botffet_voice
        botffet_bot.botffet_voice = type('V', (), {'voice_for': staticmethod(boom)})
        try:
            asyncio.run(botffet_bot._speak(Msg(), '你好', {'label': 'Dad'}))
            self.assertEqual(Msg.sent, [])
            # and an opted-out person gets no bubble at all, engine or not
            asyncio.run(botffet_bot._speak(Msg(), '你好', {'label': 'Dad', 'voice': False}))
        finally:
            botffet_bot.botffet_voice = real


if __name__ == '__main__':
    unittest.main()
