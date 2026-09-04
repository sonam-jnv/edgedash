"""
Unit tests for Skill Gap Trends reporting (Rules 22, 25).
"""

import io
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

from edgedash import storage
from edgedash.gaps import display_gap_trends


class TestGapTrends(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp_dir.name, "test_trends.db")
        storage.init_db(self.db_path)

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def test_single_snapshot_behavior(self) -> None:
        """Verify message when only 1 snapshot exists (no extrapolation)."""
        storage.save_skill_gap_snapshot(
            self.db_path,
            run_id="run_1",
            computed_at="2026-08-20T10:00:00.000000+00:00",
            gaps=[
                {"skill": "kubernetes", "listings_blocked": 5, "opportunity_cost": 4.5, "mean_score": 90, "top_score": 95, "example_ids": ["id1"]}
            ],
        )

        buf = io.StringIO()
        with patch("sys.stdout", buf):
            display_gap_trends(self.db_path)

        output = buf.getvalue()
        self.assertIn("Only 1 snapshot recorded", output)
        self.assertIn("Trend reporting requires at least 2 snapshots", output)
        self.assertIn("No trend can be extrapolated", output)
        self.assertIn("1 more day of scheduled runs needed", output)

    def test_multi_snapshot_trend_and_dropped_skills(self) -> None:
        """Verify trend calculation, new skills, and dropped skills across 2+ snapshots."""
        # Snapshot 1 (earliest)
        storage.save_skill_gap_snapshot(
            self.db_path,
            run_id="run_1",
            computed_at="2026-08-15T10:00:00.000000+00:00",
            gaps=[
                {"skill": "kubernetes", "listings_blocked": 5, "opportunity_cost": 4.0, "mean_score": 80, "top_score": 90, "example_ids": ["1"]},
                {"skill": "postgres", "listings_blocked": 3, "opportunity_cost": 2.5, "mean_score": 83, "top_score": 85, "example_ids": ["2"]},
                {"skill": "tableau", "listings_blocked": 2, "opportunity_cost": 1.8, "mean_score": 90, "top_score": 90, "example_ids": ["3"]},
            ],
        )

        # Snapshot 2 (latest)
        storage.save_skill_gap_snapshot(
            self.db_path,
            run_id="run_2",
            computed_at="2026-08-22T10:00:00.000000+00:00",
            gaps=[
                {"skill": "kubernetes", "listings_blocked": 8, "opportunity_cost": 6.8, "mean_score": 85, "top_score": 95, "example_ids": ["1", "4"]},
                {"skill": "postgres", "listings_blocked": 2, "opportunity_cost": 1.6, "mean_score": 80, "top_score": 80, "example_ids": ["2"]},
                {"skill": "docker", "listings_blocked": 4, "opportunity_cost": 3.2, "mean_score": 80, "top_score": 85, "example_ids": ["5"]},
            ],
        )

        buf = io.StringIO()
        with patch("sys.stdout", buf):
            display_gap_trends(self.db_path)

        output = buf.getvalue()
        # Dates present
        self.assertIn("2026-08-15", output)
        self.assertIn("2026-08-22", output)
        # Rising skill
        self.assertIn("kubernetes", output)
        self.assertIn("+2.80", output)
        # New skill
        self.assertIn("docker", output)
        self.assertIn("NEW IN TOP 10", output)
        # Dropped skill
        self.assertIn("DROPPED OUT OF TOP 10", output)
        self.assertIn("tableau", output)


if __name__ == "__main__":
    unittest.main()
