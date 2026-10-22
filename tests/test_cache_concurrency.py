"""Cross-process cache construction/publication (review of 1.2.3). Separate OS processes share one cache
directory, as concurrent Codex chats or remote-server workers do. Synthetic data only, no network."""
import multiprocessing as mp
from pathlib import Path

import pytest

import _cache_worker as w

N = 6


def _run(tmp_path, jobs):
    ctx = mp.get_context("spawn")
    barrier = ctx.Barrier(len(jobs))
    q = ctx.Queue()
    procs = [ctx.Process(target=w.run, args=(task, str(tmp_path / "cache"), str(tmp_path / "data"), barrier, q),
                         kwargs=kw) for task, kw in jobs]
    for p in procs:
        p.start()
    out = [q.get(timeout=240) for _ in procs]
    for p in procs:
        p.join(60)
        assert p.exitcode == 0
    return out


@pytest.fixture
def data(tmp_path):
    d = tmp_path / "data"
    d.mkdir()
    w.make_puf_zip(d / "puf_20261008.zip", tier="1")
    w.make_puf_zip(d / "puf_20261108.zip", tier="2")
    w.make_landscape_zip(d / "landscape.zip")
    (tmp_path / "cache").mkdir()
    return d


def _builds(data, kind):
    log = data / "builds.log"
    return [l for l in log.read_text().splitlines() if l.startswith(kind)] if log.exists() else []


def _leftovers(tmp_path):
    cms_dir = tmp_path / "cache" / "cms"
    return sorted(p.name for p in cms_dir.glob(".*") if ".tmp" in p.name)


@pytest.mark.parametrize("task,pattern", [("puf", "partd_puf_2027_*.sqlite"),
                                          ("landscape", "landscape_v2_2027_*.sqlite"),
                                          ("zcta", "zcta_county_2020.sqlite")])
def test_concurrent_first_build_is_built_once_and_every_process_succeeds(tmp_path, data, task, pattern):
    out = _run(tmp_path, [(task, {}) for _ in range(N)])
    assert all(o["ok"] for o in out), out
    assert len({str(o["rows"]) for o in out}) == 1, out           # every reader saw the same complete file
    assert len(_builds(data, task)) == 1                            # one builder; others waited and reused it
    assert len(list((tmp_path / "cache" / "cms").glob(pattern))) == 1
    assert _leftovers(tmp_path) == []                              # no temp/journal files left


def test_concurrent_refresh_to_new_file_while_readers_query(tmp_path, data):
    first = _run(tmp_path, [("puf", {})])                          # publish the OLD file
    assert first[0]["file_date"] == "20261008"
    jobs = ([("puf", {"stamp": "20261108", "refresh": True}) for _ in range(3)]
            + [("puf", {"stamp": "20261108", "repeat": 20}) for _ in range(3)])
    out = _run(tmp_path, jobs)
    assert all(o["ok"] for o in out), out
    assert {o["file_date"] for o in out if o.get("file_date")} <= {"20261008", "20261108"}
    assert len(_builds(data, "puf")) == 2                           # OLD once, NEW once (not once per process)
    files = sorted(p.name for p in (tmp_path / "cache" / "cms").glob("partd_puf_2027_*.sqlite"))
    assert files == ["partd_puf_2027_20261108.sqlite"]
    assert _leftovers(tmp_path) == []


def test_failed_build_cleans_its_temp_releases_lock_and_reports_error(tmp_path, data):
    out = _run(tmp_path, [("puf", {"fail": True}) for _ in range(3)])
    assert not any(o["ok"] for o in out) and all("synthetic build failure" in o["error"] for o in out), out
    assert _leftovers(tmp_path) == [] and not list((tmp_path / "cache" / "cms").glob("partd_puf_*.sqlite"))
    ok = _run(tmp_path, [("puf", {}) for _ in range(2)])         # lock was released: a later build succeeds
    assert all(o["ok"] for o in ok) and ok[0]["rows"] == 40000


def test_orphan_temp_from_crashed_builder_is_removed_only_under_lock(tmp_path, data):
    cms_dir = tmp_path / "cache" / "cms"
    cms_dir.mkdir(parents=True)
    orphan = cms_dir / ".partd_puf_2027_20261008.sqlite.99999.deadbeef.tmp"
    orphan.write_bytes(b"partial")
    unrelated = cms_dir / ".landscape_v2_2027_202610.sqlite.99999.cafe.tmp"
    unrelated.write_bytes(b"other dataset")
    out = _run(tmp_path, [("puf", {}) for _ in range(2)])
    assert all(o["ok"] for o in out)
    assert not orphan.exists() and unrelated.exists()            # only the locked dataset's orphans are removed


def test_published_cache_is_opened_read_only(tmp_path, data, monkeypatch):
    _run(tmp_path, [("puf", {})])
    import sqlite3
    monkeypatch.setenv("MEDCARE_CACHE_DIR", str(tmp_path / "cache"))
    from medcare import cms
    path = next((tmp_path / "cache" / "cms").glob("partd_puf_2027_*.sqlite"))
    conn = cms._connect(path)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("create table formulary(x)")
    before = path.stat().st_mtime_ns
    cms._set_meta(path, last_checked_at="2026-10-21T00:00:00+00:00")   # freshness stamp -> sidecar only
    assert path.stat().st_mtime_ns == before and cms._meta(cms._connect(path))["last_checked_at"].startswith("2026-10-21")


def test_cache_errors_keep_every_entry_accounted_as_lookup_failed(monkeypatch):
    """A cache that cannot be opened (lock timeout, I/O error) is an explicit per-plan failure, never a crash
    and never 'not covered'; every pasted entry still gets exactly one result in order."""
    import sqlite3
    from medcare import batch, cms
    from test_batch import PRODUCTS
    monkeypatch.setattr(batch, "_rxnav", lambda q: PRODUCTS.get(q.lower().split()[0], []))
    for exc in (sqlite3.OperationalError("disk I/O error"), cms.CMSDataError("Timed out waiting for the cache")):
        monkeypatch.setattr(cms, "puf_db", lambda year, *a, _e=exc, **k: (_ for _ in ()).throw(_e))
        r = batch.check_list("lisinopril 10 mg tablet\natorvastatin 40 mg tablet\nXyzzaprel 5 mg",
                             plan_ids=["H5767-001"], include_medcare_site=False)
        assert [x["index"] for x in r["results"]] == [1, 2, 3]
        assert r["plan_errors"]["H5767-001"].startswith("CMS Part D file unavailable")
        assert [x["status"] for x in r["results"]] == ["lookup_failed", "lookup_failed", "unrecognized"]
        assert all(p["coverage"] == "lookup_failed" for x in r["results"][:2] for p in x["plans"])
