#!/usr/bin/env python3
"""Residency measurement for a long-lived MCP server (spec 02).

usage: scripts/fork-rss.py BINARY [EXTRA_ARGS...]     e.g. ... target/fast/speedread --cache-mb 128

Builds a synthetic tree (1,200 Rust files of ~200 KB, 2,600 small functions each; ~250 MiB)
in target/eval-big, then over one MCP session reads one symbol from every file twice and runs
one search and one trace. Prints resident memory and the time of both passes. The corpus is
deliberately hostile: outlines of files made of tiny functions are larger than the files.
"""
import json, os, subprocess, sys, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIG = os.path.join(ROOT, "target", "eval-big")
FILES = 1200


def corpus():
    src = os.path.join(BIG, "src")
    if os.path.isdir(src):
        return
    os.makedirs(src)
    body = "".join(
        f"pub fn helper_{i}(x: u64) -> u64 {{\n    let y = x.wrapping_mul({i});\n    y ^ 0x{i:x}\n}}\n\n"
        for i in range(2600)
    )
    for n in range(FILES):
        with open(os.path.join(src, f"m{n:04}.rs"), "w") as f:
            f.write(f"// module {n}\n" + body.replace("helper_", f"h{n}_"))
    subprocess.run(["git", "init", "-q", BIG], check=True)


def main():
    binary, extra = os.path.abspath(sys.argv[1]), sys.argv[2:]
    corpus()
    p = subprocess.Popen([binary, *extra, "--root", BIG, "mcp"], cwd="/",
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    n = 0

    def call(method, params):
        nonlocal n
        n += 1
        p.stdin.write((json.dumps({"jsonrpc": "2.0", "id": n, "method": method, "params": params}) + "\n").encode())
        p.stdin.flush()
        while True:
            m = json.loads(p.stdout.readline())
            if m.get("id") == n:
                return m

    def tool(name, args):
        return call("tools/call", {"name": name, "arguments": args})["result"]["content"][0]["text"]

    def rss():
        for line in open(f"/proc/{p.pid}/status"):
            if line.startswith("VmRSS"):
                return int(line.split()[1]) // 1024

    call("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                        "clientInfo": {"name": "fork-rss", "version": "0"}})
    p.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
    p.stdin.flush()

    def sweep():
        t, out = time.time(), 0
        for k in range(0, FILES, 20):
            out += len(tool("read", {"targets": [f"src/m{j:04}.rs#h{j}_7" for j in range(k, k + 20)]}))
        return time.time() - t, out

    t1, out1 = sweep()
    r1 = rss()
    t2, out2 = sweep()
    tool("search", {"pattern": "h77_1234"})
    tool("trace", {"target": "src/m0005.rs#h5_9"})
    print(f"{' '.join(extra) or 'default'}\tfirst pass {t1:.1f} s\trepeat pass {t2:.1f} s\t"
          f"rss {r1} MiB after reads, {rss()} MiB at end\tout {out1}+{out2} bytes")
    p.stdin.close()
    p.wait()


if __name__ == "__main__":
    main()
