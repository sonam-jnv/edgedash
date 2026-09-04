"""
MockFetcher — a stand-in Fetcher agent that returns realistic fake listings.

Produces 12 listings on every run:
  • 8 are generated fresh each run (unique URLs containing a timestamp).
  • 4 have fully stable URLs so their storage ID is identical on every run —
    the second run should report 0 new rows for those 4, proving dedup works.

Replace this module with the real Fetcher once live scraping is ready;
the orchestrator registry swap is a one-line change.
"""

from __future__ import annotations

from datetime import datetime, timezone

from edgedash.agents.base import Agent, AgentResult
from edgedash.config import Config
from edgedash import storage


# ---------------------------------------------------------------------------
# The 4 listings that are STABLE across every run (fixed URLs → fixed IDs).
# Their content intentionally references a mix of skills so the future Scorer
# has something realistic to work with.
# ---------------------------------------------------------------------------
_STABLE_LISTINGS: list[dict] = [
    {
        "title": "Data Analyst",
        "company": "Flipkart",
        "location": "Bengaluru, Karnataka",
        "url": "https://careers.flipkart.com/jobs/da-001",
        "description": (
            "Analyse seller and logistics data using SQL and Python. "
            "Build Tableau dashboards for ops leadership. "
            "Own weekly KPI reporting. 2–4 yrs exp required."
        ),
        "source": "mock",
        "posted_at": "2026-08-01",
    },
    {
        "title": "Senior Data Analyst",
        "company": "Swiggy",
        "location": "Bengaluru, Karnataka",
        "url": "https://careers.swiggy.com/jobs/sda-042",
        "description": (
            "Drive growth analytics for the consumer funnel. "
            "Proficient in Python (Pandas, NumPy), SQL, and A/B testing. "
            "Experience with dbt and BigQuery a strong plus. 4+ yrs."
        ),
        "source": "mock",
        "posted_at": "2026-08-02",
    },
    {
        "title": "Business Analyst",
        "company": "Razorpay",
        "location": "Bengaluru, Karnataka",
        "url": "https://razorpay.com/careers/ba-117",
        "description": (
            "Translate payments data into product insights. "
            "Advanced Excel, SQL mandatory. Power BI preferred. "
            "Work closely with PMs and engineering. 2–5 yrs."
        ),
        "source": "mock",
        "posted_at": "2026-08-03",
    },
    {
        "title": "Data Analyst – Marketing",
        "company": "Myntra",
        "location": "Bengaluru, Karnataka",
        "url": "https://myntra.com/careers/da-mkt-009",
        "description": (
            "Marketing mix modelling, campaign attribution, cohort analysis. "
            "SQL, Python (Pandas), Google Analytics. "
            "Familiarity with ML models a plus. 1–3 yrs."
        ),
        "source": "mock",
        "posted_at": "2026-08-04",
    },
]

# ---------------------------------------------------------------------------
# The 8 fresh listings (URL contains a run-time slug so IDs change each run).
# ---------------------------------------------------------------------------

