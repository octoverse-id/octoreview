"""`make run`: the no-Docker server ignores the shell's DATABASE_URL and PORT (README, Makefile)."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.skipif(shutil.which("make") is None, reason="needs make")


def dry_run(*args: str) -> str:
    # A shell that exported .env.example, or set PORT for another tool, must not redirect make run.
    env = {**os.environ, "DATABASE_URL": "postgres://nowhere", "PORT": "3000"}
    completed = subprocess.run(
        ["make", "--no-print-directory", "-n", "run", *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    return completed.stdout


def test_make_run_ignores_the_shell_environment():
    out = dry_run()
    assert "env -u DATABASE_URL -u PORT" in out
    assert "127.0.0.1:8000" in out
    assert "postgres://nowhere" not in out
    assert "3000" not in out


def test_make_run_takes_overrides_from_the_command_line():
    out = dry_run("PORT=8001")
    assert "runserver 127.0.0.1:8001" in out
    assert "OCTOREVIEW_PUBLIC_BASE_URL=http://127.0.0.1:8001" in out
