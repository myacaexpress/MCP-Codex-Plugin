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
from urllib.parse import urlsplit
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
            scopes_supported=_split(env.get("MEDCARE_SCOPES_SUPPORTED")) or ["openid", "email", *scopes],
        )


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow redirects: a bearer token must not be forwarded to any other URL or host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None




def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def _trusted_https(url: str, trusted_hosts: set | None) -> bool:
    parts = urlsplit(url or "")
    return (parts.scheme == "https" and bool(parts.hostname) and not parts.username and not parts.password
            and (trusted_hosts is None or parts.hostname.lower() in trusted_hosts))


def _https_json(url: str, headers: dict | None = None, timeout: int = 10, trusted_hosts: set | None = None) -> dict:
    """GET JSON over HTTPS from a trusted host only, with redirects refused (3xx -> error)."""
    if not _trusted_https(url, trusted_hosts):
        raise AuthError(401, "invalid_token", "authorization server URL must be plain https on a trusted host")
    req = urllib.request.Request(url, headers={"Accept": "application/json", **(headers or {})})
    with urllib.request.build_opener(_NoRedirect).open(req, timeout=timeout) as r:  # noqa: S310 (https + trusted host enforced above)
        if r.status != 200:
            raise AuthError(401, "invalid_token", "unexpected authorization server response")
        return json.loads(r.read(1_000_000))


class Verifier:
    def __init__(self, cfg: AuthConfig, jwk_client=None, userinfo_fetch=None):
        self.cfg = cfg
        self._jwk_client = jwk_client
        # Hosts allowed to receive anything from this server: the issuer's host, plus hosts the operator
        # configured explicitly. The bearer token is only ever sent to the userinfo URL on one of these.
        self.trusted_hosts = {_host(cfg.issuer)} | {_host(u) for u in (cfg.userinfo_url, cfg.jwks_url) if u}
        self._userinfo_fetch = userinfo_fetch or (lambda url, token: _https_json(
            url, {"Authorization": f"Bearer {token}"}, trusted_hosts=self.trusted_hosts))
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
                    meta = _https_json(base + path, trusted_hosts=self.trusted_hosts)
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
            if not url or not _trusted_https(url, self.trusted_hosts):
                raise AuthError(401, "invalid_token", "issuer has no https jwks_uri on a trusted host")
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

    @staticmethod
    def _claim_verified(claims: dict, name: str, value: str) -> bool:
        """Is THIS email value asserted verified? Verification is bound to the exact email it describes.

        * standard 'email': only 'email_verified' is literal true.
        * custom claim X: 'X_verified' is literal true. If 'X_verified' is present with any other value
          (false, "true", 1, null ...) the email is unverified; nothing overrides an explicit assertion.
          Only when 'X_verified' is absent may the standard 'email_verified' be used, and only if the
          standard 'email' is present and is the same address as X.
        """
        if name == "email":
            return claims.get("email_verified") is True
        flag = name + "_verified"
        if flag in claims:
            return claims[flag] is True
        std = claims.get("email")
        return (isinstance(std, str) and std.strip().lower() == value.strip().lower()
                and claims.get("email_verified") is True)

    def _email(self, claims: dict, token: str) -> str | None:
        """The caller's email, only if the IdP asserts it is verified with the literal JSON value true."""
        for name in self.cfg.email_claims:
            value = claims.get(name)
            if isinstance(value, str) and "@" in value and self._claim_verified(claims, name, value):
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
            if url and not _trusted_https(url, self.trusted_hosts):
                url = None  # never send the token to an untrusted or non-https endpoint
            info = self._userinfo_fetch(url, token) if url else {}
        except Exception:
            info = {}
        if not isinstance(info, dict):
            info = {}
        email = info.get("email") if isinstance(info.get("email"), str) and "@" in info.get("email") else None
        if not (isinstance(info.get("sub"), str) and info.get("sub") == claims.get("sub")):
            email = None  # userinfo must name exactly the same subject as the token
        if info.get("email_verified") is not True:
            email = None
        email = email.strip().lower() if email else None
        with self._lock:
            self._email_cache[key] = (min(float(claims.get("exp", now + 300)), now + 3600), email)
            while len(self._email_cache) > 1000:
                self._email_cache.popitem(last=False)
        return email
