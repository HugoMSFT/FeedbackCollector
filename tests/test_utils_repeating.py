import unittest
import os
import sys
import types

SRC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

if "nltk" not in sys.modules:
    sys.modules["nltk"] = types.SimpleNamespace(data=types.SimpleNamespace(find=lambda *args, **kwargs: None), download=lambda *args, **kwargs: None)

if "textblob" not in sys.modules:
    sys.modules["textblob"] = types.SimpleNamespace(TextBlob=type("TextBlob", (), {}))

if "dotenv" not in sys.modules:
    sys.modules["dotenv"] = types.SimpleNamespace(load_dotenv=lambda *args, **kwargs: False)

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


if __name__ == "__main__":
    unittest.main()
