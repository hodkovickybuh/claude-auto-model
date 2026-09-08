import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


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
            with patch.dict(os.environ, {"ZDOTDIR": str(root / "ignored")}):
                install(shell, backups)
            self.assertFalse((root / "ignored").exists())
            after = shell.read_text()
            self.assertIn("export USER_SETTING=keep", after)
            self.assertNotIn("source ~/.claude/auto-model.zsh", after)
            self.assertEqual(len(list(backups.iterdir())), 1)
            self.assertEqual(next(backups.iterdir()).read_text(), original)
            install(shell, backups)
            self.assertEqual(shell.read_text(), after)
            self.assertEqual(len(list(backups.iterdir())), 1)

    def test_default_install_uses_zdotdir_and_preserves_backup_and_idempotence(self):
        from install import install
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            user_home, zdotdir = root / "home", root / "zsh config"
            user_home.mkdir()
            zdotdir.mkdir()
            home_shell = user_home / ".zshrc"
            home_shell.write_text("export HOME_SETTING=untouched\n")
            shell = zdotdir / ".zshrc"
            original = "export ZSH_SETTING=keep\n"
            shell.write_text(original)
            backups = root / "archive"
            with patch.object(Path, "home", return_value=user_home), \
                 patch.dict(os.environ, {"ZDOTDIR": str(zdotdir)}):
                backup = install(backup_dir=backups)
                after = shell.read_text()
                self.assertIn("# claude-auto-model begin", after)
                self.assertIn(original, after)
                self.assertEqual(home_shell.read_text(), "export HOME_SETTING=untouched\n")
                self.assertEqual(backup.read_text(), original)
                self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
                self.assertIsNone(install(backup_dir=backups))
                self.assertEqual(shell.read_text(), after)
                self.assertEqual(len(list(backups.iterdir())), 1)


if __name__ == "__main__":
    unittest.main()
