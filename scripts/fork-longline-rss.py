#!/usr/bin/env python3
"""Memory of a text search over files with very long lines (spec 02, "Long lines").

usage: scripts/fork-longline-rss.py LABEL=BINARY [LABEL=BINARY...]
       e.g. ... upstream=target/eval-bin/baseline fork=target/fast/speedread

Builds target/eval-longlines: a 200 MiB minified bundle that is one line, a 200 MiB
JSON-lines file of 400 lines of 500 KB, and 50 small Rust files; every line contains
`needle`. Then, per binary: the peak memory and time of one CLI search, and the resident
memory of an MCP server before, after each of three searches, and idle afterwards.
"""
import json, os, subprocess, sys, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
D = os.path.join(ROOT, "target", "eval-longlines")


def corpus():
    if os.path.isdir(D):
        return
    os.makedirs(os.path.join(D, "src"))
    chunk = ("var a=function(){return needle(1)};" * 30000).encode()  # ~1 MiB, no newline
    with open(os.path.join(D, "bundle.min.js"), "wb") as f:
        for _ in range(200):
            f.write(chunk)
        f.write(b"\n")
    line = b'{"k":"needle","v":"' + b"x" * 500000 + b'"}\n'
    with open(os.path.join(D, "data.jsonl"), "wb") as f:
        for _ in range(400):
            f.write(line)
    for i in range(50):
        with open(os.path.join(D, "src", f"m{i}.rs"), "w") as f:
            f.write(f"pub fn f{i}() -> u32 {{\n    needle({i})\n}}\n")
    subprocess.run(["git", "init", "-q", D], check=True)


def cli(binary, args):
    t = time.perf_counter()
    p = subprocess.Popen([binary, "--root", D, *args], cwd="/", stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    out = p.stdout.read()
    _, _, ru = os.wait4(p.pid, 0)
    head = out.split(b"\n", 1)[0].decode()
    tail = out.rstrip(b"\n").rsplit(b"\n", 1)[-1].decode()[:110]
    return f"{(time.perf_counter() - t) * 1000:.0f} ms, peak {ru.ru_maxrss // 1024} MiB, {len(out)} bytes out | {head} | {tail}"


def mcp(binary, calls):
    p = subprocess.Popen([binary, "--root", D, "mcp"], cwd="/",
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

    def rss():
        for line in open(f"/proc/{p.pid}/status"):
            if line.startswith("VmRSS"):
                return int(line.split()[1]) // 1024

    call("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                        "clientInfo": {"name": "fork-longline-rss", "version": "0"}})
    p.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
    p.stdin.flush()
    res = [f"start {rss()} MiB"]
    for name, args in calls:
        t = time.time()
        r = call("tools/call", {"name": name, "arguments": args})
        txt = r.get("result", {}).get("content", [{}])[0].get("text", str(r))
        res.append(f"{name} {json.dumps(args)} -> {rss()} MiB ({time.time() - t:.1f} s, {len(txt)} bytes)")
    time.sleep(1)
    res.append(f"idle {rss()} MiB")
    p.stdin.close()
    p.wait()
    return res


def main():
    corpus()
    for spec in sys.argv[1:]:
        label, binary = spec.split("=", 1)
        binary = os.path.abspath(binary)
        print(f"{label}: CLI search needle: {cli(binary, ['search', 'needle'])}")
        print(f"{label}: CLI search needle src: {cli(binary, ['search', 'needle', 'src'])}")
        calls = [("search", {"pattern": "needle"}), ("search", {"pattern": "needle", "path": "src"}),
                 ("search", {"pattern": "needle"})]
        for line in mcp(binary, calls):
            print(f"   {label} MCP {line}")


if __name__ == "__main__":
    main()
