"""On-behalf-of tokens for the accounts-payable tools.

The identity provider signs Alice's assertion. The gateway signs the exchanged
token. The tools accept only that token, and only for the ap-payables audience.
"""
from __future__ import annotations

import re
import time

import jwt

HS256 = "HS256"
USER = "user"
APP = "app"
OBO = "obo"
TOKEN_EXCHANGE = "urn:ietf:params:oauth:grant-type:token-exchange"
SUBJECT_OK = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def mint_user_assertion(sub: str, groups: list[str], audience: str, secret: str, ttl: int = 600) -> str:
    now = int(time.time())
    return jwt.encode(
        {"sub": sub, "groups": groups, "aud": audience, "idtyp": USER, "iat": now, "exp": now + ttl},
        secret,
        algorithm=HS256,
    )


def issue_obo(subject: str, actor: str, groups: list[str], audience: str, secret: str, ttl: int = 120) -> str:
    now = int(time.time())
    return jwt.encode(
        {
            "sub": subject,
            "act": {"sub": actor},
            "groups": groups,
            "aud": audience,
            "idtyp": USER,
            "grant": OBO,
            "iat": now,
            "exp": now + ttl,
        },
        secret,
        algorithm=HS256,
    )


def read_subject_token(token: str, idp_secret: str, obo_secret: str, gateway_audience: str) -> tuple[dict | None, str | None]:
    exchanged = _decode(token, obo_secret, audience=None)
    if exchanged is not None and (exchanged.get("grant") == OBO or isinstance(exchanged.get("act"), dict)):
        return None, "token has already been exchanged"
    claims = _decode(token, idp_secret, audience=gateway_audience)
    if claims is None:
        return None, "invalid subject token"
    return claims, None


def authorize_exchange(
    claims: dict,
    actor: str,
    requested_audience: str,
    *,
    allowed_groups: set[str],
    allowed_actors: set[str],
    resource_audience: str,
    group_name: str,
) -> str | None:
    if claims.get("idtyp") != USER:
        return "app-only token has no person"
    if claims.get("act"):
        return "token has already been exchanged"
    subject = claims.get("sub")
    if not isinstance(subject, str) or SUBJECT_OK.fullmatch(subject) is None:
        return "subject is not a person id"
    groups = claims.get("groups") or []
    if not isinstance(groups, list) or not allowed_groups.intersection(groups):
        return f"caller is not in group {group_name}"
    if actor not in allowed_actors:
        return f"{actor} is not on the tool allowlist"
    if requested_audience != resource_audience:
        return f"audience {requested_audience} is not {resource_audience!r}"
    return None


def downscoped_groups(claims: dict, allowed_groups: set[str]) -> list[str]:
    groups = claims.get("groups") or []
    if not isinstance(groups, list):
        return []
    return sorted(allowed_groups.intersection(str(item) for item in groups))


def verify_obo(token: str, secret: str, resource_audience: str, group_name: str) -> tuple[dict | None, str | None]:
    try:
        claims = jwt.decode(
            token,
            secret,
            algorithms=[HS256],
            audience=resource_audience,
            options={"require": ["sub", "exp", "act", "groups"]},
        )
    except jwt.InvalidAudienceError:
        unverified = jwt.decode(token, options={"verify_signature": False})
        return None, f"audience {unverified.get('aud')} is not {resource_audience!r}"
    except jwt.PyJWTError:
        return None, "not an on-behalf-of token"
    if claims.get("grant") != OBO or claims.get("idtyp") != USER:
        return None, "not an on-behalf-of token"
    act = claims.get("act")
    if not isinstance(act, dict) or not act.get("sub"):
        return None, "on-behalf-of token has no actor"
    groups = claims.get("groups") or []
    if not isinstance(groups, list) or group_name not in groups:
        return None, f"caller is not in group {group_name}"
    if not isinstance(claims.get("sub"), str) or SUBJECT_OK.fullmatch(claims["sub"]) is None:
        return None, "subject is not a person id"
    return claims, None


def actor_matches(claims: dict, workload: str) -> str | None:
    act = claims.get("act") or {}
    if not isinstance(act, dict) or act.get("sub") != workload:
        return "actor does not match the workload"
    return None


def _decode(token: str, secret: str, audience: str | None) -> dict | None:
    try:
        if audience is None:
            return jwt.decode(token, secret, algorithms=[HS256], options={"verify_aud": False, "require": ["sub", "exp"]})
        return jwt.decode(token, secret, algorithms=[HS256], audience=audience, options={"require": ["sub", "exp"]})
    except jwt.PyJWTError:
        return None
