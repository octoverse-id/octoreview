"""Shared model pieces: the namespace axis (docs/spec.md section 1)."""

from __future__ import annotations

import re

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q

RESERVED_NAMESPACE_TYPE_GLOBAL = "global"

# One source of truth for the namespace column width; the header parser validates against it so
# caller input never reaches the database longer than the column (a DataError/500 on PostgreSQL).
NAMESPACE_FIELD_MAX_LENGTH = 100

# Tenant and application ids must start with an alphanumeric character, so neither can collide
# with the `_staging/` or `media/` storage prefixes (docs/spec.md section 7.2).
SCOPE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")


def namespace_scope_check() -> Q:
    return Q(namespace_type__isnull=True, namespace_id__isnull=True) | (
        Q(namespace_type__isnull=False, namespace_id__isnull=False, application_id__isnull=False)
        & ~Q(namespace_type="")
        & ~Q(namespace_id="")
        & ~Q(namespace_type=RESERVED_NAMESPACE_TYPE_GLOBAL)
    )


def namespace_scope_constraint() -> models.CheckConstraint:
    return models.CheckConstraint(
        condition=namespace_scope_check(),
        name="%(app_label)s_%(class)s_namespace_scope",
    )


class NamespaceScopedModel(models.Model):
    """Global rows are NULL/NULL; namespaced rows always sit below an application."""

    namespace_type = models.CharField(max_length=NAMESPACE_FIELD_MAX_LENGTH, null=True, blank=True)
    namespace_id = models.CharField(max_length=NAMESPACE_FIELD_MAX_LENGTH, null=True, blank=True)

    class Meta:
        abstract = True
        constraints = [namespace_scope_constraint()]

    def clean(self) -> None:
        super().clean()
        if self.namespace_type is None and self.namespace_id is None:
            return
        errors = {}
        if not self.namespace_type or not str(self.namespace_type).strip():
            errors["namespace_type"] = "This value cannot be blank."
        if self.namespace_type == RESERVED_NAMESPACE_TYPE_GLOBAL:
            errors["namespace_type"] = "The literal 'global' is reserved; omit namespace fields."
        if not self.namespace_id or not str(self.namespace_id).strip():
            errors["namespace_id"] = "This value cannot be blank."
        if getattr(self, "application_id", None) is None:
            errors["application_id"] = "Namespaced rows require an application_id."
        if errors:
            raise ValidationError(errors)
