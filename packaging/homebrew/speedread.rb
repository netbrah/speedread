# Homebrew formula for the netbrah fork of speedread.
# Upstream's tap is still https://github.com/brennengreen/homebrew-tap
# (`brew install brennengreen/tap/speedread`); that builds Brennen Green's
# repository, not this one. A tap for this fork would install with
# `brew install netbrah/tap/speedread`.
# Builds from source, so no notarization is needed.
#
# Name collision: homebrew-core already has an unrelated `speedread` formula
# (pasky/speedread, an RSVP speed-reading tool), and both install
# bin/speedread. Plain `brew install speedread` installs that one, so document
# the fully qualified command.
class Speedread < Formula
  desc "Token-efficient code search and navigation for AI coding agents (MCP)"
  homepage "https://github.com/netbrah/speedread"
  url "https://github.com/netbrah/speedread/archive/refs/tags/v0.2.0-netbrah.3.tar.gz"
  sha256 "b490df9f413b2603632f1a6f2702cc204d208cea53bf2a1b04653bc57eac2d65"
  license "MIT"
  head "https://github.com/netbrah/speedread.git", branch: "main"

  depends_on "rust" => :build

  def install
    system "cargo", "install", *std_cargo_args
  end

  test do
    (testpath/"a.rs").write("fn main() {\n    println!(\"hi\");\n}\n")
    assert_match "fn main()", shell_output("#{bin}/speedread --root #{testpath} read a.rs#main")
  end
end
