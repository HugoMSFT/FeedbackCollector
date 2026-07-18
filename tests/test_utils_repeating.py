import unittest
import os
import sys
import types
from unittest import mock

SRC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

if "nltk" not in sys.modules:
    sys.modules["nltk"] = types.SimpleNamespace(data=types.SimpleNamespace(find=lambda *args, **kwargs: None), download=lambda *args, **kwargs: None)

if "textblob" not in sys.modules:
    sys.modules["textblob"] = types.SimpleNamespace(TextBlob=type("TextBlob", (), {}))

if "dotenv" not in sys.modules:
    sys.modules["dotenv"] = types.SimpleNamespace(load_dotenv=lambda *args, **kwargs: False)

import utils
from utils import collapse_repeating_feedback_items


class CollapseRepeatingFeedbackItemsTests(unittest.TestCase):
    def test_collapses_exact_duplicates_to_one_item(self):
        first = {"Feedback": "Login button not working", "Feedback_Gist": "Login button not working"}
        duplicate = {"Feedback": "Login button not working", "Feedback_Gist": "Login button not working"}
        unique = {"Feedback": "Export fails when file names contain spaces", "Feedback_Gist": "Export fails"}

        result = collapse_repeating_feedback_items([first, duplicate, unique])

        self.assertEqual(result, [first, unique])

    def test_returns_copy_when_no_repeats_are_found(self):
        feedback = [{"Feedback": "Improve search"}, {"Feedback": "Add dark mode"}]

        result = collapse_repeating_feedback_items(feedback)

        self.assertEqual(result, feedback)
        self.assertIsNot(result, feedback)

    def test_collapse_applies_to_more_than_ten_repeating_clusters(self):
        phrases = [
            "orchid",
            "satellite",
            "volcano",
            "harbor",
            "compass",
            "lantern",
            "meadow",
            "quartz",
            "turbine",
            "willow",
            "zephyr",
        ]
        feedback = [
            {"Feedback": phrase}
            for phrase in phrases
            for _ in range(2)
        ]

        with mock.patch.object(
            utils,
            "find_similar_feedback",
            side_effect=AssertionError("all-pairs helper must not be used"),
        ):
            result = collapse_repeating_feedback_items(feedback)

        self.assertEqual(
            [item["Feedback"] for item in result],
            phrases,
        )

    def test_malformed_runtime_taxonomy_entries_are_ignored(self):
        original_categories = utils.ENHANCED_FEEDBACK_CATEGORIES
        original_impacts = utils.IMPACT_TYPES_CONFIG
        try:
            utils.ENHANCED_FEEDBACK_CATEGORIES = {
                "broken": {"subcategories": "not-a-mapping"},
                "usable": {
                    "name": "Usable",
                    "audience": "All",
                    "subcategories": {
                        "broken": {"keywords": "not-a-list"},
                        "working": {
                            "name": "Working",
                            "keywords": ["reliable"],
                            "priority": "high",
                            "feature_area": "Quality",
                        },
                    },
                },
            }
            utils.IMPACT_TYPES_CONFIG = {
                "broken": {"keywords": "not-a-list"},
                "usable": {
                    "name": "Usable impact",
                    "keywords": ["reliable"],
                },
            }

            result = utils.enhanced_categorize_feedback(
                "A reliable result",
                "General User",
            )

            self.assertEqual(result["primary_category"], "Usable")
            self.assertEqual(result["impact_type"], "usable")
        finally:
            utils.ENHANCED_FEEDBACK_CATEGORIES = original_categories
            utils.IMPACT_TYPES_CONFIG = original_impacts


if __name__ == "__main__":
    unittest.main()
