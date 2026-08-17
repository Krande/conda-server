from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_health(client):
    response = await client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert "version" in body


@pytest.mark.asyncio
async def test_ready(client):
    response = await client.get("/ready")
    assert response.status_code == 200
