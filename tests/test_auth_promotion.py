"""Covers the login-time admin promotion path for users who existed before
their email was added to ``auth.initial_admins``.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from conda_server.api import auth as auth_api
from conda_server.config import reset_settings
from conda_server.db import get_sessionmaker
from conda_server.models import User


@pytest.mark.asyncio
async def test_existing_user_promoted_when_email_added_later(app, monkeypatch):
    # User signs in before their email is in initial_admins.
    sm = get_sessionmaker()
    async with sm() as session:
        user = await auth_api.upsert_user_from_userinfo(
            session,
            {"sub": "s-promote", "email": "kristoffer@example.com", "preferred_username": "k"},
        )
        await session.commit()
        assert user.role == "user"

    # Config is updated.
    monkeypatch.setenv(
        "CONDA_SERVER_AUTH__INITIAL_ADMINS",
        '["kristoffer@example.com"]',
    )
    reset_settings()

    # Next login promotes them.
    async with sm() as session:
        refreshed = await auth_api.upsert_user_from_userinfo(
            session,
            {"sub": "s-promote", "email": "kristoffer@example.com", "preferred_username": "k"},
        )
        await session.commit()
        assert refreshed.role == "admin"
        assert refreshed.id == user.id


@pytest.mark.asyncio
async def test_admin_not_demoted_when_email_removed(app, monkeypatch):
    # Seed an admin.
    monkeypatch.setenv(
        "CONDA_SERVER_AUTH__INITIAL_ADMINS",
        '["boss@example.com"]',
    )
    reset_settings()

    sm = get_sessionmaker()
    async with sm() as session:
        user = await auth_api.upsert_user_from_userinfo(
            session,
            {"sub": "s-boss", "email": "boss@example.com", "preferred_username": "b"},
        )
        await session.commit()
        assert user.role == "admin"

    # Remove from config.
    monkeypatch.setenv("CONDA_SERVER_AUTH__INITIAL_ADMINS", "[]")
    reset_settings()

    # Subsequent login keeps the admin role.
    async with sm() as session:
        refreshed = await auth_api.upsert_user_from_userinfo(
            session,
            {"sub": "s-boss", "email": "boss@example.com", "preferred_username": "b"},
        )
        await session.commit()
        assert refreshed.role == "admin"

    # Sanity: DB really shows admin.
    async with sm() as session:
        row = (await session.execute(select(User).where(User.subject == "s-boss"))).scalar_one()
        assert row.role == "admin"
