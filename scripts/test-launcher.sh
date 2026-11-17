#!/bin/sh
# Tests for the plugin launcher, with a stub in place of the binary and a
# file:// mirror in place of GitHub, so it runs anywhere in a second.
#
#   scripts/test-launcher.sh            run under sh
#   SH=dash scripts/test-launcher.sh    run the launcher under another shell
set -eu
cd "$(dirname "$0")/.."
launcher=plugins/speedread-netbrah/skills/speedread/scripts/speedread.sh
shell="${SH:-sh}"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

case "$(uname -s)" in Darwin) os=apple-darwin ;; *) os=unknown-linux-musl ;; esac
case "$(uname -m)" in arm64 | aarch64) arch=aarch64 ;; *) arch=x86_64 ;; esac
target="$arch-$os"
var="SHA_$(printf %s "$target" | tr - _)"

# A stub release: `speedread` prints its arguments.
mkdir -p "$work/mirror" "$work/stub"
printf '#!/bin/sh\necho "stub $*"\n' > "$work/stub/speedread"
chmod 755 "$work/stub/speedread"
tar -czf "$work/mirror/speedread-$target.tar.gz" -C "$work/stub" speedread
if command -v sha256sum >/dev/null 2>&1; then
  sum="$(sha256sum "$work/mirror/speedread-$target.tar.gz" | cut -d ' ' -f 1)"
else
  sum="$(shasum -a 256 "$work/mirror/speedread-$target.tar.gz" | cut -d ' ' -f 1)"
fi
pin() { sed -e 's/^VERSION=".*"$/VERSION="9.9.9"/' -e "s/^$var=\".*\"\$/$var=\"$1\"/" "$launcher" > "$2"; }
pin "$sum" "$work/good.sh"
pin 0000000000000000000000000000000000000000000000000000000000000000 "$work/bad.sh"

n=0
fail=0
# check NAME EXPECTED_STATUS EXPECTED_STDOUT STDERR_SUBSTRING -- command...
check() {
  name=$1 want_status=$2 want_out=$3 want_err=$4
  shift 5
  n=$((n + 1))
  status=0
  out="$("$@" 2> "$work/err")" || status=$?
  err="$(cat "$work/err")"
  if [ "$status" != "$want_status" ] || [ "$out" != "$want_out" ]; then
    fail=$((fail + 1))
    echo "FAIL $name: status $status (want $want_status), stdout '$out' (want '$want_out'), stderr '$err'"
  elif [ -n "$want_err" ] && ! printf %s "$err" | grep -qF -- "$want_err"; then
    fail=$((fail + 1))
    echo "FAIL $name: stderr '$err' lacks '$want_err'"
  else
    echo "ok   $name"
  fi
}
run() { env -i PATH="$PATH" HOME="$work/home" "$@"; }
mirror="file://$work/mirror"

check "first run downloads, verifies and runs" 0 "stub mcp --x" "" -- \
  run SPEEDREAD_PLUGIN_DATA="$work/data" SPEEDREAD_RELEASE_URL="$mirror" "$shell" "$work/good.sh" mcp --x
[ -x "$work/data/9.9.9/speedread" ] || { fail=$((fail + 1)); echo "FAIL binary not cached under the version"; }
[ "$(find "$work/data/9.9.9" -mindepth 1 | wc -l | tr -d ' ')" = 1 ] || { fail=$((fail + 1)); echo "FAIL temporary files left in the cache"; }

check "second run uses the cache, not the network" 0 "stub map" "" -- \
  run SPEEDREAD_PLUGIN_DATA="$work/data" SPEEDREAD_RELEASE_URL="file:///nonexistent" "$shell" "$work/good.sh" map

check "a checksum mismatch installs nothing and keeps stdout empty" 1 "" "checksum mismatch" -- \
  run SPEEDREAD_PLUGIN_DATA="$work/data-bad" SPEEDREAD_RELEASE_URL="$mirror" "$shell" "$work/bad.sh" mcp
[ ! -e "$work/data-bad/9.9.9/speedread" ] || { fail=$((fail + 1)); echo "FAIL a mismatched download was installed"; }

check "a failed download says which URL" 1 "" "download failed: file:///nonexistent/speedread-$target.tar.gz" -- \
  run SPEEDREAD_PLUGIN_DATA="$work/data-none" SPEEDREAD_RELEASE_URL="file:///nonexistent" "$shell" "$work/good.sh" mcp

check "SPEEDREAD_NO_DOWNLOAD forbids the network" 1 "" "SPEEDREAD_NO_DOWNLOAD is set" -- \
  run SPEEDREAD_PLUGIN_DATA="$work/data-off" SPEEDREAD_NO_DOWNLOAD=1 SPEEDREAD_RELEASE_URL="$mirror" "$shell" "$work/good.sh" mcp

check "SPEEDREAD_BIN wins" 0 "stub read a" "" -- \
  run SPEEDREAD_BIN="$work/stub/speedread" SPEEDREAD_NO_DOWNLOAD=1 "$shell" "$work/good.sh" read a

check "a missing SPEEDREAD_BIN is an error, not a fallback" 1 "" "not an executable file" -- \
  run SPEEDREAD_BIN="$work/nope" SPEEDREAD_RELEASE_URL="$mirror" "$shell" "$work/good.sh" read a

# shellcheck disable=SC2016
check "an unexpanded host variable falls back to the user cache" 0 "stub mcp" "" -- \
  run SPEEDREAD_PLUGIN_DATA='${CLAUDE_PLUGIN_DATA}' SPEEDREAD_RELEASE_URL="$mirror" "$shell" "$work/good.sh" mcp
[ -x "$work/home/.cache/speedread-netbrah/9.9.9/speedread" ] || { fail=$((fail + 1)); echo "FAIL fallback cache not used"; }

check "the host's own data directory is used when ours is unset" 0 "stub mcp" "" -- \
  run GROK_PLUGIN_DATA="$work/grok" SPEEDREAD_RELEASE_URL="$mirror" "$shell" "$work/good.sh" mcp
[ -x "$work/grok/9.9.9/speedread" ] || { fail=$((fail + 1)); echo "FAIL GROK_PLUGIN_DATA not used"; }

# PATH fallback: only this fork at this version.
# shellcheck disable=SC2016
{
mkdir -p "$work/path-ok" "$work/path-other"
printf '#!/bin/sh\n[ "$1" = --version ] && { echo "speedread 9.9.9"; exit 0; }\necho "path $*"\n' > "$work/path-ok/speedread"
printf '#!/bin/sh\n[ "$1" = --version ] && { echo "speedread 0.1.0"; exit 0; }\necho "path $*"\n' > "$work/path-other/speedread"
}
chmod 755 "$work/path-ok/speedread" "$work/path-other/speedread"
check "a matching speedread on PATH is used when the download fails" 0 "path mcp" "" -- \
  env -i PATH="$work/path-ok:$PATH" HOME="$work/home2" SPEEDREAD_NO_DOWNLOAD=1 "$shell" "$work/good.sh" mcp
check "a different speedread on PATH is refused" 1 "" "Install speedread 9.9.9" -- \
  env -i PATH="$work/path-other:$PATH" HOME="$work/home3" SPEEDREAD_NO_DOWNLOAD=1 "$shell" "$work/good.sh" mcp

echo "$((n - fail)) of $n launcher checks passed ($shell, $target)"
[ "$fail" = 0 ]
