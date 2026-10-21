"""Bounded, allowlisted HTTPS retrieval for public sources.

* HTTPS only, no embedded credentials, default port only.
* Every redirect target is re-validated against the same allowlist.
* Downloads are size-bounded; nothing identifying is ever sent (no cookies, no auth).
* Identifies itself honestly; uses normal content-negotiation headers (the MedCare site
  rejects Python's default user agent with HTTP 406 but serves ordinary clients).
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import socket
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from . import __version__

USER_AGENT = (f"Mozilla/5.0 (compatible; MedCareResources/{__version__}; "
              "+https://github.com/myacaexpress/MCP-Codex-Plugin)")
HTML_ACCEPT = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"

MEDCARE_HOSTS = frozenset({"mcpartnerstx.com", "www.mcpartnerstx.com"})
VENDOR_HOSTS = frozenset({
    "client.formularynavigator.com", "openenrollment.medimpact.com",
    "www1.deltadentalins.com", "www.deltadentalins.com", "www.vsp.com", "vsp.com",
    "www.ashlink.com", "ashlink.com", "www.silverandfit.com", "m3p-form.optumrx.com",
    "www.mcg.com",
})
GOV_HOSTS = frozenset({
    "data.cms.gov", "www.cms.gov", "cms.gov", "www.medicare.gov", "medicare.gov",
    "npiregistry.cms.hhs.gov", "rxnav.nlm.nih.gov", "www.nucc.org",
})
# Official carrier publication domains commonly used for Summary of Benefits PDFs.
# A document from these hosts still has to pass content identity checks.
CARRIER_HOSTS = frozenset({
    "mcpartnerstx.com", "www.mcpartnerstx.com",
    "www.uhc.com", "www.aarpmedicareplans.com", "www.uhcmedicaresolutions.com",
    "www.myuhcagent.com", "uhc.com",
    "www.humana.com", "humana.com", "assets.humana.com", "docushare-web.apps.external.pioneer.humana.com",
    "www.aetna.com", "www.aetnamedicare.com", "aetnamedicare.com", "www.aetnabetterhealth.com",
    "www.anthem.com", "file.anthem.com", "www22.anthem.com",
    "www.wellcare.com", "wellcare.com", "www.ambetterhealth.com",
    "www.devoted.com", "assets.devoted.com",
    "www.cigna.com", "www.cignamedicare.com", "static.cigna.com", "www.healthspring.com",
    "www.bcbstx.com", "www.hcsc.com",
    "www.memorialhermann.org", "www.mhhealthplan.com",
    "www.zinghealth.com", "www.alignmenthealthplan.com", "www.alignmenthealth.com",
    "www.clevercarehealthplan.com", "www.championhealthplan.com", "www.championhmo.com",
    "www.molinahealthcare.com", "www.molinamarketplace.com",
    "www.superiorhealthplan.com", "www.texaschildrenshealthplan.org",
    "www.communityhealthchoice.org", "www.elevancehealth.com",
    "www.kelseycareadvantage.com", "www.baylorscottandwhitehealthplan.com", "www.bswhealthplan.com",
    "www.scottandwhitehealthplan.com", "www.wellpoint.com", "www.simplyhealthcareplans.com",
    "www.carefirst.com", "www.kp.org", "healthy.kaiserpermanente.org",
})
ALL_PUBLIC = MEDCARE_HOSTS | VENDOR_HOSTS | GOV_HOSTS | CARRIER_HOSTS


class FetchError(Exception):
    """Raised for any retrieval failure; message is safe to show to the user."""


@dataclass
class Response:
    url: str
    final_url: str
    status: int
    headers: dict
    body: bytes
    fetched_at: str = field(default_factory=lambda: now_iso())

    @property
    def content_type(self) -> str:
        return (self.headers.get("content-type") or "").split(";")[0].strip().lower()

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.body).hexdigest()

    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    def json(self):
        return json.loads(self.body.decode("utf-8-sig"))


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def cache_dir() -> Path:
    """Writable per-user cache. Never inside the installed plugin (it may be read-only)."""
    for env in ("MEDCARE_CACHE_DIR", "PLUGIN_DATA", "CLAUDE_PLUGIN_DATA"):
        value = os.environ.get(env)
        if value:
            path = Path(value).expanduser() / ("" if env == "MEDCARE_CACHE_DIR" else "medcare-resources")
            break
    else:
        base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
        path = Path(base) / "medcare-resources"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _private_host(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except OSError:
        return False  # resolution may happen at a proxy; the allowlist still applies
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            return True
    return False


def validate_url(url: str, hosts: frozenset | set | None) -> str:
    if not isinstance(url, str) or len(url) > 2048:
        raise FetchError("A public https URL (max 2048 characters) is required.")
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not host:
        raise FetchError("Only https:// URLs are supported.")
    if parts.username or parts.password:
        raise FetchError("URLs with embedded credentials are refused.")
    if parts.port not in (None, 443):
        raise FetchError("Only the default HTTPS port is supported.")
    if hosts is not None and host not in hosts:
        raise FetchError(f"Host '{host}' is not on this tool's public-source allowlist.")
    if hosts is None and _private_host(host):
        raise FetchError("Refusing to contact a private, loopback or reserved network address.")
    return url.strip()


class _CheckedRedirects(urllib.request.HTTPRedirectHandler):
    def __init__(self, hosts):
        super().__init__()
        self.hosts = hosts

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_url(newurl, self.hosts)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch(url: str, *, hosts=ALL_PUBLIC, max_bytes: int = 25_000_000, accept: str = HTML_ACCEPT,
          headers: dict | None = None, timeout: float = 30, method: str = "GET",
          data: bytes | None = None, retries: int = 1) -> Response:
    """Fetch a public URL. Raises FetchError with a user-safe message."""
    url = validate_url(url, hosts)
    request_headers = {"User-Agent": USER_AGENT, "Accept": accept,
                       "Accept-Language": "en-US,en;q=0.9"}
    request_headers.update(headers or {})
    opener = urllib.request.build_opener(_CheckedRedirects(hosts),
                                         urllib.request.HTTPSHandler(context=ssl.create_default_context()))
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, headers=request_headers, method=method, data=data)
        try:
            with opener.open(req, timeout=timeout) as resp:
                final = resp.geturl()
                validate_url(final, hosts)
                body = resp.read(max_bytes + 1)
                if len(body) > max_bytes:
                    raise FetchError(f"Response exceeds the {max_bytes:,}-byte safety limit.")
                hdrs = {k.lower(): v for k, v in resp.headers.items()}
                return Response(url, final, resp.status, hdrs, body)
        except urllib.error.HTTPError as err:
            if err.code in (429, 502, 503, 504) and attempt < retries:
                time.sleep(1.5 * (attempt + 1))
                last_error = err
                continue
            raise FetchError(f"HTTP {err.code} from {urlsplit(url).hostname}") from None
        except FetchError:
            raise
        except (urllib.error.URLError, OSError, ssl.SSLError) as err:
            last_error = err
            if attempt < retries:
                time.sleep(1.0)
                continue
    reason = getattr(last_error, "reason", last_error)
    raise FetchError(f"Could not reach {urlsplit(url).hostname}: {type(last_error).__name__}: {reason}")


def check_link(url: str, *, hosts=None, timeout: float = 20) -> dict:
    """Light-weight availability check (small ranged GET; many servers reject HEAD)."""
    result = {"url": url, "checked_at": now_iso()}
    try:
        resp = fetch(url, hosts=hosts, max_bytes=2_000_000, timeout=timeout,
                     headers={"Range": "bytes=0-65535"}, retries=0)
        result.update(ok=200 <= resp.status < 400, http_status=resp.status,
                      final_url=resp.final_url, content_type=resp.content_type)
    except FetchError as err:
        result.update(ok=False, error=str(err))
    return result
