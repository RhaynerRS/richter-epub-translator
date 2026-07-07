from __future__ import annotations

import time
import uuid
from typing import Optional
from urllib.parse import urlencode

import httpx
import jwt
from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import RedirectResponse

from .config import settings
from .db import User, user_store

router = APIRouter(prefix="/auth", tags=["auth"])

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"

GITHUB_AUTH_URL = "https://github.com/login/oauth/authorize"
GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token"
GITHUB_USER_URL = "https://api.github.com/user"
GITHUB_EMAILS_URL = "https://api.github.com/user/emails"


def _make_state(provider: str) -> str:
    # Signed short-lived JWT instead of an in-memory store, so CSRF state
    # survives across worker restarts/multiple processes for free.
    payload = {
        "provider": provider,
        "nonce": uuid.uuid4().hex,
        "exp": int(time.time()) + settings.oauth_state_ttl_seconds,
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def _verify_state(state: str, provider: str) -> None:
    try:
        payload = jwt.decode(state, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except jwt.PyJWTError:
        raise HTTPException(400, "Invalid or expired OAuth state")
    if payload.get("provider") != provider:
        raise HTTPException(400, "OAuth state provider mismatch")


def _issue_jwt(user: User) -> dict:
    now = int(time.time())
    payload = {"sub": user.id, "iat": now, "exp": now + settings.jwt_expires_minutes * 60}
    token = jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    return {"access_token": token, "token_type": "bearer"}


@router.get("/google/login")
async def google_login():
    if not settings.google_auth_enabled:
        raise HTTPException(404, "Google login is not enabled")
    state = _make_state("google")
    params = {
        "client_id": settings.google_client_id,
        "redirect_uri": settings.google_redirect_uri,
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "access_type": "online",
        "prompt": "select_account",
    }
    return RedirectResponse(f"{GOOGLE_AUTH_URL}?{urlencode(params)}", status_code=302)


@router.get("/google/callback")
async def google_callback(code: str, state: str):
    if not settings.google_auth_enabled:
        raise HTTPException(404, "Google login is not enabled")
    _verify_state(state, "google")
    async with httpx.AsyncClient() as client:
        token_resp = await client.post(
            GOOGLE_TOKEN_URL,
            data={
                "client_id": settings.google_client_id,
                "client_secret": settings.google_client_secret,
                "code": code,
                "redirect_uri": settings.google_redirect_uri,
                "grant_type": "authorization_code",
            },
        )
        if token_resp.status_code != 200:
            raise HTTPException(400, f"Google token exchange failed: {token_resp.text}")
        access_token = token_resp.json()["access_token"]

        profile_resp = await client.get(
            GOOGLE_USERINFO_URL, headers={"Authorization": f"Bearer {access_token}"}
        )
        if profile_resp.status_code != 200:
            raise HTTPException(400, f"Google profile fetch failed: {profile_resp.text}")
        profile = profile_resp.json()

    user = user_store.upsert_from_oauth(
        provider="google",
        provider_user_id=profile["sub"],
        email=profile.get("email"),
        name=profile.get("name"),
    )
    return _issue_jwt(user)


@router.get("/github/login")
async def github_login():
    if not settings.github_auth_enabled:
        raise HTTPException(404, "GitHub login is not enabled")
    state = _make_state("github")
    params = {
        "client_id": settings.github_client_id,
        "redirect_uri": settings.github_redirect_uri,
        "scope": "read:user user:email",
        "state": state,
    }
    return RedirectResponse(f"{GITHUB_AUTH_URL}?{urlencode(params)}", status_code=302)


@router.get("/github/callback")
async def github_callback(code: str, state: str):
    if not settings.github_auth_enabled:
        raise HTTPException(404, "GitHub login is not enabled")
    _verify_state(state, "github")
    async with httpx.AsyncClient() as client:
        token_resp = await client.post(
            GITHUB_TOKEN_URL,
            data={
                "client_id": settings.github_client_id,
                "client_secret": settings.github_client_secret,
                "code": code,
                "redirect_uri": settings.github_redirect_uri,
            },
            headers={"Accept": "application/json"},
        )
        token_data = token_resp.json() if token_resp.status_code == 200 else {}
        if "access_token" not in token_data:
            raise HTTPException(400, f"GitHub token exchange failed: {token_resp.text}")
        access_token = token_data["access_token"]

        headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}
        profile_resp = await client.get(GITHUB_USER_URL, headers=headers)
        if profile_resp.status_code != 200:
            raise HTTPException(400, f"GitHub profile fetch failed: {profile_resp.text}")
        profile = profile_resp.json()

        email = profile.get("email")
        if not email:
            emails_resp = await client.get(GITHUB_EMAILS_URL, headers=headers)
            if emails_resp.status_code == 200:
                for entry in emails_resp.json():
                    if entry.get("primary") and entry.get("verified"):
                        email = entry.get("email")
                        break

    user = user_store.upsert_from_oauth(
        provider="github",
        provider_user_id=str(profile["id"]),
        email=email,
        name=profile.get("name"),
    )
    return _issue_jwt(user)


async def get_current_user(authorization: Optional[str] = Header(None)) -> Optional[User]:
    if not settings.auth_enabled:
        return None
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing or invalid Authorization header")
    token = authorization.removeprefix("Bearer ").strip()
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except jwt.PyJWTError:
        raise HTTPException(401, "Invalid or expired token")
    user = user_store.get(payload["sub"])
    if user is None:
        raise HTTPException(401, "User not found")
    return user


@router.get("/me")
async def me(current_user: Optional[User] = Depends(get_current_user)):
    if current_user is None:
        return {"user": None}
    return {
        "user": {
            "id": current_user.id,
            "provider": current_user.provider,
            "email": current_user.email,
            "name": current_user.name,
        }
    }
