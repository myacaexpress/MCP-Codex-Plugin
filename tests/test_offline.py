"""Offline unit tests (no network)."""
import json
import subprocess
import sys

import pytest

from conftest import PLUGIN, ROOT
from medcare import catalog, cms, net, research, sitecrawl
from medcare.htmlparse import extract
from medcare.mcp_stdio import Server, ToolError, schema
from medcare.textsearch import Index, line_coverage, matching_lines, snippet, tokens


def test_plan_id_parsing():
    assert cms.parse_plan_id("H5767-001") == ("H5767", "001", None)
    assert cms.parse_plan_id("h5767_001_0") == ("H5767", "001", "000")
    assert cms.parse_plan_id("R6801-009-000") == ("R6801", "009", "000")
    for bad in ("H576-001", "5767-001", "H5767", "H5767-01", "DROP TABLE"):
        with pytest.raises(ValueError):
            cms.parse_plan_id(bad)


def test_url_validation_blocks_unsafe():
    for bad in ("http://mcpartnerstx.com/", "https://user:pw@mcpartnerstx.com/", "https://mcpartnerstx.com:8443/",
                "file:///etc/passwd", "https://evil.example.com/x.pdf"):
        with pytest.raises(net.FetchError):
            net.validate_url(bad, net.ALL_PUBLIC)
    assert net.validate_url("https://mcpartnerstx.com/forms/", net.MEDCARE_HOSTS)
    with pytest.raises(net.FetchError):
        net.validate_url("https://127.0.0.1/x", None)


def test_tokens_and_search():
    assert tokens("Évidence of Coverage!") == ["evidence", "coverage"]
    idx = Index([{"text": "Specialist visit $15 copay"}, {"text": "Emergency care $150 copay"}])
    top = idx.search("specialist copay", limit=1)[0][0]
    assert "Specialist" in top["text"]
    assert "Emergency" in snippet("x " * 400 + "Emergency care $150", "emergency", 80)
    assert matching_lines("a\nSpecialist $15\nb", "specialist") == ["a", "Specialist $15", "b"]
    assert line_coverage("Primary care\n$0 copay", "primary care copay") == 1.0


def test_html_extract_links_and_placeholders():
    html = """<html><head><title>T</title></head><body><header><a href="#">Members</a></header>
    <h2>MedCare Classic</h2><h2>Summary of Benefits</h2><a href="/a.pdf">English</a><a href="#">Español</a>
    <script>var x='<a href=/nope>';</script><p>Text line</p></body></html>"""
    d = extract(html)
    assert d["title"] == "T"
    links = {(l["text"], l["href"]) for l in d["links"]}
    assert ("English", "/a.pdf") in links and ("Español", "#") in links
    eng = next(l for l in d["links"] if l["text"] == "English")
    assert eng["section"] == "Summary of Benefits" and eng["plan_context"] == "MedCare Classic"
    assert all("/nope" not in l["href"] for l in d["links"])
    assert "Text line" in d["text_lines"]


def test_identity_and_plan_ids():
    assert sitecrawl.plan_ids_from("H5767_SB0032027SP_M MedCare Choice") == ["H5767-003"]
    assert sitecrawl.plan_ids_from("H5767_2027HRA_C") == []
    ident = sitecrawl.identify("Lorem ipsum dolor sit amet", "https://x/file-sample_150kB.pdf")
    assert ident["is_sample_or_placeholder"]


def test_hidden_pharmacy_text_removed():
    text = ("MCP 2027 Pharmacy Directory - Harris County\nPage 2\nAHF PHARMACY\n(713) 520-2328\n"
            "Primary Care Providers (PCPs)\nMCP 2027 Provider Directory - Harris County\nPage 41\nDr Example")
    cleaned, did = sitecrawl.clean_page_text(text, "https://x/MCP_2027_Provider_Directory.pdf")
    assert did and "AHF PHARMACY" not in cleaned and "Dr Example" in cleaned and "Primary Care Providers" in cleaned


def test_identity_verification_synthetic(monkeypatch):
    monkeypatch.setattr(cms, "plan_landscape", lambda y, p: {"segments_in_landscape": ["H1234-001-000"],
                                                              "found": True, "plans": [{}]})
    pages = ["2027 Summary of Benefits\nExample Plan (HMO) H1234-001\nJanuary 1, 2027 - December 31, 2027"]
    ok = research._verify_identity(pages, "H1234-001", 2027)
    assert ok["identity_verified"] and ok["segment_check"] == "segment_inferred_single_segment_in_cms"
    assert not research._verify_identity(pages, "H1234-002", 2027)["identity_verified"]
    assert not research._verify_identity(pages, "H1234-001", 2026)["identity_verified"]
    monkeypatch.setattr(cms, "plan_landscape", lambda y, p: {"segments_in_landscape": ["H1234-001-000", "H1234-001-001"],
                                                              "found": True, "plans": [{}]})
    amb = research._verify_identity(pages, "H1234-001-001", 2027)
    assert not amb["identity_verified"] and amb["segment_check"] == "segment_ambiguous_multiple_segments_in_cms"


def test_bundled_catalog_integrity():
    s = catalog._load()
    c = s["catalog"]
    assert c["site"] == "https://mcpartnerstx.com/"
    statuses = {r["status"] for r in c["resources"]}
    assert "unavailable_placeholder" in statuses and "available" in statuses
    # placeholders never carry a URL and are never indexed as evidence
    assert all(r["url"] is None for r in c["resources"] if r["status"] == "unavailable_placeholder")
    indexed = {d["resource_id"] for d in s["docs"].values()}
    for r in c["resources"]:
        if r["id"] in indexed:
            assert r["status"] == "available" and not r.get("unlinked_media")
    mismatch = [r for r in c["resources"] if r["status"] == "available_identity_mismatch"]
    assert all(r["issues"] for r in mismatch)


