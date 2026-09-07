# Auto model/effort router for Claude Code.  Sourced from ~/.zshrc.
#
#   claude "your task"    -> haiku classifies the prompt, launches on that tier
#   claude / claude -c    -> passthrough, no classification
#   claude --model X ...  -> passthrough, your flags win
#   _claude_stock ...     -> escape hatch, the original launcher, never routed
#   ca-test               -> the check: 8 known prompts must land on their tier
#
# Session-level. Claude Code has no hook or tool that sets the model mid-turn
# (control-protocol set_model exists but only over SDK / Remote Control / IDE),
# so this picks once at launch. Subagent tiering is handled by the rule in
# ~/.claude/CLAUDE.md instead, which is per task.

_CA_RUBRIC='Classify the task prompt above into one tier. Reply with the tier code only.

XS = a pure lookup or factual question with a known answer, a status check, one shell command, "what is X".
S  = one trivial edit: typo, rename, reformat, a single obvious change.
M  = normal work: build or change a feature, fix a normal bug, write or edit copy and content, multi-file edits, research, code review.
L  = hard or high-stakes: architecture or design decisions, debugging already tried and failed, big refactors, security, money, legal or contract text, anything irreversible or customer-facing at scale.
XL = reserve for work L clearly cannot carry: novel system design with no established answer, or a long multi-system build where one wrong early decision wastes days. Almost nothing is XL. If L could plausibly do it, answer L.

If torn between two tiers, pick the higher one, except never reach for XL to break a tie.
Reply with ONE of: XS, S, M, L, XL. Nothing else. Do not do the task.'

# Prints S, M or L. Empty on any failure; callers treat that as L.
_ca_tier() {
  local launcher=claude p="$1"
  (( $+functions[_claude_stock] )) && launcher=_claude_stock
  # Strip image refs: the classifier cannot open them, rambles, and the whole
  # prompt then falls through to the L failsafe. Screenshot-only -> M.
  p=$(printf '%s' "$p" | sed -E 's/\[Image[^]]*\]//g' | sed -E 's/^[[:space:]]+|[[:space:]]+$//g')
  [[ -z "$p" ]] && { print M; return; }
  printf 'TASK PROMPT:\n"""\n%s\n"""\n\n%s' "$p" "$_CA_RUBRIC" \
    | "$launcher" -p --model haiku --effort low --setting-sources '' \
        --disable-slash-commands --strict-mcp-config --mcp-config '{"mcpServers":{}}' \
        2>/dev/null | tr -d '[:space:]' | grep -oE '^(XS|XL|S|M|L)$' || true
}

# ponytail: unknown tier (incl. classifier failure) -> today's default, opus/high.
# Never silently downgrade: a wrong L costs money, a wrong S costs a redo.
_ca_launch() {
  local tier model effort
  tier=$(_ca_tier "$*")
  case "$tier" in
    XS) model=haiku      effort=low   ;;
    S)  model=sonnet     effort=low   ;;
    M)  model=sonnet     effort=high  ;;
    XL) model=fable      effort=xhigh ;;
    # ponytail: unknown / classifier failure -> opus, never fable. A wrong L
    # costs money, a wrong XL costs double, a wrong XS costs a redo.
    *)  model=opus effort=high tier=L ;;
  esac
  print -P "%F{244}auto: ${tier} -> ${model} / ${effort}%f"
  _claude_stock --model "$model" --effort "$effort" "$@"
}

# Take over plain `claude`. The stock launcher (Pavel's env-stripping wrapper)
# is preserved verbatim as _claude_stock. claude-kimi / claude-glm / claude-key
# call `command claude` and are untouched.
if ! (( $+functions[_claude_stock] )); then
  if (( $+functions[claude] )); then
    eval "_claude_stock() ${functions[claude]}"   # preserve an existing wrapper
  else
    _claude_stock() { command claude "$@"; }
  fi
  claude() {
    if [[ $# -eq 0 || "$1" == -* ]]; then _claude_stock "$@"; return; fi
    _ca_launch "$@"
  }
  ca() { claude "$@"; }
fi

ca-test() {
  local fails=0 n=0 got
  while IFS='|' read -r want prompt; do
    (( n++ )); got=$(_ca_tier "$prompt")
    if [[ "$got" == "$want" ]]; then print -P "%F{green}ok%f   $want  $prompt"
    else print -P "%F{red}MISS%f want=$want got=${got:-<empty>}  $prompt"; (( fails++ )); fi
  done <<'EOF'
XS|whats the git command to undo the last commit
XS|check what claude version im on
S|fix the typo in the README
S|rename getUser to fetchUser in this file
M|add a dark mode toggle to the settings page
M|write me 5 instagram captions for the new challenge
L|the checkout webhook silently drops events sometimes, ive tried 3 fixes already
L|review the UZO risk disclosure clause 4 for enforceability
L|should we use postgres or mongo for the affiliate ledger
EOF
  (( fails == 0 )) && print -P "%F{green}$n/$n ok%f" || print -P "%F{red}$fails failed%f"
  return $fails
}
