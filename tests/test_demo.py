"""Tests for the demo application (create_demo_app)."""
from __future__ import annotations

import time

import pytest

from tortoise.demo import create_demo_app

ROOM = "!design:localhost"


@pytest.fixture
def demo():
    app = create_demo_app(latency=0.8, jitter=0.1)
    from fastapi.testclient import TestClient

    with TestClient(app) as tc:
        yield tc


def test_legacy_send_blocks_for_server_latency(demo):
    t0 = time.perf_counter()
    resp = demo.post(
        "/api/demo/legacy/send",
        json={"room_id": ROOM, "body": "blocking"},
    )
    elapsed = time.perf_counter() - t0
    assert resp.status_code == 200
    assert elapsed >= 0.5
    assert resp.json()["blocked_seconds"] >= 0.5


def test_optimistic_send_beats_server_latency(demo):
    t0 = time.perf_counter()
    resp = demo.post(f"/api/rooms/{ROOM}/messages", json={"body": "fast"})
    elapsed = time.perf_counter() - t0
    assert resp.status_code == 201
    assert elapsed < 0.5


def test_demo_config_exposes_rooms(demo):
    data = demo.get("/api/demo/config").json()
    assert data["local_user"] == "@me:localhost"
    assert any(r["room_id"] == ROOM for r in data["rooms"])
    assert data["latency"] == 0.8


def test_latency_slider_endpoint(demo):
    resp = demo.post(
        "/api/demo/latency",
        json={"latency": 5.0, "jitter": 1.0, "failure_rate": 0.2},
    )
    assert resp.status_code == 200
    assert resp.json() == {"latency": 5.0, "jitter": 1.0, "failure_rate": 0.2}


def test_index_page_served(demo):
    resp = demo.get("/")
    assert resp.status_code == 200
    assert "Tortoise" in resp.text
