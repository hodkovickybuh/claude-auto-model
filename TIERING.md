# Subagent tiering rule

Paste into `~/.claude/CLAUDE.md`. Subagent model/effort **is** settable per task,
so this is the part that is genuinely automatic on every prompt.

---

## Model & effort tiering

Pick the cheapest tier that does the job. This is a standing rule, not a suggestion.

**Subagents** (Agent tool `model`/`effort`, Workflow `opts.model`/`opts.effort`).
Every time you spawn one, set its tier explicitly. Never let a search agent inherit Opus.

| Subagent work | model | effort |
|---|---|---|
| Search, grep, file location, scraping, mechanical edits, format conversion | `haiku` | low |
| Reading and summarising, drafting copy, routine code changes, tests, single-lens review | `sonnet` | medium |
| Architecture, adversarial verify, security, legal or money-critical text, multi-file refactor | `opus` | high |

If genuinely torn, take the higher tier. Correctness beats the saving.

**Your own turn.** You cannot switch your own model. If the session tier is clearly
wrong for the task, say so in one line and name the switch (`/model sonnet` + `/effort low`,
or `/model opus` + `/effort high`).
