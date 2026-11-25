#!/usr/bin/env python3
"""Fetch the pinned bench repos, including a partial Chromium tree.

    python3 evals/fetch_corpora.py --dest /path/to/bench
    python3 evals/fetch_corpora.py --dest /path/to/bench --check
    python3 evals/fetch_corpora.py --dest /path/to/bench --only flask,gin

Each repo is a shallow fetch of the commit in evals/corpora.json. Every pin
is a full 40-character SHA. Chromium is
3a7520d2d79d6569d3a11f968d32b20665fa4db3 from
https://chromium.googlesource.com/chromium/src.git. A 12-character prefix is
rejected: googlesource answers ``couldn't find remote ref``.
with a blobless fetch and a sparse checkout that drops the extensions listed
in that file (images, media, fonts, archives). After checkout the script
checks the sentinel lines the graders depend on.

The historical measurement in docs/specs/eval.md was 474,447 files and 4.5 GB
after those kinds of files were left out. This script records the file count
and disk use of the tree it actually checked out. A count that differs is a
different tree; do not compare its CLI timings to evals/fork/big-chromium.txt
as if the trees were the same.
"""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

# googlesource (and a depth-1 fetch of a short hash) does not resolve a prefix.
FULL_SHA = re.compile(r"^[0-9a-fA-F]{40}$")

HERE = Path(__file__).resolve().parent
LOCK = HERE / "corpora.json"


def run(cmd, **kw):
    subprocess.run(cmd, check=True, **kw)


def have(dest):
    return (dest / ".git").is_dir()


def require_full_sha(commit, name):
    """Abort unless ``commit`` is a full 40-character object name."""
    if not isinstance(commit, str) or FULL_SHA.fullmatch(commit) is None:
        raise SystemExit(f"{name}: commit must be a full 40-character SHA, got {commit!r}")
    return commit.lower()


def fetch_commit(dest, url, commit):
    dest.mkdir(parents=True, exist_ok=True)
    if not have(dest):
        run(["git", "init", "-q", str(dest)])
        run(["git", "-C", str(dest), "remote", "add", "origin", url])
    run(["git", "-C", str(dest), "fetch", "-q", "--depth", "1", "origin", commit])
    run(["git", "-C", str(dest), "checkout", "-q", "--detach", "FETCH_HEAD"])
    got = subprocess.run(["git", "-C", str(dest), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
    if got != commit:
        raise SystemExit(f"{dest.name}: fetched {got}, wanted {commit}")
    return got


def chromium_sparse_patterns(extensions):
    # Non-cone sparse-checkout: include the tree, then drop listed extensions.
    lines = ["/*", "/**"]
    for ext in extensions:
        lines.append(f"!*.{ext}")
        lines.append(f"!/**/*.{ext}")
    return "\n".join(lines) + "\n"


def fetch_chromium(dest, spec):
    dest.mkdir(parents=True, exist_ok=True)
    if not have(dest):
        run(["git", "init", "-q", str(dest)])
        run(["git", "-C", str(dest), "remote", "add", "origin", spec["url"]])
    info = dest / ".git" / "info"
    info.mkdir(parents=True, exist_ok=True)
    (info / "sparse-checkout").write_text(chromium_sparse_patterns(spec["omit_extensions"]))
    run(["git", "-C", str(dest), "config", "core.sparseCheckout", "true"])
    run(["git", "-C", str(dest), "config", "extensions.partialClone", "origin"])
    run([
        "git", "-C", str(dest), "-c", "protocol.version=2",
        "fetch", "-q", "--filter=blob:none", "--depth", "1", "origin", spec["commit"],
    ])
    run(["git", "-C", str(dest), "checkout", "-q", "--detach", "FETCH_HEAD"])
    got = subprocess.run(["git", "-C", str(dest), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
    if got != spec["commit"]:
        raise SystemExit(f"chromium: fetched {got}, wanted {spec['commit']}")
    files = sum(1 for p in dest.rglob("*") if p.is_file() and ".git" not in p.parts)
    du = subprocess.run(["du", "-sh", str(dest)], capture_output=True, text=True).stdout.split()
    size = du[0] if du else "?"
    report = dest / "CHECKOUT.txt"
    # CHECKOUT.txt is untracked (it is not in the sparse set of source). Write it.
    report.write_text(
        f"commit {got}\nfiles {files}\ndisk {size}\nomitted extensions: {', '.join(spec['omit_extensions'])}\n"
        "Historical reference: 474447 files, 4.5 GB (docs/specs/eval.md). "
        "Compare CLI timings to evals/fork/big-chromium.txt only when this file count is that count.\n"
    )
    print(f"chromium checkout {got}  files={files}  disk={size}", flush=True)
    print(f"wrote {report}", flush=True)
    return got


def check_repo(dest, spec):
    bad = []
    for s in spec.get("sentinels", []):
        path = dest / s["path"]
        if not path.is_file():
            bad.append(f"missing {s['path']}")
            continue
        lines = path.read_text(errors="replace").splitlines()
        n = s["line"]
        got = lines[n - 1] if 1 <= n <= len(lines) else "<short file>"
        if s["contains"] not in got:
            bad.append(f"{s['path']}:{n} expected {s['contains']!r} in {got[:120]!r}")
    return bad


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dest", type=Path, required=True, help="directory that will contain one folder per repo")
    ap.add_argument("--only", default=None, help="comma-separated repo names")
    ap.add_argument("--check", action="store_true", help="only verify sentinels; do not fetch")
    args = ap.parse_args()
    lock = json.loads(LOCK.read_text())
    names = args.only.split(",") if args.only else list(lock["repos"])
    args.dest.mkdir(parents=True, exist_ok=True)
    failed = False
    for name in names:
        spec = lock["repos"][name]
        spec["commit"] = require_full_sha(spec["commit"], name)
        dest = args.dest / name
        if not args.check:
            print(f"fetch {name} {spec['commit']}", flush=True)
            if spec.get("partial"):
                fetch_chromium(dest, spec)
            else:
                got = fetch_commit(dest, spec["url"], spec["commit"])
                print(f"  {name} @ {got}", flush=True)
        bad = check_repo(dest, spec) if dest.exists() else [f"{name} is not checked out"]
        if bad:
            failed = True
            print(f"SENTINEL FAIL {name}:", flush=True)
            for b in bad:
                print(f"  {b}", flush=True)
        else:
            print(f"sentinels ok {name}", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
