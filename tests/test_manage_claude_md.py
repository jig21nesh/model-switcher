import json
from pathlib import Path

import pytest

import manage_claude_md

BLOCK_SOURCE = Path(__file__).resolve().parent.parent / "config" / "claude-md-section.md"
EXISTING = "# My Global Standards\n\n- rule one\n- rule two\n"


@pytest.fixture
def paths(tmp_path):
    block_file = tmp_path / "claude-md-section.md"
    block_file.write_text(BLOCK_SOURCE.read_text())
    return {
        "claude_md": tmp_path / "CLAUDE.md",
        "block": block_file,
        "manifest": tmp_path / "installed.json",
    }


def run_cli(action, paths):
    argv = [action, "--claude-md", str(paths["claude_md"]), "--manifest", str(paths["manifest"])]
    if action == "install":
        argv += ["--block-file", str(paths["block"])]
    return manage_claude_md.main(argv)


def manifest(paths):
    return json.loads(paths["manifest"].read_text()) if paths["manifest"].exists() else {}


class TestInstall:
    def test_creates_file_when_missing(self, paths):
        run_cli("install", paths)
        content = paths["claude_md"].read_text()
        assert "model-switcher:begin" in content
        assert "MANDATORY" in content
        assert manifest(paths)["created_claude_md"] is True

    def test_appends_to_existing_without_touching_content(self, paths):
        paths["claude_md"].write_text(EXISTING)
        run_cli("install", paths)
        content = paths["claude_md"].read_text()
        assert content.startswith("# My Global Standards")
        assert "- rule two" in content
        assert "model-switcher:begin" in content
        assert manifest(paths)["created_claude_md"] is False

    def test_backup_created_once_for_existing_file(self, paths):
        paths["claude_md"].write_text(EXISTING)
        run_cli("install", paths)
        backup = paths["claude_md"].with_name("CLAUDE.md.model-switcher.bak")
        assert backup.read_text() == EXISTING
        paths["claude_md"].write_text(EXISTING + "\n# user edit\n")
        run_cli("install", paths)
        assert backup.read_text() == EXISTING

    def test_reinstall_is_idempotent(self, paths):
        paths["claude_md"].write_text(EXISTING)
        run_cli("install", paths)
        once = paths["claude_md"].read_text()
        run_cli("install", paths)
        assert paths["claude_md"].read_text() == once
        assert once.count("model-switcher:begin") == 1

    def test_reinstall_updates_block_content(self, paths):
        paths["claude_md"].write_text(EXISTING)
        run_cli("install", paths)
        paths["block"].write_text(
            "<!-- model-switcher:begin -->\nNEW POLICY TEXT v2\n<!-- model-switcher:end -->\n"
        )
        run_cli("install", paths)
        content = paths["claude_md"].read_text()
        assert "NEW POLICY TEXT v2" in content
        assert "MANDATORY" not in content
        assert content.count("model-switcher:begin") == 1
        assert content.startswith("# My Global Standards")


class TestUninstall:
    def test_removes_block_and_keeps_user_content(self, paths):
        paths["claude_md"].write_text(EXISTING)
        run_cli("install", paths)
        run_cli("uninstall", paths)
        content = paths["claude_md"].read_text()
        assert "model-switcher" not in content
        assert "- rule two" in content

    def test_deletes_file_only_if_installer_created_it(self, paths):
        run_cli("install", paths)
        run_cli("uninstall", paths)
        assert not paths["claude_md"].exists()

    def test_keeps_file_created_by_installer_if_user_added_content(self, paths):
        run_cli("install", paths)
        content = paths["claude_md"].read_text()
        paths["claude_md"].write_text(content + "\n# my own additions\n")
        run_cli("uninstall", paths)
        remaining = paths["claude_md"].read_text()
        assert "# my own additions" in remaining
        assert "model-switcher" not in remaining

    def test_uninstall_without_install_is_noop(self, paths):
        paths["claude_md"].write_text(EXISTING)
        run_cli("uninstall", paths)
        assert paths["claude_md"].read_text() == EXISTING

    def test_uninstall_when_file_absent_is_noop(self, paths):
        assert run_cli("uninstall", paths) == 0
        assert not paths["claude_md"].exists()

    def test_duplicate_blocks_all_removed(self, paths):
        block = paths["block"].read_text().strip() + "\n"
        paths["claude_md"].write_text(f"{EXISTING}\n{block}\n{block}")
        run_cli("uninstall", paths)
        assert "model-switcher" not in paths["claude_md"].read_text()
