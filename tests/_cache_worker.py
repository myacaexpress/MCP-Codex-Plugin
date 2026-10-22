"""Worker process for tests/test_cache_concurrency.py (spawned; synthetic CMS/Census data, no network).

Every worker is a separate Python process sharing ONE cache directory, exactly like several Codex chats
or remote-server workers sharing the per-user cache. Upstream downloads are replaced with synthetic
archives whose reads are slowed down to widen the race window.
"""
from __future__ import annotations

import io
import json
import os
import sys
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "plugins" / "medcare-resources"))


def _inner(name: str, header: list[str], rows: list[list[str]]) -> bytes:
    txt = "|".join(header) + "\n" + "\n".join("|".join(r) for r in rows) + "\n"
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(name + ".txt", txt)
    return b.getvalue()


def make_puf_zip(path: Path, n_formulary: int = 40000, tier: str = "1") -> None:
    members = {
        "plan information 2027.zip": (["CONTRACT_ID", "PLAN_ID", "SEGMENT_ID", "PLAN_NAME", "FORMULARY_ID",
                                       "DEDUCTIBLE", "COUNTY_CODE"],
                                      [["H1111", "001", "000", "Synthetic Plan", "F1", "0", "48201"]]),
        "basic drugs formulary file 2027.zip": (
            ["FORMULARY_ID", "RXCUI", "TIER_LEVEL_VALUE", "PRIOR_AUTHORIZATION_YN", "STEP_THERAPY_YN",
             "QUANTITY_LIMIT_YN", "QUANTITY_LIMIT_AMOUNT", "QUANTITY_LIMIT_DAYS"],
            [["F1", str(100000 + i), tier, "N", "N", "N", "", ""] for i in range(n_formulary)]),
        "beneficiary cost file 2027.zip": (["CONTRACT_ID", "PLAN_ID", "SEGMENT_ID", "TIER"], [["H1111", "001", "000", "1"]]),
        "insulin beneficiary cost file 2027.zip": (["CONTRACT_ID", "PLAN_ID", "SEGMENT_ID", "TIER"],
                                                   [["H1111", "001", "000", "1"]]),
        "excluded drugs formulary file 2027.zip": (["CONTRACT_ID", "PLAN_ID", "RXCUI"], [["H1111", "001", "1"]]),
        "geographic locator file 2027.zip": (["COUNTY_CODE", "COUNTY", "STATENAME"], [["48201", "Harris", "Texas"]]),
    }
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w", zipfile.ZIP_STORED) as z:
        for name, (header, rows) in members.items():
            z.writestr(name, _inner(name[:-4], header, rows))
    path.write_bytes(b.getvalue())


def make_landscape_zip(path: Path, n: int = 30000) -> None:
    header = ["Contract ID", "Plan ID", "Segment ID", "County Name", "State Territory Abbreviation", "Plan Name",
              "Plan Type", "Contract Year"]
    rows = [["H1111", "001", "0", f"County{i}", "TX", "Synthetic", "HMO", "2027"] for i in range(n)]
    csv_text = ",".join(header) + "\n" + "\n".join(",".join(r) for r in rows) + "\n"
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr("landscape.csv", csv_text)
        z.writestr("readme.txt", "Data Last Updated: 10/01/2026")
    path.write_bytes(b.getvalue())


def _install_fakes(cms, net, data_dir: Path, delay: float, fail: bool, stamp: str, land_stamp: str = "202610"):
    log = data_dir / "builds.log"
    puf = (data_dir / f"puf_{stamp}.zip").read_bytes()
    lz = data_dir / f"landscape_{land_stamp}.zip"
    land = (lz if lz.exists() else data_dir / "landscape.zip").read_bytes()
    zcta = "GEOID_ZCTA5_20|GEOID_COUNTY_20|NAMELSAD_COUNTY_20|AREALAND_PART\n" + "\n".join(
        f"{77000 + i:05d}|48201|Harris County|{1000 + i}" for i in range(20000)) + "\n"

    class SlowZip(io.BytesIO):
        last_modified = "synthetic"
        read_total = 0

        def read(self, *a):
            time.sleep(delay)
            if fail:
                raise RuntimeError("synthetic build failure")
            return super().read(*a)

    def record(kind):
        with open(log, "a") as f:
            f.write(f"{kind} {os.getpid()}\n")

    cms._discover_puf = lambda year: ("https://data.cms.gov/fake.zip", stamp)
    cms._discover_landscape = lambda year: f"https://www.cms.gov/files/zip/cy2027-landscape-{land_stamp}.zip"
    cms._RemoteZip = lambda url: (record("puf"), SlowZip(puf))[1]

    def fetch(url, **kw):
        time.sleep(delay * 20)
        if "landscape" in url:
            if kw.get("max_bytes") == 1:
                return net.Response(url, url, 206, {"last-modified": "synthetic"}, b"x")
            record("landscape")
            if fail:
                raise net.FetchError("synthetic download failure")
            return net.Response(url, url, 200, {"last-modified": "synthetic"}, land)
        if "zcta" in url:
            record("zcta")
            return net.Response(url, url, 200, {}, zcta.encode())
        raise AssertionError(url)
    cms.net.fetch = fetch


