"""Remote MedCare Resources MCP server for ChatGPT (MCP Streamable HTTP transport + OAuth).

It serves exactly the same tools as the Codex plugin: it imports the plugin's `server.mcp`
object and dispatches every JSON-RPC message through the same `Server.handle_sync()` code path
(same schemas, validation, lookup and batch-medication logic). Only the transport differs.

Endpoints
  POST /mcp                                     MCP Streamable HTTP (JSON responses, stateless)
  GET  /.well-known/oauth-protected-resource    RFC 9728 protected-resource metadata
  GET  /.well-known/oauth-protected-resource/mcp  (same, path-inserted form)
  GET  /healthz                                 liveness (no auth, no data)

Safety limits (env, see .env.example): body size, per-user rate limit, global concurrency for
tool calls with a bounded wait, per-request timeout. Request bodies, tool arguments and results
are never logged; logs carry method, status, duration and a hash of the user id only.

Run:  python -m remote.medcare_http      (reads configuration from the environment)
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import sys
import threading
import time
from pathlib import Path

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

try:
    from .oauth import AuthConfig, AuthError, Verifier
except ImportError:  # executed as a script
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from oauth import AuthConfig, AuthError, Verifier  # type: ignore

PLUGIN_DIR = Path(os.environ.get("MEDCARE_PLUGIN_DIR") or
                  Path(__file__).resolve().parent.parent / "plugins" / "medcare-resources")
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

import server as plugin_server  # noqa: E402  (the Codex plugin's MCP server definition)
from medcare.mcp_stdio import SUPPORTED_VERSIONS  # noqa: E402

log = logging.getLogger("medcare.http")


class Settings:
    def __init__(self, env=os.environ):
        g = lambda k, d: int(env.get(k, d))  # noqa: E731
        f = lambda k, d: float(env.get(k, d))  # noqa: E731
        self.max_body = g("MEDCARE_MAX_BODY_BYTES", 262_144)
        self.max_concurrent = g("MEDCARE_MAX_CONCURRENT_CALLS", 4)
        self.queue_timeout = f("MEDCARE_QUEUE_TIMEOUT_SECONDS", 20)
        self.call_timeout = f("MEDCARE_CALL_TIMEOUT_SECONDS", 300)
        self.rate_per_min = g("MEDCARE_RATE_LIMIT_PER_MINUTE", 30)
        self.rate_burst = g("MEDCARE_RATE_LIMIT_BURST", 15)
        # Default work budget for medication_list_check when the caller gives none. ChatGPT's tool-call
        # timeout is not documented (Secure MCP Tunnel: 120 s), so stay well under it. Items not finished
        # in time are reported per item as lookup_failed (timed out), never dropped or shown as not covered.
        self.batch_budget = g("MEDCARE_REMOTE_BATCH_BUDGET_SECONDS", 90)
        origins = env.get("MEDCARE_ALLOWED_ORIGINS", "https://chatgpt.com,https://chat.openai.com")
        self.allowed_origins = {o.strip().rstrip("/") for o in origins.split(",") if o.strip()}


class RateLimiter:
    """Token bucket per user (in memory)."""

    def __init__(self, per_minute: int, burst: int):
        self.rate, self.burst = per_minute / 60.0, float(burst)
        self._b: dict[str, tuple[float, float]] = {}
        self._lock = threading.Lock()

    def take(self, key: str) -> float:
        """Return 0 if allowed, else seconds to wait."""
        now = time.monotonic()
        with self._lock:
            tokens, last = self._b.get(key, (self.burst, now))
            tokens = min(self.burst, tokens + (now - last) * self.rate)
            if tokens >= 1:
                self._b[key] = (tokens - 1, now)
                if len(self._b) > 10_000:
                    self._b.clear()
                return 0.0
            self._b[key] = (tokens, now)
            return (1 - tokens) / self.rate if self.rate else 60.0


# Only these fixed names ever reach the log; any other caller-supplied method text is logged as "unknown".
_LOGGABLE_METHODS = frozenset({
    "initialize", "ping", "tools/list", "tools/call", "resources/list", "resources/templates/list", "prompts/list",
    "notifications/initialized", "notifications/cancelled", "notifications/progress",
    "notifications/roots/list_changed"})


def _log_method(m) -> str:
    return m if isinstance(m, str) and m in _LOGGABLE_METHODS else "unknown"


def _rpc_error(mid, code, message):
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def create_app(cfg: AuthConfig, verifier: Verifier | None = None, settings: Settings | None = None,
               mcp=None) -> Starlette:
    settings = settings or Settings()
    verifier = verifier or Verifier(cfg)
    mcp = mcp or plugin_server.mcp
    security = [{"type": "oauth2", "scopes": list(cfg.required_scopes)}]
    limiter = RateLimiter(settings.rate_per_min, settings.rate_burst)
    slots = asyncio.Semaphore(settings.max_concurrent)
    base = cfg.resource.split("/", 3)
    origin = "/".join(base[:3])
    metadata_url = origin + "/.well-known/oauth-protected-resource"

    def challenge(status: int, error: str | None = None, desc: str | None = None) -> dict:
        parts = [f'resource_metadata="{metadata_url}"', f'scope="{" ".join(cfg.required_scopes)}"']
        if error:
            parts.append(f'error="{error}"')
        if desc:
            parts.append('error_description="' + desc.replace('"', "'") + '"')
        return {"WWW-Authenticate": "Bearer " + ", ".join(parts)}

    async def protected_resource(_request: Request):
        return JSONResponse({
            "resource": cfg.resource,
            "authorization_servers": [cfg.issuer],
            "scopes_supported": cfg.scopes_supported,
            "bearer_methods_supported": ["header"],
            "resource_name": "MedCare Resources (team)",
            "resource_documentation": "https://github.com/myacaexpress/MCP-Codex-Plugin/blob/main/CHATGPT_SETUP.md",
        }, headers={"Cache-Control": "public, max-age=300"})

    async def healthz(_request: Request):
        return JSONResponse({"status": "ok", "service": "medcare-resources", "version": mcp.version,
                             "tools": len(mcp.tools)}, headers={"Cache-Control": "no-store"})

    async def mcp_get(_request: Request):
        # No server-initiated SSE stream is offered; clients POST and receive JSON (allowed by the spec).
        return Response(status_code=405, headers={"Allow": "POST"})

    async def mcp_post(request: Request):
        started = time.monotonic()
        who = "-"
        status = 500
        method = "-"
        try:
            resp, status, who, method = await _mcp_post(request)
            return resp
        finally:
            log.info("mcp method=%s status=%s ms=%d user=%s", method, status,
                     (time.monotonic() - started) * 1000, who)

    async def _mcp_post(request: Request):
        org = request.headers.get("origin")
        if org and "*" not in settings.allowed_origins and org.rstrip("/") not in settings.allowed_origins:
            return JSONResponse(_rpc_error(None, -32600, "Origin not allowed"), 403), 403, "-", "-"
        auth = request.headers.get("authorization", "")
        if not auth.lower().startswith("bearer "):
            return (JSONResponse(_rpc_error(None, -32001, "Authentication required"), 401,
                                 headers=challenge(401)), 401, "-", "-")
        try:
            user = await asyncio.to_thread(verifier.verify, auth[7:].strip())
        except AuthError as err:
            hdr = challenge(err.status, err.error, err.description) if err.error != "access_denied" else {}
            return JSONResponse(_rpc_error(None, -32001, err.description), err.status, headers=hdr), err.status, "-", "-"
        who = hashlib.sha256(user["sub"].encode()).hexdigest()[:12]
        pv = request.headers.get("mcp-protocol-version")
        if pv and pv not in SUPPORTED_VERSIONS:
            return JSONResponse(_rpc_error(None, -32600, f"Unsupported MCP-Protocol-Version {pv}"), 400), 400, who, "-"
        ctype = request.headers.get("content-type", "").split(";")[0].strip().lower()
        if ctype != "application/json":
            return JSONResponse(_rpc_error(None, -32600, "Content-Type must be application/json"), 415), 415, who, "-"
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > settings.max_body:
            return JSONResponse(_rpc_error(None, -32600, "Request too large"), 413), 413, who, "-"
        body = bytearray()
        async for chunk in request.stream():
            body += chunk
            if len(body) > settings.max_body:
                return JSONResponse(_rpc_error(None, -32600, "Request too large"), 413), 413, who, "-"
        try:
            msg = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            return JSONResponse(_rpc_error(None, -32700, "Parse error"), 400), 400, who, "-"
        msgs = msg if isinstance(msg, list) else [msg]
        methods = [m.get("method") for m in msgs if isinstance(m, dict)]
        method = ",".join(_log_method(x) for x in methods[:5]) or "-"
        wait = limiter.take(user["sub"])
        if wait:
            return (JSONResponse(_rpc_error(None, -32000, "Rate limit exceeded; retry shortly"), 429,
                                 headers={"Retry-After": str(int(wait) + 1)}), 429, who, method)
        ids = [m.get("id") for m in msgs if isinstance(m, dict) and "id" in m and m.get("method")]
        if msgs and all(isinstance(m, dict) and "method" in m and "id" not in m for m in msgs):
            await asyncio.to_thread(mcp.handle_sync, msg)
            return Response(status_code=202), 202, who, method
        heavy = "tools/call" in methods
        for m in msgs:
            p = m.get("params") if isinstance(m, dict) and m.get("method") == "tools/call" else None
            if isinstance(p, dict) and p.get("name") == "medication_list_check":
                if p.get("arguments") is None:
                    p["arguments"] = {}
                if isinstance(p["arguments"], dict):
                    p["arguments"].setdefault("time_budget_seconds", settings.batch_budget)
        if heavy:
            try:
                await asyncio.wait_for(slots.acquire(), settings.queue_timeout)
            except asyncio.TimeoutError:
                return (JSONResponse(_rpc_error(None, -32000, "Server busy; retry shortly"), 503,
                                     headers={"Retry-After": "10"}), 503, who, method)
        task = asyncio.ensure_future(asyncio.to_thread(mcp.handle_sync, msg))
        if heavy:
            task.add_done_callback(lambda _t: slots.release())  # slot is held until the work really ends
        try:
            replies = await asyncio.wait_for(asyncio.shield(task), settings.call_timeout)
        except asyncio.TimeoutError:
            replies = [_rpc_error(i, -32603, "Timed out; for long medication lists use start_at to continue")
                       for i in ids]
        if not replies:
            return Response(status_code=202), 202, who, method
        for rep in replies:  # declare per-tool auth for ChatGPT (transport-level metadata only)
            for t in ((rep.get("result") or {}).get("tools") or []) if isinstance(rep, dict) else []:
                t["securitySchemes"] = security
        payload = replies if isinstance(msg, list) else replies[0]
        return (JSONResponse(payload, headers={"Cache-Control": "no-store"}), 200, who, method)

    routes = [
        Route("/mcp", mcp_post, methods=["POST"]),
        Route("/mcp", mcp_get, methods=["GET", "DELETE"]),
        Route("/.well-known/oauth-protected-resource", protected_resource, methods=["GET"]),
        Route("/.well-known/oauth-protected-resource/mcp", protected_resource, methods=["GET"]),
        Route("/healthz", healthz, methods=["GET"]),
    ]
    return Starlette(routes=routes)


def app_from_env() -> Starlette:
    os.environ.setdefault("MEDCARE_LOG_TRACEBACKS", "0")  # never print argument-bearing tracebacks
    return create_app(AuthConfig.from_env())


def main():
    import uvicorn
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    uvicorn.run(app_from_env(), host=os.environ.get("HOST", "0.0.0.0"), port=int(os.environ.get("PORT", "8080")),
                proxy_headers=True, forwarded_allow_ips=os.environ.get("FORWARDED_ALLOW_IPS", "127.0.0.1"),
                access_log=False, timeout_keep_alive=15,
                limit_concurrency=int(os.environ.get("MEDCARE_MAX_CONNECTIONS", "64")),
                server_header=False)


if __name__ == "__main__":
    main()
