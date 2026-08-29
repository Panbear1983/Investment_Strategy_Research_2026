"""The video engine: links in, quoted reading material out, and nothing else.

The engine is deliberately inert — it must never steer the scraper. What it must get right is
that a note it stores was actually said in the video, which is what the verbatim-quote rule
enforces here.
"""

import os
import sys
import tempfile
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

import video_intel as vi
from research_state import DEEP_RESEARCHED, WorkflowState


class LinkParsingTests(unittest.TestCase):
    VALID = {
        'https://www.youtube.com/watch?v=aircAruvnKk': 'aircAruvnKk',
        'https://www.youtube.com/watch?v=aircAruvnKk&t=90s': 'aircAruvnKk',
        'https://youtu.be/aircAruvnKk': 'aircAruvnKk',
        'https://youtu.be/aircAruvnKk?si=abc123': 'aircAruvnKk',
        'https://www.youtube.com/shorts/aircAruvnKk': 'aircAruvnKk',
        'https://www.youtube.com/embed/aircAruvnKk': 'aircAruvnKk',
        'https://www.youtube.com/live/aircAruvnKk': 'aircAruvnKk',
        'https://m.youtube.com/watch?v=aircAruvnKk&list=PLxyz&index=4': 'aircAruvnKk',
        'youtube.com/watch?v=aircAruvnKk': 'aircAruvnKk',
        'aircAruvnKk': 'aircAruvnKk',
    }
    INVALID = ['https://vimeo.com/12345', 'https://example.com/watch?v=aircAruvnKk',
               'https://www.youtube.com/watch?v=tooshort', 'not a url', '', None,
               'https://www.youtube.com/feed/subscriptions']

    def test_every_shape_youtube_hands_out(self):
        for url, expected in self.VALID.items():
            self.assertEqual(vi.video_id_from_url(url), expected, url)

    def test_non_youtube_and_malformed_are_rejected(self):
        for url in self.INVALID:
            self.assertIsNone(vi.video_id_from_url(url), url)

    def test_a_lookalike_host_is_not_accepted(self):
        self.assertIsNone(vi.video_id_from_url('https://youtube.com.evil.tld/watch?v=aircAruvnKk'))

    def test_ids_are_pulled_out_of_a_pasted_wall_of_text(self):
        blob = ('watch this https://youtu.be/aircAruvnKk it was good,\n'
                'and https://www.youtube.com/watch?v=dQw4w9WgXcQ too.\n'
                'https://youtu.be/aircAruvnKk again\n'
                'oHg5SJYRHA0\n')
        self.assertEqual(vi.extract_video_ids(blob),
                         ['aircAruvnKk', 'dQw4w9WgXcQ', 'oHg5SJYRHA0'])

    def test_trailing_punctuation_does_not_break_a_link(self):
        self.assertEqual(vi.extract_video_ids('see https://youtu.be/aircAruvnKk.'),
                         ['aircAruvnKk'])

    def test_watch_url_carries_a_timestamp(self):
        self.assertTrue(vi.watch_url('aircAruvnKk', 90).endswith('&t=90s'))
        self.assertNotIn('&t=', vi.watch_url('aircAruvnKk', 0))


class VttTests(unittest.TestCase):
    VTT = """WEBVTT
Kind: captions
Language: en

00:00:01.000 --> 00:00:03.000
first line

00:00:03.000 --> 00:00:05.000
first line
second line

00:01:05.500 --> 00:01:07.000
<c>third</c> line
"""

    def test_timings_headers_and_markup_are_stripped(self):
        segments = vi.parse_vtt(self.VTT)
        self.assertEqual([line for _, line in segments],
                         ['first line', 'second line', 'third line'])

    def test_rolling_caption_duplicates_are_dropped(self):
        # Auto captions repeat each line as the next scrolls in; keeping them roughly doubles
        # the transcript and wrecks the chunk budget.
        self.assertEqual(sum(1 for _, line in vi.parse_vtt(self.VTT) if line == 'first line'), 1)

    def test_start_times_survive_as_seconds(self):
        self.assertEqual([start for start, _ in vi.parse_vtt(self.VTT)], [1, 3, 65])

    def test_empty_input_is_not_an_error(self):
        self.assertEqual(vi.parse_vtt(''), [])


