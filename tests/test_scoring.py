"""
Unit tests for deterministic fit scoring (rules 16, 19).
"""

import unittest

from edgedash.scoring import compute_fit_score


class TestComputeFitScore(unittest.TestCase):
    def setUp(self) -> None:
        self.aliases = {
            "postgresql": "postgres",
            "k8s": "kubernetes",
        }
        self.my_skills = ["Python", "SQL", "Excel", "Pandas", "Tableau"]
        self.keywords = ["data analyst", "SQL", "Python"]
        self.target_role = "Data Analyst"
        self.experience_years = 3

    def test_strong_match_scores_high(self) -> None:
        extraction = {
            "required_skills": ["python", "sql", "tableau"],
            "nice_to_have": ["pandas"],
            "seniority": "mid",
            "years_required": 2,
            "remote_ok": True,
        }
        score, reason = compute_fit_score(
            title="Data Analyst",
            extraction=extraction,
            my_skills=self.my_skills,
            experience_years=self.experience_years,
            target_role=self.target_role,
            keywords=self.keywords,
            skill_aliases=self.aliases,
        )
        self.assertGreaterEqual(score, 70)
        self.assertIn("→", reason)
        self.assertIn("required 3/3", reason)

    def test_missing_required_skills_scores_lower(self) -> None:
        extraction = {
            "required_skills": ["kubernetes", "terraform", "aws"],
            "nice_to_have": [],
            "seniority": "senior",
            "years_required": 8,
            "remote_ok": False,
        }
        score, _ = compute_fit_score(
            title="Platform Engineer",
            extraction=extraction,
            my_skills=self.my_skills,
            experience_years=self.experience_years,
            target_role=self.target_role,
            keywords=self.keywords,
            skill_aliases=self.aliases,
        )
        self.assertLess(score, 50)

    def test_score_clamped_0_to_100(self) -> None:
        extraction = {
            "required_skills": [],
            "nice_to_have": [],
            "seniority": "unknown",
            "years_required": None,
            "remote_ok": None,
        }
        score, _ = compute_fit_score(
            title="Unknown Role",
            extraction=extraction,
            my_skills=[],
            experience_years=0,
            target_role="Data Analyst",
            keywords=[],
            skill_aliases={},
        )
        self.assertGreaterEqual(score, 0)
        self.assertLessEqual(score, 100)


if __name__ == "__main__":
    unittest.main()
