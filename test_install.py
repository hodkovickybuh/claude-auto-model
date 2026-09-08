import importlib.util
from pathlib import Path
import tempfile
import unittest


class InstallTests(unittest.TestCase):
    def test_install_is_idempotent_preserves_shell_content_and_backs_up(self):
        self.assertIsNotNone(importlib.util.find_spec("install"), "installer is not implemented")
        from install import install
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shell = root / ".zshrc"
            original = 'export USER_SETTING=keep\n[[ -f ~/.claude/auto-model.zsh ]] && source ~/.claude/auto-model.zsh\n'
            shell.write_text(original)
            backups = root / "archive"
            install(shell, backups)
            after = shell.read_text()
            self.assertIn("export USER_SETTING=keep", after)
            self.assertNotIn("source ~/.claude/auto-model.zsh", after)
            self.assertEqual(len(list(backups.iterdir())), 1)
            self.assertEqual(next(backups.iterdir()).read_text(), original)
            install(shell, backups)
            self.assertEqual(shell.read_text(), after)
            self.assertEqual(len(list(backups.iterdir())), 1)


if __name__ == "__main__":
    unittest.main()
