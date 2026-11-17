#!/usr/bin/env python3
"""Fork ship-gate measurements: wall time, peak RSS and output bytes per query.

usage: scripts/fork-eval.py LABEL [BINARY] [--runs N] [--queries FILE] [-- EXTRA_ARGS...]

Clones pinned reference repositories into target/eval-repos (once), runs every
query in evals/fork/queries.tsv from an unrelated working directory and writes
evals/fork/LABEL.tsv. EXTRA_ARGS are passed to the binary before the subcommand
(e.g. `-- --cache-mb 64`).
"""
import hashlib, os, statistics, subprocess, sys, tempfile, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPOS = {
    # name: (url, commit)
    "ripgrep": ("https://github.com/BurntSushi/ripgrep", "178cc027e283fd965cb72d0a7395b219eada170c"),
    "zod": ("https://github.com/colinhacks/zod", "19c40d2072288b4cafec851e9d056a72a88e5973"),
}
REPO_DIR = os.path.join(ROOT, "target", "eval-repos")


def ensure_repos():
    os.makedirs(REPO_DIR, exist_ok=True)
    for name, (url, commit) in REPOS.items():
        d = os.path.join(REPO_DIR, name)
        if os.path.isdir(os.path.join(d, ".git")):
            continue
        subprocess.run(["git", "init", "-q", d], check=True)
        subprocess.run(["git", "-C", d, "fetch", "-q", "--depth", "1", url, commit], check=True)
        subprocess.run(["git", "-C", d, "checkout", "-q", "FETCH_HEAD"], check=True)


def run(cmd, cwd):
    t = time.perf_counter()
    with tempfile.TemporaryFile() as out:
        p = subprocess.Popen(cmd, cwd=cwd, stdout=out, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
        _, status, ru = os.wait4(p.pid, 0)
        p.returncode = os.waitstatus_to_exitcode(status)
        wall = time.perf_counter() - t
        out.seek(0)
        data = out.read()
    return wall, ru.ru_maxrss, data, p.returncode


def main():
    args = sys.argv[1:]
    extra = []
    if "--" in args:
        i = args.index("--")
        args, extra = args[:i], args[i + 1:]
    runs, queries = 5, os.path.join(ROOT, "evals", "fork", "queries.tsv")
    pos = []
    it = iter(args)
    for a in it:
        if a == "--runs":
            runs = int(next(it))
        elif a == "--queries":
            queries = next(it)
        else:
            pos.append(a)
    label = pos[0]
    binary = os.path.abspath(pos[1]) if len(pos) > 1 else os.path.join(ROOT, "target", "fast", "speedread")
    ensure_repos()
    rows = []
    for line in open(queries):
        line = line.rstrip("\n")
        if not line or line.startswith("#"):
            continue
        qid, repo, *argv = line.split("\t")
        root = os.path.join(REPO_DIR, repo)
        cmd = [binary, "--root", root, *extra, *argv]
        walls, rss, data, code = [], [], b"", 0
        for _ in range(runs):
            w, r, data, code = run(cmd, "/")  # unrelated CWD on purpose
            walls.append(w)
            rss.append(r)
        rows.append((qid, repo, " ".join(argv), code, statistics.median(walls) * 1000,
                     statistics.median(rss), len(data), hashlib.sha256(data).hexdigest()[:12]))
    out = os.path.join(ROOT, "evals", "fork", label + ".tsv")
    with open(out, "w") as f:
        f.write("id\trepo\targs\texit\twall_ms\tpeak_rss_kb\tout_bytes\tout_sha\n")
        for r in rows:
            f.write("%s\t%s\t%s\t%d\t%.1f\t%d\t%d\t%s\n" % r)
    tw = sum(r[4] for r in rows)
    print(f"{label}: {len(rows)} queries, total median wall {tw:.0f} ms, max peak RSS {max(r[5] for r in rows) / 1024:.1f} MiB -> {out}")


if __name__ == "__main__":
    main()
