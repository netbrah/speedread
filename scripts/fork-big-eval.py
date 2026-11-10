#!/usr/bin/env python3
"""Cost of ordinary and worst-case requests on very large trees (spec 02, "Defaults").

usage: scripts/fork-big-eval.py REPO BINARY [EXTRA_ARGS...] [--session]
       e.g. ... linux target/fast/speedread --cache-mb 512

REPO is one of:
  linux     git clone --depth 1 https://github.com/torvalds/linux target/eval-repos/linux
            (about 97,000 files, 1.6 GB, C)
  chromium  the source tree of https://github.com/chromium/chromium at one commit, unpacked into
            target/eval-repos/chromium and `git init`ed so .gitignore applies (about 474,000
            files, 4.5 GB without images, media, fonts and archives; C++, Java, TS, Python)

Without --session: wall time (first run and best of three), CPU time and peak memory of each CLI
request. With --session: one MCP server answering 400 symbol reads, 40 searches and 20 traces
twice over; resident memory and the time of each pass.
"""
import json, os, re, resource, subprocess, sys, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPOS = {
    "linux": {
        "queries": [
            ["search", "kmalloc"],
            ["search", "-w", "schedule_timeout"],
            ["search", "struct file_operations", "--output", "files"],
            ["search", "zzz_not_there_zzz"],
            ["read", "#vfs_read"],
            ["trace", "vfs_read"],
            ["trace", "kmalloc"],
            ["trace", "mutex_lock", "--depth", "2"],
            ["map"],
            ["search", "--ast", "kfree($A)", "--lang", "c", "fs/ext4"],
            ["search", "--ast", "kfree($A)", "--lang", "c"],
            ["search", "--ast", "kfree($A, $B)", "--lang", "c"],
        ],
        "names": (r"^(?:static )?(?:int|void|long|ssize_t) ([a-z_0-9]+)\(", ["fs", "mm", "kernel"], "c"),
    },
    "chromium": {
        "queries": [
            ["search", "base::BindOnce"],
            ["search", "-w", "ScopedObservation"],
            ["search", "DCHECK_CURRENTLY_ON", "--output", "files"],
            ["search", "zzz_not_there_zzz"],
            ["search", "NavigationThrottle", "content/browser"],
            ["read", "#DidCommitNavigation"],
            ["read", "content/browser/renderer_host/render_frame_host_impl.cc#RenderFrameHostImpl::DidCommitNavigation"],
            ["trace", "DidCommitNavigation"],
            ["trace", "PostTask"],
            ["trace", "Run", "--depth", "2"],
            ["trace", "content/browser/renderer_host/render_frame_host_impl.cc#DidCommitNavigation", "--depth", "2"],
            ["map"],
            ["map", "content/browser"],
            ["search", "--ast", "base::BindOnce($$$A)", "--lang", "cpp", "content/browser/renderer_host"],
            ["search", "--ast", "base::BindOnce($$$A)", "--lang", "cpp"],
            ["search", "--ast", "std::make_unique<$T>()", "--lang", "cpp", "content"],
            ["search", "--ast", "zzz_not_there($A)", "--lang", "cpp"],
        ],
        "names": (r"^[A-Za-z_:<>*& ]+ [A-Z][A-Za-z0-9]+::([A-Z][A-Za-z0-9]+)\(", ["content/browser", "base", "net"], "cpp"),
    },
}
D = None


def cli(binary, extra, queries):
    for q in queries:
        runs = []
        for _ in range(3):
            before = resource.getrusage(resource.RUSAGE_CHILDREN)
            t = time.perf_counter()
            p = subprocess.Popen([binary, "--root", D, *extra, *q], cwd="/", stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            out = p.stdout.read()
            _, _, ru = os.wait4(p.pid, 0)
            runs.append(((time.perf_counter() - t) * 1000, ru.ru_maxrss // 1024, ru.ru_utime + ru.ru_stime))
            if runs[-1][0] > 6000:
                break
        head = out.split(b"\n", 1)[0].decode(errors="replace")[:78]
        note = re.search(rb"^(incomplete|not fully searched): [^\n]{0,60}", out, re.M)
        label = " ".join(a if len(a) < 40 else "…" + a[-38:] for a in q)
        print(f"{label:58} first {runs[0][0]:6.0f} ms  best {min(r[0] for r in runs):6.0f} ms  cpu {min(r[2] for r in runs):5.1f} s  "
              f"peak {max(r[1] for r in runs):4} MiB  {len(out):6} B | {head}"
              + (f" | {note.group(0).decode()}" if note else ""), flush=True)


def session(binary, extra, names):
    pattern, dirs, kind = names
    found = subprocess.run(["rg", "-oNI", "--no-heading", "-t", kind, "-r", "$1", pattern, *dirs],
                           cwd=D, capture_output=True, text=True).stdout.split("\n")
    found = sorted({n for n in found if n})
    step = max(1, len(found) // 400)
    reads = found[::step][:400]
    p = subprocess.Popen([binary, *extra, "--root", D, "mcp"], cwd="/",
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
                        "clientInfo": {"name": "fork-linux-eval", "version": "0"}})
    p.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
    p.stdin.flush()
    print(f"start {rss()} MiB; {len(reads)} symbols")
    for label in ("first pass", "repeat pass"):
        t = time.time()
        size = 0
        for name in reads:
            size += len(tool("read", {"targets": [f"#{name}"]}))
        a = time.time() - t
        t = time.time()
        for name in reads[::10]:
            size += len(tool("search", {"pattern": name}))
        b = time.time() - t
        t = time.time()
        for name in reads[::20]:
            size += len(tool("trace", {"target": name}))
        c = time.time() - t
        print(f"{label}: reads {a:.1f} s, searches {b:.1f} s, traces {c:.1f} s, {size} bytes -> {rss()} MiB")
    p.stdin.close()
    p.wait()


def main():
    args = sys.argv[1:]
    if len(args) < 2 or args[0] not in REPOS:
        sys.exit(__doc__)
    global D
    D = os.path.join(ROOT, "target", "eval-repos", args[0])
    if not os.path.isdir(D):
        sys.exit(__doc__)
    repo = REPOS[args[0]]
    do_session = "--session" in args
    args = [a for a in args[1:] if a != "--session"]
    binary, extra = os.path.abspath(args[0]), args[1:]
    if do_session:
        session(binary, extra, repo["names"])
    else:
        cli(binary, extra, repo["queries"])


if __name__ == "__main__":
    main()
