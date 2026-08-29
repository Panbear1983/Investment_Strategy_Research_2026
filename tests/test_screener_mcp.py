"""Protocol-level tests for the MCP screener server.

These spawn the real server as a subprocess and speak newline-delimited JSON-RPC to it —
the same transport `claude -p --mcp-config` uses — so a framing or handshake bug fails here
instead of live in a Telegram conversation.
"""

import json
import os
import subprocess
import sys
import unittest

SCRIPTS = os.path.join(os.path.dirname(__file__), '..', 'scripts')
SERVER = os.path.join(SCRIPTS, 'screener_mcp.py')


class McpServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.proc = subprocess.Popen(
            [sys.executable, SERVER],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1)
        cls._id = 0
        # handshake once for the whole class — the server is stateless after initialize
        reply = cls._rpc('initialize', {'protocolVersion': '2024-11-05',
                                        'capabilities': {},
                                        'clientInfo': {'name': 'test', 'version': '0'}})
        assert reply['result']['serverInfo']['name'] == 'screener'
        cls._notify('notifications/initialized')

    @classmethod
    def tearDownClass(cls):
        cls.proc.stdin.close()
        cls.proc.wait(timeout=10)

    @classmethod
    def _rpc(cls, method, params=None):
        cls._id += 1
        cls.proc.stdin.write(json.dumps(
            {'jsonrpc': '2.0', 'id': cls._id, 'method': method,
             'params': params or {}}) + '\n')
        line = cls.proc.stdout.readline()
        assert line, f'server closed stdout during {method}'
        return json.loads(line)

    @classmethod
    def _notify(cls, method):
        cls.proc.stdin.write(json.dumps({'jsonrpc': '2.0', 'method': method}) + '\n')

    @classmethod
    def _call(cls, name, arguments):
        reply = cls._rpc('tools/call', {'name': name, 'arguments': arguments})
        result = reply['result']
        payload = json.loads(result['content'][0]['text'])
        return result, payload

    def test_tools_list_exposes_exactly_the_readonly_tools(self):
        tools = self._rpc('tools/list')['result']['tools']
        self.assertEqual(sorted(t['name'] for t in tools),
                         ['brief', 'facets', 'memory_search', 'screen', 'video_brief',
                          'video_search'])
        # No tool schema may accept a path — that would be a file-read primitive.
        for t in tools:
            props = json.dumps(t['inputSchema']).lower()
            self.assertNotIn('path', props, t['name'])

    def test_no_tool_can_write_anything(self):
        """The real invariant behind the list above.

        video_search and video_brief were added so 爸菲特 can discuss what a video said. They
        read. Research direction is suggested in the model's own words and filed as a proposal
        by plain Python in botffet, so nothing here needs — or has — a write primitive.
        """
        tools = self._rpc('tools/list')['result']['tools']
        forbidden = ('propose', 'set_', 'write', 'update', 'insert', 'delete', 'focus')
        for t in tools:
            for word in forbidden:
                self.assertNotIn(word, t['name'].lower(), t['name'])

    def test_memory_search_answers_nothing_without_an_identity(self):
        # This class spawns the server with no ISR_CHAT_ID, exactly as a stray launch would.
        _, payload = self._call('memory_search', {'text': '2330'})
        self.assertEqual(payload['matched'], 0)
        self.assertIn('identity', payload['note'])

    def test_video_tools_answer_before_any_video_has_been_added(self):
        """A fresh install has no video tables; that must not fail a conversation."""
        _, payload = self._call('video_search', {'text': 'anything'})
        self.assertIn('results', payload)
        self.assertEqual(payload.get('matched', 0), len(payload['results']))

    def test_screen_flagship_query(self):
        _, payload = self._call('screen', {'country': 'Japan', 'tier': '隱形冠軍',
                                           'match': ['ASML']})
        names = [r['Company'] for r in payload['results']]
        self.assertIn('Harmonic Drive Systems (6324.T)', names)
        self.assertGreaterEqual(payload['matched'], 3)

    def test_screen_truncates_prose_and_caps_limit(self):
        _, payload = self._call('screen', {'country': 'Taiwan', 'limit': 999})
        self.assertLessEqual(len(payload['results']), 40)
        for r in payload['results']:
            self.assertLessEqual(len(r['Core Business']), 221)  # 220 + ellipsis

    def test_brief_returns_full_record_with_known_since(self):
        _, payload = self._call('brief', {'query': '2330'})
        self.assertGreaterEqual(payload['matched'], 1)
        rec = payload['results'][0]
        self.assertIn('台積電', rec['Company'])
        self.assertIn('Technical Moat', rec)          # full 22-field record
        self.assertIn('known_since', rec)             # per-row freshness, not file mtime
        if rec['known_since'] is not None:
            self.assertRegex(rec['known_since'], r'^\d{4}-\d{2}-\d{2}$')

    def test_facets_lists_vocabulary(self):
        _, payload = self._call('facets', {'name': 'tiers', 'min_count': 10})
        values = [v['value'] for v in payload['values']]
        self.assertIn('龍頭股', values)

    def test_unknown_tool_is_an_error_result_not_a_crash(self):
        result, _ = self._rpc('tools/call', {'name': 'read_file',
                                             'arguments': {}})['result'], None
        self.assertTrue(result['isError'])
        # and the server must still answer afterwards
        _, payload = self._call('facets', {'name': 'countries'})
        self.assertTrue(payload['values'])

    def test_bad_facet_name_is_flagged_as_error(self):
        result, payload = self._call('facets', {'name': 'secrets'})
        self.assertTrue(result['isError'])
        self.assertIn('error', payload)

    def test_unknown_method_returns_jsonrpc_error(self):
        reply = self._rpc('resources/list')
        self.assertEqual(reply['error']['code'], -32601)


