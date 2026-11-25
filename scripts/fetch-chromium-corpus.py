#!/usr/bin/env python3
"""Fetch the Chromium tree the fork's large-tree suite was written against.

The historical corpus is https://github.com/chromium/chromium at 3a7520d2d79d,
unpacked under target/eval-repos/chromium and `git init`ed so .gitignore applies,
after images, media, fonts, and archives are left out (docs/specs/eval.md).

This script repeats that method and writes a provenance file a later run can
commit. It does not put the tree itself in git (target/ is ignored).

Usage:
  python3 scripts/fetch-chromium-corpus.py
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMMIT = "3a7520d2d79d6569d3a11f968d32b20665fa4db3"
URL = f"https://codeload.github.com/chromium/chromium/tar.gz/{COMMIT}"
DEST = ROOT / "target" / "eval-repos" / "chromium"
TARBALL = ROOT / "target" / "eval-repos" / f"chromium-{COMMIT}.tar.gz"
PROVENANCE = ROOT / "evals" / "fork" / "definition-tokens" / "chromium-provenance.json"

# What "leaving out images, media, fonts and archives" means for this fetch.
# Counts per extension are recorded before deletion.
OMIT = {
    "image": [".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".bmp", ".svg", ".icns", ".tif", ".tiff", ".avif"],
    "media": [".mp4", ".webm", ".mp3", ".wav", ".ogg", ".mov", ".avi", ".flac", ".m4a", ".opus", ".mkv", ".aac"],
    "font": [".ttf", ".otf", ".woff", ".woff2", ".eot"],
    "archive": [".zip", ".jar", ".apk", ".aar", ".7z", ".rar", ".iso", ".dmg", ".tar", ".tgz", ".bz2", ".xz", ".gz", ".whl"],
}
EXT_KIND = {ext: kind for kind, exts in OMIT.items() for ext in exts}

# Query paths from scripts/fork-big-eval.py. The tree is not usable if these are missing.
REQUIRED = [
    "content/browser/renderer_host/render_frame_host_impl.cc",
    "content/browser",
    "content",
]


def walk_files(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        for name in filenames:
            yield Path(dirpath) / name


def snapshot(root: Path):
    files = 0
    size = 0
    for path in walk_files(root):
        files += 1
        try:
            size += path.stat().st_size
        except OSError:
            pass
    return {"files": files, "bytes": size}


def omit(root: Path):
    removed = {kind: {"files": 0, "bytes": 0, "by_ext": {}} for kind in OMIT}
    for path in list(walk_files(root)):
        kind = EXT_KIND.get(path.suffix.lower())
        if kind is None:
            continue
        try:
            nbytes = path.stat().st_size
        except OSError:
            nbytes = 0
        try:
            path.unlink()
        except OSError:
            continue
        bucket = removed[kind]
        bucket["files"] += 1
        bucket["bytes"] += nbytes
        ext = path.suffix.lower()
        slot = bucket["by_ext"].setdefault(ext, {"files": 0, "bytes": 0})
        slot["files"] += 1
        slot["bytes"] += nbytes
    return removed


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def download(url: str, dest: Path):
    dest.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["curl", "-L", "--fail", "--retry", "5", "--retry-delay", "2", "-C", "-", "-o", str(dest), url],
        check=True,
    )


def main() -> int:
    if (DEST / ".git").is_dir() and (DEST / "content").is_dir():
        print(f"already present: {DEST}", flush=True)
    else:
        if DEST.exists():
            shutil.rmtree(DEST)
        print(f"downloading {URL}", flush=True)
        t0 = time.time()
        download(URL, TARBALL)
        tarball_bytes = TARBALL.stat().st_size
        digest = sha256(TARBALL)
        print(f"tarball {tarball_bytes} bytes sha256 {digest} in {time.time() - t0:.0f}s", flush=True)
        parent = DEST.parent
        parent.mkdir(parents=True, exist_ok=True)
        print("extracting", flush=True)
        subprocess.run(["tar", "-xzf", str(TARBALL), "-C", str(parent)], check=True)
        extracted = parent / f"chromium-{COMMIT}"
        if not extracted.is_dir():
            # codeload uses the short directory name when the archive root differs
            cands = [p for p in parent.glob("chromium-*") if p.is_dir() and p != DEST]
            if len(cands) != 1:
                sys.exit(f"expected one extracted directory, found {cands}")
            extracted = cands[0]
        extracted.rename(DEST)
        TARBALL.unlink()
        before = snapshot(DEST)
        print(f"before omit: {before}", flush=True)
        removed = omit(DEST)
        after = snapshot(DEST)
        print(f"after omit: {after}", flush=True)
        subprocess.run(["git", "init"], cwd=DEST, check=True, capture_output=True)
        missing = [rel for rel in REQUIRED if not (DEST / rel).exists()]
        if missing:
            sys.exit(f"required query paths missing after fetch: {missing}")
        prov = {
            "method": "codeload tarball of one commit, delete image/media/font/archive extensions, git init",
            "url": URL,
            "commit": COMMIT,
            "tarball_sha256": digest,
            "tarball_bytes": tarball_bytes,
            "omit_extensions": OMIT,
            "removed": removed,
            "before_omit": before,
            "after_omit": after,
            "git_init": True,
            "require_git_note": "speedread's walker sets require_git(false); git init still matches the historical corpus and stops parent-repo ignore rules (.parents in src/walk.rs).",
            "required_paths_present": REQUIRED,
            "dest": "target/eval-repos/chromium",
        }
        # tarball was unlinked; size was recorded only in the log. Re-read from digest step:
        # store size from the sha pass by stating it was deleted after extract.
        PROVENANCE.parent.mkdir(parents=True, exist_ok=True)
        PROVENANCE.write_text(json.dumps(prov, indent=2) + "\n")
        print(f"wrote {PROVENANCE}", flush=True)
        return 0

    # Refresh provenance from an existing tree without re-downloading.
    after = snapshot(DEST)
    head = subprocess.run(["git", "-C", str(DEST), "rev-parse", "HEAD"], capture_output=True, text=True)
    PROVENANCE.parent.mkdir(parents=True, exist_ok=True)
    existing = {}
    if PROVENANCE.exists():
        existing = json.loads(PROVENANCE.read_text())
    existing["after_omit_recount"] = after
    existing["dest_git_head"] = head.stdout.strip() or None
    existing["dest"] = "target/eval-repos/chromium"
    PROVENANCE.write_text(json.dumps(existing, indent=2) + "\n")
    print(json.dumps(after))
    return 0


if __name__ == "__main__":
    sys.exit(main())
