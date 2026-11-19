"""scripts/version-check.sh: a release's version stamps agree (make version-check)."""

from __future__ import annotations

import pytest


@pytest.fixture
def tree(tmp_path):
    def build(pyproject="1.0.0rc1", openapi="1.0.0-rc.1", settings="1.0.0-rc.1", heading=True):
        (tmp_path / "docs").mkdir(exist_ok=True)
        (tmp_path / "config").mkdir(exist_ok=True)
        (tmp_path / "pyproject.toml").write_text(f'[project]\nversion = "{pyproject}"\n')
        (tmp_path / "docs" / "openapi.yaml").write_text(
            f"openapi: 3.1.0\ninfo:\n  title: Octoreview API\n  version: {openapi}\npaths: {{}}\n"
        )
        (tmp_path / "config" / "settings.py").write_text(
            f'SPECTACULAR_SETTINGS = {{\n    "VERSION": "{settings}",\n}}\n'
        )
        changelog = "# Changelog\n\n## [Unreleased]\n"
        if heading:
            changelog += f"\n## [{openapi}] - 2026-11-20\n"
        (tmp_path / "CHANGELOG.md").write_text(changelog)
        return str(tmp_path)

    return build


def test_agreeing_candidate_stamps_pass(run_script, tree):
    result = run_script("version-check.sh", tree())
    assert result.returncode == 0, result.output
    assert "version-check OK (1.0.0-rc.1)" in result.stdout


def test_agreeing_final_stamps_pass(run_script, tree):
    result = run_script(
        "version-check.sh", tree(pyproject="1.0.0", openapi="1.0.0", settings="1.0.0")
    )
    assert result.returncode == 0, result.output


@pytest.mark.parametrize(
    ("stamps", "message"),
    [
        ({"openapi": "1.0.0-rc.2"}, "docs/openapi.yaml info.version is 1.0.0-rc.2"),
        ({"settings": "1.0.0-dev"}, "config/settings.py VERSION is 1.0.0-dev"),
        ({"heading": False}, 'no "## [1.0.0-rc.1]" heading'),
        ({"pyproject": "0.0.0"}, "info.version is 1.0.0-rc.1, not 0.0.0"),
    ],
)
def test_a_disagreeing_stamp_fails(run_script, tree, stamps, message):
    result = run_script("version-check.sh", tree(**stamps))
    assert result.returncode == 1
    assert message in result.stdout


def test_the_repository_today_names_its_stamps(run_script):
    """Before the first release PR the stamps disagree; the check must say so, not crash."""

    result = run_script("version-check.sh")
    assert result.returncode in (0, 1), result.output
    assert result.stdout.startswith("pyproject=")
