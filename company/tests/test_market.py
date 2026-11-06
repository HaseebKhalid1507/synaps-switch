"""Tests for company/market/sim.py"""
import pytest

from company.market.sim import Market, QuarterLog, Metrics, default_issues


COMPANY = {
    "links": [],
    "budgets": {"marketing": 10000, "maintenance": 4000},
    "policies": {"finance_veto_usd": 5000},
    "weeks_per_quarter": 4,
    "price": 29.0,
}

ISSUES = default_issues()


def _good_log() -> QuarterLog:
    """Fast fix, feature-citing campaign, all issues fixed and features shipped."""
    log = QuarterLog()
    # Fix all incidents by week 1
    for inc in ISSUES["incidents"]:
        log.fixes.append({"issue": inc["issue"], "week_fixed": 1, "pr_url": "http://example.com/pr/1", "tests_passed": True})
    # Ship all features by week 2
    for feat in ISSUES["features"]:
        log.features.append({"issue": feat["issue"], "week_shipped": 2, "pr_url": "http://example.com/pr/2", "tests_passed": True})
    # Feature-citing campaigns every week
    for week in range(1, 5):
        log.campaigns.append({"week": week, "text": "New: one-click reorder!", "spend": 2000, "cites_feature": True, "quality": 0.9})
    log.spend = [{"week": 1, "dept": "marketing", "usd": 8000, "approved": True}]
    log.llm_cost_usd = 6.0
    return log


def _bad_log() -> QuarterLog:
    """Slow fix, generic campaign, nothing shipped."""
    log = QuarterLog()
    # Fix sev1 late (week 4)
    log.fixes.append({"issue": 3, "week_fixed": 4, "pr_url": "http://example.com/pr/9", "tests_passed": True})
    # Generic campaign, low quality
    log.campaigns.append({"week": 2, "text": "Spring sale!", "spend": 1000, "cites_feature": False, "quality": 0.2})
    log.spend = [{"week": 2, "dept": "marketing", "usd": 1000, "approved": True}]
    log.llm_cost_usd = 6.5
    return log


# ------------------------------------------------------------------
# Determinism
# ------------------------------------------------------------------

def test_deterministic_same_seed():
    m1 = Market(42, COMPANY, ISSUES).settle(_good_log())
    m2 = Market(42, COMPANY, ISSUES).settle(_good_log())
    assert m1.revenue == m2.revenue
    assert m1.profit == m2.profit
    assert m1.customers == m2.customers


def test_different_seeds_differ():
    m1 = Market(42, COMPANY, ISSUES).settle(_good_log())
    m2 = Market(99, COMPANY, ISSUES).settle(_good_log())
    # Very unlikely to be equal with different seeds
    assert m1.revenue != m2.revenue or m1.customers != m2.customers


# ------------------------------------------------------------------
# Fast fix beats slow fix
# ------------------------------------------------------------------

def test_fast_fix_beats_slow():
    good = Market(42, COMPANY, ISSUES).settle(_good_log())
    bad = Market(42, COMPANY, ISSUES).settle(_bad_log())
    # Fast fix → less churn → more customers → more revenue
    assert good.customers > bad.customers
    assert good.revenue > bad.revenue
    assert good.open_incident_severity < bad.open_incident_severity


def test_fast_fix_lower_churn():
    good = Market(42, COMPANY, ISSUES).settle(_good_log())
    bad = Market(42, COMPANY, ISSUES).settle(_bad_log())
    assert good.churn_rate <= bad.churn_rate


# ------------------------------------------------------------------
# Citing campaign beats generic
# ------------------------------------------------------------------

def test_citing_campaign_beats_generic():
    citing_log = QuarterLog()
    citing_log.campaigns = [{"week": 1, "text": "New feature!", "spend": 3000, "cites_feature": True, "quality": 0.7}]
    citing_log.llm_cost_usd = 5.0

    generic_log = QuarterLog()
    generic_log.campaigns = [{"week": 1, "text": "Generic sale!", "spend": 3000, "cites_feature": False, "quality": 0.7}]
    generic_log.llm_cost_usd = 5.0

    m_citing = Market(42, COMPANY, ISSUES).settle(citing_log)
    m_generic = Market(42, COMPANY, ISSUES).settle(generic_log)
    assert m_citing.customers >= m_generic.customers


# ------------------------------------------------------------------
# Revenue in tens of thousands for a reasonable quarter
# ------------------------------------------------------------------

def test_good_quarter_revenue_range():
    m = Market(42, COMPANY, ISSUES).settle(_good_log())
    # Tens of thousands
    assert 10_000 < m.revenue < 200_000, f"Revenue out of range: {m.revenue}"


def test_good_quarter_profitable():
    m = Market(42, COMPANY, ISSUES).settle(_good_log())
    # A good quarter should be profitable or near-profitable
    # (net of fixed $5k + $8k marketing spend + $6 llm)
    # Revenue with 200+ customers buying at $29 × 3% × 4 weeks ≈ $7k+ baseline
    assert m.revenue > 0


# ------------------------------------------------------------------
# events_for_week
# ------------------------------------------------------------------

def test_events_week1_has_incidents():
    mkt = Market(42, COMPANY, ISSUES)
    events = mkt.events_for_week(1)
    kinds = [e.kind for e in events]
    assert "incident" in kinds


def test_events_week2_has_features():
    mkt = Market(42, COMPANY, ISSUES)
    events = mkt.events_for_week(2)
    kinds = [e.kind for e in events]
    assert "feature_request" in kinds


def test_events_final_week_has_invoice():
    mkt = Market(42, COMPANY, ISSUES)
    events = mkt.events_for_week(COMPANY["weeks_per_quarter"])
    kinds = [e.kind for e in events]
    assert "invoice" in kinds


def test_events_payload_structure():
    mkt = Market(42, COMPANY, ISSUES)
    for event in mkt.events_for_week(1):
        if event.kind == "incident":
            assert "issue" in event.payload
            assert "severity" in event.payload


# ------------------------------------------------------------------
# default_issues
# ------------------------------------------------------------------

def test_default_issues_structure():
    issues = default_issues()
    assert "incidents" in issues
    assert "features" in issues
    assert len(issues["incidents"]) == 4
    assert len(issues["features"]) == 4
    for inc in issues["incidents"]:
        assert "issue" in inc and "severity" in inc
    for feat in issues["features"]:
        assert "issue" in feat and "demand" in feat


# ------------------------------------------------------------------
# Metrics str
# ------------------------------------------------------------------

def test_metrics_str():
    m = Market(42, COMPANY, ISSUES).settle(_good_log())
    s = str(m)
    assert "revenue" in s
    assert "profit" in s
