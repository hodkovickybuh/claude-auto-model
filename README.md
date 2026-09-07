# claude-auto-model

Auto-selects the Claude Code model and effort level from your prompt, so you stop
opening every session on the top dial.

```
claude "whats the git command to undo a commit"  auto: XS -> haiku / low
claude "fix the typo in the README"             auto: S  -> sonnet / low
claude "write 5 instagram captions"             auto: M  -> sonnet / high
claude "postgres or mongo for the ledger"       auto: L  -> opus / high
claude -c   |   claude --model opus "..."     passthrough, your flags win
```

A Haiku call classifies the prompt (XS/S/M/L/XL, ~3s, well under a cent), then launches
Claude Code on that tier. No API key needed, it uses your existing auth.

## Install

```sh
git clone https://github.com/<you>/claude-auto-model ~/.claude-auto-model
echo '[[ -f ~/.claude-auto-model/claude-auto-model.zsh ]] && source ~/.claude-auto-model/claude-auto-model.zsh' >> ~/.zshrc
```

New shell, then `ca-test` to verify (8 known prompts must land on their tier).

Also paste [TIERING.md](TIERING.md) into `~/.claude/CLAUDE.md`. That half is the
one that runs on every prompt, see below.

## Read this before you install it

**It routes at session launch, not per prompt.** If you open a bare `claude` and
then chat inside the TUI, nothing is classified and nothing changes. You get
routing only when the task is on the command line.

**There is no way to change a running session's model automatically.** I checked:

| Path | set model | set effort | reaches an interactive terminal session |
|---|---|---|---|
| You typing `/model` / `/effort` | yes | yes | yes, manually |
| Control protocol `set_model` + `apply_flag_settings{effortLevel}` | yes | yes | SDK stream-json, Remote Control, or IDE only |
| Hooks (`UserPromptSubmit`, ...) | no such field | no | no |
| Anything the agent can call itself | no tool exists | no | no |

Rewriting `settings.json` from a hook takes effect on the *next* session, never the
running one. `claude auto-mode` is the permission classifier, unrelated to model choice.
Upstream request: [anthropics/claude-code#43326](https://github.com/anthropics/claude-code/issues/43326).

So the honest split:

- **Per prompt, automatic:** subagent tiering, via `TIERING.md`. A grep agent stops
  inheriting Opus. This is where most of the tokens are.
- **Per session, automatic:** this script.
- **Not available:** re-tiering a live session.

If you mostly chat inside long sessions rather than launching with a task, the
script will rarely fire, and your default model in `settings.json` matters far more
than anything here.

## Tiers

| Tier | Model | Effort | For | $/1M in-out |
|---|---|---|---|---|
| XS | haiku | low | pure lookup, factual question, status check, one command | $1 / $5 |
| S | sonnet | low | typo, rename, reformat, one obvious edit | $2 / $10 |
| M | sonnet | high | features, normal bugs, copy, research, review | $2 / $10 |
| L | opus | high | architecture, failed debugging, security, money, legal | $5 / $25 |
| XL | fable | xhigh | novel system design where L clearly cannot carry it | $10 / $50 |

**Fable is the most expensive model, not a cheaper one.** XL costs 2x L, so the
rubric is written to make XL almost never fire. If nothing you do ever needs it,
delete the XL case; if you want it more often, loosen the XL paragraph in
`_CA_RUBRIC`. Either way rerun `ca-test`.

Unknown or a failed classification falls back to **L**, never XL. It never silently
downgrades and never silently doubles: a wrong L costs money, a wrong XL costs
double, a wrong XS costs a redo.

Edit `_CA_RUBRIC` in the script to retune, then rerun `ca-test`.

## Notes

- Adds ~3s to session start (the classifier is a real Haiku call).
- Preserves any existing `claude` shell function as `_claude_stock`, which is also
  your escape hatch.
- Wrappers that call `command claude` directly are untouched.
- zsh only.

MIT.
