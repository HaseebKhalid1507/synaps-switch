"""company/core/fitness.py — Scalar fitness score for env selection."""
from __future__ import annotations

from company.market.sim import Metrics


def fitness(m: Metrics, weights: dict) -> float:
    """
    fitness = profit * w_profit
            + growth  * w_growth
            - open_incident_severity * w_incident
            - llm_cost_usd * w_llm_cost

    Weights loaded from company/fitness.yaml.
    A good quarter (profit ~$8k, 12% growth, 0 open sev) scores ~12.
    A bad quarter (loss $3k, open sev 6, 6% llm) scores ~ -11.
    """
    w = weights
    return (
        m.profit          * w.get("profit", 0.001)
        + m.growth        * w.get("growth", 0.5)
        - m.open_incident_severity * w.get("incident_penalty", 2.0)
        - m.llm_cost_usd  * w.get("llm_cost", 1.0)
    )
