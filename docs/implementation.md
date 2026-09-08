# Automatic routing implementation

## Correction: preserve native Claude Code

Pavel explicitly rejected replacing Claude Code's terminal interface. Passing
the controller's tests did not satisfy that product requirement. Plain `claude`
now keeps its original launcher. Existing controller code remains available only
through `ca` or direct Python execution. Re-sourcing repairs a previous router
takeover without overwriting unrelated launcher customizations.

The notes below describe the historical controller implementation, not a
completed native-interface integration. Native per-prompt model and effort
routing remains unresolved. Do not infer support from the SDK control protocol
or claim a different interface is equivalent to the requested one.

Recovered requirements from Pavel's September 7 conversation:

- Select the main model and effort before every user task, including follow-ups.
- Keep one conversation. Starting Claude again for every prompt is unacceptable.
- Explicit model and effort requests take precedence over automatic routing.
- Route subagents by their own task, without universally forcing Haiku.
- Use all four families, including Fable for exceptionally difficult work.
- Work with the user's Claude authentication and project context.
- Keep an unfinished task's model for status checks and short continuations.
- Account for model-specific warm-cache costs before automatic downgrades.
- Treat 40 percent savings as a measurable target, never a guaranteed outcome.

## Audit of the original implementation

The three-file zsh implementation routes only startup positional prompts. Bare
`claude`, continued sessions and any first argument beginning with `-` bypass it.
Manual flags after a prompt conflict with generated flags. Classifier calls have
no timeout or tool prohibition. The subagent rule is advice, not enforcement.
The XL rubric explicitly suppresses almost all Fable use. There is no offline
regression test, and `ca-test` spends inference credits. The README's claim that
live switching is impossible confuses the interactive terminal with the SDK.
The original function-copy eval also executes trailing statements in multiline
shell launch functions at source time. Direct zsh function assignment fixes it.

## Design

Python 3.10+ standard library controller with a persistent Claude Code subprocess.
The terminal interface sends `set_model` and `apply_flag_settings` control requests
and checks their acknowledgments before submitting each task. Claude retains its
session, tools, project settings, memory and MCP connections. No API proxy or
editing of global model settings. The original terminal stays available as
`_claude_stock`; the router has a plain text terminal interface.

A tool-free isolated Haiku classification receives the current task and a bounded
summary of recent conversation. Structured output is validated, and failed
classification conservatively selects Opus/high. An explicit hard task can select
Fable/xhigh or max. Context limits prevent automatic Haiku downgrades of long
sessions. Explicit session `/model` and `/effort` locks are independently released
with `auto`. Explicit natural language requests apply to that turn.

Before Agent/Task calls without explicit choices, a PreToolUse callback sizes the
delegated task. It selects model aliases and registered general-purpose agent
definitions with effort, not a nonexistent Agent effort argument. Specialized
definitions keep their instructions and effort. The callback never grants permission.
The host displays tool permission requests and collects actual user answers.

## Implementation checklist

- [x] Routing: validated classifier output, follow-up context, manual locks,
  large-context guard, available model filtering and bounded failures.
- [x] Controller: persistent stream-json transport, acknowledged switches,
  permission and hook callbacks, interrupt, disconnect cleanup, readable output.
- [x] Integration: zsh wrapper preserving the original launcher, installation,
  version/connection doctor, documented commands and native escape hatch.
- [x] Verification: offline regression tests plus real CLI switching and
  multi-turn inference on the same session, with observed model IDs.

## Acceptance evidence

Real model responses passed Haiku, Opus 5/high, Sonnet 5/high, Fable 5.1/xhigh,
and Haiku again, with one process and one session retaining a synthetic marker.
A real Agent invocation for a typo correction selected the Sonnet/low definition
and completed. Reproduction commands are in the README. These synthetic checks
prove routing mechanics, not task quality or a percentage saving.

Initial implementation checks: 91 stdlib unittest tests, Python compilation, zsh syntax,
and git whitespace checks. Later review fixes cover correlated background
results, replayed and cancelled approvals, unknown interactions, and environment
pin release. Those failures were reproduced offline before patching.

Interactive acceptance retained Fable/xhigh through a misspelled status request
and short continuation, then downgraded for a clearly new small task. A fresh
controller resumed a stored CSS preference. A real harmless Bash command waited
for explicit permission before running. An Opus/max marker prompt was refused
upstream and reported as an error without model-hopping retries. No claim is made
that routing prevents upstream refusals, outages, rate limits or poor answers.

The local shell installer archives the old source configuration and leaves
provider files untouched. The implementation is published on the repository's
`master` branch. Updating code is separate from changing provider configuration.

## Public beta hardening, 2026-09-08

The follow-on audit reproduced and fixed canonical-mode long-paste stalls,
blocking partial pastes, dropped piped documents when a positional instruction
was present, typeahead approval ambiguity, shrinking context after compaction,
shutdown blocked by surviving subprocess descendants, and ignored `ZDOTDIR`.
Regression tests exercise real PTYs and subprocesses as well as routing policy.

Independent quality floors prevent an accidental XS classification from sending
unbounded work to Haiku. Recognized deepest-reasoning requests elevate to Fable;
uncertain follow-ups retain both the previous model and effort. Manual choices
remain authoritative. These safeguards deliberately favor quality over the
lowest possible price and cannot prove perfect intent detection.

The fixed 32-case development corpus and all three paid evaluation reports are checked
in. The initial run had five conservative over-routes and one safe timeout; after
correction the same corpus passed 32/32. This does not establish general accuracy
or savings. See the README for cost, latency, public-beta limitations and commands
to repeat the tests. Repository images are conceptual artwork, not test evidence.

Independent review additionally reproduced an old low effort overriding a new
escalation, model-discussion text becoming a manual override, unrelated negation
suppressing a security floor, and per-chunk waits slowing large queued pastes.
All were fixed with regressions. The final suite passes 126 tests and the reviewed
live run again passed 32/32. Real engine checks also repeated the five-model
same-session test, subagent routing, resume, long paste, and explicit permission.

## Deliberate limits

The controller uses a new plain-text terminal, not Claude's native TUI. It cannot
retrofit a running native terminal. Full history remains in Claude; switching
models does not eliminate context input or guarantee cache reuse. Routing changes
happen at turn boundaries, not halfway through a model response. Arbitrary extra
provider models can be pinned by exact supported ID but are not automatically
assigned speculative complexity tiers. Account availability remains authoritative.

## Evidence

- https://code.claude.com/docs/en/model-config
- https://code.claude.com/docs/en/headless
- https://code.claude.com/docs/en/agent-sdk/typescript
- https://code.claude.com/docs/en/hooks

Installed CLI at audit time: 2.1.263. Fable alias resolves to Fable 5.1 on that
version, subject to provider configuration and account availability. The original
conversation is local under `.claude/projects/-Users-pavelgerz`, session
`f311983c-ce08-4d0b-8caa-cec182470a50`. Private transcripts are not copied here.
