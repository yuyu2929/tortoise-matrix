"""Enterprise features: the paid tier of the dual-license model.

Community (free, AGPL-3.0): the full client - local-first state, optimistic
updates, background sync, the REST/WS API. Everything a person needs.

Enterprise (commercial license): audit trails, retention policies, the
admin API and usage reporting. Everything an organization's compliance
team needs. See COMMERCIAL.md for the licensing rationale.

Usage::

    from tortoise.enterprise import EnterpriseManager, LicenseManager

    ent = EnterpriseManager()
    ent.activate_license(LicenseManager.generate(org="Acme Corp", seats=50))
    app = create_app(client, enterprise=ent)
"""
from __future__ import annotations

from typing import Any, Optional

from .admin import build_admin_router
from .audit import AuditLogger
from .license import (
    ENTERPRISE_FEATURES,
    LicenseError,
    LicenseInfo,
    LicenseManager,
)
from .retention import RetentionManager, RetentionPolicy

__all__ = [
    "AuditLogger",
    "EnterpriseManager",
    "ENTERPRISE_FEATURES",
    "LicenseError",
    "LicenseInfo",
    "LicenseManager",
    "RetentionManager",
    "RetentionPolicy",
]


class EnterpriseManager:
    """Wires license, audit, retention and admin API together."""

    def __init__(
        self,
        audit_db: str = ":memory:",
        enable_audit: bool = True,
        enable_retention: bool = True,
    ) -> None:
        self.licenses = LicenseManager()
        self.audit: Optional[AuditLogger] = AuditLogger(audit_db) if enable_audit else None
        self.retention: Optional[RetentionManager] = None  # bound to a store
        self._retention_enabled = enable_retention
        self.admin_router = build_admin_router(
            self.licenses,
            audit=self.audit,
            retention=None,
            usage_provider=self._usage,
        )

    # ------------------------------------------------------------- lifecycle

    async def start(self, store: Any) -> None:
        """Bind to a LocalStore and start background enterprise services."""
        if self._retention_enabled:
            self.retention = RetentionManager(store)
            await self.retention.start()
        if self.audit is not None:
            await self.audit.connect()
        # Rebuild the admin router now that audit/retention exist.
        self.admin_router = build_admin_router(
            self.licenses,
            audit=self.audit,
            retention=self.retention,
            usage_provider=self._usage,
        )

    async def stop(self) -> None:
        if self.retention is not None:
            await self.retention.stop()
        if self.audit is not None:
            await self.audit.close()

    # ---------------------------------------------------------------- license

    def activate_license(self, key: str) -> LicenseInfo:
        return self.licenses.activate(key)

    @property
    def is_enterprise(self) -> bool:
        return self.licenses.is_enterprise()

    # ------------------------------------------------------------------ usage

    async def _usage(self) -> dict[str, Any]:
        info = self.licenses.active
        return {
            "org": info.org if info else None,
            "seats_licensed": info.seats if info else 0,
            "audit_events": await self.audit.count() if self.audit else 0,
            "retention": self.retention.status() if self.retention else None,
        }
