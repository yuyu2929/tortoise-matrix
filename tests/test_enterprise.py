"""Enterprise tests: licensing, audit, retention, admin gate."""
from __future__ import annotations

import asyncio
import time

import pytest

from tortoise.core.localstore import LocalStore
from tortoise.enterprise import (
    AuditLogger,
    EnterpriseManager,
    LicenseError,
    LicenseManager,
    RetentionManager,
)


# ------------------------------------------------------------------- license


def test_license_roundtrip():
    key = LicenseManager.generate(org="Acme Corp", seats=25, days=30)
    mgr = LicenseManager()
    info = mgr.activate(key)
    assert info.org == "Acme Corp"
    assert info.seats == 25
    assert info.tier == "enterprise"
    assert mgr.is_enterprise()
    assert mgr.check("audit")
    assert not mgr.check("nonexistent-feature")


def test_tampered_key_rejected():
    key = LicenseManager.generate(org="Acme", seats=5)
    bad = key[:-2] + ("aa" if not key.endswith("aa") else "bb")
    mgr = LicenseManager()
    with pytest.raises(LicenseError):
        mgr.activate(bad)


def test_garbage_key_rejected():
    mgr = LicenseManager()
    with pytest.raises(LicenseError):
        mgr.activate("TORT-ENT.not-a-key.0000")


def test_expired_key_rejected():
    key = LicenseManager.generate(org="Old Corp", days=-1)
    mgr = LicenseManager()
    with pytest.raises(LicenseError):
        mgr.activate(key)


def test_community_tier_has_no_enterprise_features():
    mgr = LicenseManager()  # nothing activated
    assert not mgr.is_enterprise()
    assert not mgr.check("audit")
    with pytest.raises(LicenseError):
        mgr.require("admin")


# --------------------------------------------------------------------- audit


async def test_audit_record_and_query():
    log = AuditLogger(":memory:")
    await log.connect()
    await log.record("alice", "POST /messages", "!r:1", {"status": 201})
    await log.record("bob", "POST /messages", "!r:1")
    await log.record("alice", "DELETE /messages/x", "!r:1")
    events = await log.query(limit=10)
    assert len(events) == 3
    alice_only = await log.query(actor="alice")
    assert len(alice_only) == 2
    posts = await log.query(action_prefix="POST")
    assert len(posts) == 2
    assert await log.count() == 3
    await log.close()


# ----------------------------------------------------------------- retention


async def test_retention_purge():
    store = LocalStore(":memory:")
    await store.connect()
    await store.upsert_room("!r:1", name="R")
    await store.upsert_received_message("$old", "!r:1", "@b:x", "old", time.time() - 100 * 86400)
    await store.upsert_received_message("$new", "!r:1", "@b:x", "new", time.time())
    rm = RetentionManager(store)
    rm.configure(max_age_days=30)
    purged = await rm.purge()
    assert purged == 1
    msgs = await store.get_messages("!r:1")
    assert [m["body"] for m in msgs] == ["new"]
    await store.close()


async def test_retention_disabled_purges_nothing():
    store = LocalStore(":memory:")
    await store.connect()
    await store.upsert_received_message("$old", "!r:1", "@b:x", "old", 1.0)
    rm = RetentionManager(store)
    rm.configure(max_age_days=30, enabled=False)
    assert await rm.purge() == 0
    await store.close()


# -------------------------------------------------------------- admin gate


async def test_admin_blocked_without_license():
    from fastapi.testclient import TestClient

    from tortoise import MockMatrixServer, TortoiseClient
    from tortoise.server import create_app

    ent = EnterpriseManager()
    client = TortoiseClient(transport=MockMatrixServer(latency=0.05, jitter=0.01))
    await client.start()
    app = create_app(client, enterprise=ent)
    with TestClient(app) as tc:
        resp = tc.get("/api/admin/license")
        assert resp.status_code == 402
        assert "enterprise license" in resp.json()["detail"]
    await client.stop()


async def test_admin_open_with_license():
    from fastapi.testclient import TestClient

    from tortoise import MockMatrixServer, TortoiseClient
    from tortoise.server import create_app

    ent = EnterpriseManager()
    ent.activate_license(LicenseManager.generate(org="Acme", seats=10))
    client = TortoiseClient(transport=MockMatrixServer(latency=0.05, jitter=0.01))
    await client.start()
    await ent.start(client.store)
    app = create_app(client, enterprise=ent)

    with TestClient(app) as tc:
        # audit was recorded by the middleware for this very request
        ok = tc.get("/api/admin/license")
        assert ok.status_code == 200
        assert ok.json()["org"] == "Acme"
        audit = tc.get("/api/admin/audit")
        assert audit.status_code == 200
        usage = tc.get("/api/admin/usage")
        assert usage.status_code == 200
        assert usage.json()["seats_licensed"] == 10
    await ent.stop()
    await client.stop()


async def test_audit_middleware_records_requests():
    from fastapi.testclient import TestClient

    from tortoise import MockMatrixServer, TortoiseClient
    from tortoise.server import create_app

    ent = EnterpriseManager()
    ent.activate_license(LicenseManager.generate(org="Acme", seats=10))
    client = TortoiseClient(transport=MockMatrixServer(latency=0.05, jitter=0.01))
    await client.start()
    await ent.start(client.store)
    app = create_app(client, enterprise=ent)
    with TestClient(app) as tc:
        tc.get("/api/rooms")
        events = await ent.audit.query(action_prefix="GET /api/rooms")
        assert len(events) >= 1
        assert events[0]["actor"] == "anonymous"
    await ent.stop()
    await client.stop()
