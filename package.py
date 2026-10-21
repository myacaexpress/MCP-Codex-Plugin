#!/usr/bin/env python3
"""Build the teammate distribution ZIP: dist/medcare-resources-<version>.zip

Contents: the plugin folder, the Git/local marketplace file, install.py and docs.
Only files under the allowlisted roots are included; caches and bytecode are skipped.
"""
from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
INCLUDE = ["plugins/medcare-resources", ".agents/plugins/marketplace.json", "install.py", "README.md",
           "THIRD_PARTY_NOTICES.md", "CHANGELOG.md"]
SKIP_PARTS = {"__pycache__", ".pytest_cache", ".DS_Store"}


def files():
    for item in INCLUDE:
        p = ROOT / item
        if p.is_file():
            yield p
        elif p.is_dir():
            for f in sorted(p.rglob("*")):
                if f.is_file() and not (set(f.relative_to(ROOT).parts) & SKIP_PARTS) and f.suffix != ".pyc":
                    if f.is_symlink() or not f.resolve().is_relative_to(ROOT):
                        raise SystemExit(f"Refusing unsafe file {f}")
                    yield f
        else:
            raise SystemExit(f"Missing package file: {item}")


def main():
    version = json.loads((ROOT / "plugins/medcare-resources/.codex-plugin/plugin.json").read_text())["version"]
    out = ROOT / "dist" / f"medcare-resources-{version}.zip"
    out.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for f in files():
            rel = f.relative_to(ROOT)
            info = zipfile.ZipInfo(f"medcare-resources-{version}/{rel.as_posix()}", date_time=(2026, 1, 1, 0, 0, 0))
            mode = 0o755 if f.name == "medcare-mcp" or f.suffix == ".py" and f.parent == ROOT else 0o644
            info.external_attr = (0o100000 | mode) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, f.read_bytes())
    print(out)


if __name__ == "__main__":
    if sys.version_info < (3, 9):
        raise SystemExit("Python 3.9+ required")
    main()
