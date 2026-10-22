"""Remote (ChatGPT) transport: MCP Streamable HTTP initialize/list/call + OAuth resource-server tests.

Uses a locally generated RSA key and an in-process JWKS stand-in; no network, no real identity provider.
"""
import json
import logging
import time

import pytest

pytest.importorskip("starlette")
pytest.importorskip("jwt")
pytest.importorskip("httpx")

import jwt  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

from conftest import ROOT  # noqa: E402
import sys  # noqa: E402

sys.path.insert(0, str(ROOT))
from remote.medcare_http import Settings, create_app  # noqa: E402
from remote.oauth import AuthConfig, AuthError, Verifier  # noqa: E402
from medcare.mcp_stdio import Server, schema  # noqa: E402
import server as plugin_server  # noqa: E402
from test_batch import MIXED, synthetic  # noqa: E402,F401  (fixture reuse)

ISS = "https://team-idp.example.com/"
RES = "https://medcare.example.com/mcp"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


class FakeJWKS:
    class _K:
        def __init__(self, key):
            self.key = key

    def get_signing_key_from_jwt(self, token):
        if jwt.get_unverified_header(token).get("kid") != "k1":
            raise jwt.PyJWKClientError("unknown kid")
        return self._K(KEY.public_key())


def cfg(**kw):
    base = dict(issuer=ISS, resource=RES, audiences=[RES], required_scopes=["medcare:read"],
                allowed_emails={"agent1@team.example.com"}, allowed_subjects={"auth0|allowed-sub"},
                email_claims=["email", "https://medcare-resources/email"], scopes_supported=["openid", "email",
                                                                                            "medcare:read"])
    base.update(kw)
    return AuthConfig(**base)


def token(key=KEY, kid="k1", alg="RS256", **claims):
    now = int(time.time())
    body = {"iss": ISS, "aud": RES, "sub": "auth0|someone", "iat": now, "exp": now + 600,
            "scope": "openid email medcare:read", "email": "agent1@team.example.com", "email_verified": True}
    body.update(claims)
    body = {k: v for k, v in body.items() if v is not None}
    return jwt.encode(body, key, algorithm=alg, headers={"kid": kid})


def client(settings=None, mcp=None, userinfo=None, config=None):
    c = config or cfg()
    v = Verifier(c, jwk_client=FakeJWKS(), userinfo_fetch=userinfo or (lambda url, tok: {}))
    v._meta = {"issuer": ISS, "userinfo_endpoint": ISS + "userinfo"}
    s = settings or Settings({})
    return TestClient(create_app(c, v, s, mcp=mcp))


def rpc(c, body, tok=None, headers=None):
    h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if tok is not False:
        h["Authorization"] = f"Bearer {tok or token()}"
    h.update(headers or {})
    return c.post("/mcp", content=json.dumps(body), headers=h)


INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}


# ---------------------------------------------------------------- public endpoints
def test_health_and_metadata_are_public_and_data_free():
    c = client()
    h = c.get("/healthz")
    assert h.status_code == 200 and h.json()["status"] == "ok" and h.json()["tools"] == len(plugin_server.mcp.tools)
    for path in ("/.well-known/oauth-protected-resource", "/.well-known/oauth-protected-resource/mcp"):
        m = c.get(path).json()
        assert m["resource"] == RES and m["authorization_servers"] == [ISS]
        assert "medcare:read" in m["scopes_supported"] and m["bearer_methods_supported"] == ["header"]
    assert c.get("/mcp").status_code == 405


# ---------------------------------------------------------------- auth failures
def test_missing_token_gets_401_with_resource_metadata_challenge():
    r = rpc(client(), INIT, tok=False)
    assert r.status_code == 401
    www = r.headers["www-authenticate"]
    assert www.startswith("Bearer ")
    assert 'resource_metadata="https://medcare.example.com/.well-known/oauth-protected-resource"' in www
    assert 'scope="medcare:read"' in www
    # a token in the query string is never accepted
    r2 = client().post("/mcp?access_token=" + token(), content=json.dumps(INIT),
                       headers={"Content-Type": "application/json"})
    assert r2.status_code == 401


