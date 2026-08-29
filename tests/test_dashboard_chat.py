"""爸菲特 inside the TUI dashboard.

The dashboard shares one asyncio loop between the marquee (0.2 s), the clock (1.0 s), the
5-second snapshot poll and the kill switch. A chat answer takes 12-18 s, so the provider call
must never touch that loop. These tests pin the contract without launching a terminal app.
"""

import types
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

import dashboard


class ChatWiringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.DashApp, _ = dashboard.run_tui(return_app=True)

    def test_c_is_bound_to_chat_and_nothing_was_displaced(self):
        keys = [b.key for b in self.DashApp.BINDINGS]
        self.assertIn('c', keys)
        # every previously-bound key must survive
        for key in ('q', 's', 'g', 'space', 'n', 'r', 'l', 'p', 'v'):
            self.assertIn(key, keys, key)
        self.assertEqual(len(keys), len(set(keys)), 'a duplicate binding would shadow one')

    def test_action_chat_exists(self):
        self.assertTrue(callable(getattr(self.DashApp, 'action_chat', None)))

    def test_chat_label_is_bilingual(self):
        for lang in ('en', 'zh'):
            self.assertTrue(dashboard.tr(lang, 'k_chat'), lang)
        self.assertNotEqual(dashboard.tr('en', 'k_chat'), dashboard.tr('zh', 'k_chat'))

    def test_run_tui_return_app_contract_is_unchanged(self):
        # tests/test_telegram_users.py unpacks this positionally as a 2-tuple.
        result = dashboard.run_tui(return_app=True)
        self.assertEqual(len(result), 2)


class ChatThreadingTests(unittest.TestCase):
    """The modal's worker body and reply guard, exercised directly."""

    @staticmethod
    def _chat_screen_class():
        """ChatScreen lives in run_tui's closure, not module globals.

        Reached through action_chat's closure cells rather than by widening
        run_tui(return_app=True), whose 2-tuple shape tests/test_telegram_users.py:124,151
        unpack positionally.
        """
        DashApp, _ = dashboard.run_tui(return_app=True)
        fn = DashApp.action_chat
        cells = dict(zip(fn.__code__.co_freevars,
                         (c.cell_contents for c in (fn.__closure__ or ()))))
        return cells['ChatScreen']

    def setUp(self):
        self.ChatScreen = self._chat_screen_class()
        self.applied = []

    def _worker_self(self):
        """A stand-in `self` for the worker body.

        ChatScreen's methods are plain functions, so they can be driven against a namespace.
        A real instance is unusable here: Textual's `is_mounted` is a read-only property.
        """
        applied = self.applied

        class App:
            @staticmethod
            def call_from_thread(fn, *a):
                fn(*a)

        return types.SimpleNamespace(
            _gen=1, user='test-user', is_mounted=True, app=App(),
            _apply=lambda gen, text, meta: applied.append((gen, text, meta)))

    def _widget_self(self, gen, mounted, sink):
        return types.SimpleNamespace(
            _gen=gen, is_mounted=mounted,
            query_one=lambda *a: types.SimpleNamespace(
                write=sink.append, update=lambda _t: None))

    def test_worker_calls_botffet_with_the_user_identity(self):
        seen = {}

        def fake_answer(question, user=None, on_event=None):
            seen['q'], seen['user'] = question, user
            return {'text': 'ok', 'kind': 'screen', 'provider_calls': 0}

        import botffet
        real = botffet.answer
        botffet.answer = fake_answer
        try:
            self.ChatScreen._ask(self._worker_self(), '/screen japan', 1)
        finally:
            botffet.answer = real
        self.assertEqual(seen['q'], '/screen japan')
        self.assertEqual(seen['user'], 'test-user')
        self.assertEqual(len(self.applied), 1)

    def test_provider_exception_becomes_a_message_not_a_crash(self):
        import botffet
        real = botffet.answer

        def boom(*a, **k):
            raise RuntimeError('provider down')

        botffet.answer = boom
        try:
            self.ChatScreen._ask(self._worker_self(), 'why?', 1)
        finally:
            botffet.answer = real
        _, text, meta = self.applied[0]
        self.assertIn('provider down', text)
        self.assertEqual(meta, 'error')

    def test_superseded_reply_is_discarded(self):
        # Worker.cancel stops the asyncio task, not the OS thread, so a stale answer can
        # still arrive after the user has asked something new.
        written = []
        me = self._widget_self(gen=5, mounted=True, sink=written)
        self.ChatScreen._apply(me, 3, 'stale answer', 'meta')
        self.assertEqual(written, [])
        self.ChatScreen._apply(me, 5, 'current answer', 'meta')
        self.assertEqual(written, ['current answer'])

    def test_reply_after_dismissal_is_discarded(self):
        written = []
        me = self._widget_self(gen=1, mounted=False, sink=written)
        self.ChatScreen._apply(me, 1, 'answer', 'meta')
        self.assertEqual(written, [], 'must not write into an unmounted widget')


if __name__ == '__main__':
    unittest.main()


class ChatTraceTests(unittest.TestCase):
    """The live reasoning trace — the modal must show WHICH queries ran, under the same
    generation/mount guards as the final answer."""

    def setUp(self):
        self.ChatScreen = ChatThreadingTests._chat_screen_class()

    def _widget(self, gen, mounted, sink):
        return types.SimpleNamespace(
            _gen=gen, is_mounted=mounted,
            query_one=lambda *a: types.SimpleNamespace(
                write=sink.append, update=lambda _t: None))

    def test_trace_line_is_rendered(self):
        written = []
        self.ChatScreen._trace(self._widget(1, True, written), 1, '   ↳ screen(country=Japan)')
        self.assertEqual(written, ['   ↳ screen(country=Japan)'])

    def test_superseded_trace_is_discarded(self):
        written = []
        self.ChatScreen._trace(self._widget(4, True, written), 2, '   ↳ stale')
        self.assertEqual(written, [])

    def test_trace_after_dismissal_is_discarded(self):
        written = []
        self.ChatScreen._trace(self._widget(1, False, written), 1, '   ↳ gone')
        self.assertEqual(written, [])

    def test_worker_forwards_an_on_event_callback(self):
        """botffet.answer must receive on_event, or the modal shows no reasoning."""
        seen = {}

        def fake_answer(question, user=None, on_event=None):
            seen['on_event'] = on_event
            if on_event:
                on_event('tool', {'name': 'brief', 'input': {'query': '2330'}})
                on_event('done', {'turns': 2})
            return {'text': 'ok', 'kind': 'synthesis', 'provider_calls': 1, 'turns': 2}

        lines, applied = [], []

        class App:
            @staticmethod
            def call_from_thread(fn, *a):
                fn(*a)

        me = types.SimpleNamespace(
            _gen=1, user='u', is_mounted=True, app=App(),
            _trace=lambda gen, line: lines.append(line),
            _apply=lambda gen, text, meta: applied.append(meta))

        import botffet
        real = botffet.answer
        botffet.answer = fake_answer
        try:
            self.ChatScreen._ask(me, 'why?', 1)
        finally:
            botffet.answer = real
        self.assertIsNotNone(seen['on_event'])
        self.assertEqual(len(lines), 2)
        self.assertIn('brief', lines[0])
        self.assertIn('2 turns', applied[0])