def _fresh_listings(run_slug: str) -> list[dict]:
    """Build 8 listings whose URLs embed `run_slug` so they're new each run."""
    return [
        {
            "title": "Data Analyst – Supply Chain",
            "company": "Amazon India",
            "location": "Bengaluru, Karnataka",
            "url": f"https://amazon.jobs/en/jobs/sc-da-{run_slug}",
            "description": (
                "Inventory forecasting and supplier scorecards. "
                "SQL, Python, and QuickSight. Tableau a plus. 2–4 yrs."
            ),
            "source": "mock",
            "posted_at": "2026-08-05",
        },
        {
            "title": "Junior Data Analyst",
            "company": "PhonePe",
            "location": "Bengaluru, Karnataka",
            "url": f"https://phonepe.com/careers/jda-{run_slug}",
            "description": (
                "Support product analytics team. "
                "Strong SQL required; Python basics welcome. "
                "Fresh graduates with projects considered. 0–1 yr."
            ),
            "source": "mock",
            "posted_at": "2026-08-05",
        },
        {
            "title": "Analyst – People Analytics",
            "company": "Infosys",
            "location": "Bengaluru, Karnataka",
            "url": f"https://infosys.com/careers/hr-analyst-{run_slug}",
            "description": (
                "Workforce planning, attrition modelling, headcount reporting. "
                "Excel advanced, SQL, Power BI. Python a bonus. 2–3 yrs."
            ),
            "source": "mock",
            "posted_at": "2026-08-06",
        },
        {
            "title": "Data Analyst – Fintech",
            "company": "CRED",
            "location": "Bengaluru, Karnataka",
            "url": f"https://cred.club/careers/da-{run_slug}",
            "description": (
                "Credit risk and rewards analytics. "
                "Python (Pandas, scikit-learn), SQL, statistical analysis. "
                "Spark exposure helpful. 2–4 yrs."
            ),
            "source": "mock",
            "posted_at": "2026-08-06",
        },
        {
            "title": "Senior Analyst – Revenue Operations",
            "company": "Freshworks",
            "location": "Bengaluru, Karnataka",
            "url": f"https://freshworks.com/careers/rev-ops-{run_slug}",
            "description": (
                "Pipeline analytics, forecasting, and CRM hygiene. "
                "Salesforce, SQL, Tableau mandatory. Python preferred. 3–6 yrs."
            ),
            "source": "mock",
            "posted_at": "2026-08-07",
        },
        {
            "title": "Data Analyst – Healthcare",
            "company": "Practo",
            "location": "Bengaluru, Karnataka",
            "url": f"https://practo.com/careers/da-health-{run_slug}",
            "description": (
                "Clinical and operational data analysis. "
                "SQL, Python, statistical testing. "
                "HL7 / FHIR familiarity a strong plus. 2–4 yrs."
            ),
            "source": "mock",
            "posted_at": "2026-08-07",
        },
        {
            "title": "Business Intelligence Analyst",
            "company": "Ola",
            "location": "Bengaluru, Karnataka",
            "url": f"https://ola.com/careers/bi-analyst-{run_slug}",
            "description": (
                "End-to-end BI: data modelling, dashboarding, self-serve analytics. "
                "Looker or Power BI, SQL, dbt. Python scripting a plus. 2–5 yrs."
            ),
            "source": "mock",
            "posted_at": "2026-08-08",
        },
        {
            "title": "Analyst – Growth & Experimentation",
            "company": "Meesho",
            "location": "Bengaluru, Karnataka",
            "url": f"https://meesho.com/careers/growth-analyst-{run_slug}",
            "description": (
                "Design and analyse A/B tests for seller and buyer funnels. "
                "Python (statsmodels), SQL, Mixpanel. 1–3 yrs."
            ),
            "source": "mock",
            "posted_at": "2026-08-08",
        },
    ]


# ---------------------------------------------------------------------------
# Agent implementation
# ---------------------------------------------------------------------------

class MockFetcher:
    """Fetches fake job listings. Drop-in replacement for the real Fetcher."""

    name: str = "MockFetcher"

    def run(
        self,
        config: Config,
        db_path: str,
        stop_conditions: dict | None = None,
        goal: str | None = None,
    ) -> AgentResult:
        run_slug = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        all_rows = _STABLE_LISTINGS + _fresh_listings(run_slug)
        if stop_conditions and "max_listings" in stop_conditions:
            max_listings = stop_conditions["max_listings"]
            if isinstance(max_listings, int) and max_listings > 0:
                all_rows = all_rows[:max_listings]

        try:
            new_count = storage.upsert_listings(db_path, all_rows)
        except Exception as exc:
            return AgentResult(
                agent=self.name,
                status="failed",
                records_touched=0,
                notes=f"upsert_listings raised: {exc}",
            )

        total = len(all_rows)
        duplicate_count = total - new_count
        notes = (
            f"Presented {total} listings to storage. "
            f"New: {new_count}, already known (deduped): {duplicate_count}."
        )
        return AgentResult(
            agent=self.name,
            status="ok",
            records_touched=new_count,
            notes=notes,
        )

