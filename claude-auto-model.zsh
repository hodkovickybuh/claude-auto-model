# Optional controller commands. Plain claude keeps its native launcher and UI.
typeset -g _CA_ROOT="${${(%):-%N}:A:h}"

# Undo only this project's previous takeover when re-sourced in an existing shell.
# zsh's function-body round trip removes spaces around case-pattern separators.
if (( $+functions[_claude_stock] && $+functions[ca] && $+functions[claude] )) &&
   [[ "${functions[claude]// | /|}" == "${functions[ca]// | /|}" ]]; then
  functions[claude]=$functions[_claude_stock]
fi

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

function ca-test {
  (cd -- "$_CA_ROOT" && python3 -m unittest discover -v)
}

function ca-doctor {
  CA_STOCK_FUNCTION="$functions[_claude_stock]" python3 "$_CA_ROOT/auto_model.py" --doctor "$@"
}