@pytest.mark.parametrize("bad,expect", [
    (lambda: token(key=OTHER_KEY), "could not be verified"),
    (lambda: token(aud="https://other.example.com/mcp"), "audience"),
    (lambda: token(iss="https://evil.example.com/"), "issuer"),
    (lambda: token(exp=int(time.time()) - 3600, iat=int(time.time()) - 7200), "expired"),
    (lambda: token(kid="unknown"), "could not be verified"),
    (lambda: jwt.encode({"iss": ISS, "aud": RES, "sub": "x", "exp": int(time.time()) + 600}, "secret-for-test-only-0123456789abcdef",
                        algorithm="HS256", headers={"kid": "k1"}), "algorithm"),
    (lambda: "e30.e30.", "algorithm"),
    (lambda: "not-a-jwt", "malformed"),
    (lambda: token(sub=None), "could not be verified"),
])
def test_invalid_tokens_rejected(bad, expect):
    r = rpc(client(), INIT, tok=bad())
    assert r.status_code == 401, r.text
    assert 'error="invalid_token"' in r.headers["www-authenticate"]
    assert expect in r.json()["error"]["message"]


def test_scope_and_allowlist_enforced():
    r = rpc(client(), INIT, tok=token(scope="openid email"))
    assert r.status_code == 403 and 'error="insufficient_scope"' in r.headers["www-authenticate"]
    r = rpc(client(), INIT, tok=token(email="stranger@team.example.com"))
    assert r.status_code == 403 and "allowlist" in r.json()["error"]["message"]
    r = rpc(client(), INIT, tok=token(email_verified=False))
    assert r.status_code == 403
    # allowed by subject even without an email claim; Auth0-style permissions array also counts as scope
    r = rpc(client(), INIT, tok=token(sub="auth0|allowed-sub", email=None, scope=None, permissions=["medcare:read"]))
    assert r.status_code == 200


def test_userinfo_fallback_for_tokens_without_email():
    seen = []

    def ui(url, tok):
        seen.append(url)
        return {"sub": "auth0|someone", "email": "Agent1@Team.example.com", "email_verified": True}
    c = client(userinfo=ui)
    t = token(email=None)
    assert rpc(c, INIT, tok=t).status_code == 200
    assert rpc(c, INIT, tok=t).status_code == 200
    assert seen == [ISS + "userinfo"]  # cached per token
    assert rpc(client(userinfo=lambda u, t: {"sub": "auth0|someone", "email": "agent1@team.example.com",
                                            "email_verified": False}), INIT, tok=token(email=None)).status_code == 403
    assert rpc(client(userinfo=lambda u, t: {"sub": "auth0|different", "email": "agent1@team.example.com",
                                            "email_verified": True}), INIT, tok=token(email=None)).status_code == 403


def test_config_requires_https_and_allowlist():
    env = {"MEDCARE_OAUTH_ISSUER": ISS, "MEDCARE_RESOURCE_URL": RES}
    with pytest.raises(SystemExit):
        AuthConfig.from_env(env)
    with pytest.raises(SystemExit):
        AuthConfig.from_env({**env, "MEDCARE_RESOURCE_URL": "http://x/mcp", "MEDCARE_ALLOWED_EMAILS": "a@b.c"})
    c = AuthConfig.from_env({**env, "MEDCARE_ALLOWED_EMAILS": "A@B.example, c@d.example"})
    assert c.allowed_emails == {"a@b.example", "c@d.example"} and c.audiences == [RES]


# ---------------------------------------------------------------- protocol
def test_initialize_list_and_tools_match_stdio():
    c = client()
    r = rpc(c, INIT)
    assert r.status_code == 200 and r.json()["result"]["serverInfo"]["name"] == "medcare-resources"
    assert r.json()["result"]["protocolVersion"] == "2025-06-18"
    assert rpc(c, {"jsonrpc": "2.0", "method": "notifications/initialized"}).status_code == 202
    tools = rpc(c, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                headers={"MCP-Protocol-Version": "2025-06-18"}).json()["result"]["tools"]
    stdio = {t.name: t for t in plugin_server.mcp.tools.values()}
    assert {t["name"] for t in tools} == set(stdio) and "medication_list_check" in stdio
    for t in tools:
        assert t["inputSchema"] == stdio[t["name"]].input_schema
        assert t["securitySchemes"] == [{"type": "oauth2", "scopes": ["medcare:read"]}]


