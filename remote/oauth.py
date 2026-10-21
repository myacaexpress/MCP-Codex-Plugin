"""OAuth 2.1 resource-server checks for the remote (ChatGPT) MedCare Resources MCP server.

This module never issues tokens and never sees passwords. A maintained identity provider
(Auth0, Stytch, WorkOS, Okta, ...) runs the authorization server; ChatGPT obtains an access
token from it for each individual team member and sends it as `Authorization: Bearer ...`.
This server only *verifies* that token with PyJWT:

- signature against the issuer's published JWKS (asymmetric algorithms only, never "none"/HS*),
- `iss` equals the configured issuer exactly,
- `aud` contains this server's canonical resource URL (ChatGPT sends it as the RFC 8707 `resource`),
- `exp` / `nbf` / `iat` (small leeway),
- the required scope,
- and the individual is on the team allowlist (email and/or subject), so a valid token from the same
  tenant is still refused unless the person was explicitly added.

Nothing about the request body is logged here.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import urllib.request
from collections import OrderedDict
from dataclasses import dataclass, field

import jwt
from jwt import PyJWKClient

ALLOWED_ALGS = ["RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512", "EdDSA"]


class AuthError(Exception):
    """Authentication failed (401) or the user is not allowed (403)."""

    def __init__(self, status: int, error: str, description: str):
        super().__init__(description)
        self.status, self.error, self.description = status, error, description


def _split(value: str | None) -> list[str]:
    return [v.strip() for v in (value or "").replace("\n", ",").split(",") if v.strip()]


@dataclass
class AuthConfig:
    issuer: str
    resource: str
    audiences: list[str]
    required_scopes: list[str]
    allowed_emails: set[str]
    allowed_subjects: set[str]
    email_claims: list[str]
    jwks_url: str | None = None
    userinfo_url: str | None = None
    userinfo_fallback: bool = True
    require_email_verified: bool = True
    leeway: int = 30
    scopes_supported: list[str] = field(default_factory=list)

    @classmethod
    def from_env(cls, env=os.environ) -> "AuthConfig":
        issuer = env.get("MEDCARE_OAUTH_ISSUER", "").strip()
        resource = env.get("MEDCARE_RESOURCE_URL", "").strip()
        if not issuer.startswith("https://") or not resource.startswith("https://"):
            raise SystemExit("MEDCARE_OAUTH_ISSUER and MEDCARE_RESOURCE_URL must be set to https:// URLs. "
                             "There is no unauthenticated mode.")
        scopes = _split(env.get("MEDCARE_REQUIRED_SCOPES", "medcare:read"))
        emails = {e.lower() for e in _split(env.get("MEDCARE_ALLOWED_EMAILS"))}
        subjects = set(_split(env.get("MEDCARE_ALLOWED_SUBJECTS")))
        if not emails and not subjects:
            raise SystemExit("Set MEDCARE_ALLOWED_EMAILS and/or MEDCARE_ALLOWED_SUBJECTS: the server refuses to start "
                             "without a team allowlist.")
        return cls(
            issuer=issuer,
            resource=resource,
            audiences=_split(env.get("MEDCARE_OAUTH_AUDIENCE")) or [resource],
            required_scopes=scopes,
            allowed_emails=emails,
            allowed_subjects=subjects,
            email_claims=_split(env.get("MEDCARE_EMAIL_CLAIMS", "email,https://medcare-resources/email")),
            jwks_url=env.get("MEDCARE_OAUTH_JWKS_URL") or None,
            userinfo_url=env.get("MEDCARE_OAUTH_USERINFO_URL") or None,
            userinfo_fallback=env.get("MEDCARE_USERINFO_FALLBACK", "1") == "1",
            require_email_verified=env.get("MEDCARE_REQUIRE_EMAIL_VERIFIED", "1") == "1",
            scopes_supported=_split(env.get("MEDCARE_SCOPES_SUPPORTED")) or ["openid", "email", *scopes],
        )


def _https_json(url: str, headers: dict | None = None, timeout: int = 10) -> dict:
    if not url.startswith("https://"):
        raise AuthError(401, "invalid_token", "authorization server URL must be https")
    req = urllib.request.Request(url, headers={"Accept": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 (https enforced above)
        return json.loads(r.read(1_000_000))


class Verifier:
    def __init__(self, cfg: AuthConfig, jwk_client=None, userinfo_fetch=None):
        self.cfg = cfg
        self._jwk_client = jwk_client
        self._userinfo_fetch = userinfo_fetch or (lambda url, token: _https_json(
            url, {"Authorization": f"Bearer {token}"}))
        self._lock = threading.Lock()
        self._email_cache: "OrderedDict[str, tuple[float, str | None]]" = OrderedDict()
        self._meta: dict | None = None

    # -------------------------------------------------------------- discovery
    def _as_metadata(self) -> dict:
        if self._meta is None:
            base = self.cfg.issuer.rstrip("/")
            last = None
            for path in ("/.well-known/oauth-authorization-server", "/.well-known/openid-configuration"):
                try:
                    meta = _https_json(base + path)
                    if meta.get("issuer") == self.cfg.issuer:
                        self._meta = meta
                        break
                except Exception as err:  # try the next well-known location
                    last = err
            if self._meta is None:
                raise AuthError(401, "invalid_token", f"authorization server metadata unavailable ({type(last).__name__})")
        return self._meta

    def jwk_client(self) -> PyJWKClient:
        if self._jwk_client is None:
            url = self.cfg.jwks_url or self._as_metadata().get("jwks_uri")
            if not url or not url.startswith("https://"):
                raise AuthError(401, "invalid_token", "issuer has no https jwks_uri")
            self._jwk_client = PyJWKClient(url, cache_keys=True, lifespan=3600, timeout=10)
        return self._jwk_client

    # -------------------------------------------------------------- verification
    def verify(self, token: str) -> dict:
        """Return {'sub', 'email', 'scopes'} for an allowed team member, else raise AuthError."""
        if not token or len(token) > 8192:
            raise AuthError(401, "invalid_token", "missing or oversized bearer token")
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError:
            raise AuthError(401, "invalid_token", "malformed token") from None
        if header.get("alg") not in ALLOWED_ALGS:
            raise AuthError(401, "invalid_token", "unsupported token algorithm")
        try:
            key = self.jwk_client().get_signing_key_from_jwt(token).key
            claims = jwt.decode(token, key, algorithms=ALLOWED_ALGS, audience=self.cfg.audiences,
                                issuer=self.cfg.issuer, leeway=self.cfg.leeway,
                                options={"require": ["exp", "iss", "aud", "sub"], "verify_signature": True})
        except AuthError:
            raise
        except jwt.ExpiredSignatureError:
            raise AuthError(401, "invalid_token", "token expired") from None
        except jwt.InvalidAudienceError:
            raise AuthError(401, "invalid_token", "token audience is not this server") from None
        except jwt.InvalidIssuerError:
            raise AuthError(401, "invalid_token", "token issuer is not trusted") from None
        except (jwt.PyJWTError, jwt.PyJWKClientError):
            raise AuthError(401, "invalid_token", "token could not be verified") from None
        scopes = set(str(claims.get("scope", "")).split()) | set(claims.get("scp") or []) | set(
            claims.get("permissions") or [])
        missing = [s for s in self.cfg.required_scopes if s not in scopes]
        if missing:
            raise AuthError(403, "insufficient_scope", f"token lacks required scope(s): {' '.join(missing)}")
        sub = str(claims["sub"])
        email = self._email(claims, token)
        allowed = sub in self.cfg.allowed_subjects or (email is not None and email in self.cfg.allowed_emails)
        if not allowed:
            raise AuthError(403, "access_denied", "this account is not on the MedCare Resources team allowlist")
        return {"sub": sub, "email": email, "scopes": sorted(scopes)}

    def _email(self, claims: dict, token: str) -> str | None:
        for name in self.cfg.email_claims:
            value = claims.get(name)
            if isinstance(value, str) and "@" in value:
                verified = claims.get("email_verified", claims.get(name + "_verified"))
                if self.cfg.require_email_verified and verified is False:
                    return None
                return value.strip().lower()
        if not (self.cfg.allowed_emails and self.cfg.userinfo_fallback):
            return None
        key = hashlib.sha256(token.encode()).hexdigest()
        now = time.time()
        with self._lock:
            hit = self._email_cache.get(key)
            if hit and hit[0] > now:
                return hit[1]
        url = self.cfg.userinfo_url
        try:
            url = url or self._as_metadata().get("userinfo_endpoint")
            info = self._userinfo_fetch(url, token) if url else {}
        except Exception:
            info = {}
        email = info.get("email") if isinstance(info.get("email"), str) else None
        if email and info.get("sub") not in (None, claims.get("sub")):
            email = None  # userinfo must describe the same subject
        if email and self.cfg.require_email_verified and info.get("email_verified") is not True:
            email = None
        email = email.strip().lower() if email else None
        with self._lock:
            self._email_cache[key] = (min(float(claims.get("exp", now + 300)), now + 3600), email)
            while len(self._email_cache) > 1000:
                self._email_cache.popitem(last=False)
        return email