class OffsetTests(unittest.TestCase):
    SEGMENTS = [(0, 'alpha'), (10, 'beta'), (20, 'gamma')]

    def test_flatten_builds_text_and_an_offset_map(self):
        text, index = vi.flatten(self.SEGMENTS)
        self.assertEqual(text, 'alpha\nbeta\ngamma')
        self.assertEqual(index, [(0, 0), (6, 10), (11, 20)])

    def test_an_offset_resolves_to_the_moment_it_was_said(self):
        _, index = vi.flatten(self.SEGMENTS)
        self.assertEqual(vi.seconds_for_offset(index, 0), 0)
        self.assertEqual(vi.seconds_for_offset(index, 7), 10)
        self.assertEqual(vi.seconds_for_offset(index, 99), 20)

    def test_a_missing_map_costs_a_timestamp_not_a_note(self):
        self.assertEqual(vi.seconds_for_offset([], 42), 0)


class ChunkTests(unittest.TestCase):
    def test_short_text_is_one_chunk(self):
        self.assertEqual(vi.chunks('abc', size=10), ['abc'])

    def test_long_text_overlaps_so_a_sentence_is_not_cut_in_half(self):
        text = 'x' * 250
        parts = vi.chunks(text, size=100, overlap=20)
        self.assertGreater(len(parts), 1)
        self.assertEqual(len(parts[0]), 100)
        self.assertEqual(''.join(p[:80] for p in parts)[:len(text)][:50], text[:50])


class QuoteVerificationTests(unittest.TestCase):
    TRANSCRIPT = ('低軌衛星的需求在明年會明顯增加\n'
                  'we think the supply chain is tightening\n'
                  '台積電的先進封裝產能仍然吃緊')

    def _index(self):
        return [(0, 0), (16, 30), (55, 60)]

    def test_a_real_quote_is_kept_and_timestamped(self):
        payload = {'summary': 'overview',
                   'sectors': [{'sector': '低軌衛星', 'note': '需求增加',
                                'quote': '低軌衛星的需求在明年會明顯增加'}]}
        notes, dropped = vi.validate_notes(payload, self.TRANSCRIPT, self._index())
        self.assertEqual(dropped, 0)
        sector = [n for n in notes if n['kind'] == 'sector'][0]
        self.assertEqual(sector['sector'], '低軌衛星')
        self.assertEqual(sector['start_seconds'], 0)

    def test_a_quote_the_video_never_said_is_dropped(self):
        payload = {'sectors': [{'sector': '核融合', 'note': '大幅成長',
                                'quote': '核融合將在明年商轉'}]}
        notes, dropped = vi.validate_notes(payload, self.TRANSCRIPT, self._index())
        self.assertEqual(dropped, 1)
        self.assertEqual([n for n in notes if n['kind'] == 'sector'], [])

    def test_whitespace_differences_do_not_reject_a_real_quote(self):
        payload = {'companies': [{'name': '台積電', 'ticker': '2330', 'note': '產能吃緊',
                                  'quote': '台積電的 先進封裝 產能仍然吃緊'}]}
        notes, dropped = vi.validate_notes(payload, self.TRANSCRIPT, self._index())
        self.assertEqual(dropped, 0)
        self.assertEqual(notes[0]['ticker'], '2330')

    def test_a_note_missing_its_quote_is_dropped(self):
        payload = {'companies': [{'name': '台積電', 'note': '產能吃緊'}]}
        _, dropped = vi.validate_notes(payload, self.TRANSCRIPT, self._index())
        self.assertEqual(dropped, 1)

    def test_the_summary_survives_without_a_quote(self):
        notes, _ = vi.validate_notes({'summary': 'overview'}, self.TRANSCRIPT)
        self.assertEqual(notes, [{'kind': 'summary', 'note': 'overview'}])

    def test_a_non_object_response_yields_nothing_rather_than_raising(self):
        self.assertEqual(vi.validate_notes(['not', 'a', 'dict'], self.TRANSCRIPT), ([], 0))

    def test_tickers_are_upper_cased_for_matching(self):
        payload = {'companies': [{'name': 'x', 'ticker': 'tsm', 'note': 'n',
                                  'quote': '台積電的先進封裝產能仍然吃緊'}]}
        notes, _ = vi.validate_notes(payload, self.TRANSCRIPT, self._index())
        self.assertEqual(notes[0]['ticker'], 'TSM')


