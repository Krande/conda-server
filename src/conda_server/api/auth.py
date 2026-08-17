"""OIDC login / logout / callback routes.

Uses ``authlib``'s Starlette integration against any OIDC-compliant provider
(Authentik, Keycloak, Authelia, Azure AD, GitHub-as-OIDC, …). Session state
lives in the signed cookie installed by ``SessionMiddleware``.

Users are identified by the OIDC ``sub`` claim. Email and username are kept
in sync on every login so Authentik property changes propagate.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from authlib.integrations.starlette_client import OAuth, OAuthError
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select

from conda_server.auth import current_user, generate_token, hash_token
from conda_server.config import get_settings
from conda_server.db import SessionDep
from conda_server.models import ApiKey, User

router = APIRouter(prefix="/api/auth", tags=["auth"])

_PROVIDER = "oidc"
_oauth: OAuth | None = None


def get_oauth() -> OAuth:
    """Lazily construct the authlib OAuth client from settings."""
    global _oauth
    if _oauth is not None:
        return _oauth

    oidc = get_settings().auth.oidc
    if not (oidc.issuer and oidc.client_id and oidc.client_secret):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="OIDC is not configured; set auth.oidc.{issuer,client_id,client_secret}",
        )

    oauth = OAuth()
    issuer = oidc.issuer.rstrip("/")
    oauth.register(
        name=_PROVIDER,
        server_metadata_url=f"{issuer}/.well-known/openid-configuration",
        client_id=oidc.client_id,
        client_secret=oidc.client_secret,
        client_kwargs={"scope": " ".join(oidc.scopes)},
    )
    _oauth = oauth
    return oauth


def reset_oauth() -> None:
    """Clear the cached OAuth client — tests and settings changes call this."""
    global _oauth
    _oauth = None


@router.get("/login")
async def login(request: Request, redirect: str | None = None):
    """Begin the OIDC authorization flow."""
    client = get_oauth().create_client(_PROVIDER)
    if redirect:
        request.session["post_login_redirect"] = redirect
    callback_url = str(request.url_for("auth_callback"))
    return await client.authorize_redirect(request, callback_url)


@router.get("/callback", name="auth_callback")
async def callback(request: Request, session: SessionDep):
    """Handle the OIDC redirect back from the provider."""
    client = get_oauth().create_client(_PROVIDER)
    try:
        token = await client.authorize_access_token(request)
    except OAuthError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"OIDC authorization failed: {exc.error}",
        ) from exc

    userinfo = token.get("userinfo") or {}
    if not userinfo:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="OIDC token did not include userinfo",
        )

    user = await upsert_user_from_userinfo(session, dict(userinfo))
    await session.commit()

    request.session["sub"] = user.subject
    post_login = request.session.pop("post_login_redirect", "/")
    return RedirectResponse(post_login, status_code=status.HTTP_302_FOUND)


@router.post("/logout")
async def logout(request: Request) -> dict[str, str]:
    """Clear the session cookie. Provider-side logout is not triggered."""
    request.session.clear()
    return {"status": "logged_out"}


@router.get("/me")
async def me(user: Annotated[User, Depends(current_user)]) -> dict[str, Any]:
    """Return the currently authenticated user."""
    return {
        "id": user.id,
        "subject": user.subject,
        "email": user.email,
        "username": user.username,
        "role": user.role,
    }


class TokenCreate(BaseModel):
    description: str | None = Field(default=None, max_length=255)
    expires_in_days: int | None = Field(default=None, ge=1, le=3650)


class TokenOut(BaseModel):
    id: int
    description: str | None
    created_at: datetime
    expires_at: datetime | None
    last_used_at: datetime | None

    model_config = {"from_attributes": True}

    @field_validator("created_at", "expires_at", "last_used_at", mode="before")
    @classmethod
    def _assume_utc(cls, v):
        # SQLite strips timezone info; assume all stored datetimes are UTC.
        if isinstance(v, datetime) and v.tzinfo is None:
            return v.replace(tzinfo=UTC)
        return v


class TokenCreated(TokenOut):
    token: str


@router.post(
    "/tokens",
    response_model=TokenCreated,
    status_code=status.HTTP_201_CREATED,
)
async def create_token(
    payload: TokenCreate,
    session: SessionDep,
    user: Annotated[User, Depends(current_user)],
) -> Any:
    raw = generate_token()
    expires_at = None
    if payload.expires_in_days is not None:
        expires_at = datetime.now(UTC) + timedelta(days=payload.expires_in_days)
    key = ApiKey(
        user_id=user.id,
        key_hash=hash_token(raw),
        description=payload.description,
        expires_at=expires_at,
    )
    session.add(key)
    await session.commit()
    await session.refresh(key)
    base = TokenOut.model_validate(key).model_dump()
    return TokenCreated(**base, token=raw)


@router.get("/tokens", response_model=list[TokenOut])
async def list_tokens(
    session: SessionDep,
    user: Annotated[User, Depends(current_user)],
) -> list[ApiKey]:
    result = await session.execute(
        select(ApiKey).where(ApiKey.user_id == user.id).order_by(ApiKey.created_at.desc())
    )
    return list(result.scalars())


@router.delete("/tokens/{token_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_token(
    token_id: int,
    session: SessionDep,
    user: Annotated[User, Depends(current_user)],
) -> None:
    result = await session.execute(
        select(ApiKey).where(ApiKey.id == token_id, ApiKey.user_id == user.id)
    )
    key = result.scalar_one_or_none()
    if key is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="token not found")
    await session.delete(key)
    await session.commit()


async def upsert_user_from_userinfo(session, userinfo: dict[str, Any]) -> User:
    """Create or refresh a User row from an OIDC userinfo claim set.

    Admin bootstrap: on first login, if the user's email is in
    ``auth.initial_admins``, they are promoted to the ``admin`` role. The list
    is matched case-insensitively against email addresses.
    """
    sub = userinfo.get("sub")
    if not sub:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="OIDC userinfo missing 'sub' claim",
        )

    email = userinfo.get("email")
    username = (
        userinfo.get("preferred_username")
        or userinfo.get("nickname")
        or userinfo.get("name")
        or (email.split("@", 1)[0] if email else None)
    )

    result = await session.execute(select(User).where(User.subject == sub))
    user = result.scalar_one_or_none()

    initial_admins = {a.lower() for a in get_settings().auth.initial_admins}
    should_be_admin = bool(email and email.lower() in initial_admins)

    if user is None:
        role = "admin" if should_be_admin else "user"
        user = User(subject=sub, email=email, username=username, role=role)
        session.add(user)
        await session.flush()
    else:
        user.email = email
        user.username = username
        # Promote (but never auto-demote) so adding an email to initial_admins
        # takes effect for users who signed in before the config change.
        if should_be_admin and user.role != "admin":
            user.role = "admin"

    return user
