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


def _ok_log() -> QuarterLog:
    """Middling: incidents fixed in weeks 2-3, one feature shipped late, two generic campaigns."""
    log = QuarterLog()
    log.fixes = [{"issue": 3, "week_fixed": 2}, {"issue": 7, "week_fixed": 2},
                 {"issue": 11, "week_fixed": 3}, {"issue": 15, "week_fixed": 3}]
    log.features = [{"issue": 20, "week_shipped": 3}]
    log.campaigns = [{"week": w, "text": "Spring sale", "spend": 2000, "cites_feature": False, "quality": 0.6}
                     for w in (2, 3)]
    log.spend = [{"week": 2, "dept": "marketing", "usd": 4000, "approved": True}]
    log.llm_cost_usd = 6.2
    return log


# ------------------------------------------------------------------
# Calibration (#16): GOOD > OK > BAD on profit AND growth, not only fitness
# ------------------------------------------------------------------

@pytest.mark.parametrize("seed", [1, 2, 7, 42, 99, 123, 299])
def test_good_ok_bad_order_on_profit_growth_fitness(seed):
    from company.core.fitness import fitness
    from company.board.quarter import load_weights
    w = load_weights()
    g, o, b = (Market(seed, COMPANY, ISSUES).settle(f()) for f in (_good_log, _ok_log, _bad_log))
    assert g.profit > o.profit > b.profit
    assert g.growth > o.growth > b.growth
    assert fitness(g, w) > fitness(o, w) > fitness(b, w)
    assert g.growth > 0, "a good quarter grows the customer base"


def _with_campaign(week, cites, quality, spend=2000):
    base = QuarterLog()
    base.fixes = [{"issue": i["issue"], "week_fixed": 1} for i in ISSUES["incidents"]]
    base.features = [{"issue": f["issue"], "week_shipped": 1} for f in ISSUES["features"]]
    camp = QuarterLog(fixes=list(base.fixes), features=list(base.features))
    camp.campaigns = [{"week": week, "spend": spend, "cites_feature": cites, "quality": quality}]
    camp.spend = [{"week": week, "dept": "marketing", "usd": spend, "approved": True}]
    return camp, base


@pytest.mark.parametrize("seed", [1, 2, 42])
def test_good_campaign_pays_back_weak_one_does_not(seed):
    good, base = _with_campaign(1, True, 0.9)
    weak, _ = _with_campaign(1, False, 0.2)
    m0 = Market(seed, COMPANY, ISSUES).settle(base)
    assert Market(seed, COMPANY, ISSUES).settle(good).profit > m0.profit
    assert Market(seed, COMPANY, ISSUES).settle(weak).profit < m0.profit


@pytest.mark.parametrize("seed", [1, 2, 42])
def test_fixing_a_week_sooner_raises_profit_and_growth(seed):
    def fixed_in(week):
        log = QuarterLog()
        log.fixes = [{"issue": i["issue"], "week_fixed": week} for i in ISSUES["incidents"]]
        return Market(seed, COMPANY, ISSUES).settle(log)
    early, late = fixed_in(1), fixed_in(2)
    assert early.profit > late.profit + 1000
    assert early.growth > late.growth
    assert early.mean_fix_weeks < late.mean_fix_weeks


def test_feature_raises_conversion_from_its_ship_week():
    def shipped_in(week):
        log = QuarterLog()
        log.features = [{"issue": 20, "week_shipped": week}]
        log.campaigns = [{"week": 2, "spend": 2000, "cites_feature": False, "quality": 0.5}]
        return Market(42, COMPANY, ISSUES).settle(log)
    assert shipped_in(1).customers > shipped_in(3).customers


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
