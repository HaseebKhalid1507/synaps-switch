"""Department agents (LangGraph-side employees): see company/depts/agent.py."""
from company.depts.agent import (  # noqa: F401
    ACTION_TYPES, CampaignJudge, DeptAgent, Departments, TokenBudget, parse_action, render,
    transport_rooms, validate_action)
