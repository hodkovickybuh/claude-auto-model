"""Offline shell integration checks. Run: python3 -m unittest -v test_shell."""
import os
from pathlib import Path
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parent
ZSH = shutil.which("zsh")
PRELUDE = r'''
function claude {
  print -r -- STOCK_FIRST
  print -r -- STOCK_SECOND
  if (( $# )); then printf '%s\0' "$@"; fi
}
original_body=$functions[claude]
function python3 {
  [[ "$CA_STOCK_FUNCTION" == "$original_body" ]] || return 91
  [[ "$CA_PROVIDER_FIXTURE" == provider-fixture ]] || return 92
  printf '%s\0' "$@"
}
'''


@unittest.skipUnless(ZSH, "zsh is required")
class ShellTests(unittest.TestCase):
    def run_shell(self, script, args=(), *, prelude=PRELUDE):
        return subprocess.run(
            [ZSH, "-fc", prelude + '\nsource "$CA_SHELL_FILE"\n' + script,
             "shell-test", *args],
            cwd=ROOT.parent,
            env={"PATH": str(Path(ZSH).parent) + os.pathsep + os.defpath,
                 "CA_SHELL_FILE": str(ROOT / "claude-auto-model.zsh"),
                 "CA_PROVIDER_FIXTURE": "provider-fixture"},
            capture_output=True, timeout=5,
        )

    def assert_output(self, result, expected=b"", *, status=0):
        self.assertEqual(result.returncode, status, result.stderr.decode())
        self.assertEqual(result.stdout, expected)
        self.assertEqual(result.stderr, b"")

    @staticmethod
    def argv_bytes(args):
        return b"".join(os.fsencode(arg) + b"\0" for arg in args)

    def test_source_is_inert_and_preserves_multiline_body(self):
        self.assert_output(self.run_shell(
            '[[ "$functions[_claude_stock]" == "$original_body" ]]'
        ))
        self.assert_output(self.run_shell('_claude_stock "$@"', ["first", "two words"]),
                           b"STOCK_FIRST\nSTOCK_SECOND\n" + self.argv_bytes(["first", "two words"]))

    def test_resourcing_refreshes_root_and_functions_without_recursion(self):
        self.assert_output(self.run_shell(r'''
_CA_ROOT=/stale-fixture
functions[claude]=$functions[ca]
function ca-test { print -r -- STALE_TEST; }
function ca-doctor { print -r -- STALE_DOCTOR; }
source "$CA_SHELL_FILE"
source "$CA_SHELL_FILE"
[[ "$functions[_claude_stock]" == "$original_body" ]] || exit 93
claude "$@"
ca "$@"
ca-doctor
''', ["task"]), b"STOCK_FIRST\nSTOCK_SECOND\n" + self.argv_bytes(["task"]) + self.argv_bytes([
            str(ROOT / "auto_model.py"), "task",
            str(ROOT / "auto_model.py"), "--doctor",
        ]))

    def test_plain_claude_keeps_native_launcher_and_all_arguments(self):
        for args in ([], ["task"], ["--resume", "session", "--model", "fable"],
                     ["--unknown-native-option", "two words"]):
            self.assert_output(self.run_shell('claude "$@"', args),
                               b"STOCK_FIRST\nSTOCK_SECOND\n" + self.argv_bytes(args))

    def test_resourcing_does_not_overwrite_a_later_user_launcher(self):
        self.assert_output(self.run_shell(r'''
function claude { print -r -- USER_REPLACEMENT; }
source "$CA_SHELL_FILE"
claude
'''), b"USER_REPLACEMENT\n")

    def test_controller_receives_exact_argv_and_original_function_environment(self):
        config = '{"env":{"FAKE_SECRET":"fixture-only"}}'
        cases = [
            [],
            ["--model", "haiku", "--effort", "low", "task"],
            ["task", "--model", "fable", "--effort", "xhigh"],
            ["--model=sonnet", "task", "--effort=high"],
            ["--settings", config, "task"],
            ["task", "--settings", config],
            ["line one\nline two", "", "quotes ' \" $() `literal`", "Příliš"],
            ["--", "--model", "literal prompt"],
        ]
        for command in ("ca",):
            for args in cases:
                with self.subTest(command=command, args=args):
                    self.assert_output(self.run_shell(command + ' "$@"', args),
                                       self.argv_bytes([str(ROOT / "auto_model.py"), *args]))

    def test_function_environment_is_scoped_to_controller_call(self):
        self.assert_output(self.run_shell(r'''
CA_STOCK_FUNCTION=outer-fixture
ca task
[[ "$CA_STOCK_FUNCTION" == outer-fixture ]]
'''), self.argv_bytes([str(ROOT / "auto_model.py"), "task"]))

    def test_preserved_body_runs_in_parent_launcher_subprocess(self):
        self.assert_output(self.run_shell(r'''
function python3 {
  shift
  zsh -fc 'functions[_claude_stock]="$CA_STOCK_FUNCTION"; _claude_stock "$@"' claude "$@"
}
ca "$@"
''', ["--model", "opus", "task"]),
            b"STOCK_FIRST\nSTOCK_SECOND\n" + self.argv_bytes(["--model", "opus", "task"]))

    def test_native_subcommands_and_version_flags_pass_through_unchanged(self):
        commands = ("auth mcp plugin plugins update install doctor setup-token agents attach "
                    "logs stop kill respawn rm project import remote-control gateway version "
                    "--version -v").split()
        for command in commands:
            with self.subTest(command=command):
                args = [command, "two words", "--model", "fixture"]
                self.assert_output(self.run_shell('claude "$@"', args),
                                   b"STOCK_FIRST\nSTOCK_SECOND\n" + self.argv_bytes(args))

    def test_native_escape_removes_only_its_own_prefix(self):
        for args in ([], ["--model", "opus", "task"], ["--", "--native"]):
            with self.subTest(args=args):
                self.assert_output(self.run_shell('ca --native "$@"', args),
                                   b"STOCK_FIRST\nSTOCK_SECOND\n" + self.argv_bytes(args))

    def test_alias_and_original_function_are_preserved_without_warnings(self):
        result = self.run_shell(r'''
[[ "$aliases[claude]" == "$original_alias" ]] || exit 94
[[ "$functions[claude]" == "$original_body" ]] || exit 95
source /dev/stdin <<'ALIAS_CALL'
claude
ALIAS_CALL
_claude_stock
ca task
''', prelude=PRELUDE + r'''
alias claude='print -r -- ALIAS_INTACT'
original_alias=$aliases[claude]
''')
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertEqual(result.stdout,
                         b"ALIAS_INTACT\nSTOCK_FIRST\nSTOCK_SECOND\n" +
                         self.argv_bytes([str(ROOT / "auto_model.py"), "task"]))
        self.assertEqual(result.stderr, b"")

    def test_no_original_function_installs_an_inert_binary_escape(self):
        self.assert_output(self.run_shell(r'''
[[ "$functions[_claude_stock]" == *'command claude'* ]]
(( ! $+functions[claude] ))
''', prelude=""))

    def test_ca_test_discovers_offline_from_repo_without_changing_caller_cwd(self):
        self.assert_output(self.run_shell(r'''
function python3 { printf '%s\0' "$PWD" "$@"; }
caller_cwd=$PWD
ca-test
[[ "$PWD" == "$caller_cwd" ]]
'''), self.argv_bytes([str(ROOT), "-m", "unittest", "discover", "-v"]))

    def test_doctor_uses_controller_with_original_function(self):
        self.assert_output(self.run_shell('ca-doctor "$@"', ["--verbose"]),
                           self.argv_bytes([str(ROOT / "auto_model.py"), "--doctor", "--verbose"]))

    def test_exit_status_and_stdin_are_preserved(self):
        self.assert_output(self.run_shell(r'''
function python3 { IFS= read -r line; print -r -- "$line"; return 23; }
print -r -- stdin-fixture | ca task
'''), b"stdin-fixture\n", status=23)
        self.assert_output(self.run_shell(r'''
functions[_claude_stock]='return 24'
ca --native
'''), status=24)


if __name__ == "__main__":
    unittest.main()
