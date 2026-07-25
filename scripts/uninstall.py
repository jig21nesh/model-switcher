"""Remove everything the installer added.

Lives here rather than in install.sh so that it works from an installed copy as well as
from the repo: once installed, model-switcher is expected to be usable without the clone,
and that has to include getting rid of it. install.sh --uninstall calls this too, so there
is one implementation rather than two that can drift.
"""

import argparse
import shutil
import sys
from pathlib import Path

import generate_agent
import manage_claude_md
import merge_settings

# Everything install.sh copies in, plus the manifest that tracks what it changed.
INSTALLED_FILES = (
    "complexity_router.py",
    "cost_statusline.py",
    "merge_settings.py",
    "manage_claude_md.py",
    "generate_agent.py",
    "uninstall.py",
    "status_report.py",
    "agent_router.py",
    "tune_threshold.py",
    "classifier_report.py",
    "decision_boundary.py",
    "cli.py",
    "analyze_history.py",
    "update_pricing.py",
    "claude-md-section.md",
    "pricing.json",
    "model-switcher",
    "installed.json",
)
# User data. Removing these would throw away pricing choices and learned history.
KEPT_FILES = ("config.json", "classifier.json", "classifier.candidate.json")


def run(claude_dir: Path, install_dir: Path) -> list[str]:
    """Undo the install and return the paths that were deliberately kept."""
    manifest = install_dir / "installed.json"
    merge_settings.main([
        "uninstall",
        "--settings", str(claude_dir / "settings.json"),
        "--install-dir", str(install_dir),
        "--config", str(install_dir / "config.json"),
        "--manifest", str(manifest),
    ])
    manage_claude_md.main([
        "uninstall", "--claude-md", str(claude_dir / "CLAUDE.md"), "--manifest", str(manifest),
    ])
    generate_agent.main([
        "uninstall", "--agents-dir", str(claude_dir / "agents"), "--manifest", str(manifest),
    ])

    for name in INSTALLED_FILES:
        target = install_dir / name
        # Only ever remove a plain file this installer put here.
        if target.is_file() and not target.is_symlink():
            target.unlink()
    for directory in ("state", "__pycache__"):
        # Stale bytecode would otherwise shadow a later install.
        target = install_dir / directory
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target, ignore_errors=True)

    return [str(install_dir / name) for name in KEPT_FILES if (install_dir / name).exists()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--claude-dir", required=True, type=Path)
    parser.add_argument("--install-dir", required=True, type=Path)
    args = parser.parse_args(argv)

    kept = run(args.claude_dir, args.install_dir)
    print("model-switcher removed.")
    if kept:
        print("Kept: " + ", ".join(kept))
    print("Restart Claude Code sessions to apply.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