def run(task: str, cache_dir: str, data_dir: str, barrier, results, delay: float = 0.002, fail: bool = False,
        stamp: str = "20261008", refresh: bool = False, repeat: int = 1, land_stamp: str = "202610"):
    os.environ["MEDCARE_CACHE_DIR"] = cache_dir
    os.environ["MEDCARE_CACHE_LOCK_TIMEOUT"] = "120"
    from medcare import cms, net
    _install_fakes(cms, net, Path(data_dir), delay, fail, stamp, land_stamp)
    out = {"pid": os.getpid(), "task": task}
    try:
        barrier.wait(30)
        for _ in range(repeat):
            if task == "puf":
                conn = cms.puf_db(2027, refresh=refresh)
                out["rows"] = conn.execute("select count(*) from formulary").fetchone()[0]
                out["file_date"] = cms._meta(conn)["file_date"]
            elif task == "landscape":
                conn = cms.landscape_db(2027, refresh=refresh)
                out["rows"] = conn.execute("select count(*) from area").fetchone()[0]
            elif task == "zcta":
                r = cms.zip_counties("77005")
                out["rows"] = len(r["counties"]) if isinstance(r, dict) and "counties" in r else json.dumps(r)[:80]
        out["ok"] = True
    except Exception as err:  # reported to the parent test, never swallowed
        out["ok"] = False
        out["error"] = f"{type(err).__name__}: {err}"
    results.put(out)


def _wait_for(flag: Path, timeout: float = 120) -> None:
    end = time.monotonic() + timeout
    while not flag.exists():
        if time.monotonic() > end:
            raise TimeoutError(f"no {flag.name} signal")
        time.sleep(0.01)


def paused_read(task: str, cache_dir: str, data_dir: str, sync_dir: str, results, where: str = "before_open"):
    """Reader that pauses at an exact point while the parent runs a real refresh in another process.

    where='before_open': after the published generation was selected, before it is opened (the 1.2.4 P2
    window; on 1.2.4 this point is just after _fresh(path) returned True). where='after_open': after the
    connection is obtained, before any query (the reader must keep reading the generation it opened).
    """
    os.environ["MEDCARE_CACHE_DIR"] = cache_dir
    from medcare import cms, net
    _install_fakes(cms, net, Path(data_dir), 0.0, False, "20261008")
    sync = Path(sync_dir)
    state = {"paused": False}

    def pause():
        if not state["paused"]:
            state["paused"] = True
            (sync / "paused").touch()
            _wait_for(sync / "go")

    if where == "before_open":
        if hasattr(cms, "_before_open"):
            cms._before_open = lambda path: pause()
        else:                                   # 1.2.4: the window is between _fresh(path) and _connect(path)
            orig = cms._fresh

            def fresh(path):
                ok = orig(path)
                if ok:
                    pause()
                return ok
            cms._fresh = fresh
    out = {"pid": os.getpid(), "task": task, "where": where}
    try:
        conn = cms.puf_db(2027) if task == "puf" else cms.landscape_db(2027)
        if where == "after_open":
            pause()
        meta = cms._meta(conn)
        if task == "puf":
            out["file_date"] = meta["file_date"]
            out["tiers"] = sorted({r[0] for r in conn.execute("select distinct TIER_LEVEL_VALUE from formulary")})
            out["source_file_date"] = cms.puf_source(2027, conn)["file_date"]
        else:
            out["source_url"] = meta["source_url"]
            out["rows"] = conn.execute("select count(*) from area").fetchone()[0]
        out["ok"] = True
    except Exception as err:
        out["ok"] = False
        out["error"] = f"{type(err).__name__}: {err}"
    results.put(out)
