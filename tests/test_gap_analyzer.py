"""
Unit tests for the GapAnalyzer agent (Rules 22, 24, 25, 26, 27).
"""

import os
import tempfile
import unittest
from dataclasses import dataclass, field

from edgedash import storage
from edgedash.agents.gap_analyzer import GapAnalyzer
from edgedash.config import Config


class TestGapAnalyzer(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp_dir.name, "test_edgedash.db")
        storage.init_db(self.db_path)

        self.config = Config(
            target_role="Data Analyst",
            target_city="Bengaluru",
            keywords=["SQL", "Python"],
            my_skills=["Python", "SQL", "Excel"],
            experience_years=3,
            db_path=self.db_path,
            skill_aliases={
                "k8s": "kubernetes",
                "postgresql": "postgres",
                "psql": "postgres",
                "google cloud": "gcp",
            },
        )

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def test_gap_analysis_and_opportunity_cost(self) -> None:
        """
        Verify gap extraction, canonicalisation, and opportunity_cost ranking:
        opportunity_cost = sum(listing.score / 100).
        """
        # Insert test listings
        listings = [
            {
                "title": "Senior Data Analyst",
                "company": "Alpha Corp",
                "location": "Bengaluru",
                "url": "https://example.com/1",
                "description": "Requires PostgreSQL and Kubernetes.",
                "source": "test",
                "posted_at": "2026-08-20",
            },
            {
                "title": "BI Engineer",
                "company": "Beta Inc",
                "location": "Remote",
                "url": "https://example.com/2",
                "description": "Requires Kubernetes and Tableau.",
                "source": "test",
                "posted_at": "2026-08-21",
            },
            {
                "title": "Junior Analyst",
                "company": "Gamma LLC",
                "location": "Bengaluru",
                "url": "https://example.com/3",
                "description": "Requires k8s.",
                "source": "test",
                "posted_at": "2026-08-22",
            },
        ]
        storage.upsert_listings(self.db_path, listings)

        # Set fit scores
        with storage._connect(self.db_path) as conn:
            conn.execute("UPDATE listings SET fit_score = 90 WHERE url = 'https://example.com/1'")
            conn.execute("UPDATE listings SET fit_score = 80 WHERE url = 'https://example.com/2'")
            conn.execute("UPDATE listings SET fit_score = 50 WHERE url = 'https://example.com/3'")

        # Cache extractions
        # Listing 1 (score 90): requires postgres, kubernetes (my_skills has neither)
        storage.save_cached_extraction(
            self.db_path,
            storage.make_listing_id("test", "https://example.com/1"),
            {},
        )
        import hashlib
        h1 = hashlib.sha256(b"Requires PostgreSQL and Kubernetes.").hexdigest()
        h2 = hashlib.sha256(b"Requires Kubernetes and Tableau.").hexdigest()
        h3 = hashlib.sha256(b"Requires k8s.").hexdigest()

        storage.save_cached_extraction(
            self.db_path,
            h1,
            {"required_skills": ["PostgreSQL", "Kubernetes"], "nice_to_have": ["Docker"]},
        )
        storage.save_cached_extraction(
            self.db_path,
            h2,
            {"required_skills": ["kubernetes", "Tableau"], "nice_to_have": []},
        )
        storage.save_cached_extraction(
            self.db_path,
            h3,
            {"required_skills": ["k8s"], "nice_to_have": []},
        )

        agent = GapAnalyzer()
        result = agent.run(self.config, self.db_path)

        self.assertEqual(result.status, "ok")
        self.assertIn("kubernetes", result.notes)

        # Inspect saved snapshot
        gaps = storage.get_latest_skill_gaps(self.db_path)
        self.assertTrue(len(gaps) > 0)

        # kubernetes should be #1: blocked in 3 listings (scores 90, 80, 50)
        # opportunity cost = 0.90 + 0.80 + 0.50 = 2.20
        top = gaps[0]
        self.assertEqual(top["skill"], "kubernetes")
        self.assertEqual(top["listings_blocked"], 3)
        self.assertAlmostEqual(top["opportunity_cost"], 2.20, places=2)
        self.assertAlmostEqual(top["mean_score"], (90 + 80 + 50) / 3, places=1)
        self.assertEqual(top["top_score"], 90)
        self.assertEqual(len(top["example_ids"]), 3)
        self.assertFalse(top["low_confidence"])  # N = 3 >= 3

        # postgres should be in gaps (blocked in 1 listing, score 90 -> cost 0.90)
        postgres_gap = next((g for g in gaps if g["skill"] == "postgres"), None)
        self.assertIsNotNone(postgres_gap)
        self.assertEqual(postgres_gap["listings_blocked"], 1)
        self.assertAlmostEqual(postgres_gap["opportunity_cost"], 0.90, places=2)
        self.assertTrue(postgres_gap["low_confidence"])  # N = 1 < 3

    def test_no_scored_listings(self) -> None:
        """Verify handling when no listings have fit_score."""
        agent = GapAnalyzer()
        result = agent.run(self.config, self.db_path)
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.records_touched, 0)
        self.assertIn("0 gaps found", result.notes)


if __name__ == "__main__":
    unittest.main()
