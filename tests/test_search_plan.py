import os
import sys
import unittest
from datetime import datetime, timezone

SRC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from id_generator import FeedbackIDGenerator
from search_plan import (
    CoverageStats,
    build_query_shards,
    build_search_profile,
    canonicalize_url,
    extract_topic_terms,
    match_topic,
    prepare_feedback_item,
)


class SearchPlanTests(unittest.TestCase):
    def test_natural_language_topic_builds_source_terms(self):
        terms = extract_topic_terms(
            "Collect feedback about Query Store performance on Azure SQL",
            ["Query Store", "Azure SQL", "SQL Server"],
        )

        self.assertIn("Query Store", terms)
        self.assertIn("Azure SQL", terms)
        self.assertNotIn("feedback", [term.casefold() for term in terms])

    def test_profile_honors_lookback_and_advanced_terms(self):
        now = datetime(2026, 9, 22, tzinfo=timezone.utc)
        profile = build_search_profile(
            {
                "topic": "Query Store",
                "searchTerms": ["Query Store", "QDS"],
                "excludedTerms": ["PostgreSQL"],
                "timeRangeMonths": 12,
                "candidateMultiplier": 25,
                "includeReplies": False,
            },
            ["SQL Server"],
            now=now,
        )

        self.assertEqual(profile.terms, ["Query Store", "QDS"])
        self.assertEqual(profile.created_after.date().isoformat(), "2025-09-22")
        self.assertEqual(profile.candidate_multiplier, 25)
        self.assertFalse(profile.include_replies)

    def test_topic_matching_uses_boundaries_overlap_and_exclusions(self):
        profile = build_search_profile(
            {
                "topic": "Delta table performance",
                "excludedTerms": ["Spark"],
                "timeRangeMonths": 0,
            },
            [],
        )

        relevant = match_topic(
            "Delta tables become slow during concurrent writes",
            profile,
        )
        excluded = match_topic(
            "Spark Delta table performance is slow",
            profile,
        )
        substring_only = match_topic("The deltatable helper changed", profile)

        self.assertTrue(relevant.relevant)
        self.assertFalse(excluded.relevant)
        self.assertFalse(substring_only.relevant)

    def test_query_shards_are_bounded(self):
        shards = build_query_shards(
            ["one phrase", "two phrase", "three phrase", "four phrase"],
            max_terms=2,
            max_length=40,
        )

        self.assertEqual(len(shards), 2)
        self.assertTrue(all(len(shard) <= 40 for shard in shards))

    def test_url_identity_is_canonical_and_populates_all_url_fields(self):
        item = prepare_feedback_item(
            {
                "Url": (
                    "HTTPS://Example.COM:443/path/?utm_source=test"
                    "&b=2&a=1#comments"
                )
            }
        )

        expected = "https://example.com/path?a=1&b=2"
        self.assertEqual(canonicalize_url(item["Url"]), expected)
        self.assertEqual(item["URL"], expected)
        self.assertEqual(item["Source_URL"], expected)
        self.assertEqual(item["External_ID"], expected)

    def test_external_identity_survives_content_edits(self):
        first = FeedbackIDGenerator.generate_id_from_feedback_dict(
            {
                "External_ID": "issue:123",
                "Source": "GitHub Issues",
                "Title": "Original",
                "Content": "Original content",
            }
        )
        second = FeedbackIDGenerator.generate_id_from_feedback_dict(
            {
                "External_ID": "issue:123",
                "Source": "GitHub Issues",
                "Title": "Edited",
                "Content": "Changed content",
            }
        )

        self.assertEqual(first, second)

    def test_coverage_snapshot_tracks_collection_depth(self):
        coverage = CoverageStats()
        coverage.record_query()
        coverage.record_page()
        coverage.record_candidate("2026-01-01T00:00:00Z")
        coverage.record_match()

        snapshot = coverage.snapshot()
        self.assertEqual(snapshot["queries_run"], 1)
        self.assertEqual(snapshot["pages_fetched"], 1)
        self.assertEqual(snapshot["candidates_scanned"], 1)
        self.assertEqual(snapshot["matched"], 1)


if __name__ == "__main__":
    unittest.main()
