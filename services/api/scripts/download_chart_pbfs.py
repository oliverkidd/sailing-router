#!/usr/bin/env python3
"""Download Geofabrik PBFs needed for full-region coastal chart extract."""

from __future__ import annotations

import concurrent.futures
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(Path(__file__).resolve().parent))

from extract_osm_pbf_features import PBF_DIR, _PBF_URLS  # noqa: E402


def _download_one(name: str, url: str) -> str:
    PBF_DIR.mkdir(parents=True, exist_ok=True)
    path = PBF_DIR / name
    if path.exists() and path.stat().st_size > 1_000_000:
        return f"HAVE {name} ({path.stat().st_size / 1e9:.2f} GB)"
    part = path.with_suffix(path.suffix + ".part")
    print(f"[dl] GET {name}", flush=True)
    try:
        # Resume if .part exists: urllib doesn't resume; use curl via subprocess for -C -
        import subprocess

        cmd = [
            "curl",
            "-L",
            "--fail",
            "--retry",
            "8",
            "--retry-delay",
            "15",
            "-C",
            "-",
            "--connect-timeout",
            "30",
            "-o",
            str(part),
            url,
        ]
        subprocess.run(cmd, check=True)
        part.replace(path)
        return f"DONE {name} ({path.stat().st_size / 1e9:.2f} GB)"
    except Exception as exc:  # noqa: BLE001
        return f"FAIL {name}: {exc}"


def main() -> int:
    items = list(_PBF_URLS.items())
    print(f"[dl] {len(items)} extracts → {PBF_DIR}", flush=True)
    ok = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        futs = [pool.submit(_download_one, name, url) for name, url in items]
        for fut in concurrent.futures.as_completed(futs):
            msg = fut.result()
            print(f"[dl] {msg}", flush=True)
            if msg.startswith(("HAVE", "DONE")):
                ok += 1
    print(f"[dl] finished ok={ok}/{len(items)}", flush=True)
    return 0 if ok >= 2 else 1


if __name__ == "__main__":
    raise SystemExit(main())
