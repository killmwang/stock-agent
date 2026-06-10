import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tradingagents.dataflows.reddit_utils import fetch_top_from_category


class RedditUtilsTests(unittest.TestCase):
    def test_missing_reddit_category_returns_empty_results(self):
        with tempfile.TemporaryDirectory() as data_dir:
            result = fetch_top_from_category(
                "company_news",
                "2026-06-10",
                5,
                query="600519",
                data_path=data_dir,
            )

        self.assertEqual(result, [])

    def test_unknown_company_ticker_does_not_raise_key_error(self):
        with tempfile.TemporaryDirectory() as data_dir:
            category_dir = Path(data_dir) / "company_news"
            category_dir.mkdir()
            (category_dir / "stocks.jsonl").write_text(
                '{"created_utc": 1781049600, "title": "Some stock", '
                '"selftext": "No known mapping", "url": "https://example.com", "ups": 1}\n',
                encoding="utf-8",
            )

            result = fetch_top_from_category(
                "company_news",
                "2026-06-10",
                5,
                query="600519",
                data_path=data_dir,
            )

        self.assertEqual(result, [])


if __name__ == "__main__":
    unittest.main()
