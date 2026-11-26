#!/usr/bin/env python
"""Report-only latency bench (docs/spec.md section 17, gate d17b).

Seeds a throwaway database with 1k entities and 10k reviews, plus a page of 20 reviews with media.
It uses Django's test-database machinery, so it never touches the configured database. Then it
times the budgeted endpoints in-process, through the middleware, auth, DRF and the ORM, on the
`local` backend; network and gunicorn time are not in the numbers.

Report-only: a budget overrun is printed, never fatal (contradiction row 40; the query-count
budgets are the CI gate). It exits 1 only when a request answers unexpectedly, so a broken bench
never reads as a fast one.

It always runs on the `local` backend under a temporary media root it deletes afterwards, with
request logging off. On PostgreSQL its database is `test_octoreview_bench`, created and dropped
here, so it never collides with the test suite's `test_octoreview`.

    make bench                      # SQLite
    make bench-postgres             # a throwaway PostgreSQL 17 container (needs Docker)
    uv run python scripts/bench.py --help
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import random
import shutil
import sys
import tempfile
import time
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings_pytest")

TENANT = "bench"
APP = "app-bench"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
MIB = 1024 * 1024
ADMIN_SCOPES = [
    "entities:read",
    "entities:write",
    "reviews:read",
    "reviews:write",
    "reviews:moderate",
    "media:write",
]


class BenchError(Exception):
    """A request answered unexpectedly: the numbers would be meaningless."""


@dataclass
class Result:
    name: str
    samples: list[float] = field(default_factory=list)
    budget_ms: float | None = None

    def percentile(self, p: float) -> float:
        # Nearest rank: the smallest sample with at least p% of the samples at or below it.
        ordered = sorted(self.samples)
        return ordered[max(0, math.ceil(p / 100 * len(ordered)) - 1)]

    def as_dict(self) -> dict:
        p95 = self.percentile(95)
        return {
            "name": self.name,
            "n": len(self.samples),
            "p50_ms": round(self.percentile(50), 2),
            "p95_ms": round(p95, 2),
            "budget_p95_ms": self.budget_ms,
            "over_budget": self.budget_ms is not None and p95 >= self.budget_ms,
        }


class Retry(Exception):
    """The round crossed a boundary (a new signing epoch); it runs again, unrecorded."""


def timed(result: Result, rounds: int, run, prepare=None) -> Result:
    """Record `rounds` timings of `run(prepare())`, after one unrecorded warm-up round.

    A round that raises Retry runs again and is not recorded; more retries than rounds means
    the server keeps answering unexpectedly, which fails the bench.
    """

    warmed, retries = False, 0
    while len(result.samples) < rounds:
        state = prepare() if prepare else None
        started = time.perf_counter()
        try:
            run(state)
        except Retry:
            retries += 1
            if retries > rounds:
                raise BenchError(f"{result.name}: {retries} rounds had to be retried") from None
            continue
        elapsed = (time.perf_counter() - started) * 1000
        if warmed:
            result.samples.append(elapsed)
        warmed = True
    return result


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--entities", type=int, default=1000)
    parser.add_argument("--reviews", type=int, default=10_000)
    parser.add_argument("--iterations", type=int, default=200, help="timed rounds per endpoint")
    parser.add_argument(
        "--complete-iterations", type=int, default=10, help="timed rounds of the 10 MiB complete"
    )
    parser.add_argument("--image-mib", type=float, default=10, help="size of the completed image")
    parser.add_argument("--seed", type=int, default=7, help="random seed for the batch ids")
    parser.add_argument("--json", action="store_true", help="print JSON instead of a table")
    args = parser.parse_args(argv)
    if min(args.entities, args.reviews, args.iterations, args.complete_iterations) < 1:
        parser.error("every count must be at least 1")
    return args


class Bench:
    def __init__(self, args):
        from rest_framework.test import APIClient

        self.args = args
        self.client = APIClient()
        self.random = random.Random(args.seed)

    # --- requests ---------------------------------------------------------------------------

    def headers(self, token: str, **extra) -> dict:
        result = {"HTTP_AUTHORIZATION": f"Bearer {token}", "HTTP_X_TENANT_ID": TENANT}
        for name, value in extra.items():
            result[f"HTTP_{name.upper().replace('-', '_')}"] = value
        return result

    def call(self, name: str, expected: int, method: str, path: str, token: str, body=None, **h):
        if method == "GET":
            response = self.client.get(path, **self.headers(token, **h))
        else:
            response = self.client.generic(
                method,
                path,
                json.dumps(body or {}),
                content_type="application/json",
                **self.headers(token, **h),
            )
        if response.status_code != expected:
            raise BenchError(
                f"{name}: {method} {path} answered {response.status_code}, not {expected}: "
                f"{response.content[:300]!r}"
            )
        return response

    def put_bytes(self, url: str, payload: bytes) -> None:
        response = self.client.generic("PUT", urlsplit(url).path, payload, content_type="image/png")
        if response.status_code != 204:
            raise BenchError(f"upload PUT answered {response.status_code}, not 204")

    def intent(self, token: str, review_id: str, size: int) -> dict:
        response = self.call(
            "intent",
            201,
            "POST",
            f"/api/v1/reviews/{review_id}/media?application_id={APP}",
            token,
            {"kind": "image", "content_type": "image/png", "size_bytes": size},
        )
        return response.json()["data"]

    def complete(self, token: str, review_id: str, media_id: str) -> None:
        self.call(
            "complete",
            200,
            "POST",
            f"/api/v1/reviews/{review_id}/media/{media_id}/complete?application_id={APP}",
            token,
        )

    def upload(self, token: str, review_id: str, payload: bytes) -> None:
        created = self.intent(token, review_id, len(payload))
        self.put_bytes(created["upload"]["url"], payload)
        self.complete(token, review_id, created["media"]["id"])

    # --- seeding ----------------------------------------------------------------------------

    def seed(self) -> None:
        from django.utils import timezone

        from octoreview.entities.models import Entity
        from octoreview.lifecycle.recompute import recompute_ratings
        from octoreview.reviews.models import Review, ReviewStatus
        from octoreview.service_auth.services import create_service_client_token

        def key(name, scopes):
            grant = {
                "tenant_id": TENANT,
                "application_id": APP,
                "namespace_type": None,
                "namespace_id": None,
                "namespace_wildcard": False,
                "scopes": scopes,
            }
            return create_service_client_token(name=name, grants=[grant])[0]

        self.admin = key("bench-admin", ADMIN_SCOPES)
        self.reader = key("bench-reader", ["entities:read", "reviews:read"])

        entities = Entity.objects.bulk_create(
            Entity(
                tenant_id=TENANT, application_id=APP, entity_type="product", external_id=f"sku-{i}"
            )
            for i in range(self.args.entities)
        )
        self.external_ids = [entity.external_id for entity in entities]
        now = timezone.now()
        Review.objects.bulk_create(
            (
                Review(
                    tenant_id=TENANT,
                    application_id=APP,
                    entity=entities[i % len(entities)],
                    author_id=f"user-{i}",
                    rating=1 + i % 5,
                    title=f"Review {i}",
                    body="Solid product, would buy again. " * 4,
                    status=ReviewStatus.PUBLISHED,
                    published_at=now,
                )
                for i in range(self.args.reviews)
            ),
            batch_size=1000,
        )
        recompute_ratings(repair=True)
        self.rated_entity = str(entities[0].id)

        # The budgeted page: 20 published reviews with three images each, through the API.
        image = PNG_MAGIC + bytes(56)
        hot_ids = []
        for i in range(20):
            created = self.call(
                "seed review",
                201,
                "POST",
                "/api/v1/reviews",
                self.admin,
                {
                    "application_id": APP,
                    "entity_type": "product",
                    "external_id": "hot",
                    "author_id": f"hot-{i}",
                    "rating": 5,
                    "title": "With photos",
                },
            ).json()["data"]
            hot_ids.append(created["id"])
            for _ in range(3):
                self.upload(self.admin, created["id"], image)
        self.hot_entity = Entity.objects.get(tenant_id=TENANT, external_id="hot").id
        self.hot_review = hot_ids[0]

        # One fresh review per intent and per complete, so no media limit is ever reached.
        rounds = self.args.iterations + self.args.complete_iterations + 2
        target = Entity.objects.create(
            tenant_id=TENANT, application_id=APP, entity_type="product", external_id="uploads"
        )
        self.upload_reviews = [
            str(review.id)
            for review in Review.objects.bulk_create(
                Review(
                    tenant_id=TENANT,
                    application_id=APP,
                    entity=target,
                    author_id=f"uploader-{i}",
                    rating=4,
                    status=ReviewStatus.PUBLISHED,
                    published_at=now,
                )
                for i in range(rounds)
            )
        ]

    # --- timing -----------------------------------------------------------------------------

    def run(self) -> list[Result]:
        from django.utils import timezone

        from octoreview.core import etags
        from octoreview.media import url_cache

        n = self.args.iterations
        hot_list = f"/api/v1/reviews?application_id={APP}&entity_id={self.hot_entity}&limit=20"
        results = []

        def batch(_):
            ids = self.random.sample(self.external_ids, min(100, len(self.external_ids)))
            # Always 100 ids, as budgeted: unknown ones pad a small seed (and still look up).
            ids += [f"missing-{i}" for i in range(100 - len(ids))]
            body = {"application_id": APP, "entity_type": "product", "external_ids": ids}
            self.call("batch", 200, "POST", "/api/v1/entities/ratings/query", self.reader, body)

        results.append(timed(Result("batch summaries, 100 ids", budget_ms=50), n, batch))

        rating = f"/api/v1/entities/{self.rated_entity}/rating?application_id={APP}"
        results.append(
            timed(
                Result("rating summary"),
                n,
                lambda _: self.call("rating", 200, "GET", rating, self.reader),
            )
        )

        def listed(_):
            response = self.call("list", 200, "GET", hot_list, self.reader)
            if len(response.json()["data"]) != 20:
                raise BenchError("the budgeted page does not hold 20 reviews")

        results.append(
            timed(
                Result("review list, 20 with media, cold URL cache", budget_ms=150),
                n,
                listed,
                prepare=url_cache.clear,
            )
        )
        results.append(timed(Result("review list, 20 with media, warm URL cache"), n, listed))
        # The ETag carries the signing epoch (spec 5.3): a round that crosses into a new epoch
        # gets a fresh ETag and runs again, instead of failing a healthy server's 200.
        validator: dict = {}

        def current_etag():
            epoch = etags.signing_epoch(timezone.now())
            if validator.get("epoch") != epoch:
                response = self.call("etag", 200, "GET", hot_list, self.reader)
                validator.update(epoch=epoch, etag=response["ETag"])
            return validator["etag"]

        def conditional(etag):
            try:
                self.call("304", 304, "GET", hot_list, self.reader, **{"If-None-Match": etag})
            except BenchError:
                if etags.signing_epoch(timezone.now()) != validator["epoch"]:
                    raise Retry from None
                raise

        results.append(
            timed(Result("review list, 20 with media, 304"), n, conditional, prepare=current_etag)
        )
        detail = f"/api/v1/reviews/{self.hot_review}?application_id={APP}"
        results.append(
            timed(
                Result("review detail, 3 media"),
                n,
                lambda _: self.call("detail", 200, "GET", detail, self.reader),
            )
        )
        newest = f"/api/v1/reviews?application_id={APP}&limit=20"
        results.append(
            timed(
                Result(f"review list, newest 20 of {self.args.reviews}"),
                n,
                lambda _: self.call("newest", 200, "GET", newest, self.reader),
            )
        )

        reviews = iter(self.upload_reviews)
        results.append(
            timed(
                Result("intent", budget_ms=100),
                n,
                lambda review_id: self.intent(self.admin, review_id, 1024),
                prepare=lambda: next(reviews),
            )
        )

        payload = PNG_MAGIC + bytes(int(self.args.image_mib * MIB) - len(PNG_MAGIC))

        def staged():
            review_id = next(reviews)
            created = self.intent(self.admin, review_id, len(payload))
            self.put_bytes(created["upload"]["url"], payload)
            return review_id, created["media"]["id"]

        results.append(
            timed(
                Result(f"complete, {self.args.image_mib:g} MiB image", budget_ms=3000),
                self.args.complete_iterations,
                lambda state: self.complete(self.admin, *state),
                prepare=staged,
            )
        )
        return results


def report(args, results: list[Result], vendor: str) -> str:
    from django.conf import settings

    context = {
        "database": vendor,
        "entities": args.entities,
        "reviews": args.reviews,
        "iterations": args.iterations,
        "url_cache_size": settings.MEDIA_URL_CACHE_SIZE,
        "storage_backend": settings.STORAGE_BACKEND,
    }
    rows = [result.as_dict() for result in results]
    if args.json:
        return json.dumps({**context, "results": rows}, indent=2)
    lines = [
        "octoreview bench (report-only; docs/spec.md section 17): "
        + ", ".join(f"{k} {v}" for k, v in context.items()),
        "",
        f"{'endpoint':<46} {'n':>5} {'p50 ms':>9} {'p95 ms':>9}  budget p95",
    ]
    for row in rows:
        budget = row["budget_p95_ms"]
        verdict = "" if budget is None else (" over" if row["over_budget"] else " ok")
        budget_text = "" if budget is None else f"< {budget:g} ms{verdict}"
        lines.append(
            f"{row['name']:<46} {row['n']:>5} {row['p50_ms']:>9.2f} {row['p95_ms']:>9.2f}  "
            + budget_text
        )
    return "\n".join(lines)


def main(argv=None) -> int:
    args = parse_args(argv)
    # Never the operator's media directory or cloud bucket: a throwaway root on `local`.
    media_root = tempfile.mkdtemp(prefix="octoreview-bench-")
    os.environ["OCTOREVIEW_MEDIA_ROOT"] = media_root
    os.environ["OCTOREVIEW_STORAGE_BACKEND"] = "local"
    import django

    django.setup()
    from django.db import connection

    if connection.vendor != "sqlite":  # SQLite's test database is in memory
        connection.settings_dict.setdefault("TEST", {})["NAME"] = "test_octoreview_bench"
    logging.disable(logging.INFO)  # one JSON line per request would flood the report
    # WhiteNoise warns that STATIC_ROOT is missing outside the image (no collectstatic here).
    warnings.filterwarnings("ignore", message="No directory at", category=UserWarning)
    from django.test.utils import (
        setup_databases,
        setup_test_environment,
        teardown_databases,
        teardown_test_environment,
    )

    setup_test_environment()
    databases = setup_databases(verbosity=0, interactive=False)
    try:
        bench = Bench(args)
        bench.seed()
        results = bench.run()
        print(report(args, results, connection.vendor))
    except BenchError as exc:
        print(f"bench failed: {exc}", file=sys.stderr)
        return 1
    finally:
        teardown_databases(databases, verbosity=0)
        teardown_test_environment()
        shutil.rmtree(media_root, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
