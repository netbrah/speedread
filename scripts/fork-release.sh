#!/usr/bin/env bash
# Build every release asset of the fork from one machine (Linux or macOS).
#
#   scripts/fork-release.sh v0.2.0-netbrah.2 [OUT_DIR]
#
# Writes, into OUT_DIR (default target/fork-release):
#   speedread-<target>.tar.gz   one per target below; the binary alone
#   speedread-aarch64-apple-darwin.mcpb   the Claude Desktop bundle
#   sha256.txt                  checksums of all of the above
# and pins the plugin launcher (plugins/speedread-netbrah/skills/speedread/
# scripts/speedread.sh) to this version and these checksums. Commit that
# change and tag THAT commit: the launcher downloads the assets by tag, so a
# plugin installed at the tag must already know their checksums. The binaries
# are therefore built from the tag's parent, which differs only in the pin.
#
# The release job in .github/workflows/ci.yml builds the macOS arm64 assets
# natively when Actions are enabled. This script cross-compiles instead
# (cargo-zigbuild), so only targets the build machine can execute are run
# before release; the release notes must say which were not.
#
# Needs: cargo-zigbuild + zig, the Rust targets below, python3, ImageMagick
# (convert), npx.
set -euo pipefail

tag="${1:?usage: fork-release.sh TAG [OUT_DIR]}"
out="$(mkdir -p "${2:-target/fork-release}" && cd "${2:-target/fork-release}" && pwd)"
cd "$(dirname "$0")/.."

version="${tag#v}"
crate="$(sed -n 's/^version = "\(.*\)"/\1/p' Cargo.toml | head -n 1)"
[ "$version" = "$crate" ] || { echo "tag $tag does not match Cargo.toml version $crate" >&2; exit 1; }

# musl: one static Linux binary per architecture, whatever the distribution's libc.
targets="aarch64-apple-darwin x86_64-apple-darwin x86_64-unknown-linux-musl aarch64-unknown-linux-musl"
rm -f "$out"/speedread-*.tar.gz "$out"/speedread-*.mcpb "$out"/sha256.txt

stage="$(mktemp -d)"
trap 'rm -rf "$stage"' EXIT

for target in $targets; do
  # 11.0 is the oldest macOS on Apple Silicon; without it the linker stamps the SDK's version.
  MACOSX_DEPLOYMENT_TARGET=11.0 cargo-zigbuild zigbuild --release --locked --target "$target"
  mkdir -p "$stage/$target"
  install -m 755 "target/$target/release/speedread" "$stage/$target/speedread"
  tar czf "$out/speedread-$target.tar.gz" -C "$stage/$target" speedread
done

# Claude Desktop bundle (macOS arm64, as upstream ships it).
bundle="$stage/mcpb"
mkdir -p "$bundle/server"
install -m 755 "$stage/aarch64-apple-darwin/speedread" "$bundle/server/speedread"
convert docs/assets/logo-1024.png -resize 512x512 "$bundle/icon.png"
# Links in the bundle point at the tag, which exists whatever happens to branches.
python3 - "$version" "$tag" packaging/mcpb/manifest.json "$bundle/manifest.json" <<'PY'
import json, sys
version, tag, src, dst = sys.argv[1:]
m = json.load(open(src))
m["version"] = version
m["documentation"] = m["documentation"].replace("/blob/main/", f"/blob/{tag}/")
json.dump(m, open(dst, "w"), indent=2)
PY
npx -y @anthropic-ai/mcpb@2.1.2 validate "$bundle/manifest.json"
npx -y @anthropic-ai/mcpb@2.1.2 pack "$bundle" "$out/speedread-aarch64-apple-darwin.mcpb"

(cd "$out" && sha256sum speedread-*.tar.gz speedread-*.mcpb > sha256.txt && cat sha256.txt)

# Pin the plugin launcher to exactly these files.
python3 - "$version" "$out/sha256.txt" plugins/speedread-netbrah/skills/speedread/scripts/speedread.sh <<'PY'
import re, sys
version, sums, launcher = sys.argv[1:]
text = open(launcher).read()
text, n = re.subn(r'^VERSION=".*"$', f'VERSION="{version}"', text, count=1, flags=re.M)
assert n == 1, "VERSION line not found"
for line in open(sums):
    digest, name = line.split()
    m = re.fullmatch(r"speedread-(.+)\.tar\.gz", name)
    if not m:
        continue
    var = "SHA_" + m.group(1).replace("-", "_")
    text, n = re.subn(rf'^{var}=".*"$', f'{var}="{digest}"', text, count=1, flags=re.M)
    assert n == 1, f"{var} line not found"
open(launcher, "w").write(text)
PY
git diff --stat -- plugins/speedread-netbrah/skills/speedread/scripts/speedread.sh
