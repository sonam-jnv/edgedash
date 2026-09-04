"""
Unit tests for the Scorer agent (rules 17, 18, 21).
"""

import os
import tempfile
import unittest
from unittest.mock import patch

from edgedash import storage
from edgedash.agents.scorer import Scorer
from edgedash.config import Config


class TestScorer(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp_dir.name, "test_scorer.db")
        storage.init_db(self.db_path)
        self.config = Config(
            target_role="Data Analyst",
            target_city="Bengaluru",
            keywords=["SQL", "Python"],
            my_skills=["Python", "SQL", "Excel"],
            experience_years=3,
            db_path=self.db_path,
            llm_score_batch_size=25,
            skill_aliases={"postgresql": "postgres"},
        )
        storage.upsert_listings(self.db_path, [
            {
                "title": "Data Analyst",
                "company": "Test Co",
                "location": "Bengaluru",
                "url": "https://example.com/job/1",
                "description": "Requires Python and SQL.",
                "source": "test",
                "posted_at": "2026-08-20",
            },
        ])

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def test_scores_unscored_listings_without_llm(self) -> None:
        """Scorer uses cached extraction and writes fit_score."""
        import hashlib
        desc_hash = hashlib.sha256(b"Requires Python and SQL.").hexdigest()
        storage.save_cached_extraction(
            self.db_path,
            desc_hash,
            {
                "required_skills": ["python", "sql"],
                "nice_to_have": [],
                "seniority": "mid",
                "years_required": 2,
                "remote_ok": True,
            },
        )

        agent = Scorer()
        result = agent.run(self.config, self.db_path)

        self.assertEqual(result.status, "ok")
        self.assertEqual(result.records_touched, 1)
        self.assertIn("1 scored", result.notes)

        listings = storage.get_listings(self.db_path, limit=10, min_score=0)
        scored = [l for l in listings if l["fit_score"] is not None]
        self.assertEqual(len(scored), 1)
        self.assertIsNotNone(scored[0]["fit_reason"])

    def test_idempotent_does_not_rescore(self) -> None:
        """Already-scored listings are skipped (rule 18)."""
        import hashlib
        desc_hash = hashlib.sha256(b"Requires Python and SQL.").hexdigest()
        storage.save_cached_extraction(
            self.db_path,
            desc_hash,
            {
                "required_skills": ["python"],
                "nice_to_have": [],
                "seniority": "unknown",
                "years_required": None,
                "remote_ok": None,
            },
        )

        agent = Scorer()
        first = agent.run(self.config, self.db_path)
        second = agent.run(self.config, self.db_path)

        self.assertEqual(first.records_touched, 1)
        self.assertEqual(second.records_touched, 0)
        self.assertIn("nothing unscored", second.notes)

    def test_extraction_failure_skips_listing(self) -> None:
        """One bad extraction must not abort the batch (rule 17)."""
        from edgedash.llm import LLMError

        agent = Scorer()
        with patch("edgedash.agents.scorer.extract", side_effect=LLMError("quota")):
            result = agent.run(self.config, self.db_path)

        self.assertEqual(result.records_touched, 0)
        self.assertEqual(storage.count_unscored(self.db_path), 1)


if __name__ == "__main__":
    unittest.main()