def test_protocol_errors():
    c = client()
    assert rpc(c, INIT, headers={"MCP-Protocol-Version": "1999-01-01"}).status_code == 400
    assert rpc(c, INIT, headers={"Content-Type": "text/plain"}).status_code == 415
    assert rpc(c, INIT, headers={"Origin": "https://evil.example.com"}).status_code == 403
    assert rpc(c, INIT, headers={"Origin": "https://chatgpt.com"}).status_code == 200
    r = c.post("/mcp", content=b"{not json", headers={"Content-Type": "application/json",
                                                       "Authorization": f"Bearer {token()}"})
    assert r.status_code == 400 and r.json()["error"]["code"] == -32700
    small = client(settings=Settings({"MEDCARE_MAX_BODY_BYTES": "1000"}))
    big = {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
           "params": {"name": "medication_list_check", "arguments": {"text": "x" * 5000}}}
    assert rpc(small, big).status_code == 413
    r = rpc(c, {"jsonrpc": "2.0", "id": 4, "method": "no/such"})
    assert r.json()["error"]["code"] == -32601
    r = rpc(c, {"jsonrpc": "2.0", "id": 5})
    assert r.json()["error"]["code"] == -32600
    r = rpc(c, [{"jsonrpc": "2.0", "id": 6, "method": "ping"}, {"jsonrpc": "2.0", "id": 7, "method": "ping"}])
    assert [x["id"] for x in r.json()] == [6, 7]
    r = rpc(c, {"jsonrpc": "2.0", "id": 8, "method": "tools/call", "params": {"name": "medication_list_check",
                                                                            "arguments": {"year": "abc"}}})
    assert r.json()["result"]["isError"] is True


def test_batch_call_over_http_matches_stdio(synthetic, caplog):
    caplog.set_level(logging.DEBUG)
    args = {"text": MIXED, "plan_ids": ["H1111-001", "H1111-002"], "year": 2027, "include_medcare_site": False}
    call = {"jsonrpc": "2.0", "id": 9, "method": "tools/call",
            "params": {"name": "medication_list_check", "arguments": args}}
    r = rpc(client(), call)
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    http_res = r.json()["result"]
    stdio_res = plugin_server.mcp.handle_sync(call)[0]["result"]
    strip = lambda s: {k: v for k, v in s.items() if k not in ("generated_at", "elapsed_seconds")}  # noqa: E731
    assert strip(http_res["structuredContent"]) == strip(stdio_res["structuredContent"])
    sc = http_res["structuredContent"]
    assert sum(sc["summary"]["by_status"].values()) == 24 and len(sc["results"]) == 24
    assert all(p["year"] == 2027 for it in sc["results"] for p in (it.get("plans") or []))
    assert http_res["content"][0]["text"].count("\n|") >= 24
    logs = "\n".join(rec.getMessage() for rec in caplog.records)
    assert "Lisinopril" not in logs and "Eliquis" not in logs and "agent1@" not in logs
    assert "method=tools/call status=200" in logs


# ---------------------------------------------------------------- limits
def _slow_server(delay):
    s = Server("slow", "0")

    @s.tool("slow", "sleep", schema({}))
    def slow():
        time.sleep(delay)
        return {"ok": True}
    return s


def test_rate_limit_per_user():
    c = client(settings=Settings({"MEDCARE_RATE_LIMIT_PER_MINUTE": "1", "MEDCARE_RATE_LIMIT_BURST": "2"}))
    ping = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    assert rpc(c, ping).status_code == 200 and rpc(c, ping).status_code == 200
    r = rpc(c, ping)
    assert r.status_code == 429 and int(r.headers["retry-after"]) >= 1
    other = token(sub="auth0|allowed-sub", email=None)
    assert rpc(c, ping, tok=other).status_code == 200  # buckets are per user


