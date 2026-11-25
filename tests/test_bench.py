"""The latency bench runs end to end and fails loudly (scripts/bench.py, docs/spec.md 17)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TINY = ["--entities", "3", "--reviews", "10", "--iterations", "2", "--complete-iterations", "1"]


def bench(*args: str) -> subprocess.CompletedProcess:
    # SQLite in memory, whatever this run's DATABASE_URL: the bench must never create its
    # database next to the suite's own PostgreSQL test database.
    env = {k: v for k, v in os.environ.items() if k != "DATABASE_URL"}
    env["DJANGO_SETTINGS_MODULE"] = "config.settings_pytest"
    return subprocess.run(
        [sys.executable, "scripts/bench.py", *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )


def test_the_bench_reports_every_endpoint():
    run = bench(*TINY, "--image-mib", "0.01", "--json")
    assert run.returncode == 0, run.stderr
    report = json.loads(run.stdout)
    assert (report["database"], report["storage_backend"]) == ("sqlite", "local")
    budgets = {row["name"]: row["budget_p95_ms"] for row in report["results"]}
    assert budgets["batch summaries, 100 ids"] == 50
    assert budgets["review list, 20 with media, cold URL cache"] == 150
    assert budgets["intent"] == 100
    assert budgets["complete, 0.01 MiB image"] == 3000
    assert "review list, 20 with media, 304" in budgets
    for row in report["results"]:
        assert row["n"] == (1 if row["name"].startswith("complete") else 2)
        assert 0 < row["p50_ms"] <= row["p95_ms"]


def test_an_unexpected_answer_fails_the_bench():
    # 20 MiB is over the default image limit, so the first complete's intent answers 400.
    run = bench(*TINY, "--image-mib", "20")
    assert run.returncode == 1
    assert "bench failed: intent" in run.stderr
