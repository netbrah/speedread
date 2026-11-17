#!/bin/sh
# Launcher for the netbrah fork of speedread: runs the pinned release for this
# machine, fetching it once. Plugins are text; this is how the binary arrives.
#
#   speedread.sh mcp --unrestricted --no-cwd-root     (what the plugin's MCP config runs)
#   speedread.sh search 'TODO' src                    (the CLI, same arguments as speedread)
#
# Order: $SPEEDREAD_BIN if set; else the cached copy of the pinned version; else
# download that version from the fork's GitHub release, check its SHA-256
# against the value below, and cache it; else a `speedread` on PATH that
# reports this fork and version. Nothing here writes to stdout, which belongs
# to the MCP session.
#
# Environment:
#   SPEEDREAD_BIN          run this binary; no download, no checks
#   SPEEDREAD_PLUGIN_DATA  cache directory (hosts pass their plugin data directory)
#   SPEEDREAD_NO_DOWNLOAD  if set, never use the network
#   SPEEDREAD_RELEASE_URL  fetch speedread-<target>.tar.gz from this base URL instead
#                          (a mirror); the checksum below still has to match
set -eu

# Pinned by scripts/fork-release.sh; do not edit by hand.
VERSION="0.0.0"
SHA_aarch64_apple_darwin=""
SHA_x86_64_apple_darwin=""
SHA_x86_64_unknown_linux_musl=""
SHA_aarch64_unknown_linux_musl=""
# End of pin.

say() { printf 'speedread launcher: %s\n' "$*" >&2; }
die() { say "$*"; exit 1; }

if [ -n "${SPEEDREAD_BIN:-}" ]; then
  [ -x "$SPEEDREAD_BIN" ] || die "SPEEDREAD_BIN is set to '$SPEEDREAD_BIN', which is not an executable file"
  exec "$SPEEDREAD_BIN" "$@"
fi

case "$(uname -s)" in
  Darwin) os=apple-darwin ;;
  Linux) os=unknown-linux-musl ;;
  *) os= ;;
esac
case "$(uname -m)" in
  arm64 | aarch64) arch=aarch64 ;;
  x86_64 | amd64) arch=x86_64 ;;
  *) arch= ;;
esac
target="$arch-$os"
case "$target" in
  aarch64-apple-darwin) want=$SHA_aarch64_apple_darwin ;;
  x86_64-apple-darwin) want=$SHA_x86_64_apple_darwin ;;
  x86_64-unknown-linux-musl) want=$SHA_x86_64_unknown_linux_musl ;;
  aarch64-unknown-linux-musl) want=$SHA_aarch64_unknown_linux_musl ;;
  *) want= ;;
esac

# A host that does not expand its data-directory variable passes the literal `${...}`.
data="${SPEEDREAD_PLUGIN_DATA:-}"
# shellcheck disable=SC2016
case "$data" in
  '' | *'${'*) data="${CLAUDE_PLUGIN_DATA:-${GROK_PLUGIN_DATA:-${PLUGIN_DATA:-}}}" ;;
esac
[ -n "$data" ] || data="${XDG_CACHE_HOME:-${HOME:-/tmp}/.cache}/speedread-netbrah"
dir="$data/$VERSION"
bin="$dir/speedread"

[ -x "$bin" ] && exec "$bin" "$@"

# Why the pinned binary could not be fetched, for the message if nothing else works.
why=
sha256_of() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | cut -d ' ' -f 1
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$1" | cut -d ' ' -f 1
  elif command -v openssl >/dev/null 2>&1; then
    openssl dgst -sha256 -r "$1" | cut -d ' ' -f 1
  else
    return 1
  fi
}

fetch() {
  url="${SPEEDREAD_RELEASE_URL:-https://github.com/netbrah/speedread/releases/download/v$VERSION}/speedread-$target.tar.gz"
  tmp="$dir/.fetch.$$"
  mkdir -p "$tmp" 2>/dev/null || { why="cannot create $dir"; return 1; }
  trap 'rm -rf "$tmp"' EXIT INT TERM
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL --retry 2 --connect-timeout 15 -o "$tmp/a.tar.gz" "$url" 2>/dev/null \
      || { why="download failed: $url"; return 1; }
  elif command -v wget >/dev/null 2>&1; then
    wget -q -T 15 -O "$tmp/a.tar.gz" "$url" 2>/dev/null \
      || { why="download failed: $url"; return 1; }
  else
    why="neither curl nor wget is installed"
    return 1
  fi
  got="$(sha256_of "$tmp/a.tar.gz")" || { why="no sha256sum, shasum or openssl to check the download"; return 1; }
  if [ "$got" != "$want" ]; then
    why="checksum mismatch for $url (expected $want, got $got); nothing was installed"
    return 1
  fi
  tar -xzf "$tmp/a.tar.gz" -C "$tmp" speedread 2>/dev/null || { why="could not unpack the download"; return 1; }
  chmod 755 "$tmp/speedread"
  # rename is atomic: a second session starting now sees the old state or the whole binary.
  mv -f "$tmp/speedread" "$bin" || { why="cannot write $bin"; return 1; }
  rm -rf "$tmp"
  trap - EXIT INT TERM
}

if [ -z "$want" ]; then
  why="no build of $VERSION for $(uname -s) $(uname -m) (macOS and Linux, arm64 and x86_64, are built)"
elif [ -n "${SPEEDREAD_NO_DOWNLOAD:-}" ]; then
  why="SPEEDREAD_NO_DOWNLOAD is set and $bin does not exist"
elif fetch; then
  exec "$bin" "$@"
fi

# An installed copy is acceptable only if it is this fork at this version: the
# plugin's arguments (`--no-cwd-root`, `base_dir`) do not exist upstream.
if command -v speedread >/dev/null 2>&1 && [ "$(speedread --version 2>/dev/null)" = "speedread $VERSION" ]; then
  exec speedread "$@"
fi

die "$why. Install speedread $VERSION from https://github.com/netbrah/speedread/releases/tag/v$VERSION and set SPEEDREAD_BIN to its path."