class TranscriptChoiceTests(unittest.TestCase):
    """Prefer what was actually SAID over a community translation of it."""

    @staticmethod
    def _track(code, generated):
        return types.SimpleNamespace(language_code=code, is_generated=generated)

    def test_a_human_transcript_in_the_spoken_language_wins(self):
        tracks = [self._track('zh-TW', False), self._track('en', False),
                  self._track('en', True)]
        self.assertEqual(vi._pick_transcript(tracks).language_code, 'en')
        self.assertFalse(vi._pick_transcript(tracks).is_generated)

    def test_the_generated_track_is_used_when_there_is_no_human_one(self):
        tracks = [self._track('zh-TW', False), self._track('ja', True)]
        self.assertEqual(vi._pick_transcript(tracks).language_code, 'ja')

    def test_chinese_is_the_fallback_when_nothing_identifies_the_original(self):
        tracks = [self._track('fr', False), self._track('zh-TW', False)]
        self.assertEqual(vi._pick_transcript(tracks).language_code, 'zh-TW')

    def test_no_tracks_at_all_is_handled(self):
        self.assertIsNone(vi._pick_transcript([]))


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = WorkflowState(os.path.join(self.tmp.name, 'workflow.sqlite3'))

    def tearDown(self):
        self.tmp.cleanup()

    def test_the_same_video_from_two_url_shapes_is_stored_once(self):
        added, known = vi.add_links(
            self.state,
            'https://youtu.be/aircAruvnKk and https://www.youtube.com/watch?v=aircAruvnKk')
        self.assertEqual((added, known), (['aircAruvnKk'], []))
        added2, known2 = vi.add_links(self.state, 'https://youtu.be/aircAruvnKk')
        self.assertEqual((added2, known2), ([], ['aircAruvnKk']))
        self.assertEqual(len(self.state.videos()), 1)

    def test_text_with_no_links_adds_nothing(self):
        self.assertEqual(vi.add_links(self.state, 'just a thought'), ([], []))

    def test_a_transcript_and_its_offsets_round_trip(self):
        source_id, _ = self.state.add_video('aircAruvnKk', 'https://youtu.be/aircAruvnKk')
        self.state.store_transcript(source_id, 'hello world', lang='en',
                                    offsets=[(0, 0), (6, 12)], title='T', channel='C')
        source = self.state.video(source_id=source_id)
        self.assertEqual((source['status'], source['char_count'], source['channel']),
                         ('fetched', 11, 'C'))
        self.assertEqual(vi._index_for(source), [(0, 0), (6, 12)])

    def test_a_video_stored_before_offsets_existed_still_works(self):
        self.assertEqual(vi._index_for({'offsets_json': ''}), [])
        self.assertEqual(vi._index_for({'offsets_json': 'not json'}), [])

    def test_re_summarising_replaces_rather_than_duplicates(self):
        source_id, _ = self.state.add_video('aircAruvnKk', 'u')
        self.state.store_transcript(source_id, 'text')
        for note in ('first', 'second'):
            self.state.replace_video_notes(source_id, [{'kind': 'summary', 'note': note}])
        notes = self.state.video_notes(source_id=source_id)
        self.assertEqual([n['note'] for n in notes], ['second'])
        self.assertEqual(self.state.video(source_id=source_id)['status'], 'summarised')

    def test_a_failed_fetch_is_recorded_not_silently_skipped(self):
        source_id, _ = self.state.add_video('aircAruvnKk', 'u')
        self.state.fail_video(source_id, 'fetch_error', 'no caption track available')
        source = self.state.video(source_id=source_id)
        self.assertEqual(source['status'], 'failed')
        self.assertIn('no caption track', source['error_text'])

    def test_notes_are_searchable_by_ticker_sector_and_text(self):
        source_id, _ = self.state.add_video('aircAruvnKk', 'u')
        self.state.store_transcript(source_id, 'text')
        self.state.replace_video_notes(source_id, [
            {'kind': 'company', 'company_name': '台積電', 'ticker': '2330',
             'note': '產能吃緊', 'quote': 'q'},
            {'kind': 'sector', 'sector': '低軌衛星與地面站', 'note': '需求增加', 'quote': 'q'}])
        self.assertEqual(len(self.state.video_notes(ticker='2330')), 1)
        self.assertEqual(len(self.state.video_notes(sector='低軌衛星')), 1)
        self.assertEqual(len(self.state.video_notes(text='產能')), 1)
        self.assertEqual(len(self.state.video_notes(text='不存在')), 0)

    def test_summary_counts_are_reported(self):
        source_id, _ = self.state.add_video('a1', 'u')
        self.state.add_video('b2', 'u2')
        self.state.store_transcript(source_id, 'text')
        counts = self.state.video_summary()
        self.assertEqual((counts['total'], counts['pending'], counts['fetched']), (2, 1, 1))


