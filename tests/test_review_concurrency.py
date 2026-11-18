import threading

import pytest
from django.db import connection
from rest_framework.test import APIClient

from octoreview.reviews.models import Review
from tests.conftest import postgres_only

pytestmark = [postgres_only, pytest.mark.django_db(transaction=True)]


def test_concurrent_duplicate_reviews_one_201_one_409(make_key, headers):
    token = make_key({"application_id": "app-1", "scopes": ["reviews:write", "entities:write"]})
    barrier = threading.Barrier(2)
    statuses, errors = [], []

    def worker():
        try:
            client = APIClient()
            barrier.wait(timeout=10)
            response = client.post(
                "/api/v1/reviews",
                {
                    "application_id": "app-1",
                    "entity_type": "product",
                    "external_id": "race",
                    "author_id": "same-author",
                    "rating": 5,
                },
                format="json",
                **headers(token),
            )
            statuses.append(response.status_code)
        except Exception as exc:
            errors.append(exc)
        finally:
            connection.close()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert errors == []
    assert sorted(statuses) == [201, 409]
    assert Review.objects.count() == 1
