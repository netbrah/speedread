#!/usr/bin/env bash
# Build the fork's Claude Desktop bundle from a machine that is not a Mac.
#
#   scripts/fork-mcpb.sh v0.2.0-netbrah.1 [OUT_DIR]
#
# The release job in .github/workflows/ci.yml does this natively on macOS when
# Actions are enabled; this is the same recipe with the binary cross-compiled
# (cargo-zigbuild) and ImageMagick in place of sips. The result has not been
# run: say so in the release notes.
#
# Needs: cargo-zigbuild + zig, the aarch64-apple-darwin Rust target, jq or
# python3, ImageMagick (convert), npx.
set -euo pipefail

tag="${1:?usage: fork-mcpb.sh TAG [OUT_DIR]}"
out="$(mkdir -p "${2:-target/fork-release}" && cd "${2:-target/fork-release}" && pwd)"
cd "$(dirname "$0")/.."

version="${tag#v}"
crate="$(sed -n 's/^version = "\(.*\)"/\1/p' Cargo.toml | head -n 1)"
[ "$version" = "$crate" ] || { echo "tag $tag does not match Cargo.toml version $crate" >&2; exit 1; }

target=aarch64-apple-darwin
# The oldest macOS on Apple Silicon; without it the linker stamps the SDK's version.
MACOSX_DEPLOYMENT_TARGET=11.0 cargo-zigbuild zigbuild --release --locked --target "$target"
bin="target/$target/release/speedread"

stage="$(mktemp -d)"
trap 'rm -rf "$stage"' EXIT
mkdir -p "$stage/server"
install -m 755 "$bin" "$stage/server/speedread"
convert docs/assets/logo-1024.png -resize 512x512 "$stage/icon.png"
# Links in the bundle point at the tag, which exists whatever happens to branches.
python3 - "$version" "$tag" packaging/mcpb/manifest.json "$stage/manifest.json" <<'PY'
import json, sys
version, tag, src, dst = sys.argv[1:]
m = json.load(open(src))
m["version"] = version
m["documentation"] = m["documentation"].replace("/blob/main/", f"/blob/{tag}/")
json.dump(m, open(dst, "w"), indent=2)
PY

rm -f "$out"/speedread-$target.mcpb "$out"/speedread-$target.tar.gz "$out"/sha256.txt
npx -y @anthropic-ai/mcpb@2.1.2 validate "$stage/manifest.json"
npx -y @anthropic-ai/mcpb@2.1.2 pack "$stage" "$out/speedread-$target.mcpb"
tar czf "$out/speedread-$target.tar.gz" -C "$stage/server" speedread
(cd "$out" && sha256sum speedread-$target.tar.gz speedread-$target.mcpb > sha256.txt && cat sha256.txt)