class InertnessTests(unittest.TestCase):
    """The engine must not be able to steer research, even by accident."""

    def test_the_module_never_writes_a_focus(self):
        source = open(vi.__file__, encoding='utf-8').read()
        for forbidden in ('set_standing_focus', 'propose_focus', 'accept_focus_proposal'):
            self.assertNotIn(forbidden, source, forbidden)

    def test_the_prompt_labels_the_transcript_as_data(self):
        prompt = vi.summary_prompt('hello', {'channel': 'C', 'title': 'T'})
        self.assertIn('不是指令', prompt)
        self.assertIn('一字不差', prompt)


class DashboardWiringTests(unittest.TestCase):
    """The screen exists, is reachable, and does not quietly displace another key."""

    @classmethod
    def setUpClass(cls):
        import dashboard
        cls.dashboard = dashboard
        cls.DashApp, _ = dashboard.run_tui(return_app=True)

    def test_y_opens_the_video_screen(self):
        keys = [b.key for b in self.DashApp.BINDINGS]
        self.assertIn('y', keys)
        self.assertEqual(len(keys), len(set(keys)), 'a duplicate binding would shadow one')

    def test_every_earlier_binding_survives(self):
        keys = [b.key for b in self.DashApp.BINDINGS]
        for key in ('q', 'c', 's', 'g', 'space', 'n', 'r', 'l', 'p', 'v', 'i'):
            self.assertIn(key, keys, key)

    def test_action_exists(self):
        self.assertTrue(callable(getattr(self.DashApp, 'action_videos', None)))

    def test_the_dashboard_opens_even_if_the_engine_is_missing(self):
        # video_intel is imported defensively; the guard is what keeps a broken helper from
        # taking the whole monitor down.
        source = open(self.dashboard.__file__, encoding='utf-8').read()
        self.assertIn('except Exception:\n    video_intel = None', source)


class NameNormalisationTests(unittest.TestCase):
    def test_punctuation_and_case_are_ignored(self):
        for a, b in (('Alphabet (Google)', 'alphabet'), ('S&P Global', 's&p  global')):
            self.assertEqual(vi.normalise_company(a), vi.normalise_company(b), (a, b))

    def test_two_spellings_share_a_variant(self):
        for a, b in (('Berkshire Hathaway Inc.', 'Berkshire Hathaway'),
                     ('ARM Holdings plc', 'ARM Holdings'),
                     ('Lennar Corp', 'Lennar')):
            self.assertTrue(set(vi.name_variants(a)) & set(vi.name_variants(b)), (a, b))

    def test_variants_run_most_specific_first(self):
        self.assertEqual(vi.name_variants('ARM Holdings plc')[0], 'armholdingsplc')

    def test_different_companies_do_not_collapse(self):
        self.assertNotEqual(vi.normalise_company('Spirit'),
                            vi.normalise_company('Spirit AeroSystems'))

    def test_a_suffix_that_is_the_whole_name_survives(self):
        self.assertTrue(vi.normalise_company('Group'))


class TickerResolutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = WorkflowState(os.path.join(self.tmp.name, 'workflow.sqlite3'))
        for name in ('Berkshire Hathaway (BRK.B)', 'Spirit AeroSystems (SPR)',
                     'Nvidia (NVDA)', 'Acme Duplicate (AAA)', 'Acme Duplicate (BBB)'):
            self.state.upsert_company({'Company': name, 'Country': 'USA'}, DEEP_RESEARCHED)

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_blank_ticker_is_filled_from_the_corpus(self):
        notes = [{'kind': 'company', 'company_name': 'Berkshire Hathaway Inc.', 'ticker': ''}]
        self.assertEqual(vi.resolve_tickers(self.state, notes), 1)
        self.assertEqual(notes[0]['ticker'], 'BRK.B')

    def test_a_partial_name_is_left_blank_rather_than_guessed(self):
        # 'Spirit' in a video means Spirit Airlines; SPR is Spirit AeroSystems. A wrong ticker
        # attributes the remark to the wrong company, which is worse than no ticker.
        notes = [{'kind': 'company', 'company_name': 'Spirit', 'ticker': ''}]
        self.assertEqual(vi.resolve_tickers(self.state, notes), 0)
        self.assertEqual(notes[0]['ticker'], '')

    def test_an_ambiguous_name_is_skipped(self):
        notes = [{'kind': 'company', 'company_name': 'Acme Duplicate', 'ticker': ''}]
        self.assertEqual(vi.resolve_tickers(self.state, notes), 0)

    def test_a_ticker_the_model_supplied_is_not_overwritten(self):
        notes = [{'kind': 'company', 'company_name': 'Nvidia', 'ticker': 'NVDA.MX'}]
        vi.resolve_tickers(self.state, notes)
        self.assertEqual(notes[0]['ticker'], 'NVDA.MX')

    def test_sector_and_summary_notes_are_untouched(self):
        notes = [{'kind': 'sector', 'sector': 'Nvidia', 'ticker': ''},
                 {'kind': 'summary', 'note': 'x'}]
        self.assertEqual(vi.resolve_tickers(self.state, notes), 0)

    def test_a_company_absent_from_the_corpus_stays_blank(self):
        notes = [{'kind': 'company', 'company_name': 'Kroger', 'ticker': ''}]
        self.assertEqual(vi.resolve_tickers(self.state, notes), 0)


class DedupeTests(unittest.TestCase):
    @staticmethod
    def _note(name, quote, note='n'):
        return {'kind': 'company', 'company_name': name, 'quote': quote, 'note': note}

    def test_the_same_observation_repeated_across_chunks_is_dropped(self):
        quote = 'A few names immediately stand out: PayPal, Adobe, and Flutter.'
        kept = vi.dedupe_notes([self._note('Adobe', quote), self._note('Adobe', quote)])
        self.assertEqual(len(kept), 1)

    def test_two_different_remarks_about_one_company_both_survive(self):
        # Berkshire trimming Capital One and SoftBank opening a position in it are two facts.
        kept = vi.dedupe_notes([self._note('Capital One', 'They trimmed half their Capital One'),
                                self._note('Capital One', 'he opened two small new positions')])
        self.assertEqual(len(kept), 2)

    def test_the_fuller_wording_wins_a_tie(self):
        quote = 'same quote'
        kept = vi.dedupe_notes([self._note('Adobe', quote, 'short'),
                                self._note('Adobe', quote, 'a much longer explanation')])
        self.assertEqual(kept[0]['note'], 'a much longer explanation')

    def test_whitespace_differences_do_not_defeat_the_match(self):
        kept = vi.dedupe_notes([self._note('Adobe', 'one two three'),
                                self._note('Adobe', 'one  two\nthree')])
        self.assertEqual(len(kept), 1)

    def test_summaries_are_never_deduped_away(self):
        kept = vi.dedupe_notes([{'kind': 'summary', 'note': 'a'}, {'kind': 'summary', 'note': 'b'}])
        self.assertEqual(len(kept), 2)

    def test_order_is_preserved(self):
        kept = vi.dedupe_notes([self._note('A', 'q1'), self._note('B', 'q2'),
                                self._note('A', 'q1')])
        self.assertEqual([n['company_name'] for n in kept], ['A', 'B'])


