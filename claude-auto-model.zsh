# Source this file from zsh to route interactive tasks through the controller.
typeset -g _CA_ROOT="${${(%):-%N}:A:h}"

if ! (( $+functions[_claude_stock] )); then
  if (( $+functions[claude] )); then
    functions[_claude_stock]=$functions[claude]
  else
    function _claude_stock { command claude "$@"; }
  fi
fi

function ca {
  case "${1-}" in
    auth|mcp|plugin|plugins|update|install|doctor|setup-token|agents|attach|logs|stop|kill|respawn|rm|project|import|remote-control|gateway|version|--version|-v)
      _claude_stock "$@"
      ;;
    --native)
      shift
      _claude_stock "$@"
      ;;
    *)
      CA_STOCK_FUNCTION="$functions[_claude_stock]" python3 "$_CA_ROOT/auto_model.py" "$@"
      ;;
  esac
}

if (( $+aliases[claude] )); then
  print -ru2 -- "claude-auto-model: alias 'claude' left unchanged; use ca for routing or _claude_stock for the original launcher."
else
  functions[claude]=$functions[ca]
fi

function ca-test {
  (cd -- "$_CA_ROOT" && python3 -m unittest discover -v)
}

function ca-doctor {
  CA_STOCK_FUNCTION="$functions[_claude_stock]" python3 "$_CA_ROOT/auto_model.py" --doctor "$@"
}