def test_concurrency_bound_and_timeout():
    import threading
    s = Settings({"MEDCARE_MAX_CONCURRENT_CALLS": "1", "MEDCARE_QUEUE_TIMEOUT_SECONDS": "0.2",
                  "MEDCARE_CALL_TIMEOUT_SECONDS": "5"})
    c = client(settings=s, mcp=_slow_server(1.0))
    call = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "slow", "arguments": {}}}
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("a", rpc(c, call)))
    t.start()
    time.sleep(0.3)
    busy = rpc(c, call)
    t.join()
    assert out["a"].status_code == 200 and out["a"].json()["result"]["structuredContent"] == {"ok": True}
    assert busy.status_code == 503 and busy.headers["retry-after"]
    assert rpc(c, {"jsonrpc": "2.0", "id": 2, "method": "ping"}).status_code == 200  # ping is not slot-bound

    c2 = client(settings=Settings({"MEDCARE_CALL_TIMEOUT_SECONDS": "0.2"}), mcp=_slow_server(0.6))
    r = rpc(c2, call)
    assert r.status_code == 200 and r.json()["error"]["code"] == -32603 and r.json()["id"] == 1


def test_auth_error_is_exception_type():
    assert issubclass(AuthError, Exception)


def test_remote_default_batch_budget(monkeypatch):
    seen = []
    monkeypatch.setattr(plugin_server.batch, "check_list", lambda *a: seen.append(a[-1]) or {"summary": {}})
    monkeypatch.setattr(plugin_server.batch, "render_text", lambda r: "ok")
    c = client(settings=Settings({"MEDCARE_REMOTE_BATCH_BUDGET_SECONDS": "60"}))
    call = lambda args: {"jsonrpc": "2.0", "id": 1, "method": "tools/call",  # noqa: E731
                         "params": {"name": "medication_list_check", "arguments": args}}
    assert rpc(c, call({"text": "aspirin 81 mg"})).status_code == 200
    assert rpc(c, call({"text": "aspirin 81 mg", "time_budget_seconds": 150})).status_code == 200
    assert seen == [60, 150]


# ---------------------------------------------------------------- review of 1.2.0: findings 9-12
@pytest.mark.parametrize("verified", [None, "false", "False", 0, 1, "true", "True", "yes", [], {}])
def test_f9_email_verified_must_be_literal_true(verified):
    r = rpc(client(), INIT, tok=token(email_verified=verified))
    assert r.status_code == 403, (verified, r.status_code)
    assert rpc(client(), INIT, tok=token(email_verified=True)).status_code == 200


def test_f9_custom_email_claim_needs_its_own_literal_true():
    ns = "https://medcare-resources/email"
    ok = token(email=None, email_verified=None, **{ns: "agent1@team.example.com", ns + "_verified": True})
    assert rpc(client(), INIT, tok=ok).status_code == 200
    for bad in ("true", 1, None):
        t = token(email=None, email_verified=None, **{ns: "agent1@team.example.com", ns + "_verified": bad})
        assert rpc(client(), INIT, tok=t).status_code == 403, bad


def test_f10_userinfo_never_sent_to_untrusted_or_plain_http_hosts():
    called = []
    spy = lambda url, tok: called.append(url) or {"sub": "auth0|someone", "email": "agent1@team.example.com",  # noqa: E731
                                                 "email_verified": True}
    for endpoint in ("https://evil.example.net/userinfo", "http://team-idp.example.com/userinfo",
                     "https://user:pw@team-idp.example.com/userinfo"):
        c = cfg()
        v = Verifier(c, jwk_client=FakeJWKS(), userinfo_fetch=spy)
        v._meta = {"issuer": ISS, "userinfo_endpoint": endpoint}
        app = TestClient(create_app(c, v, Settings({})))
        assert rpc(app, INIT, tok=token(email=None)).status_code == 403, endpoint
    assert called == [] or all(u.startswith("https://team-idp.example.com/") for u in called)
    assert not any("evil" in u or u.startswith("http://") for u in called)


