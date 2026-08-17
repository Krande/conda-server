from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_list_channels_empty(client):
    response = await client.get("/api/channels")
    assert response.status_code == 200
    assert response.json() == []


@pytest.mark.asyncio
async def test_create_channel_requires_auth(client):
    response = await client.post(
        "/api/channels",
        json={"name": "my-channel", "description": "test"},
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_get_missing_channel(client):
    response = await client.get("/api/channels/does-not-exist")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_get_missing_repodata(client):
    response = await client.get("/does-not-exist/linux-64/repodata.json")
    assert response.status_code == 404
