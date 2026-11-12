"""Authentication (JWT) and role-based access control.

Roles (descending privilege): admin > engineer > operator > viewer.
* viewer   : read dashboards, telemetry, reports
* operator : + simulation lab control (simulation mode only)
* engineer : + investigations: run, review, verify, approve
* admin    : + users, connectors, settings
No role can issue commands to plant equipment - the platform has no such path.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import time
from typing import Any

import jwt
from fastapi import Depends, HTTPException, Request, WebSocket, status
from fastapi.security import OAuth2PasswordBearer

from app.core.config import get_settings

ROLE_RANK = {"viewer": 0, "operator": 1, "engineer": 2, "admin": 3}
oauth2 = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/token", auto_error=False)
_ITER = 210_000


def hash_password(pw: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, _ITER)
    return f"pbkdf2_sha256${_ITER}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def verify_password(pw: str, stored: str) -> bool:
    try:
        _, it, salt, dk = stored.split("$")
        calc = hashlib.pbkdf2_hmac("sha256", pw.encode(), base64.b64decode(salt), int(it))
        return hmac.compare_digest(calc, base64.b64decode(dk))
    except Exception:
        return False


def create_token(username: str, role: str) -> str:
    s = get_settings()
    now = int(time.time())
    return jwt.encode({"sub": username, "role": role, "iat": now, "exp": now + s.jwt_expire_minutes * 60},
                      s.jwt_secret, algorithm="HS256")


def decode_token(token: str) -> dict[str, Any]:
    return jwt.decode(token, get_settings().jwt_secret, algorithms=["HS256"])


class Principal(dict):
    @property
    def username(self) -> str:
        return self["sub"]

    @property
    def role(self) -> str:
        return self["role"]


def current_user(request: Request, token: str | None = Depends(oauth2)) -> Principal:
    s = get_settings()
    if not s.auth_required:
        return Principal(sub=s.open_access_user, role=s.open_access_role)
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "not authenticated", {"WWW-Authenticate": "Bearer"})
    try:
        p = Principal(decode_token(token))
    except jwt.PyJWTError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or expired token", {"WWW-Authenticate": "Bearer"})
    request.state.user = p
    return p


def require_role(min_role: str):
    def dep(user: Principal = Depends(current_user)) -> Principal:
        if ROLE_RANK.get(user.role, -1) < ROLE_RANK[min_role]:
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"requires role '{min_role}' or higher")
        return user
    return dep


def ws_principal(ws: WebSocket) -> Principal | None:
    s = get_settings()
    if not s.auth_required:
        return Principal(sub=s.open_access_user, role=s.open_access_role)
    token = ws.query_params.get("token")
    if not token:
        return None
    try:
        return Principal(decode_token(token))
    except jwt.PyJWTError:
        return None
