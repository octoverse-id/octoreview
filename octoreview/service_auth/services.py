"""Service keys: `ocr_<8 hex>_<secret>`, stored only as a peppered HMAC (docs/spec.md section 2).

Ported from octonomy `service_auth/services.py` with the `ocr` prefix.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from octoreview.core.auth import SCOPES
from octoreview.service_auth.models import ServiceClient, ServiceClientGrant

TOKEN_PREFIX = "ocr"
MAX_SERVICE_TOKEN_LENGTH = 200


def hash_service_token(token: str) -> str:
    # Only a peppered HMAC is stored: the prefix finds the row, and a database leak does not
    # reveal usable keys.
    pepper = settings.SERVICE_TOKEN_PEPPER.encode()
    return hmac.new(pepper, token.encode(), hashlib.sha256).hexdigest()


def generate_service_token() -> tuple[str, str]:
    key_prefix = secrets.token_hex(4)
    secret = secrets.token_urlsafe(32)
    return f"{TOKEN_PREFIX}_{key_prefix}_{secret}", key_prefix


def parse_service_token(token: str) -> tuple[str, str] | None:
    """Return (token, key_prefix), or None for anything that is not a well-formed key.

    Malformed tokens are rejected before hashing or a database lookup, so arbitrary bearer
    strings cannot force expensive work.
    """

    if len(token) > MAX_SERVICE_TOKEN_LENGTH:
        return None
    parts = token.split("_", 2)
    if len(parts) != 3 or parts[0] != TOKEN_PREFIX or not parts[1] or not parts[2]:
        return None
    return token, parts[1]


def authenticate_service_token(token: str) -> ServiceClient | None:
    """The active, unexpired client for a key, or None (unknown, revoked or expired)."""

    parsed = parse_service_token(token)
    if parsed is None:
        return None
    raw_token, key_prefix = parsed
    try:
        client = ServiceClient.objects.get(
            key_prefix=key_prefix, hashed_key=hash_service_token(raw_token)
        )
    except ServiceClient.DoesNotExist:
        return None
    if not client.is_active:
        return None
    if client.expires_at and client.expires_at <= timezone.now():
        return None
    return client


def unknown_scopes(scopes: list[str]) -> list[str]:
    return sorted(set(scopes) - SCOPES)


def create_service_client_token(
    *,
    name: str,
    grants: list[dict],
    is_active: bool = True,
    expires_at=None,
    metadata: dict | None = None,
) -> tuple[str, ServiceClient]:
    for grant in grants:
        unknown = unknown_scopes(grant.get("scopes", []))
        if unknown:
            raise ValueError(f"Unknown scopes: {', '.join(unknown)}")
    token, key_prefix = generate_service_token()
    with transaction.atomic():
        # The raw key is returned exactly once; only its prefix and hash are stored.
        client = ServiceClient.objects.create(
            name=name,
            key_prefix=key_prefix,
            hashed_key=hash_service_token(token),
            is_active=is_active,
            expires_at=expires_at,
            metadata=metadata or {},
        )
        records = [
            ServiceClientGrant(
                service_client=client,
                tenant_id=grant["tenant_id"],
                application_id=grant.get("application_id"),
                namespace_type=grant.get("namespace_type"),
                namespace_id=grant.get("namespace_id"),
                namespace_wildcard=grant.get("namespace_wildcard", False),
                scopes=sorted(set(grant.get("scopes", []))),
            )
            for grant in grants
        ]
        for record in records:
            # bulk_create skips model validation; validate first so blank namespace values and
            # missing parent applications fail the same way on SQLite and PostgreSQL.
            record.full_clean()
        ServiceClientGrant.objects.bulk_create(records)
    return token, client


def revoke_service_client(key_prefix: str) -> ServiceClient | None:
    client = ServiceClient.objects.filter(key_prefix=key_prefix).first()
    if client is None:
        return None
    client.is_active = False
    client.save(update_fields=["is_active", "updated_at"])
    return client
