#!/usr/bin/env python3
"""End-to-end check of the remote server as a real process (opt-in; uses live public sources).

    python3 tests/http_e2e.py --server-python /path/to/venv/bin/python

What it does (nothing leaves this machine except the server's normal public lookups):
1. Starts a throwaway local HTTPS "identity provider" (self-signed cert, generated RSA key) that
   serves OpenID metadata, a JWKS and /userinfo. No real IdP, no real accounts.
2. Starts `python -m remote.medcare_http` (uvicorn) with that issuer and an allowlist.
3. Uses the official MCP Python SDK Streamable HTTP client with a locally minted access token to run
   initialize, tools/list and tools/call (including medication_list_check on a SYNTHETIC list).
4. Checks 401 (no token), 403 (not on allowlist), healthz and protected-resource metadata.
Writes tests/http-e2e-report.json.
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import ipaddress
import json
import os
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import jwt
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_live import SYNTHETIC_LIST  # noqa: E402

RESOURCE = "https://medcare-e2e.invalid/mcp"
ALLOWED = "synthetic.agent@example.com"


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def make_cert(tmp: Path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - dt.timedelta(minutes=5))
            .not_valid_after(now + dt.timedelta(hours=2))
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), False)
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), True)
            .sign(key, hashes.SHA256()))
    (tmp / "cert.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (tmp / "key.pem").write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                    serialization.NoEncryption()))
    return tmp / "cert.pem", tmp / "key.pem"


def start_idp(tmp: Path, signing_key):
    port = free_port()
    issuer = f"https://127.0.0.1:{port}/"
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(signing_key.public_key()))
    jwk.update({"kid": "e2e-1", "use": "sig", "alg": "RS256"})
    docs = {"/.well-known/openid-configuration": {
        "issuer": issuer, "jwks_uri": issuer + "jwks.json", "userinfo_endpoint": issuer + "userinfo",
        "authorization_endpoint": issuer + "authorize", "token_endpoint": issuer + "oauth/token",
        "code_challenge_methods_supported": ["S256"]}, "/jwks.json": {"keys": [jwk]}}

    class H(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path == "/userinfo":
                tok = self.headers.get("Authorization", "")[7:]
                claims = jwt.decode(tok, options={"verify_signature": False})
                body = {"sub": claims["sub"], "email": claims.get("_e2e_userinfo_email"), "email_verified": True}
            elif self.path in docs:
                body = docs[self.path]
            else:
                self.send_response(404)
                self.end_headers()
                return
            data = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass

    cert, key = make_cert(tmp)
    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert, key)
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return issuer, cert


def mint(key, issuer, **extra):
    now = int(time.time())
    claims = {"iss": issuer, "aud": RESOURCE, "sub": "e2e|synthetic-agent", "iat": now, "exp": now + 900,
              "scope": "openid email medcare:read"}
    claims.update(extra)
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "e2e-1"})


async def sdk_session(url, token, report):
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client
    async with streamablehttp_client(url, headers={"Authorization": f"Bearer {token}"}, timeout=60,
                                     sse_read_timeout=300) as (r, w, _sid):
        async with ClientSession(r, w) as s:
            init = await s.initialize()
            report["initialize"] = {"server": init.serverInfo.name, "version": init.serverInfo.version,
                                    "protocol": init.protocolVersion}
            tools = (await s.list_tools()).tools
            report["tools_list"] = {"count": len(tools), "has_medication_list_check":
                                    any(t.name == "medication_list_check" for t in tools)}
            calls = []

            async def call(name, args, check):
                t0 = time.monotonic()
                res = await s.call_tool(name, args)
                sc = res.structuredContent or {}
                ok, note = check(res, sc)
                calls.append({"tool": name, "ok": ok, "isError": res.isError, "note": note,
                              "seconds": round(time.monotonic() - t0, 1)})

            await call("source_status", {}, lambda r, sc: (not r.isError and "plugin_version" in sc,
                                                          sc.get("plugin_version")))
            await call("zip_to_county", {"zip_code": "77002"},
                       lambda r, sc: (not r.isError and "Harris" in json.dumps(sc), "Harris"))
            await call("medication_list_check", {"text": SYNTHETIC_LIST, "plan_ids": ["H5767-001", "H5767-002"],
                                                 "year": 2027},
                       lambda r, sc: (not r.isError and sum(sc["summary"]["by_status"].values()) == len(sc["results"])
                                      and all(p.get("year") == 2027 for it in sc["results"] for p in it.get("plans") or []),
                                      {"entries": len(sc.get("results", [])),
                                       "by_status": sc.get("summary", {}).get("by_status")}))
            await call("medication_list_check", {"text": "x", "year": 1990},
                       lambda r, sc: (r.isError is True, "out-of-range year rejected"))
            report["calls"] = calls


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--server-python", default=sys.executable)
    a = ap.parse_args()
    tmp = Path(tempfile.mkdtemp(prefix="medcare-e2e-"))
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    issuer, cert = start_idp(tmp, key)
    port = free_port()
    env = {**os.environ, "MEDCARE_OAUTH_ISSUER": issuer, "MEDCARE_RESOURCE_URL": RESOURCE,
           "MEDCARE_ALLOWED_EMAILS": ALLOWED, "PORT": str(port), "HOST": "127.0.0.1",
           "SSL_CERT_FILE": str(cert), "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost",
           "MEDCARE_CACHE_DIR": os.environ.get("MEDCARE_CACHE_DIR", str(ROOT / ".test-cache")),
           "PYTHONPATH": str(ROOT)}
    # the server's own outbound public lookups still use the normal proxy/CA settings; only the
    # throwaway IdP is trusted via SSL_CERT_FILE, so append the system bundle for public hosts
    bundle = tmp / "bundle.pem"
    system = os.environ.get("REQUESTS_CA_BUNDLE") or os.environ.get("SSL_CERT_FILE") or ssl.get_default_verify_paths().cafile
    bundle.write_bytes(cert.read_bytes() + (Path(system).read_bytes() if system and Path(system).exists() else b""))
    env["SSL_CERT_FILE"] = str(bundle)
    log_path = tmp / "server.log"
    proc = subprocess.Popen([a.server_python, "-m", "remote.medcare_http"], cwd=str(ROOT), env=env,
                            stdout=open(log_path, "w"), stderr=subprocess.STDOUT)
    base = f"http://127.0.0.1:{port}"
    report = {"generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
              "server_python": subprocess.run([a.server_python, "--version"], capture_output=True, text=True).stdout.strip()}
    try:
        for _ in range(100):
            try:
                report["healthz"] = json.loads(urllib.request.urlopen(base + "/healthz", timeout=2).read())
                break
            except OSError:
                time.sleep(0.2)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        meta = json.loads(opener.open(base + "/.well-known/oauth-protected-resource").read())
        report["protected_resource_metadata"] = meta

        def post(token):
            req = urllib.request.Request(base + "/mcp", method="POST", data=json.dumps(
                {"jsonrpc": "2.0", "id": 1, "method": "ping"}).encode(), headers={"Content-Type": "application/json",
                                                                                  **({"Authorization": f"Bearer {token}"} if token else {})})
            try:
                r = opener.open(req)
                return r.status, dict(r.headers)
            except urllib.error.HTTPError as e:
                return e.code, dict(e.headers)
        s, h = post(None)
        report["no_token"] = {"status": s, "www_authenticate": h.get("www-authenticate") or h.get("WWW-Authenticate")}
        s, _ = post(mint(key, issuer, email="someone.else@example.com", email_verified=True))
        report["not_allowlisted"] = {"status": s}
        s, _ = post(mint(key, issuer, _e2e_userinfo_email=ALLOWED))
        report["allowlisted_via_userinfo"] = {"status": s}
        os.environ["NO_PROXY"] = "127.0.0.1,localhost"
        os.environ["no_proxy"] = "127.0.0.1,localhost"
        asyncio.run(sdk_session(base + "/mcp", mint(key, issuer, email=ALLOWED, email_verified=True), report))
        try:
            status = Path(f"/proc/{proc.pid}/status").read_text()
            report["server_peak_rss_mb"] = round(int(next(l.split()[1] for l in status.splitlines()
                                                         if l.startswith("VmHWM"))) / 1024)
        except (OSError, StopIteration):
            pass
    finally:
        proc.terminate()
        proc.wait(10)
    logs = log_path.read_text()
    report["server_log_lines"] = len(logs.splitlines())
    report["server_log_contains_list_text"] = any(w in logs for w in ("Eliquis", "Lisinopril", "metformin", ALLOWED))
    report["passed"] = (report.get("healthz", {}).get("status") == "ok" and report["no_token"]["status"] == 401
                        and report["not_allowlisted"]["status"] == 403
                        and report["allowlisted_via_userinfo"]["status"] == 200
                        and report.get("tools_list", {}).get("has_medication_list_check")
                        and all(c["ok"] for c in report.get("calls", []))
                        and not report["server_log_contains_list_text"])
    out = ROOT / "tests" / "http-e2e-report.json"
    out.write_text(json.dumps(report, indent=1))
    print(json.dumps(report, indent=1))
    print("SERVER LOG (tail):\n" + "\n".join(logs.splitlines()[-15:]))
    sys.exit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
