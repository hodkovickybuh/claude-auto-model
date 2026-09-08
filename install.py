"""Install the router in zsh, keeping an archived copy of the previous config."""
import argparse
from datetime import datetime, timezone
import os
from pathlib import Path
import shlex
import shutil
import tempfile


BEGIN, END = "# claude-auto-model begin", "# claude-auto-model end"
LEGACY = (
    "[[ -f ~/.claude/auto-model.zsh ]] && source ~/.claude/auto-model.zsh",
    "[[ -f ~/.claude-auto-model/claude-auto-model.zsh ]] && source ~/.claude-auto-model/claude-auto-model.zsh",
)


def install(shell_file=None, backup_dir=None):
    target = Path(shell_file or Path(os.environ.get("ZDOTDIR", Path.home())) / ".zshrc").expanduser().resolve()
    archive = Path(backup_dir or Path.home() / "Archive" / "claude-auto-model")
    script = Path(__file__).resolve().with_name("claude-auto-model.zsh")
    if not script.is_file():
        raise RuntimeError("Router shell integration is missing")
    original = target.read_text() if target.exists() else ""
    lines = original.splitlines()
    if BEGIN in lines:
        start = lines.index(BEGIN)
        if END not in lines[start:]:
            raise RuntimeError("Existing installation block is incomplete; shell config left untouched")
        end = lines.index(END, start)
        lines[start:end + 1] = []
    lines = [line for line in lines if line.strip() not in LEGACY]
    block = [BEGIN, f"source {shlex.quote(str(script))}", END]
    updated = "\n".join(lines).rstrip() + "\n\n" + "\n".join(block) + "\n"
    if updated == original:
        print("Router already installed.")
        return None
    backup = None
    if target.exists():
        archive.mkdir(parents=True, exist_ok=True, mode=0o700)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        backup = archive / (target.name + "." + stamp)
        shutil.copy2(target, backup)
        backup.chmod(0o600)
    target.parent.mkdir(parents=True, exist_ok=True)
    mode = target.stat().st_mode & 0o777 if target.exists() else 0o600
    with tempfile.NamedTemporaryFile(mode="w", dir=target.parent, prefix=".auto-model-", delete=False) as staged:
        staged.write(updated)
        staged.flush()
        os.fsync(staged.fileno())
    os.chmod(staged.name, mode)
    os.replace(staged.name, target)
    print("Installed optional ca commands. Plain claude keeps its native launcher and UI.")
    if backup:
        print(f"Previous shell configuration archived at {backup}")
    return backup


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shell-file", type=Path)
    parser.add_argument("--backup-dir", type=Path)
    args = parser.parse_args()
    install(args.shell_file, args.backup_dir)