if __name__ == '__main__':
    unittest.main()


class MemorySearchScopeTests(unittest.TestCase):
    """memory_search sees one person's archive and nobody else's, decided by environment."""

    def _server(self, env_extra, db):
        env = dict(os.environ, ISR_WORKFLOW_DB=db, **env_extra)
        proc = subprocess.Popen([sys.executable, SERVER], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, bufsize=1, env=env)
        self.addCleanup(proc.wait, timeout=10)
        self.addCleanup(proc.stdin.close)

        def rpc(method, params=None, _id=[0]):
            _id[0] += 1
            proc.stdin.write(json.dumps({'jsonrpc': '2.0', 'id': _id[0], 'method': method,
                                         'params': params or {}}) + '\n')
            return json.loads(proc.stdout.readline())
        rpc('initialize', {'protocolVersion': '2024-11-05', 'capabilities': {},
                           'clientInfo': {'name': 'test', 'version': '0'}})
        return lambda text: json.loads(rpc('tools/call', {
            'name': 'memory_search', 'arguments': {'text': text}})['result']['content'][0]['text'])

    def test_only_the_named_persons_archive_is_searched(self):
        import tempfile
        sys.path.insert(0, SCRIPTS)
        import botffet
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, 'q.sqlite3')
            botffet.history_append('dad', 'user', '我持有台積電 2330 很多年了', db)
            botffet.history_append('dad', 'assistant', '了解，2330 是您的核心持股。', db)
            botffet.history_append('peter', 'user', '2330 today?', db)
            search = self._server({'ISR_CHAT_ID': 'dad'}, db)
            hits = search('2330')
            self.assertEqual(hits['matched'], 2)
            self.assertTrue(all('today' not in h['said'] for h in hits['results']),
                            "Peter's turn must not be visible to Dad's session")
            self.assertEqual(hits['results'][0]['who'], '爸菲特')     # newest first
            self.assertTrue(hits['results'][0]['when'])
            self.assertEqual(search('液冷')['matched'], 0)