class WhisperJsonTests(unittest.TestCase):
    """Transcribed audio must arrive in the same shape as captions, or nothing downstream works."""

    PAYLOAD = {'transcription': [
        {'offsets': {'from': 0, 'to': 4200}, 'text': ' 台積電的先進封裝產能仍然吃緊'},
        {'offsets': {'from': 4200, 'to': 9000}, 'text': '  '},
        {'offsets': {'from': 9000, 'to': 12000}, 'text': '低軌衛星的需求明年會增加'}]}

    def test_it_produces_the_same_shape_as_parse_vtt(self):
        segments = vi.parse_whisper_json(self.PAYLOAD)
        self.assertEqual(segments, [(0, '台積電的先進封裝產能仍然吃緊'),
                                    (9, '低軌衛星的需求明年會增加')])

    def test_offsets_are_milliseconds_converted_to_seconds(self):
        self.assertEqual(vi.parse_whisper_json(self.PAYLOAD)[1][0], 9)

    def test_blank_segments_are_dropped(self):
        self.assertEqual(len(vi.parse_whisper_json(self.PAYLOAD)), 2)

    def test_a_segment_without_offsets_still_survives(self):
        segments = vi.parse_whisper_json({'transcription': [{'text': 'hello'}]})
        self.assertEqual(segments, [(0, 'hello')])

    def test_empty_or_malformed_input_is_not_an_error(self):
        for payload in ({}, {'transcription': []}, {'transcription': None}, None):
            self.assertEqual(vi.parse_whisper_json(payload), [])

    def test_it_flattens_into_a_usable_transcript_and_index(self):
        text, index = vi.flatten(vi.parse_whisper_json(self.PAYLOAD))
        self.assertIn('低軌衛星', text)
        self.assertEqual(vi.seconds_for_offset(index, text.index('低軌衛星')), 9)


class WhisperAvailabilityTests(unittest.TestCase):
    def test_the_flag_can_switch_the_audio_route_off(self):
        self.assertFalse(vi.whisper_available({'whisper_enabled': False}))

    def test_a_missing_model_file_means_unavailable(self):
        self.assertFalse(vi.whisper_available({'whisper_model_path': '/nonexistent/model.bin'}))

    def test_a_missing_binary_means_unavailable(self):
        with mock.patch.object(vi.shutil, 'which', return_value=None):
            self.assertFalse(vi.whisper_available({}))

    def test_the_time_estimate_scales_with_video_length(self):
        cfg = {'whisper_realtime_factor': 4}
        self.assertEqual(vi.estimate_transcribe_seconds(1954, cfg), 488)
        self.assertGreater(vi.estimate_transcribe_seconds(7200, cfg),
                           vi.estimate_transcribe_seconds(1954, cfg))

    def test_a_nonsense_factor_does_not_divide_by_zero(self):
        self.assertGreater(vi.estimate_transcribe_seconds(60, {'whisper_realtime_factor': 0}), 0)


class TranscriptRouteOrderTests(unittest.TestCase):
    """Cheapest route first. Audio costs minutes of this machine's time; captions cost seconds."""

    def test_audio_is_not_touched_when_captions_exist(self):
        # The caption API is imported inside fetch_transcript, so it is patched at its own
        # module — otherwise this test quietly makes a real network call and proves nothing.
        track = mock.Mock(language_code='zh-TW', is_generated=True)
        track.fetch.return_value = [mock.Mock(start=0, text='有字幕')]
        api = mock.Mock()
        api.return_value.list.return_value = [track]
        with mock.patch('youtube_transcript_api.YouTubeTranscriptApi', api), \
             mock.patch.object(vi, 'transcribe_audio',
                               side_effect=AssertionError('must not transcribe')), \
             mock.patch.object(vi, '_fetch_transcript_via_ytdlp',
                               side_effect=AssertionError('must not reach yt-dlp')):
            text, _, lang = vi.fetch_transcript('abcdefghijk')
        self.assertEqual(text, '有字幕')
        self.assertEqual(lang, 'zh-TW')

    def test_audio_is_used_when_both_caption_routes_find_nothing(self):
        with mock.patch.object(vi, '_pick_transcript', return_value=None), \
             mock.patch.object(vi, '_fetch_transcript_via_ytdlp',
                               side_effect=vi.NoTranscript('none')), \
             mock.patch.object(vi, 'whisper_available', return_value=True), \
             mock.patch.object(vi, 'transcribe_audio', return_value=[(0, '語音轉出來的')]):
            text, _, lang = vi.fetch_transcript('abcdefghijk', {'whisper_language': 'zh'})
        self.assertEqual(text, '語音轉出來的')
        self.assertTrue(lang.startswith('audio'), lang)

    def test_without_whisper_the_old_behaviour_is_unchanged(self):
        with mock.patch.object(vi, '_pick_transcript', return_value=None), \
             mock.patch.object(vi, '_fetch_transcript_via_ytdlp',
                               side_effect=vi.NoTranscript('none')), \
             mock.patch.object(vi, 'whisper_available', return_value=False):
            with self.assertRaises(vi.NoTranscript):
                vi.fetch_transcript('abcdefghijk')


