# Agency Persona Roster

Agent instructions in `company/envs/` are staffed from
[**The Agency**](https://github.com/msitarzewski/agency-agents) persona library
(MIT License, © msitarzewski).

The repo is cloned read-only at `/tmp/agency-agents` at runtime.
`company/core/roster.py` loads and indexes the ~284 persona `.md` files
(all divisions except `integrations/`, `strategy/`, `examples/`, `scripts/`).

## Usage

```python
from company.core.roster import load_roster, search, instructions_for

roster = load_roster("/tmp/agency-agents")
# persona_id = "<division>/<filename-stem>"
persona = roster["support/support-infrastructure-maintainer"]
instructions = instructions_for(persona, "You run maintenance for acme-shop. ...")
```

## env1.yaml staffing

| Agent | Persona | Runbook reference |
|---|---|---|
| board | `project-management/project-manager-senior` | Quarter + evolution loops |
| ops-maint | `support/support-infrastructure-maintainer` | [Incident Response runbook](../strategy/runbooks/scenario-incident-response.md): incident commander |
| growth | `product/product-sprint-prioritizer` | Pricing + feature prioritization |
| marketing | `marketing/marketing-growth-hacker` | [Marketing Campaign runbook](../strategy/runbooks/scenario-marketing-campaign.md): acquisition strategy |
| finance | `finance/finance-fpa-analyst` | Budget ledger + spend veto |
| reviewer | `testing/testing-reality-checker` | PR review in #dev, evidence-based sign-off |
| synaps-dev | *(no persona — Synaps coding agent)* | Real code: branch, fix, PR, pytest |

## License

The Agency personas are MIT-licensed. See `/tmp/agency-agents/LICENSE` or
<https://github.com/msitarzewski/agency-agents/blob/main/LICENSE>.
