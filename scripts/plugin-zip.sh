#!/bin/sh
# Zip the plugin for upload to Claude Cowork and Perplexity Computer.
#
#   scripts/plugin-zip.sh [OUT_DIR]        (default: target/fork-release)
#
# Both hosts want the plugin folder's CONTENTS at the archive root (plugin.json first
# level, not inside a directory). Run after scripts/fork-release.sh has pinned the
# launcher; the zip's checksum is appended to OUT_DIR/sha256.txt.
set -eu
ROOT=$(cd "$(dirname "$0")/.." && pwd)
OUT=${1:-$ROOT/target/fork-release}
mkdir -p "$OUT"
python3 - "$ROOT/plugins/speedread-netbrah" "$OUT/speedread-netbrah-plugin.zip" <<'PY'
import os, stat, sys, zipfile
src, out = sys.argv[1], sys.argv[2]
if os.path.exists(out):
    os.remove(out)
paths = []
for d, dirs, files in os.walk(src):
    dirs.sort()
    paths += [os.path.join(d, f) for f in sorted(files)]
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
    for p in paths:
        # Fixed timestamp and explicit modes: the same tree gives the same zip, and the
        # launcher keeps its executable bit.
        info = zipfile.ZipInfo(os.path.relpath(p, src), date_time=(2026, 1, 1, 0, 0, 0))
        info.compress_type = zipfile.ZIP_DEFLATED
        info.create_system = 3
        mode = 0o755 if os.stat(p).st_mode & 0o111 else 0o644
        info.external_attr = (stat.S_IFREG | mode) << 16
        with open(p, "rb") as f:
            z.writestr(info, f.read())
names = zipfile.ZipFile(out).namelist()
assert "plugin.json" in names and "skills/speedread/SKILL.md" in names, names
print(f"{out}: {len(names)} files, {os.path.getsize(out)} bytes")
PY
cd "$OUT"
grep -v ' speedread-netbrah-plugin.zip$' sha256.txt > sha256.txt.tmp 2>/dev/null || true
sha256sum speedread-netbrah-plugin.zip >> sha256.txt.tmp
mv sha256.txt.tmp sha256.txt
