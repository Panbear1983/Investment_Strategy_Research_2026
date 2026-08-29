import csv
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

import screen
from screen import (
    COLS,
    canonical_country,
    canonical_tier,
    facet_counts,
    load_corpus,
    project,
    row_is_complete,
    select,
)
import research_state


def _row(**over):
    """A complete 22-column row; override any column by its COLS name."""
    base = dict.fromkeys(COLS, 'x')
    base.update({
        'Country': 'Taiwan', 'Timeframe': '中期', 'Sub-Sector': '散熱',
        'Industry': '電子零組件', 'Tier': '龍頭股', 'Company': 'Alpha (1234.TW)',
    })
    base.update(over)
    return [base[c] for c in COLS]


class CanonicalisationTests(unittest.TestCase):
    def test_tier_matches_research_state_on_every_observed_value(self):
        """screen.py inlines canonical_tier because it must not import from scripts/ (which
        holds live credentials). This guards the copy against drifting from the original."""
        rows, _ = load_corpus()
        observed = {r[screen.TIER] for r in rows}
        self.assertGreater(len(observed), 5, 'corpus should exercise several tier spellings')
        for raw in observed:
            self.assertEqual(canonical_tier(raw), research_state.canonical_tier(raw), repr(raw))

    def test_second_leader_is_not_folded_into_leader(self):
        # 二線龍頭 contains 龍頭, so rule order is load-bearing.
        self.assertEqual(canonical_tier('二線龍頭'), 'second_leader')
        self.assertEqual(canonical_tier('龍頭股'), 'leader')

    def test_known_model_typo_maps_to_leader(self):
        self.assertEqual(canonical_tier('龍體股'), 'leader')

    def test_sentence_valued_tier_still_normalises(self):
        prose = '龍頭股。作為全球快遞業的三大巨頭之一，其市場份額與機隊規模均處於世界領先地位。'
        self.assertEqual(canonical_tier(prose), 'leader')

    def test_longest_country_alias_wins(self):
        # 'United States' must not lose to the shorter 'US' substring.
        self.assertEqual(canonical_country('United States'), 'USA')
        self.assertEqual(canonical_country('美國'), 'USA')
        self.assertEqual(canonical_country('South Korea'), 'South Korea')

    def test_unknown_country_passes_through(self):
        self.assertEqual(canonical_country('Latvia'), 'Latvia')


class SelectTests(unittest.TestCase):
    def setUp(self):
        self.rows = [
            _row(Company='Alpha (1234.TW)', Country='Taiwan', Tier='龍頭股',
                 Industry='半導體業', **{'Technical Moat': '液冷散熱專利'}),
            _row(Company='Beta (6324.T)', Country='Japan', Tier='隱形冠軍',
                 Industry='半導體', **{'Technical Moat': 'ASML 供應鏈'}),
            _row(Company='Gamma (GGG)', Country='United States', Tier='二線龍頭',
                 Industry='航太與國防'),
        ]

    def test_tier_filter_accepts_chinese_label_and_english_key(self):
        self.assertEqual(len(select(self.rows, tier='隱形冠軍')), 1)
        self.assertEqual(len(select(self.rows, tier='hidden_champion')), 1)

    def test_second_leader_query_does_not_return_leaders(self):
        hits = select(self.rows, tier='second_leader')
        self.assertEqual([h[screen.COMPANY] for h in hits], ['Gamma (GGG)'])

    def test_industry_is_substring_not_equality(self):
        # The whole point: 半導體 must reach both 半導體業 and 半導體.
        self.assertEqual(len(select(self.rows, industry='半導體')), 2)

    def test_country_filter_normalises_both_sides(self):
        self.assertEqual(len(select(self.rows, country='USA')), 1)
        self.assertEqual(len(select(self.rows, country='美國')), 1)

    def test_two_char_cjk_substring_matches(self):
        # The exact case an FTS5 trigram index returns zero for.
        self.assertEqual(len(select(self.rows, match=['液冷'])), 1)

    def test_multiple_match_terms_are_anded(self):
        self.assertEqual(len(select(self.rows, match=['液冷', 'ASML'])), 0)

    def test_match_any_is_ored(self):
        self.assertEqual(len(select(self.rows, match_any=['液冷', 'ASML'])), 2)

    def test_match_ignores_low_cardinality_facets(self):
        # Country is excluded from PROSE_COLS, else --match Taiwan floods the result set.
        self.assertEqual(len(select(self.rows, match=['Taiwan'])), 0)

    def test_match_searches_subsector_and_company(self):
        self.assertEqual(len(select(self.rows, match=['散熱'])), 3)
        self.assertEqual(len(select(self.rows, match=['Gamma'])), 1)


