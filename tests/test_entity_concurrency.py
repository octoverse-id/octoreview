import threading

import pytest
from django.db import connection

from octoreview.core.audit_context import AuditContext
from octoreview.core.namespaces import GLOBAL_SCOPE
from octoreview.entities.models import Entity
from octoreview.entities.services import get_or_create_entity
from tests.conftest import postgres_only

pytestmark = [postgres_only, pytest.mark.django_db(transaction=True)]


def test_concurrent_first_use_creates_one_entity():
    barrier = threading.Barrier(2)
    results, errors = [], []

    def worker():
        try:
            barrier.wait(timeout=10)
            entity, created = get_or_create_entity(
                tenant_id="tenant-a",
                application_id="app-1",
                scope_context=GLOBAL_SCOPE,
                entity_type="product",
                external_id="race-1",
                audit_context=AuditContext(),
            )
            results.append((entity.id, created))
        except Exception as exc:  # surfaced below; a thread must not swallow it
            errors.append(exc)
        finally:
            connection.close()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert errors == []
    assert Entity.objects.filter(external_id="race-1").count() == 1
    assert len({entity_id for entity_id, _ in results}) == 1
    assert sorted(created for _, created in results) == [False, True]
