# agency: specialist subagents for Synaps

`build_plugin.py` packages [msitarzewski/agency-agents](https://github.com/msitarzewski/agency-agents)
(MIT, 283 specialists in 18 divisions) as a Synaps plugin named `agency`. Any Synaps
session, including a Switch room session, can then hand work to a specialist:

```
subagent_start(agent="agency:engineering-code-reviewer", task="Review this diff: ...")
subagent_start(agent="agency:finance-fpa-analyst",        task="Build a 4-quarter plan from: ...")
```

Install (stdlib only, clones a pinned commit):

```bash
python3 agency/build_plugin.py --out ~/.synaps-cli/plugins/agency
```

The output must be a real directory inside the plugins dir, not a symlink:
Synaps refuses a `plugin:agent` path that resolves outside it. Rebuilding replaces
only a directory this script wrote (it leaves a `.agency-build` marker).

What gets written:

| Path | What |
|---|---|
| `skills/agency/SKILL.md` | Index: the divisions and how to dispatch |
| `skills/<division>/SKILL.md` | The division's roster: slug, name, one-line description |
| `skills/<division>/agents/<slug>.md` | The specialist's prompt, copied unchanged |
| `LICENSE`, `NOTICE` | agency-agents' MIT license and the source commit |

Agents are callable as soon as the files exist. The `agency:*` skills show up in
`search_skills` / `load_skill` after `/plugins reload` (or the next daemon start).
