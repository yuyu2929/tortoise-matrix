"""Enterprise admin API.

Everything an organization's administrator needs: license status, audit
trail search, retention policy control and usage reporting. All endpoints
require an active enterprise license.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .license import LicenseError, LicenseManager


class RetentionBody(BaseModel):
    max_age_days: int = Field(default=365, ge=1, le=36500)
    enabled: bool = True


def _require_license(license_manager: LicenseManager):
    """FastAPI dependency: 402 unless an active enterprise license is present."""

    def _check() -> Any:
        if not license_manager.is_enterprise():
            raise HTTPException(
                status_code=402,
                detail=(
                    "This endpoint requires a Tortoise enterprise license. "
                    "Personal use is free (AGPL-3.0); organizations need a "
                    "commercial license - see COMMERCIAL.md."
                ),
            )
        return license_manager.active

    return _check


def build_admin_router(
    license_manager: LicenseManager,
    audit: Optional[Any] = None,
    retention: Optional[Any] = None,
    usage_provider: Optional[Any] = None,
) -> APIRouter:
    router = APIRouter(prefix="/api/admin", tags=["enterprise"])
    guard = _require_license(license_manager)

    @router.get("/license")
    async def license_status(_=Depends(guard)) -> dict[str, Any]:
        assert license_manager.active is not None
        return license_manager.active.to_public_dict()

    @router.get("/audit")
    async def audit_query(
        limit: int = 100,
        actor: Optional[str] = None,
        action_prefix: Optional[str] = None,
        _=Depends(guard),
    ) -> dict[str, Any]:
        if audit is None:
            raise HTTPException(501, "audit logging not enabled")
        events = await audit.query(
            limit=limit, actor=actor, action_prefix=action_prefix
        )
        return {"events": events, "total": await audit.count()}

    @router.get("/retention")
    async def retention_status(_=Depends(guard)) -> dict[str, Any]:
        if retention is None:
            raise HTTPException(501, "retention not enabled")
        return retention.status()

    @router.post("/retention")
    async def retention_configure(
        body: RetentionBody, _=Depends(guard)
    ) -> dict[str, Any]:
        if retention is None:
            raise HTTPException(501, "retention not enabled")
        retention.configure(max_age_days=body.max_age_days, enabled=body.enabled)
        return retention.status()

    @router.post("/retention/purge")
    async def retention_purge(_=Depends(guard)) -> dict[str, Any]:
        if retention is None:
            raise HTTPException(501, "retention not enabled")
        count = await retention.purge()
        return {"purged": count, **retention.status()}

    @router.get("/usage")
    async def usage(_=Depends(guard)) -> dict[str, Any]:
        if usage_provider is None:
            return {"seats_licensed": license_manager.active.seats if license_manager.active else 0}
        return await usage_provider()

    return router
