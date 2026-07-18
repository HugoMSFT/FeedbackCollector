import os
import sys
import unittest
from unittest import mock

SRC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

import collectors


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.calls = []
        self.closed = False

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return FakeResponse(self.payloads.pop(0))

    def close(self):
        self.closed = True


class PublicCollectorTests(unittest.TestCase):
    def test_stack_exchange_queries_each_sql_product_tag(self):
        payloads = [
            {
                "items": [
                    {
                        "question_id": index,
                        "title": title,
                        "body": "<p>Database problem</p>",
                        "tags": [tag],
                        "owner": {"display_name": "User"},
                        "creation_date": 1700000000,
                        "link": f"https://stackoverflow.test/{index}",
                    }
                ],
                "has_more": False,
            }
            for index, (tag, title) in enumerate(
                [
                    ("sql-server", "SQL Server error"),
                    ("azure-sql-database", "Azure SQL Database issue"),
                    (
                        "azure-sql-managed-instance",
                        "Azure SQL Managed Instance question",
                    ),
                ],
                start=1,
            )
        ]
        session = FakeSession(payloads)

        with mock.patch.object(
            collectors,
            "create_retry_session",
            return_value=session,
        ), mock.patch.object(
            collectors.config,
            "KEYWORDS",
            ["SQL Server", "Azure SQL Database", "Azure SQL Managed Instance"],
        ):
            collector = collectors.StackOverflowCollector()
            collector.configure({"max_items": 3})
            items = collector.collect()
            collector.close()

        self.assertEqual(len(items), 3)
        self.assertEqual(
            [call[1]["params"]["tagged"] for call in session.calls],
            [
                "sql-server",
                "azure-sql-database",
                "azure-sql-managed-instance",
            ],
        )
        self.assertTrue(all("q" not in call[1]["params"] for call in session.calls))
        self.assertEqual(items[1]["Area"], "SQL Server and Azure SQL")
        self.assertTrue(session.closed)

    def test_hacker_news_normalizes_comments_and_deduplicates_hits(self):
        hit = {
            "objectID": "42",
            "story_title": "Azure SQL Database discussion",
            "comment_text": "<p>I wish SQL Server had lower latency.</p>",
            "author": "reader",
            "created_at": "2025-01-01T00:00:00Z",
            "_tags": ["comment", "story_1"],
        }
        session = FakeSession([{"hits": [hit]}, {"hits": [hit]}, {"hits": [hit]}])

        with mock.patch.object(
            collectors,
            "create_retry_session",
            return_value=session,
        ), mock.patch.object(
            collectors.config,
            "KEYWORDS",
            ["SQL Server", "Azure SQL Database"],
        ):
            collector = collectors.HackerNewsCollector()
            collector.configure({"max_items": 3, "days": 30})
            items = collector.collect()
            collector.close()

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["Sources"], "Hacker News")
        self.assertEqual(items[0]["Impacttype"], "Feature Request")
        self.assertNotIn("<p>", items[0]["Feedback"])
        self.assertEqual(
            items[0]["Url"],
            "https://news.ycombinator.com/item?id=42",
        )
        self.assertTrue(
            all("numericFilters" in call[1]["params"] for call in session.calls)
        )

    def test_hacker_news_rejects_malformed_payload(self):
        session = FakeSession([[]])
        with mock.patch.object(
            collectors,
            "create_retry_session",
            return_value=session,
        ), mock.patch.object(
            collectors.config,
            "KEYWORDS",
            ["SQL Server"],
        ):
            collector = collectors.HackerNewsCollector()
            collector.configure(
                {
                    "max_items": 1,
                    "queries": ["SQL Server"],
                }
            )
            with self.assertRaisesRegex(ValueError, "not an object"):
                collector.collect()
            collector.close()

    def test_dev_community_uses_product_tags_and_deduplicates_articles(self):
        article = {
            "id": 7,
            "title": "Troubleshooting a database connection",
            "description": "A practical guide",
            "url": "https://dev.to/example/sql",
            "published_timestamp": "2025-01-01T00:00:00Z",
            "tag_list": ["sqlserver"],
            "user": {"name": "Developer"},
            "comments_count": 2,
            "public_reactions_count": 5,
        }
        session = FakeSession([[article], [article], [article]])

        with mock.patch.object(
            collectors,
            "create_retry_session",
            return_value=session,
        ), mock.patch.object(
            collectors.config,
            "KEYWORDS",
            ["SQL Server", "Azure SQL Database"],
        ):
            collector = collectors.DevCommunityCollector()
            collector.configure({"max_items": 3})
            items = collector.collect()
            collector.close()

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["Sources"], "DEV Community")
        self.assertIn("SQL Server", items[0]["Matched_Keywords"])
        self.assertEqual(
            [call[1]["params"]["tag"] for call in session.calls],
            ["sqlserver", "azuresql", "mssql"],
        )

    def test_dev_community_rejects_malformed_payload(self):
        session = FakeSession([{"articles": []}])
        with mock.patch.object(
            collectors,
            "create_retry_session",
            return_value=session,
        ), mock.patch.object(
            collectors.config,
            "KEYWORDS",
            ["SQL Server"],
        ):
            collector = collectors.DevCommunityCollector()
            collector.configure({"max_items": 1, "tags": ["sqlserver"]})
            with self.assertRaisesRegex(ValueError, "not a list"):
                collector.collect()
            collector.close()


if __name__ == "__main__":
    unittest.main()
