"""The latency bench runs end to end and fails loudly (scripts/bench.py, docs/spec.md 17)."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

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


def load_bench():
    spec = importlib.util.spec_from_file_location("bench", ROOT / "scripts" / "bench.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["bench"] = module  # dataclasses resolve their module while the class is built
    spec.loader.exec_module(module)
    return module


def test_a_retried_round_runs_again_unrecorded():
    # A 304 round that crosses into a new signing epoch gets a fresh ETag and runs again.
    bench = load_bench()
    attempts = []

    def run(state):
        attempts.append(state)
        if len(attempts) == 3:  # the warm-up, one good round, then a boundary
            raise bench.Retry

    result = bench.timed(bench.Result("304"), 3, run, prepare=lambda: len(attempts))
    assert len(result.samples) == 3
    assert len(attempts) == 5  # warm-up + 3 recorded + 1 retried


def test_endless_retries_fail_the_bench():
    bench = load_bench()

    def run(_):
        raise bench.Retry

    with pytest.raises(bench.BenchError, match="had to be retried"):
        bench.timed(bench.Result("304"), 2, run)