def test_document_search_has_page_citations():
    r = catalog.search_documents("Eligibility requirements Part A Part B", category="evidence_of_coverage",
                                 plan_id="H5767-002", limit=2)
    assert r["results"], r
    top = r["results"][0]
    assert top["citation_url"].endswith(f"#page={top['page']}") and top["plan_ids"] == ["H5767-002"]


def test_mcp_validation():
    srv = Server("t", "0")
    sch = schema({"q": {"type": "string"}, "n": {"type": "integer", "minimum": 1, "maximum": 5},
                  "flag": {"type": "boolean"}, "ids": {"type": "array", "items": {"type": "string"}}}, ["q"])
    assert srv._validate(sch, {"q": "x", "n": "3", "flag": "true"}) == {"q": "x", "n": 3, "flag": True}
    for bad in ({}, {"q": "x", "n": 9}, {"q": "x", "zzz": 1}, {"q": "x", "ids": [1]}, {"q": 5}):
        with pytest.raises(ToolError):
            srv._validate(sch, bad)


def _rpc(lines, timeout=60):
    proc = subprocess.run([str(PLUGIN / "bin" / "medcare-mcp")], input="\n".join(json.dumps(m) for m in lines) + "\n",
                          capture_output=True, text=True, timeout=timeout, cwd=str(PLUGIN))
    return {m.get("id"): m for m in map(json.loads, filter(None, proc.stdout.splitlines()))}, proc


def test_stdio_protocol_initialize_list_call_errors():
    out, proc = _rpc([
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05",
                                                                       "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "medcare_search_resources",
                                                                      "arguments": {"query": "appeal", "limit": 3}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "medcare_get_resource", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 5, "method": "nope"},
        {"jsonrpc": "2.0", "id": 6, "method": "ping"},
    ])
    assert out[1]["result"]["protocolVersion"] == "2024-11-05"
    names = {t["name"] for t in out[2]["result"]["tools"]}
    assert len(names) >= 25 and {"medcare_drug_lookup", "medcare_provider_search", "plan_document_retrieve"} <= names
    for t in out[2]["result"]["tools"]:
        assert t["inputSchema"]["type"] == "object" and t["description"]
    assert out[3]["result"]["isError"] is False and out[3]["result"]["structuredContent"]["resources"]
    assert out[4]["result"]["isError"] is True
    assert out[5]["error"]["code"] == -32601
    assert out[6]["result"] == {}
    # stdout must contain only protocol messages
    assert all(l.startswith("{") for l in proc.stdout.splitlines() if l.strip())


def test_package_allowlist(tmp_path):
    subprocess.run([sys.executable, str(ROOT / "package.py")], check=True, capture_output=True, cwd=str(ROOT))
    import zipfile
    z = next((ROOT / "dist").glob("medcare-resources-*.zip"))
    names = zipfile.ZipFile(z).namelist()
    assert any(n.endswith("plugins/medcare-resources/.codex-plugin/plugin.json") for n in names)
    assert any(n.endswith("plugins/medcare-resources/data/documents.json.gz") for n in names)
    assert any(n.endswith("plugins/medcare-resources/data/mcp-formulary-2027.json.gz") for n in names)
    assert not any("__pycache__" in n or n.endswith(".pyc") or "/tests/" in n or ".git/" in n for n in names)
    launcher = next(i for i in zipfile.ZipFile(z).infolist() if i.filename.endswith("bin/medcare-mcp"))
    assert (launcher.external_attr >> 16) & 0o111


def test_installer_isolated_home(tmp_path):
    home = tmp_path / "home"
    r = subprocess.run([sys.executable, str(ROOT / "install.py"), "--home", str(home)], capture_output=True, text=True,
                       timeout=120)
    assert r.returncode == 0, r.stderr
    res = json.loads(r.stdout)
    assert res["server_ok"] and res["tools"] >= 25
    market = json.loads((home / ".agents/plugins/marketplace.json").read_text())
    assert [p["name"] for p in market["plugins"]] == ["medcare-resources"]
    assert (home / ".codex/plugins/medcare-resources/bin/medcare-mcp").stat().st_mode & 0o111
    again = subprocess.run([sys.executable, str(ROOT / "install.py"), "--home", str(home)], capture_output=True, text=True)
    assert again.returncode != 0  # refuses to overwrite without --replace
    # existing unrelated entries are preserved on replace
    m = home / ".agents/plugins/marketplace.json"
    data = json.loads(m.read_text())
    data["plugins"].insert(0, {"name": "other", "source": {"source": "local", "path": "./x"},
                               "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"}, "category": "X"})
    m.write_text(json.dumps(data))
    r = subprocess.run([sys.executable, str(ROOT / "install.py"), "--home", str(home), "--replace"], capture_output=True,
                       text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    assert [p["name"] for p in json.loads(m.read_text())["plugins"]] == ["other", "medcare-resources"]
    u = subprocess.run([sys.executable, str(ROOT / "install.py"), "--home", str(home), "--uninstall"], capture_output=True, text=True)
    assert u.returncode == 0 and [p["name"] for p in json.loads(m.read_text())["plugins"]] == ["other"]
