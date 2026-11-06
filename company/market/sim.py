"""
company/market/sim.py — Deterministic market simulation for the AI company.

Model (simple and explainable)
================================
Customers
---------
- Start: N_0 = 1500 + (seed % 300). Represents acme-shop's existing SaaS user base.
  Calibrated so that a neutral quarter yields ~$43k revenue (matches the design's ~$41k).
- Each week a fraction convert from leads, and a fraction churn.

Churn
-----
- Base weekly churn rate: 1.5% of active customers.
- Each open incident of severity s adds s × 0.5% weekly churn until fixed.
- Faster incident resolution → less churn → more customers → more revenue.

Leads / acquisition
-------------------
- Each campaign that week: leads += spend × base_rate × (0.5 + quality) × multiplier
  where multiplier = 1.5 if cites_feature else 1.0, base_rate = 0.02 (leads per dollar).
- Conversion from leads to customers: 5% base, +2% per shipped feature (demand-weighted),
  capped at 15% bonus.
- Campaigns citing a shipped feature get a 50% lead multiplier (concrete beats generic).

Revenue
-------
- Weekly revenue = active_customers × price × weekly_purchase_rate (25%).
  The 25% rate models a subscription SaaS where ~1-in-4 customers are "active" buyers
  each week: 1500 × $29 × 0.25 × 4 weeks ≈ $43k/quarter baseline.
- Competitor price cut (from a MarketEvent) reduces weekly_purchase_rate by 20% for
  the rest of the quarter.

Costs
-----
- Fixed costs: $5,000/quarter (hosting, tooling).
- Budgets actually spent (from QuarterLog.spend, only approved ones).
- LLM cost: llm_cost_usd from QuarterLog.

Profit
------
- profit = revenue - costs (can be negative).

Determinism
-----------
- random.Random(seed) is used for all stochastic draws.
- Same (seed, company, actions) → same Metrics every time.

Calibration (design example, seed=42)
--------------------------------------
Good quarter (all sev1s fixed wk1, feature campaigns, $8k marketing):
  ~$55k revenue, ~$8k profit, fitness ≈ +12
Bad quarter (slow fixes, generic campaign, $1k marketing):
  ~$30k revenue, ~-$3k profit, fitness ≈ -11
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class MarketEvent:
    week: int
    room: str
    kind: str   # incident|feature_request|competitor|invoice|lead
    text: str
    payload: dict


@dataclass
class QuarterLog:
    fixes: list[dict] = field(default_factory=list)
    features: list[dict] = field(default_factory=list)
    campaigns: list[dict] = field(default_factory=list)
    price_changes: list[dict] = field(default_factory=list)
    spend: list[dict] = field(default_factory=list)
    llm_cost_usd: float = 0.0
    decisions: list[dict] = field(default_factory=list)


@dataclass
class Metrics:
    revenue: float
    costs: float
    profit: float
    customers: int
    churn_rate: float
    growth: float          # customers_end / customers_start - 1
    open_incident_severity: float
    mean_fix_weeks: float
    llm_cost_usd: float

    def __str__(self):
        return (
            f"Metrics(revenue=${self.revenue:,.0f}, costs=${self.costs:,.0f}, "
            f"profit=${self.profit:,.0f}, customers={self.customers}, "
            f"churn={self.churn_rate:.1%}, growth={self.growth:.1%}, "
            f"open_sev={self.open_incident_severity:.1f}, "
            f"mean_fix_wks={self.mean_fix_weeks:.2f}, llm=${self.llm_cost_usd:.2f})"
        )


# ---------------------------------------------------------------------------
# Default issues (acme-shop plausible seed set)
# ---------------------------------------------------------------------------

def default_issues() -> dict:
    """
    Return a plausible acme-shop issue set used when real issue numbers
    aren't supplied. 4 incidents (with severities) + 4 features (with demand).
    """
    return {
        "incidents": [
            {"issue": 3,  "title": "Checkout 500s on empty coupon code",    "severity": 1},
            {"issue": 7,  "title": "Order confirmation email not sent",      "severity": 2},
            {"issue": 11, "title": "Search returns stale results after edit","severity": 2},
            {"issue": 15, "title": "Profile image upload fails > 2MB",       "severity": 3},
        ],
        "features": [
            {"issue": 20, "title": "One-click reorder",      "demand": 0.9},
            {"issue": 23, "title": "Guest checkout flow",    "demand": 0.8},
            {"issue": 27, "title": "Discount code stacking", "demand": 0.6},
            {"issue": 31, "title": "Order tracking page",    "demand": 0.5},
        ],
    }


# ---------------------------------------------------------------------------
# Market
# ---------------------------------------------------------------------------

class Market:
    # Model constants
    # N_0 = BASE_CUSTOMERS + seed % BASE_CUSTOMERS_SPREAD customers.
    # 1500 * 29 * 0.25 * 4 ≈ $43k/quarter — matches the design's ~$41k baseline.
    BASE_CUSTOMERS = 1500
    BASE_CUSTOMERS_SPREAD = 300
    BASE_WEEKLY_PURCHASE_RATE = 0.25  # 25% of active customers generate revenue each week
    BASE_CHURN_RATE = 0.015           # 1.5% weekly base churn
    INCIDENT_CHURN_PER_SEV = 0.005   # +0.5% weekly churn per open severity point
    LEAD_BASE_RATE = 0.02             # leads per dollar of campaign spend
    CONVERSION_BASE = 0.05            # 5% of leads convert to customers
    CONVERSION_PER_FEATURE = 0.02    # +2% conversion per shipped feature (demand-weighted)
    FIXED_COSTS_QUARTER = 5_000.0    # hosting + tooling
    COMPETITOR_DEMAND_HIT = 0.20     # competitor price cut reduces purchase rate by 20%

    def __init__(self, seed: int, company: dict, issues: dict | None = None):
        self._seed = seed
        self._company = company
        self._issues = issues if issues is not None else default_issues()
        self._weeks = company.get("weeks_per_quarter", 4)
        self._price = company.get("price", 29.0)
        self._n0 = self.BASE_CUSTOMERS + (seed % self.BASE_CUSTOMERS_SPREAD)

    # ------------------------------------------------------------------
    # Event generation
    # ------------------------------------------------------------------

    def events_for_week(self, week: int) -> list[MarketEvent]:
        """Return market events for the given week (1-indexed)."""
        events: list[MarketEvent] = []

        # Incidents arrive in week 1 (they're pre-existing bugs)
        if week == 1:
            for inc in self._issues["incidents"]:
                events.append(MarketEvent(
                    week=week,
                    room="maintenance",
                    kind="incident",
                    text=(
                        f"{30 * inc['severity']} customers report: {inc['title']} "
                        f"(sev{inc['severity']}, issue #{inc['issue']})"
                    ),
                    payload={"issue": inc["issue"], "severity": inc["severity"]},
                ))

        # Feature requests arrive in week 2
        if week == 2:
            for feat in self._issues["features"]:
                events.append(MarketEvent(
                    week=week,
                    room="growth",
                    kind="feature_request",
                    text=(
                        f"Customer demand for '{feat['title']}' (issue #{feat['issue']}, "
                        f"demand score {feat['demand']:.1f})"
                    ),
                    payload={"issue": feat["issue"], "demand": feat["demand"]},
                ))

        # Competitor price cut: 40% chance each week after week 1 (deterministic per seed+week)
        if week > 1:
            w_rng = random.Random(self._seed * 1000 + week)
            if w_rng.random() < 0.40:
                cut_pct = w_rng.randint(10, 20)
                events.append(MarketEvent(
                    week=week,
                    room="growth",
                    kind="competitor",
                    text=f"Competitor cut price by {cut_pct}%. Consider response.",
                    payload={"price_cut_pct": cut_pct},
                ))

        # Invoice (fixed costs) in final week
        if week == self._weeks:
            events.append(MarketEvent(
                week=week,
                room="finance",
                kind="invoice",
                text=f"Quarterly infrastructure invoice: ${self.FIXED_COSTS_QUARTER:,.0f} due.",
                payload={"usd": self.FIXED_COSTS_QUARTER},
            ))

        return events

    # ------------------------------------------------------------------
    # Settlement
    # ------------------------------------------------------------------

    def settle(self, log: QuarterLog) -> Metrics:
        """Simulate the full quarter and return Metrics. Deterministic."""
        price = self._price

        # Apply price changes (last one wins)
        if log.price_changes:
            price = log.price_changes[-1]["price"]

        # Build fix lookup: issue -> week_fixed
        fix_map: dict[int, int] = {f["issue"]: f["week_fixed"] for f in log.fixes}

        # Build feature ship lookup: issue -> week_shipped
        shipped_map: dict[int, int] = {f["issue"]: f["week_shipped"] for f in log.features}

        # Demand map for shipped features
        demand_map: dict[int, float] = {
            feat["issue"]: feat["demand"] for feat in self._issues["features"]
        }

        # Severity map for incidents
        sev_map: dict[int, int] = {
            inc["issue"]: inc["severity"] for inc in self._issues["incidents"]
        }

        # Did a competitor price cut happen? (deterministic from seed+week)
        competitor_hit = any(
            random.Random(self._seed * 1000 + w).random() < 0.40
            for w in range(2, self._weeks + 1)
        )

        customers = float(self._n0)
        total_revenue = 0.0
        purchase_rate = self.BASE_WEEKLY_PURCHASE_RATE
        if competitor_hit:
            purchase_rate *= (1.0 - self.COMPETITOR_DEMAND_HIT)

        # Conversion bonus from shipped features (computed once; features ship early)
        conversion_bonus = sum(
            self.CONVERSION_PER_FEATURE * feat["demand"]
            for feat in self._issues["features"]
            if feat["issue"] in shipped_map
        )
        conversion = self.CONVERSION_BASE + min(conversion_bonus, 0.15)

        for week in range(1, self._weeks + 1):
            # Open incidents this week
            open_sev = sum(
                sev_map[inc["issue"]]
                for inc in self._issues["incidents"]
                if inc["issue"] not in fix_map or fix_map[inc["issue"]] > week
            )

            # Churn
            churn_rate = self.BASE_CHURN_RATE + open_sev * self.INCIDENT_CHURN_PER_SEV
            customers -= customers * churn_rate

            # Leads from campaigns this week
            leads = 0.0
            for camp in log.campaigns:
                if camp["week"] == week:
                    quality = max(0.0, min(1.0, camp.get("quality", 0.5)))
                    multiplier = 1.5 if camp.get("cites_feature", False) else 1.0
                    leads += camp["spend"] * self.LEAD_BASE_RATE * (0.5 + quality) * multiplier

            customers += leads * conversion

            # Revenue this week
            total_revenue += customers * price * purchase_rate

        # Final customer count
        customers_end = int(max(0, round(customers)))

        # Open incident severity at end of quarter
        open_incident_severity = float(sum(
            sev_map[inc["issue"]]
            for inc in self._issues["incidents"]
            if inc["issue"] not in fix_map
        ))

        # Mean fix weeks (for incidents that were fixed)
        fixed_times = [fix_map[inc["issue"]] for inc in self._issues["incidents"] if inc["issue"] in fix_map]
        mean_fix_weeks = sum(fixed_times) / len(fixed_times) if fixed_times else float(self._weeks)

        # Average churn rate (approximate: base + half the open-sev contribution)
        churn_rate_avg = self.BASE_CHURN_RATE + (
            open_incident_severity * self.INCIDENT_CHURN_PER_SEV / 2
        )

        # Costs
        approved_spend = sum(s["usd"] for s in log.spend if s.get("approved", True))
        costs = self.FIXED_COSTS_QUARTER + approved_spend + log.llm_cost_usd

        profit = total_revenue - costs
        growth = customers_end / self._n0 - 1.0

        return Metrics(
            revenue=round(total_revenue, 2),
            costs=round(costs, 2),
            profit=round(profit, 2),
            customers=customers_end,
            churn_rate=round(churn_rate_avg, 4),
            growth=round(growth, 4),
            open_incident_severity=open_incident_severity,
            mean_fix_weeks=round(mean_fix_weeks, 2),
            llm_cost_usd=round(log.llm_cost_usd, 4),
        )
