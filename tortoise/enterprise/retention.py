"""Enterprise data retention.

Organizations in regulated industries (finance, healthcare, government)
must be able to *prove* how long messages live and *enforce* that policy
automatically. Individuals never need this - so it ships as an enterprise
feature, not a community one.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Optional

from ..core.localstore import LocalStore


@dataclass
class RetentionPolicy:
    """How long messages are kept before automatic purge."""

    max_age_days: int = 365
    enabled: bool = True

    def cutoff(self, now: Optional[float] = None) -> float:
        return (now or time.time()) - self.max_age_days * 86400


class RetentionManager:
    """Applies a retention policy to the local store on a schedule."""

    def __init__(self, store: LocalStore) -> None:
        self.store = store
        self.policy = RetentionPolicy()
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        self.last_purge: Optional[float] = None
        self.last_purged_count: int = 0

    def configure(self, max_age_days: int, enabled: bool = True) -> None:
        self.policy = RetentionPolicy(max_age_days=max_age_days, enabled=enabled)

    async def purge(self, now: Optional[float] = None) -> int:
        """Delete every message older than the policy cutoff."""
        if not self.policy.enabled:
            return 0
        cutoff = self.policy.cutoff(now)
        count = await self.store.delete_messages_before(cutoff)
        self.last_purge = now or time.time()
        self.last_purged_count = count
        return count

    async def start(self, interval_seconds: float = 3600.0) -> None:
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(interval_seconds))

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _loop(self, interval: float) -> None:
        while not self._stop.is_set():
            try:
                await self.purge()
            except asyncio.CancelledError:
                raise
            except Exception:
                pass  # a failed purge must never crash the loop
            await asyncio.sleep(interval)

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.policy.enabled,
            "max_age_days": self.policy.max_age_days,
            "last_purge": self.last_purge,
            "last_purged_count": self.last_purged_count,
        }
