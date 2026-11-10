#!/usr/bin/env python3
"""Ship-gate measurement for structural search (docs/specs/04-structural-search.md).

usage: scripts/fork-ast-eval.py [BINARY] [--runs N] [--show]

For each shape: the lines a regex an agent would write finds, the lines the
structural pattern finds, what the regex-only lines are (comment, string,
`macro_rules!` body, other code), and median wall time of the structural
search against the literal search for the same text, at the default budget
and made to group every hit. Runs from an unrelated working directory
against the pinned repositories of scripts/fork-eval.py.
"""
import json, os, re, statistics, subprocess, sys, tempfile, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPOS = os.path.join(ROOT, "target", "eval-repos")
BIG = "100000000"

# id, repo, glob, structural pattern, regex, literal
SHAPES = [
    ("unwrap", "ripgrep", "*.rs", "$V.unwrap()", r"\.unwrap\(\)", ".unwrap()"),
    ("expect-tobe", "zod", "*.ts", "expect($A).toBe($B)", r"expect\(.*\)\.toBe\(", "expect("),
    ("console-log", "zod", "*.{ts,tsx,js,mjs}", "console.log($$$A)", r"console\.log\(", "console.log("),
    # C, on a large tree (optional: git clone --depth 1 https://github.com/torvalds/linux target/eval-repos/linux).
    ("c-kfree", "linux", "fs/**/*.c", "kfree($A)", r"\bkfree\(", "kfree("),
    ("c-kmalloc", "linux", "drivers/net/**/*.c", "kmalloc($S, $F)", r"\bkmalloc\(", "kmalloc("),
    ("c-field-null", "linux", "mm/**/*.c", "$P->$F == NULL", r"->\w+ == NULL", "== NULL"),
]


def ast_hits(binary, repo, glob, pattern):
    """(path, line) -> text for every structural hit (at most 400 a file)."""
    cmd = [binary, "--root", os.path.join(REPOS, repo), "search", "-g", glob, "--json",
           "--budget", BIG, "--max-budget", BIG, "--ast", pattern]
    out = subprocess.run(cmd, cwd=tempfile.gettempdir(), capture_output=True, text=True).stdout
    found = {}
    for line in out.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if "path" in row and "line" in row:
            found[(row["path"], row["line"])] = re.split(r"\t\$\w+=", row.get("text", ""))[0]
    return found


def regex_hits(repo, glob, regex):
    """(path, line) -> text for every line of a tracked file the regex matches."""
    root = os.path.join(REPOS, repo)
    files = subprocess.run(["git", "-C", root, "ls-files"], capture_output=True, text=True).stdout.split("\n")
    exts = tuple("." + e for e in glob.rsplit(".", 1)[1].strip("{}").split(","))
    under = glob.split("*", 1)[0]
    rx = re.compile(regex)
    found = {}
    for f in files:
        if not f.endswith(exts) or not f.startswith(under):
            continue
        try:
            text = open(os.path.join(root, f), encoding="utf-8").read()
        except (OSError, UnicodeDecodeError):
            continue
        for n, line in enumerate(text.split("\n"), 1):
            if rx.search(line):
                found[(f, n)] = line
    return found


def wall(binary, repo, glob, extra, runs):
    cmd = [binary, "--root", os.path.join(REPOS, repo), "search", "-g", glob] + extra
    times = []
    for _ in range(runs):
        t = time.perf_counter()
        subprocess.run(cmd, cwd=tempfile.gettempdir(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        times.append((time.perf_counter() - t) * 1000)
    return statistics.median(times)


def regions(text):
    """Line numbers inside block comments and inside `macro_rules!` bodies."""
    comment, macro = set(), set()
    in_comment, depth, in_macro = False, 0, False
    for n, line in enumerate(text.split("\n"), 1):
        if in_comment:
            comment.add(n)
            if "*/" in line:
                in_comment = False
            continue
        if "/*" in line and "*/" not in line.split("/*", 1)[1]:
            in_comment = True
            if line.lstrip().startswith("/*"):
                comment.add(n)
        if not in_macro and "macro_rules!" in line:
            in_macro, depth = True, 0
        if in_macro:
            macro.add(n)
            depth += line.count("{") - line.count("}")
            if depth <= 0 and "{" in line or depth < 0:
                in_macro = False
    return comment, macro


def kind(text, regex, line, comment, macro):
    """Why a regex-only line is not the code shape."""
    s = text.lstrip()
    if line in comment or s.startswith(("//", "/*", "*", "#")):
        return "comment"
    m = re.search(regex, text)
    before = text[: m.start()] if m else text
    if "//" in before:
        return "comment"
    for q in "\"'`":
        if before.count(q) % 2 == 1:
            return "string"
    if line in macro:
        return "macro_def"
    return "other"


def main():
    args = sys.argv[1:]
    show = "--show" in args
    args = [a for a in args if a != "--show"]
    runs = 5
    if "--runs" in args:
        i = args.index("--runs")
        runs = int(args[i + 1])
        del args[i : i + 2]
    binary = os.path.abspath(args[0]) if args else os.path.join(ROOT, "target", "fast", "speedread")
    print("shape\trepo\tregex_lines\tast_lines\tboth\tregex_only\tcomment\tstring\tmacro_def\tother\tast_only\tast_ms\tliteral_ms\tgrouped_ms")
    for sid, repo, glob, pattern, regex, literal in SHAPES:
        if not os.path.isdir(os.path.join(REPOS, repo)):
            print(f"{sid}\t{repo}\t(repository not cloned; skipped)")
            continue
        rx = regex_hits(repo, glob, regex)
        ast = ast_hits(binary, repo, glob, pattern)
        # JSON output stops at 400 hits a file: compare such a file only up
        # to the last line it reported.
        per_file = {}
        for path, line in ast:
            per_file.setdefault(path, []).append(line)
        cut = {p: max(ls) for p, ls in per_file.items() if len(ls) >= 400}
        rx = {k: v for k, v in rx.items() if k[0] not in cut or k[1] <= cut[k[0]]}
        only = sorted(set(rx) - set(ast))
        kinds = {"comment": 0, "string": 0, "macro_def": 0, "other": 0}
        why = {}
        cache = {}
        for key in only:
            if key[0] not in cache:
                cache[key[0]] = regions(open(os.path.join(REPOS, repo, key[0]), encoding="utf-8").read())
            why[key] = kind(rx[key], regex, key[1], *cache[key[0]])
            kinds[why[key]] += 1
        ast_only = sorted(set(ast) - set(rx))
        a = wall(binary, repo, glob, ["--ast", pattern], runs)
        l = wall(binary, repo, glob, ["-F", literal], runs)
        # The literal search made to do the same work: group every hit under
        # its symbol, which parses every file that has one.
        g = wall(binary, repo, glob, ["-F", literal, "--output", "symbols", "--budget", BIG, "--max-budget", BIG], runs)
        print("\t".join(str(x) for x in [
            sid, repo, len(rx), len(ast), len(set(rx) & set(ast)), len(only),
            kinds["comment"], kinds["string"], kinds["macro_def"], kinds["other"], len(ast_only),
            f"{a:.0f}", f"{l:.0f}", f"{g:.0f}"]))
        if show:
            for key in only:
                if why[key] == "other":
                    print(f"  regex-only other  {key[0]}:{key[1]}  {rx[key].strip()[:110]}")
            for key in ast_only:
                print(f"  ast-only          {key[0]}:{key[1]}  {ast[key].strip()[:110]}")


if __name__ == "__main__":
    main()
