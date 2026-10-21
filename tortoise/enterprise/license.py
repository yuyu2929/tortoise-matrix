"""Enterprise licensing.

The dual-license model in code:

* Community (free): every feature a person needs to chat, under AGPL-3.0.
* Enterprise (paid): the same core plus organization-grade capabilities
  gated behind a signed license key - audit trails, retention policies,
  the admin API, SSO/SCIM readiness and white-label branding.

Key format::

    TORT-ENT.<base64url payload>.<hmac-sha256 signature>

The payload carries the organization, seat count, expiry and feature set.
Keys are generated locally here for self-hosted trials; a hosted key
service is the production answer (see COMMERCIAL.md).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass, field
from typing import Any, Optional

#: Features unlocked by an enterprise license. Community tier has none of
#: these, and never needs them.
ENTERPRISE_FEATURES = (
    "audit",       # immutable audit trail of every operation
    "retention",   # automated data retention / purge policies
    "admin",       # admin API + usage reporting
    "sso",         # SSO/SCIM readiness hooks
    "branding",    # white-label theming
)

#: Demo signing secret. **Replace with a server-side verification service
#: before taking real money** - anything shipped to customers can be
#: reverse-engineered.
DEMO_SECRET = b"tortoise-community-demo-secret-2026"


class LicenseError(Exception):
    """Raised when a license key is malformed, tampered with or expired."""


@dataclass(frozen=True)
class LicenseInfo:
    key: str
    org: str
    seats: int
    tier: str  # "community" | "enterprise"
    expires_at: Optional[float]
    features: frozenset[str] = field(default_factory=frozenset)

    def to_public_dict(self) -> dict[str, Any]:
        """Safe to expose over the API (never the key itself)."""
        return {
            "org": self.org,
            "seats": self.seats,
            "tier": self.tier,
            "expires_at": self.expires_at,
            "features": sorted(self.features),
            "expired": self.is_expired(),
        }

    def is_expired(self, now: Optional[float] = None) -> bool:
        if self.expires_at is None:
            return False
        return (now or time.time()) > self.expires_at


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64decode(encoded: str) -> bytes:
    pad = "=" * (-len(encoded) % 4)
    return base64.urlsafe_b64decode(encoded + pad)


def _sign(body: str) -> str:
    return hmac.new(DEMO_SECRET, body.encode(), hashlib.sha256).hexdigest()[:16]


class LicenseManager:
    """Issues, activates and verifies license keys."""

    def __init__(self) -> None:
        self._active: Optional[LicenseInfo] = None

    # ------------------------------------------------------------- issuance

    @classmethod
    def generate(
        cls,
        org: str,
        seats: int = 1,
        days: int = 365,
        features: Optional[tuple[str, ...]] = None,
    ) -> str:
        """Mint a signed license key (self-hosted trials / sales demos)."""
        feats = tuple(sorted(set(features or ENTERPRISE_FEATURES)))
        payload = {
            "org": org,
            "seats": seats,
            "exp": int(time.time()) + days * 86400,
            "features": feats,
            "tier": "enterprise",
        }
        body = _b64encode(json.dumps(payload, separators=(",", ":")).encode())
        return f"TORT-ENT.{body}.{_sign(body)}"

    # ------------------------------------------------------------ activation

    def activate(self, key: str) -> LicenseInfo:
        info = self.verify(key)
        self._active = info
        return info

    def deactivate(self) -> None:
        self._active = None

    def verify(self, key: str) -> LicenseInfo:
        parts = key.split(".")
        if len(parts) != 3 or parts[0] != "TORT-ENT":
            raise LicenseError("malformed license key")
        _, body, sig = parts
        if not hmac.compare_digest(_sign(body), sig):
            raise LicenseError("license signature mismatch - key is not genuine")
        try:
            payload = json.loads(_b64decode(body))
        except Exception as exc:
            raise LicenseError(f"unreadable license payload: {exc}") from exc
        info = LicenseInfo(
            key=key,
            org=payload.get("org", "unknown"),
            seats=int(payload.get("seats", 1)),
            tier=payload.get("tier", "enterprise"),
            expires_at=payload.get("exp"),
            features=frozenset(payload.get("features", ())),
        )
        if info.is_expired():
            raise LicenseError(f"license for {info.org} expired")
        return info

    # ---------------------------------------------------------------- checks

    @property
    def active(self) -> Optional[LicenseInfo]:
        return self._active

    def is_enterprise(self) -> bool:
        return self._active is not None and self._active.tier == "enterprise"

    def check(self, feature: str) -> bool:
        """Is this feature unlocked by the active license?"""
        if self._active is None or self._active.is_expired():
            return False
        return feature in self._active.features

    def require(self, feature: str) -> LicenseInfo:
        if not self.check(feature):
            raise LicenseError(
                f"feature {feature!r} requires an active enterprise license"
            )
        assert self._active is not None
        return self._active