class PlaceholderTests(unittest.TestCase):
    def test_placeholder_cell_marks_row_incomplete(self):
        self.assertFalse(row_is_complete(_row(**{'12M Catalysts': '等待系統進行深度調查'})))

    def test_long_narrative_mentioning_a_marker_is_not_a_placeholder(self):
        prose = '公司目前面臨歐盟反壟斷調查中，預期於明年上半年結案，對營收影響有限。'
        self.assertTrue(row_is_complete(_row(**{'Key Investment Risks': prose})))

    def test_complete_only_filters_placeholders(self):
        rows = [_row(), _row(Company='B (B)', **{'12M Catalysts': '調查中'})]
        self.assertEqual(len(select(rows, complete_only=True)), 1)
        self.assertEqual(len(select(rows)), 2)

    def test_projection_reports_blank_fields_separately(self):
        # A row with no placeholder markers is "complete" yet may still be empty in places.
        d = project(_row(**{'M&A Potential': '', 'Core Patents & IP': ''}), ['Company'])
        self.assertTrue(d['_complete'])
        self.assertEqual(d['_empty_fields'], 2)


class CorpusIOTests(unittest.TestCase):
    def test_bom_is_stripped_and_placeholder_rows_dropped(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'corpus.csv')
            header = ['國家 (Country)'] + COLS[1:]
            with open(path, 'w', encoding='utf-8-sig', newline='') as f:
                w = csv.writer(f)
                w.writerow(header)
                w.writerow(_row())
                w.writerow(_row(Company='Global_Entity_042'))
                w.writerow(_row(Company=''))
            rows, meta = load_corpus(path)
            self.assertEqual(meta['header0'], '國家 (Country)')
            self.assertEqual(meta['rows'], 1)
            self.assertEqual(meta['cols'], len(COLS))

    def test_meta_carries_staleness_signal(self):
        _, meta = load_corpus()
        # Every answer must be able to show its data age; apply_batch rewrites the CSV by
        # os.replace, so the mtime is the only honest freshness signal available.
        self.assertIn('source_mtime', meta)
        self.assertRegex(meta['source_mtime'], r'^\d{4}-\d{2}-\d{2}T')

    def test_short_rows_are_padded_to_full_width(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'corpus.csv')
            with open(path, 'w', encoding='utf-8-sig', newline='') as f:
                w = csv.writer(f)
                w.writerow(COLS)
                w.writerow(['Taiwan', '中期', '散熱', '電子', '龍頭股', 'Short (S)'])
            rows, _ = load_corpus(path)
            self.assertEqual(len(rows[0]), len(COLS))

    def test_facet_counts_are_ordered_by_frequency(self):
        rows = [_row(Industry='半導體業'), _row(Industry='半導體業'), _row(Industry='航太')]
        self.assertEqual(facet_counts(rows, screen.INDUSTRY), [('半導體業', 2), ('航太', 1)])


class SelfTestTests(unittest.TestCase):
    def test_self_test_passes_against_the_live_corpus(self):
        self.assertEqual(screen.self_test(), 0)


if __name__ == '__main__':
    unittest.main()
