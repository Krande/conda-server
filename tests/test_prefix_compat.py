"""The prefix.dev-shaped upload route that ``rattler-build publish`` talks to.

Requests are built the way rattler's prefix uploader builds them: bearer
token, one multipart part named ``file`` carrying X-File-Name and
X-File-SHA256 part headers, ``?force=true`` to overwrite.
"""

from __future__ import annotations

import hashlib
from unittest.mock import patch

import pytest

from conda_server import storage as storage_module
from conda_server.auth import hash_token
from conda_server.db import get_sessionmaker
from conda_server.models import ApiKey, Channel, User
from conda_server.storage import LocalStore, ObstoreStorage
from tests.test_channel_admin import _published, _stub_index

TOKEN = "cs_prefix_compat_test_token"
FILENAME = "xtensor-0.25.0-hf036a51_0.conda"


async def _seed(channel_name: str = "up", mirror_url: str | None = None) -> None:
    sm = get_sessionmaker()
    async with sm() as session:
        user = User(subject="ci", email="ci@example.com", username="ci", role="admin")
        session.add(user)
        session.add(Channel(name=channel_name, storage_prefix=channel_name, mirror_url=mirror_url))
        await session.flush()
        session.add(ApiKey(user_id=user.id, key_hash=hash_token(TOKEN)))
        await session.commit()


def _file_part(body: bytes, sha256: str | None = None):
    sha256 = sha256 if sha256 is not None else hashlib.sha256(body).hexdigest()
    return {
        "file": (
            FILENAME,
            body,
            "application/octet-stream",
            {"X-File-Name": FILENAME, "X-File-SHA256": sha256},
        )
    }


@pytest.fixture
def store(tmp_path):
    s = ObstoreStorage(LocalStore(str(tmp_path)), supports_signing=False)
    storage_module.set_storage(s)
    try:
        yield s
    finally:
        storage_module.reset_storage()


@pytest.fixture
def stub_rattler():
    with patch(
        "conda_server.api.channels.rattler.IndexJson.from_package_archive",
        return_value=_stub_index(
            subdir="linux-64", name="xtensor", version="0.25.0", build="hf036a51_0"
        ),
    ):
        yield


async def _post(
    client, body: bytes, *, force: bool = False, sha256: str | None = None, token=TOKEN
):
    url = "/api/v1/upload/up" + ("?force=true" if force else "")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return await client.post(url, files=_file_part(body, sha256), headers=headers)


@pytest.mark.asyncio
async def test_upload_stores_and_publishes(app, client, store, stub_rattler):
    await _seed()
    body = b"FAKE_CONDA_BYTES" * 64

    resp = await _post(client, body)

    assert resp.status_code == 201, resp.text
    assert await store.get(f"up/linux-64/{FILENAME}") == body
    published = await _published(store, "up", "linux-64")
    assert published[FILENAME]["sha256"] == hashlib.sha256(body).hexdigest()


@pytest.mark.asyncio
async def test_existing_package_conflicts_unless_forced(app, client, store, stub_rattler):
    await _seed()
    assert (await _post(client, b"FIRST")).status_code == 201

    # 409 is what rattler-build's --skip-existing keys on.
    again = await _post(client, b"SECOND")
    assert again.status_code == 409
    assert await store.get(f"up/linux-64/{FILENAME}") == b"FIRST"

    forced = await _post(client, b"SECOND", force=True)
    assert forced.status_code == 201
    assert forced.json()["replaced"] is True
    assert await store.get(f"up/linux-64/{FILENAME}") == b"SECOND"


@pytest.mark.asyncio
async def test_sha256_mismatch_is_rejected_before_storage(app, client, store, stub_rattler):
    await _seed()

    resp = await _post(client, b"BYTES", sha256="0" * 64)

    assert resp.status_code == 422
    assert await store.head(f"up/linux-64/{FILENAME}") is None


@pytest.mark.asyncio
async def test_requires_token(app, client, store, stub_rattler):
    await _seed()

    assert (await _post(client, b"BYTES", token=None)).status_code == 401
    assert (await _post(client, b"BYTES", token="cs_wrong")).status_code == 401


@pytest.mark.asyncio
async def test_mirror_channel_rejected(app, client, store, stub_rattler):
    await _seed(mirror_url="https://conda.anaconda.org/conda-forge")

    assert (await _post(client, b"BYTES")).status_code == 400
