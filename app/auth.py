"""HS256 JWT verification for employee requests."""

from __future__ import annotations

import os
from dataclasses import dataclass

import jwt

DEFAULT_JWT_SECRET = "harborline-practice-signing-key-v1"
DEFAULT_JWT_ISSUER = "harborline-lab-idp"
DEFAULT_JWT_AUDIENCE = "harborline-answer-service"


class AuthError(Exception):
    """Raised when an employee bearer token is missing, invalid, or expired."""


@dataclass(frozen=True)
class AuthenticatedUser:
    sub: str
    roles: tuple[str, ...]


def verify_bearer_token(
    authorization: str | None,
    *,
    secret: str | None = None,
    issuer: str | None = None,
    audience: str | None = None,
) -> AuthenticatedUser:
    if not authorization or not isinstance(authorization, str):
        raise AuthError("missing authorization header")

    parts = authorization.split(" ")
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
        raise AuthError("malformed bearer header")

    token = parts[1].strip()
    resolved_secret = secret or os.environ.get("HARBORLINE_JWT_SECRET", DEFAULT_JWT_SECRET)
    resolved_issuer = issuer or os.environ.get("HARBORLINE_JWT_ISSUER", DEFAULT_JWT_ISSUER)
    resolved_audience = audience or os.environ.get("HARBORLINE_JWT_AUDIENCE", DEFAULT_JWT_AUDIENCE)

    try:
        claims = jwt.decode(
            token,
            resolved_secret,
            algorithms=["HS256"],
            issuer=resolved_issuer,
            audience=resolved_audience,
            options={"require": ["exp", "iss", "aud", "sub", "roles"]},
        )
    except jwt.PyJWTError as exc:
        raise AuthError(f"invalid jwt: {exc}") from exc

    sub = claims.get("sub")
    roles = claims.get("roles")
    if not isinstance(sub, str) or not sub.strip():
        raise AuthError("invalid sub claim")
    if not isinstance(roles, list) or not all(isinstance(r, str) and r.strip() for r in roles):
        raise AuthError("invalid roles claim")

    normalized_roles = tuple(sorted({r.strip() for r in roles}))
    return AuthenticatedUser(sub=sub.strip(), roles=normalized_roles)