def test_f10_redirects_are_refused_and_token_never_forwarded(tmp_path, monkeypatch):
    """Real HTTPS userinfo endpoint answering 302 -> another host over plain HTTP: the token must not follow."""
    import datetime as dt
    import ipaddress
    import ssl
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.x509.oid import NameOID
    from remote import oauth

    stolen = []

    class Sink(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            stolen.append(self.headers.get("Authorization"))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"sub":"auth0|someone","email":"agent1@team.example.com","email_verified":true}')

        def log_message(self, *a):
            pass
    sink = ThreadingHTTPServer(("127.0.0.1", 0), Sink)
    threading.Thread(target=sink.serve_forever, daemon=True).start()

    hits = []

    class Redirector(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            hits.append(self.headers.get("Authorization"))
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{sink.server_address[1]}/steal")
            self.end_headers()

        def log_message(self, *a):
            pass
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - dt.timedelta(minutes=5))
            .not_valid_after(now + dt.timedelta(hours=1))
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), False)
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), True).sign(key, hashes.SHA256()))
    (tmp_path / "c.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (tmp_path / "k.pem").write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                       serialization.NoEncryption()))
    red = ThreadingHTTPServer(("127.0.0.1", 0), Redirector)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(tmp_path / "c.pem", tmp_path / "k.pem")
    red.socket = ctx.wrap_socket(red.socket, server_side=True)
    threading.Thread(target=red.serve_forever, daemon=True).start()
    def trusting_context(*a, **k):
        tctx = ssl.create_default_context(cafile=str(tmp_path / "c.pem"))
        tctx.verify_flags &= ~getattr(ssl, "VERIFY_X509_STRICT", 0)   # throwaway self-signed test certificate
        return tctx
    monkeypatch.setattr(ssl, "_create_default_https_context", trusting_context)
    for var in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("NO_PROXY", "*")
    url = f"https://127.0.0.1:{red.server_address[1]}/userinfo"
    import urllib.error
    with pytest.raises(urllib.error.HTTPError) as exc:   # TLS succeeded; the 302 itself is refused
        oauth._https_json(url, {"Authorization": "Bearer synthetic-token"}, trusted_hosts={"127.0.0.1"})
    assert exc.value.code == 302
    c = cfg(userinfo_url=url)
    v = Verifier(c, jwk_client=FakeJWKS())          # real fetch path (no spy)
    v._meta = {"issuer": ISS}
    assert rpc(TestClient(create_app(c, v, Settings({}))), INIT, tok=token(email=None)).status_code == 403
    assert stolen == [] and len(hits) == 2   # both requests reached the HTTPS endpoint; none followed the 302
    red.shutdown()
    sink.shutdown()


def test_f11_userinfo_must_return_the_same_subject():
    for info in ({"email": "agent1@team.example.com", "email_verified": True},
                 {"sub": None, "email": "agent1@team.example.com", "email_verified": True},
                 {"sub": "", "email": "agent1@team.example.com", "email_verified": True},
                 {"sub": ["auth0|someone"], "email": "agent1@team.example.com", "email_verified": True},
                 {"sub": "auth0|someone2", "email": "agent1@team.example.com", "email_verified": True}):
        assert rpc(client(userinfo=lambda u, t, i=info: i), INIT, tok=token(email=None)).status_code == 403, info
    good = {"sub": "auth0|someone", "email": "agent1@team.example.com", "email_verified": True}
    assert rpc(client(userinfo=lambda u, t: good), INIT, tok=token(email=None)).status_code == 200


def test_f12_logs_never_contain_caller_method_text(caplog):
    caplog.set_level(logging.INFO)
    c = client()
    evil = "x\nFAKE status=200 user=admin Synthetic Example"
    rpc(c, {"jsonrpc": "2.0", "id": 1, "method": evil})
    rpc(c, [{"jsonrpc": "2.0", "id": 2, "method": "tools/call\r\nINJECTED", "params": {}},
            {"jsonrpc": "2.0", "id": 3, "method": 12345}])
    rpc(c, {"jsonrpc": "2.0", "id": 4, "method": "ping"})
    logs = "\n".join(r.getMessage() for r in caplog.records if r.name == "medcare.http")
    assert "FAKE" not in logs and "INJECTED" not in logs and "Synthetic" not in logs and "12345" not in logs
    assert "method=unknown status=200" in logs and "method=unknown,unknown" in logs and "method=ping" in logs