class AudioCleanupTests(unittest.TestCase):
    """The audio is a working file, not a record. Tens of MB per video."""

    def test_the_temp_directory_is_removed_even_when_transcription_fails(self):
        seen = {}
        real = vi.tempfile.TemporaryDirectory

        class Watched(real):
            def __init__(self, *a, **kw):
                super().__init__(*a, **kw)
                seen['path'] = self.name

        with mock.patch.object(vi.tempfile, 'TemporaryDirectory', Watched), \
             mock.patch.object(vi, 'whisper_available', return_value=True), \
             mock.patch.object(vi.subprocess, 'run',
                               side_effect=OSError('yt-dlp exploded')):
            with self.assertRaises(RuntimeError):
                vi.transcribe_audio('abcdefghijk', {'whisper_model_path': __file__})
        self.assertFalse(os.path.exists(seen['path']), 'the audio was left behind')

    def test_it_refuses_up_front_when_whisper_is_unavailable(self):
        with mock.patch.object(vi, 'whisper_available', return_value=False):
            with self.assertRaises(vi.NoTranscript):
                vi.transcribe_audio('abcdefghijk', {})


class ChunkCostTests(unittest.TestCase):
    """The Antigravity budget counts calls, so chunk size is the cost lever."""

    def test_a_typical_video_now_costs_one_call(self):
        self.assertEqual(len(vi.chunks('x' * 33218)), 1)

    def test_the_two_stored_videos_cost_far_fewer_calls_than_before(self):
        # Measured on the real stored transcripts: 7 calls at the old 12k size, 3 at 40k.
        old = len(vi.chunks('x' * 22778, size=12000)) + len(vi.chunks('x' * 46158, size=12000))
        new = len(vi.chunks('x' * 22778)) + len(vi.chunks('x' * 46158))
        self.assertEqual((old, new), (7, 3))

    def test_a_very_long_transcript_still_splits(self):
        self.assertGreater(len(vi.chunks('x' * 200000)), 1)

    def test_the_size_is_configurable_without_a_code_change(self):
        self.assertGreater(len(vi.chunks('x' * 33218, size=12000)), 1)


class AudioPromptTests(unittest.TestCase):
    """Speech recognition mishears Chinese homophones; the prompt must account for it."""

    META = {'channel': 'C', 'title': 'T'}

    def test_an_audio_transcript_warns_about_homophones(self):
        prompt = vi.summary_prompt('文字', self.META, from_audio=True)
        self.assertIn('語音辨識', prompt)
        self.assertIn('矽光子', prompt)

    def test_the_quote_must_still_be_verbatim_even_then(self):
        prompt = vi.summary_prompt('文字', self.META, from_audio=True)
        self.assertIn('不可修正', prompt)
        self.assertIn('一字不差', prompt)

    def test_a_caption_transcript_gets_no_such_warning(self):
        self.assertNotIn('語音辨識', vi.summary_prompt('文字', self.META, from_audio=False))


