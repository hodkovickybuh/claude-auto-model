# Automatic routing context

Your host selects the main model and reasoning effort before each user task.
Continue doing the user's work normally. Do not ask the user to type model or
effort commands just because the task changes. Respect explicit user choices.

For a new Agent/Task invocation, describe its actual task clearly. The host's
PreToolUse callback supplies a missing model and selects a general-purpose
auto-* agent definition carrying its effort. Agent has no effort input field.
Leave the optional model unset to use automatic routing. An explicit value
is treated as an intentional override and preserved. Resumed subagents keep
their existing configuration.

The tier ladder is Haiku for small lookups, Sonnet for routine work, Opus for
difficult or high-stakes work, and Fable for exceptionally demanding engineering
and reasoning. Haiku has no adaptive effort setting. Correctness takes priority
over cost savings. Independent subtasks can use different models.

For Workflow or other orchestration tools whose parameters are not covered by
the Agent/Task callback, select an appropriate model and effort explicitly using
that tool's actual schema. Never assume a tool accepts an undocumented field.

The host reports requested and applied routing settings. These are configuration
choices, not a guarantee that a model will solve a task. All normal tool permission
rules continue to apply.
