import os
import sys
import unittest
from unittest import mock

SRC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

import ado_client


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self):
        self.calls = []
        self.closed = False

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        if "/workitems/42" in url:
            return FakeResponse(
                {
                    "relations": [
                        {
                            "rel": "System.LinkTypes.Hierarchy-Forward",
                            "url": "https://dev.azure.test/_apis/wit/workItems/7",
                        }
                    ]
                }
            )
        return FakeResponse(
            {
                "value": [
                    {
                        "id": 7,
                        "fields": {
                            "System.Title": "Child",
                            "System.Description": "<p>Useful details</p>",
                        },
                    }
                ]
            }
        )

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return FakeResponse({"workItems": []})

    def close(self):
        self.closed = True


class AdoClientTests(unittest.TestCase):
    def test_project_path_is_encoded_and_session_is_closed(self):
        session = FakeSession()
        with mock.patch.multiple(
            ado_client.config,
            ADO_PAT="pat",
            ADO_ORG_URL="https://dev.azure.com/example",
            ADO_PROJECT_NAME="Project Name",
            ADO_PARENT_WORK_ITEM_ID="42",
            MAX_ITEMS_PER_RUN=100,
            REQUEST_TIMEOUT_SECONDS=5,
        ), mock.patch.object(
            ado_client,
            "create_retry_session",
            return_value=session,
        ):
            items = ado_client.get_working_ado_items()

        self.assertEqual(len(items), 1)
        self.assertIn("/Project%20Name/_apis/wit/", session.calls[0][1])
        self.assertEqual(items[0]["description"], "Useful details")
        self.assertTrue(session.closed)

    def test_invalid_parent_id_is_rejected_before_network_access(self):
        with mock.patch.multiple(
            ado_client.config,
            ADO_PAT="pat",
            ADO_ORG_URL="https://dev.azure.com/example",
            ADO_PROJECT_NAME="Project",
            ADO_PARENT_WORK_ITEM_ID="not-numeric",
        ):
            with self.assertRaisesRegex(ValueError, "must be numeric"):
                ado_client.get_working_ado_items()


if __name__ == "__main__":
    unittest.main()
