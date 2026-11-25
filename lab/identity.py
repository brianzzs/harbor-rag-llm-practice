"""Local issuer for the lab's signed employee JWTs."""

import base64
import hashlib
import hmac
import json
import time

SOURCE_TOKEN = "harborline-local-sync-reader"
ADMIN_TOKEN = "harborline-local-scenario-control"
JWT_SECRET = "harborline-practice-signing-key-v1"
ISSUER = "harborline-lab-idp"
AUDIENCE = "harborline-answer-service"
PERSONAS = {
    "morgan": ("dispatch",),
    "robin": ("support",),
    "avery": ("supervisor",),
    "casey": ("finance",),
    "jules": ("dispatch", "support"),
    "sam": (),
}


def base64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def mint_token(persona: str, ttl: int = 3600) -> str:
    now = int(time.time())
    header = {"alg": "HS256", "typ": "JWT"}
    claims = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": persona,
        "iat": now if ttl > 0 else now + ttl - 1,
        "exp": now + ttl,
        "roles": list(PERSONAS[persona]),
    }
    encoded = ".".join(base64url(json.dumps(part, separators=(",", ":")).encode()) for part in (header, claims))
    signature = hmac.new(JWT_SECRET.encode(), encoded.encode(), hashlib.sha256).digest()
    return f"{encoded}.{base64url(signature)}"
