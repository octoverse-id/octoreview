"""Entity and application-settings writes (docs/spec.md sections 3.1, 3.2 and 5.1)."""

from __future__ import annotations

from django.db import IntegrityError, transaction

from octoreview.audit import services as audit
from octoreview.core.audit_context import AuditContext
from octoreview.core.namespaces import ScopeContext
from octoreview.entities.models import POLICY_FIELDS, ApplicationSettings, Entity

SETTINGS_FIELDS = (*POLICY_FIELDS, "allowed_content_types", "moderation_mode", "max_storage_bytes")
ENTITY_SNAPSHOT_FIELDS = ("metadata", "is_active", *POLICY_FIELDS)


def _snapshot(instance, fields) -> dict:
    return {field: getattr(instance, field) for field in fields}


def _audit_entity(entity: Entity, action: str, ctx: AuditContext, changes: dict) -> None:
    audit.record(
        tenant_id=entity.tenant_id,
        application_id=entity.application_id,
        namespace_type=entity.namespace_type,
        namespace_id=entity.namespace_id,
        action=action,
        aggregate_type="entity",
        aggregate_id=entity.id,
        audit_context=ctx,
        changes=changes,
    )


def _entity_key(tenant_id, application_id, scope_context: ScopeContext, entity_type, external_id):
    return {
        "tenant_id": tenant_id,
        "application_id": application_id,
        "namespace_type": scope_context.namespace_type,
        "namespace_id": scope_context.namespace_id,
        "entity_type": entity_type,
        "external_id": external_id,
    }


def get_or_create_entity(
    *,
    tenant_id: str,
    application_id: str,
    scope_context: ScopeContext,
    entity_type: str,
    external_id: str,
    audit_context: AuditContext,
    defaults: dict | None = None,
) -> tuple[Entity, bool]:
    """Find or create one entity; safe under concurrent first use.

    The create runs in a savepoint. When a concurrent request wins the unique constraint, the
    read is retried once and returns the winner's row (docs/spec.md section 5.1).
    """

    key = _entity_key(tenant_id, application_id, scope_context, entity_type, external_id)
    existing = Entity.objects.filter(**key).first()
    if existing is not None:
        return existing, False
    try:
        with transaction.atomic():
            entity = Entity.objects.create(**key, **(defaults or {}))
            _audit_entity(
                entity,
                "entity.created",
                audit_context,
                {field: [None, value] for field, value in (defaults or {}).items()},
            )
            return entity, True
    except IntegrityError:
        entity = Entity.objects.filter(**key).first()
        if entity is None:
            raise
        return entity, False


def upsert_entity(
    *,
    tenant_id: str,
    application_id: str,
    scope_context: ScopeContext,
    entity_type: str,
    external_id: str,
    values: dict,
    audit_context: AuditContext,
) -> tuple[Entity, bool]:
    """PUT /entities. Sets the given metadata and overrides; never reactivates an entity."""

    with transaction.atomic():
        entity, created = get_or_create_entity(
            tenant_id=tenant_id,
            application_id=application_id,
            scope_context=scope_context,
            entity_type=entity_type,
            external_id=external_id,
            audit_context=audit_context,
            defaults=values,
        )
        if created:
            return entity, True
        entity = Entity.objects.select_for_update().get(pk=entity.pk, tenant_id=entity.tenant_id)
        before = _snapshot(entity, ENTITY_SNAPSHOT_FIELDS)
        for field, value in values.items():
            setattr(entity, field, value)
        changes = audit.diff(before, _snapshot(entity, ENTITY_SNAPSHOT_FIELDS))
        if changes:
            entity.save(update_fields=[*values.keys(), "updated_at"])
            _audit_entity(entity, "entity.updated", audit_context, changes)
        return entity, False


def update_entity(entity: Entity, values: dict, audit_context: AuditContext) -> Entity:
    """PATCH /entities/{id}: metadata replaced whole, `null` clears an override, `is_active`."""

    with transaction.atomic():
        entity = Entity.objects.select_for_update().get(pk=entity.pk, tenant_id=entity.tenant_id)
        before = _snapshot(entity, ENTITY_SNAPSHOT_FIELDS)
        for field, value in values.items():
            setattr(entity, field, value)
        after = _snapshot(entity, ENTITY_SNAPSHOT_FIELDS)
        changes = audit.diff(before, after)
        if not changes:
            return entity
        entity.save(update_fields=[*values.keys(), "updated_at"])
        action = "entity.updated"
        if "is_active" in changes:
            action = "entity.reactivated" if entity.is_active else "entity.deactivated"
        _audit_entity(entity, action, audit_context, changes)
        return entity


def deactivate_entity(entity: Entity, audit_context: AuditContext) -> Entity:
    return update_entity(entity, {"is_active": False}, audit_context)


def get_settings(tenant_id: str, application_id: str) -> ApplicationSettings | None:
    return ApplicationSettings.objects.filter(
        tenant_id=tenant_id, application_id=application_id
    ).first()


def put_settings(
    *, tenant_id: str, application_id: str, values: dict, audit_context: AuditContext
) -> ApplicationSettings:
    """PUT replaces every field; explicit null means "inherit the server default"."""

    with transaction.atomic():
        row = get_settings(tenant_id, application_id)
        if row is None:
            try:
                with transaction.atomic():
                    row = ApplicationSettings.objects.create(
                        tenant_id=tenant_id, application_id=application_id
                    )
            except IntegrityError:
                row = get_settings(tenant_id, application_id)
        row = ApplicationSettings.objects.select_for_update().get(
            pk=row.pk, tenant_id=tenant_id, application_id=application_id
        )
        before = _snapshot(row, SETTINGS_FIELDS)
        for field in SETTINGS_FIELDS:
            setattr(row, field, values[field])
        changes = audit.diff(before, _snapshot(row, SETTINGS_FIELDS))
        if changes:
            row.save()
            audit.record(
                tenant_id=tenant_id,
                application_id=application_id,
                action="application_settings.updated",
                aggregate_type="application_settings",
                aggregate_id=application_id,
                audit_context=audit_context,
                changes=changes,
            )
        return row
