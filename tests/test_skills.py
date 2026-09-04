"""
Unit tests for skill canonicalisation (Rule 22, 23).
"""

import unittest
from edgedash.skills import canonical


class TestCanonicalSkill(unittest.TestCase):
    def setUp(self) -> None:
        self.aliases = {
            "k8s": "kubernetes",
            "js": "javascript",
            "nodejs": "node.js",
            "node": "node.js",
            "node.js": "node.js",
            "postgresql": "postgres",
            "psql": "postgres",
            "postgres": "postgres",
            "google cloud": "gcp",
            "google cloud platform": "gcp",
            "gcp": "gcp",
            "ml": "machine learning",
            "machine learning": "machine learning",
            "ci cd": "ci/cd",
            "cicd": "ci/cd",
            "ci/cd": "ci/cd",
        }

    def test_case(self) -> None:
        """Test lowercase normalization."""
        self.assertEqual(canonical("PYTHON", self.aliases), "python")
        self.assertEqual(canonical("PostgreSQL", self.aliases), "postgres")
        self.assertEqual(canonical("GoLaNg", self.aliases), "golang")

    def test_whitespace(self) -> None:
        """Test stripping and collapsing internal whitespace."""
        self.assertEqual(canonical("  sql   server  ", self.aliases), "sql server")
        self.assertEqual(canonical("\t python \n", self.aliases), "python")
        self.assertEqual(canonical("google   cloud   platform", self.aliases), "gcp")

    def test_parentheses(self) -> None:
        """Test dropping parenthetical qualifiers."""
        self.assertEqual(canonical("kubernetes (eks)", self.aliases), "kubernetes")
        self.assertEqual(canonical("k8s (v1.28)", self.aliases), "kubernetes")
        self.assertEqual(canonical("AWS (Amazon Web Services)", self.aliases), "aws")
        self.assertEqual(canonical("React (frontend library)", self.aliases), "react")

    def test_aliased_term(self) -> None:
        """Test applying alias mappings."""
        self.assertEqual(canonical("k8s", self.aliases), "kubernetes")
        self.assertEqual(canonical("postgresql", self.aliases), "postgres")
        self.assertEqual(canonical("psql", self.aliases), "postgres")
        self.assertEqual(canonical("google cloud", self.aliases), "gcp")
        self.assertEqual(canonical("google cloud platform", self.aliases), "gcp")
        self.assertEqual(canonical("ml", self.aliases), "machine learning")
        self.assertEqual(canonical("ci cd", self.aliases), "ci/cd")
        self.assertEqual(canonical("cicd", self.aliases), "ci/cd")

    def test_term_with_no_alias(self) -> None:
        """Test terms without alias mappings remain clean canonical strings."""
        self.assertEqual(canonical("python", self.aliases), "python")
        self.assertEqual(canonical("rust", self.aliases), "rust")
        self.assertEqual(canonical("docker", self.aliases), "docker")
        self.assertEqual(canonical("pandas", self.aliases), "pandas")

    def test_empty_string(self) -> None:
        """Test empty string and whitespace-only/punctuation-only strings."""
        self.assertEqual(canonical("", self.aliases), "")
        self.assertEqual(canonical("   ", self.aliases), "")
        self.assertEqual(canonical("()", self.aliases), "")
        self.assertEqual(canonical(" ( ) ", self.aliases), "")
        self.assertEqual(canonical(None, self.aliases), "")

    def test_node_separate_from_javascript(self) -> None:
        """Verify Node.js and JavaScript are kept strictly distinct."""
        self.assertEqual(canonical("js", self.aliases), "javascript")
        self.assertEqual(canonical("javascript", self.aliases), "javascript")
        self.assertEqual(canonical("node", self.aliases), "node.js")
        self.assertEqual(canonical("nodejs", self.aliases), "node.js")
        self.assertEqual(canonical("node.js", self.aliases), "node.js")

    def test_punctuation_stripping_and_symbols(self) -> None:
        """Verify surrounding punctuation is stripped while preserving C++, C#, CI/CD."""
        self.assertEqual(canonical("  - Python.  ", self.aliases), "python")
        self.assertEqual(canonical("[SQL]", self.aliases), "sql")
        self.assertEqual(canonical("C++", self.aliases), "c++")
        self.assertEqual(canonical("C#", self.aliases), "c#")
        self.assertEqual(canonical("CI/CD", self.aliases), "ci/cd")


if __name__ == "__main__":
    unittest.main()
