#!/usr/bin/env python3
"""Install MedCare Resources into a personal Codex plugin marketplace (manual/offline route).

    python3 install.py              # copy plugin + register ~/.agents/plugins/marketplace.json entry
    python3 install.py --check      # only verify Python and that the server starts and lists tools
    python3 install.py --prefetch   # also download/cache CMS 2027 data now (~25 MB) so first use is fast
    python3 install.py --uninstall  # remove the copied plugin and its marketplace entry

The preferred route is Git: `codex plugin marketplace add myacaexpress/MCP-Codex-Plugin` then
`codex plugin add medcare-resources@medcare-team-tools` (see README). This script never edits
Codex trust, approval or permission settings and never touches any other plugin entry.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

NAME = "medcare-resources"
HERE = Path(__file__).resolve().parent
SOURCE = HERE / "plugins" / NAME
DEFAULT_MARKETPLACE = {"name": "personal-plugins", "interface": {"displayName": "Personal plugins"}, "plugins": []}
EXCLUDE = {"__pycache__", ".DS_Store", ".pytest_cache"}


def _copytree(src: Path, dst: Path):
    def ignore(_d, names):
        return [n for n in names if n in EXCLUDE or n.endswith(".pyc")]
    shutil.copytree(src, dst, ignore=ignore, symlinks=False)


def check_server(plugin_dir: Path, timeout: int = 60) -> dict:
    """Start the server exactly as Codex does and run initialize + tools/list."""
    launcher = plugin_dir / "bin" / "medcare-mcp"
    cmd = [str(launcher)]  # exactly what Codex runs (.mcp.json)
    msgs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "installer", "version": "1"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ]
    proc = subprocess.run(cmd, input="\n".join(json.dumps(m) for m in msgs) + "\n", cwd=str(plugin_dir),
                          capture_output=True, text=True, timeout=timeout)
    replies = [json.loads(l) for l in proc.stdout.splitlines() if l.strip()]
    tools = next((r["result"]["tools"] for r in replies if r.get("id") == 2 and "result" in r), None)
    if not tools:
        raise RuntimeError("Server did not list tools.\nstderr:\n" + proc.stderr[-2000:])
    return {"server_ok": True, "tools": len(tools), "protocol": replies[0]["result"]["protocolVersion"]}


def _atomic_write(path: Path, data: str) -> None:
    tmp = path.with_name(path.name + f".tmp-{os.getpid()}")
    try:
        tmp.write_text(data)
        tmp.replace(path)
    finally:
        if tmp.exists():
            tmp.unlink()


def install(home: Path, replace: bool) -> dict:
    """Transactional install: plugin copy and marketplace entry either both change or neither does."""
    if not (SOURCE / ".codex-plugin" / "plugin.json").is_file():
        raise SystemExit(f"Plugin source not found at {SOURCE}. Run from the unzipped package root.")
    target = home / ".codex" / "plugins" / NAME
    market = home / ".agents" / "plugins" / "marketplace.json"
    if target.is_symlink() or market.is_symlink():
        raise SystemExit("Refusing to write through a symlink.")
    if target.exists() and not replace:
        raise SystemExit(f"{target} already exists. Re-run with --replace to update it.")
    original_market = market.read_bytes() if market.exists() else None
    content = DEFAULT_MARKETPLACE
    if original_market is not None:
        content = json.loads(original_market)
        if not isinstance(content, dict) or not isinstance(content.get("plugins"), list):
            raise SystemExit(f"{market} is not a marketplace object with a 'plugins' list; not modifying it.")
    entry = {"name": NAME, "source": {"source": "local", "path": f"./.codex/plugins/{NAME}"},
             "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"}, "category": "Productivity"}
    others = [p for p in content["plugins"] if p.get("name") != NAME]
    if len(others) != len(content["plugins"]) and not replace:
        raise SystemExit("Marketplace already lists medcare-resources; re-run with --replace to update it.")
    content = {**content, "plugins": others + [entry]}
    target.parent.mkdir(parents=True, exist_ok=True)
    market.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{NAME}-staging-", dir=target.parent))
    rollback = target.with_name(f".{NAME}.rollback-{os.getpid()}")
    moved_old = moved_new = False
    try:
        _copytree(SOURCE, staging / NAME)
        (staging / NAME / "bin" / "medcare-mcp").chmod(0o755)
        check = check_server(staging / NAME)          # nothing installed has changed yet
        if target.exists():
            target.rename(rollback)
            moved_old = True
        (staging / NAME).rename(target)
        moved_new = True
        _atomic_write(market, json.dumps(content, indent=2) + "\n")
    except BaseException:
        # Roll back code and registration together.
        if moved_new:
            shutil.rmtree(target, ignore_errors=True)
        if moved_old and rollback.exists():
            rollback.rename(target)
        if original_market is None:
            if market.exists():
                market.unlink()
        elif not market.exists() or market.read_bytes() != original_market:
            market.write_bytes(original_market)
        raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    previous = None
    if moved_old:
        previous = target.with_name(f"{NAME}.previous")
        if previous.exists():
            shutil.rmtree(previous)
        rollback.rename(previous)
    return {"installed_to": str(target), "marketplace": str(market), "marketplace_name": content["name"], **check,
            "previous_version_kept_at": str(previous) if previous else None,
            "next_steps": [f"CLI: codex plugin add {NAME}@{content['name']}",
                           "Desktop: restart the ChatGPT/Codex desktop app -> Plugins -> choose the "
                           f"'{content.get('interface', {}).get('displayName', content['name'])}' source -> Install "
                           "MedCare Resources, then start a NEW chat and ask: 'Use MedCare Resources source_status'."]}


def uninstall(home: Path) -> dict:
    target = home / ".codex" / "plugins" / NAME
    market = home / ".agents" / "plugins" / "marketplace.json"
    removed = []
    if target.exists() and not target.is_symlink():
        shutil.rmtree(target)
        removed.append(str(target))
    if market.exists():
        content = json.loads(market.read_text())
        plugins = [p for p in content.get("plugins", []) if p.get("name") != NAME]
        if len(plugins) != len(content.get("plugins", [])):
            content["plugins"] = plugins
            _atomic_write(market, json.dumps(content, indent=2) + "\n")
            removed.append(f"{market} entry")
    return {"removed": removed,
            "note": f"If installed via Codex, also run: codex plugin remove {NAME}@<marketplace>. Cached public data "
                    "lives in ~/.cache/medcare-resources and can be deleted."}


def main():
    if os.name == "nt":
        raise SystemExit("MedCare Resources supports macOS and Linux only in this release (the Codex MCP launcher is a "
                         "POSIX shell script). Windows is not supported; see README 'Platform support'.")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--home", type=Path, default=Path.home(), help="Home directory (for isolated testing).")
    ap.add_argument("--replace", action="store_true", help="Replace an existing installation (keeps one backup).")
    ap.add_argument("--check", action="store_true", help="Only check that the server starts and lists tools.")
    ap.add_argument("--prefetch", action="store_true", help="Download and cache CMS 2027 data after installing.")
    ap.add_argument("--uninstall", action="store_true")
    a = ap.parse_args()
    if sys.version_info < (3, 9):
        raise SystemExit("Python 3.9+ is required.")
    if a.check:
        print(json.dumps(check_server(SOURCE), indent=2))
        return
    if a.uninstall:
        print(json.dumps(uninstall(a.home), indent=2))
        return
    result = install(a.home, a.replace)
    if a.prefetch:
        sys.path.insert(0, str(Path(result["installed_to"])))
        from medcare import cms  # noqa: E402
        result["prefetch"] = {"landscape": cms.landscape_source(2027).get("data_last_updated"),
                              "part_d_file": cms.puf_source(2027).get("file_date")}
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