class DashboardFetchPathTests(unittest.TestCase):
    """The dashboard's queue-wide fetch must obey config, not just happen to work.

    fetch_pending used to call fetch_transcript with no config at all, so it reached the audio
    fallback only through whisper_available's defaults — meaning whisper_enabled: false, a moved
    model file or a chosen language had no effect there while appearing to.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = WorkflowState(os.path.join(self.tmp.name, 'workflow.sqlite3'))
        self.state.add_video('abcdefghijk', 'https://youtu.be/abcdefghijk')

    def tearDown(self):
        self.tmp.cleanup()

    def test_the_config_actually_reaches_fetch_transcript(self):
        cfg = {'whisper_enabled': False, 'whisper_language': 'zh'}
        seen = {}

        def fake(video_id, passed_cfg=None, duration=0):
            seen['cfg'] = passed_cfg
            return 'text', [], 'zh-TW'

        with mock.patch.object(vi, 'fetch_transcript', side_effect=fake), \
             mock.patch.object(vi, 'fetch_metadata', return_value={'duration_seconds': 60}):
            vi.fetch_pending(self.state, cfg=cfg)
        self.assertEqual(seen['cfg'], cfg, 'the dashboard ignored the config')

    def test_the_duration_is_known_before_transcribing(self):
        seen = {}

        def fake(video_id, passed_cfg=None, duration=0):
            seen['duration'] = duration
            return 'text', [], 'zh-TW'

        with mock.patch.object(vi, 'fetch_transcript', side_effect=fake), \
             mock.patch.object(vi, 'fetch_metadata', return_value={'duration_seconds': 1954}):
            vi.fetch_pending(self.state, cfg={})
        self.assertEqual(seen['duration'], 1954, 'the timeout could not be sized')

    def test_a_video_that_cannot_be_read_is_marked_distinctly(self):
        with mock.patch.object(vi, 'fetch_metadata', return_value={}), \
             mock.patch.object(vi, 'fetch_transcript',
                               side_effect=vi.NoTranscript('no captions')):
            done, failed = vi.fetch_pending(self.state, cfg={})
        self.assertEqual(done, [])
        self.assertTrue(failed[0]['no_transcript'])
        self.assertEqual(self.state.video(video_id='abcdefghijk')['error_class'], 'no_transcript')

    def test_a_real_fault_is_not_mistaken_for_a_missing_transcript(self):
        with mock.patch.object(vi, 'fetch_metadata', return_value={}), \
             mock.patch.object(vi, 'fetch_transcript', side_effect=RuntimeError('network down')):
            _, failed = vi.fetch_pending(self.state, cfg={})
        self.assertNotIn('no_transcript', failed[0])
        self.assertEqual(self.state.video(video_id='abcdefghijk')['error_class'], 'fetch_error')

    def test_how_the_text_was_obtained_is_reported_back(self):
        with mock.patch.object(vi, 'fetch_metadata', return_value={'duration_seconds': 60}), \
             mock.patch.object(vi, 'fetch_transcript',
                               return_value=('text', [], 'audio:auto')):
            done, _ = vi.fetch_pending(self.state, cfg={})
        self.assertEqual(done[0]['lang'], 'audio:auto')


class RetryFailedTests(unittest.TestCase):
    """A video that failed before the audio route existed must be retryable from the dashboard."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = WorkflowState(os.path.join(self.tmp.name, 'workflow.sqlite3'))
        source_id, _ = self.state.add_video('abcdefghijk', 'https://youtu.be/abcdefghijk')
        self.state.fail_video(source_id, 'no_transcript', 'no caption track')

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_failed_video_is_picked_up_again(self):
        with mock.patch.object(vi, 'fetch_metadata', return_value={'duration_seconds': 60}), \
             mock.patch.object(vi, 'fetch_transcript',
                               return_value=('轉出來的文字', [], 'audio:auto')):
            done, failed = vi.fetch_pending(self.state, cfg={})
        self.assertEqual([s['video_id'] for s in done], ['abcdefghijk'])
        self.assertEqual(self.state.video(video_id='abcdefghijk')['status'], 'fetched')

    def test_an_already_fetched_video_is_not_refetched(self):
        source_id, _ = self.state.add_video('bbbbbbbbbbb', 'u')
        self.state.store_transcript(source_id, 'already have this')
        with mock.patch.object(vi, 'fetch_transcript',
                               side_effect=AssertionError('must not refetch')):
            done, _ = vi.fetch_pending(self.state, cfg={'whisper_enabled': False})
        self.assertNotIn('bbbbbbbbbbb', [s['video_id'] for s in done])
