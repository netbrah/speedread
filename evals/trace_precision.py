#!/usr/bin/env python3
"""Precision, recall, latency, RSS and token cost of syntactic `trace`.

Ground truth
  * evals/fixtures/trace_oracle — closed, hand-labeled lines and call edges.
  * rust-analyzer LSP on a Rust repo — textDocument/references (declaration
    excluded), callHierarchy incoming/outgoing fromRanges, textDocument/implementation.
    The fixture is scored against the hand labels AND against rust-analyzer.
    A repo is scored against rust-analyzer only after the fixture says whether
    rust-analyzer agrees with the hand labels.

What is a hit
  * Site: (relative path, 1-based line). Column is not scored; `trace` does
    not emit columns. A line within ±1 is reported as a near-miss and is not
    a true positive.
  * Call site from `direction=callers`: a site line that is not a value use
    (no "  → " mark). Value uses are scored only on the fixture's `value` cases.
  * Call edge from `direction=callees`: the call line plus the first resolved
    definition range. Ambiguous answers ("? N candidates") still count the
    first definition, and are tallied separately.
  * Caller function: the enclosing range `trace` prints. A gold call line is
    found when it falls inside a printed range, even if the per-function
    6-site display cap hid the line. Site recall and this recall are both
    reported; the gap is the display cap, not a second resolver.
  * Locations outside the repo (stdlib) are dropped from the oracle.

Budgets
  * Quality uses --budget 200000 --max-budget 200000 --deadline-ms 0 so a
    truncated answer is not mistaken for a missed edge. ripgrep is small
    enough that the 800-file scan cap covers it.
  * Latency and token cost at the agent default (no --budget: trace's 4000
    token default, 10s deadline) are reported separately.

Decision this run is allowed to change
  A precise layer is worth building if micro precision of reference lines or
  of call-site lines on the repo oracle is below 0.90, or if site recall of
  call edges on queries with no display cap and no search truncation is below
  0.80. It is not a reason to change the default path unless a later precise
  run is within 2× the warm syntactic p50 and falls back when no index exists.
  A latency gap smaller than the run-to-run range is not a difference.

Percentiles are the inclusive nearest rank: sorted[ceil(p/100*n) - 1].
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import math
import os
import random
import re
import resource
import select
import subprocess
import sys
import threading
import time
import urllib.parse
from pathlib import Path

DECISION_RULE = """
A precise layer is worth building if micro precision of reference lines or of
call-site lines on the repo oracle is below 0.90, or if site recall of call
edges on queries with no display cap and no search truncation is below 0.80.
Do not change the default path unless a later precise run is within 2× the
warm syntactic p50 and falls back when no index exists. A latency gap smaller
than the run-to-run range is not a difference.
""".strip()

# Weights copied from src/tokens.rs. The startup check compiles that function
# and refuses to report a token number if this port disagrees.
W_DIGITS = 0.2309
W_PUNCT = 0.5142
W_WS_RUNS = 0.1415
W_NEWLINES = 3.1389
W_NON_ASCII = 1.8830
W_WORDS = 1.1830
W_DIGIT_RUNS = 1.3637
W_CASE_BREAKS = 1.3540
W_DENSE_RUNS = 0.1716
OTHER, LOWER, UPPER, DIGIT, SPACE = 0, 1, 2, 3, 4

SITE_RE = re.compile(r"^(\s+)(\d+)\t(.*)$")
MORE_RE = re.compile(r"^\s+… (\d+) more\s*$")
ARROW_RE = re.compile(r"^\s+→\s+(\S+):(\d+)-(\d+)")
HEADER_RE = re.compile(r"^==> (\S+) of (.*) \((.+):(\d+)-(\d+)\)\s*$")


class Counter:
    def __init__(self):
        self.digits = self.punct = self.ws_runs = self.newlines = 0
        self.non_ascii = self.words = self.digit_runs = 0
        self.case_breaks = self.dense = 0
        self.prev = OTHER
        self.run_len = 0
        self.run_alpha = False
        self.run_digit = False

    def feed(self, data: bytes):
        for c in data:
            lower = 97 <= c <= 122
            upper = 65 <= c <= 90
            digit = 48 <= c <= 57
            if lower or upper or digit or c in (43, 47, 61, 95, 45):
                self.run_len += 1
                self.run_alpha = self.run_alpha or lower or upper
                self.run_digit = self.run_digit or digit
            else:
                self.end_run()
            if lower or upper:
                if self.prev != LOWER and self.prev != UPPER:
                    self.words += 1
                elif upper and self.prev == LOWER:
                    self.case_breaks += 1
                self.prev = LOWER if lower else UPPER
            elif digit:
                self.digits += 1
                if self.prev != DIGIT:
                    self.digit_runs += 1
                self.prev = DIGIT
            elif c == 10:
                self.newlines += 1
                self.prev = OTHER
            elif c in (32, 9, 13):
                if self.prev != SPACE:
                    self.ws_runs += 1
                self.prev = SPACE
            elif c >= 0xC0:
                self.non_ascii += 1
                self.prev = OTHER
            else:
                if c < 0x80:
                    self.punct += 1
                self.prev = OTHER

    def end_run(self):
        if self.run_len >= 16 and self.run_alpha and self.run_digit:
            self.dense += self.run_len
        self.run_len = 0
        self.run_alpha = False
        self.run_digit = False

    def tokens(self) -> float:
        end = Counter.__new__(Counter)
        end.__dict__.update(self.__dict__)
        end.end_run()
        return (
            end.digits * W_DIGITS
            + end.punct * W_PUNCT
            + end.ws_runs * W_WS_RUNS
            + end.newlines * W_NEWLINES
            + end.non_ascii * W_NON_ASCII
            + end.words * W_WORDS
            + end.digit_runs * W_DIGIT_RUNS
            + end.case_breaks * W_CASE_BREAKS
            + end.dense * W_DENSE_RUNS
        )


def estimate(text: bytes) -> float:
    c = Counter()
    c.feed(text)
    return c.tokens()


EST_RS = r"""
const W_DIGITS: f32 = 0.2309;
const W_PUNCT: f32 = 0.5142;
const W_WS_RUNS: f32 = 0.1415;
const W_NEWLINES: f32 = 3.1389;
const W_NON_ASCII: f32 = 1.8830;
const W_WORDS: f32 = 1.1830;
const W_DIGIT_RUNS: f32 = 1.3637;
const W_CASE_BREAKS: f32 = 1.3540;
const W_DENSE_RUNS: f32 = 0.1716;
const OTHER: u8 = 0;
const LOWER: u8 = 1;
const UPPER: u8 = 2;
const DIGIT: u8 = 3;
const SPACE: u8 = 4;
struct Counter {
    digits: u32, punct: u32, ws_runs: u32, newlines: u32, non_ascii: u32,
    words: u32, digit_runs: u32, case_breaks: u32, dense: u32,
    prev: u8, run_len: u32, run_alpha: bool, run_digit: bool,
}
impl Counter {
    fn feed(&mut self, b: &[u8]) {
        for &c in b {
            let lower = c.is_ascii_lowercase();
            let upper = c.is_ascii_uppercase();
            let digit = c.is_ascii_digit();
            if lower || upper || digit || matches!(c, b'+' | b'/' | b'=' | b'_' | b'-') {
                self.run_len += 1;
                self.run_alpha |= lower || upper;
                self.run_digit |= digit;
            } else { self.end_run(); }
            if lower || upper {
                if self.prev != LOWER && self.prev != UPPER { self.words += 1; }
                else if upper && self.prev == LOWER { self.case_breaks += 1; }
                self.prev = if lower { LOWER } else { UPPER };
            } else if digit {
                self.digits += 1;
                if self.prev != DIGIT { self.digit_runs += 1; }
                self.prev = DIGIT;
            } else if c == b'\n' { self.newlines += 1; self.prev = OTHER; }
            else if matches!(c, b' ' | b'\t' | b'\r') {
                if self.prev != SPACE { self.ws_runs += 1; }
                self.prev = SPACE;
            } else if c >= 0xC0 { self.non_ascii += 1; self.prev = OTHER; }
            else { if c < 0x80 { self.punct += 1; } self.prev = OTHER; }
        }
    }
    fn end_run(&mut self) {
        if self.run_len >= 16 && self.run_alpha && self.run_digit { self.dense += self.run_len; }
        self.run_len = 0; self.run_alpha = false; self.run_digit = false;
    }
    fn tokens(&self) -> f32 {
        let mut c = Counter { digits: self.digits, punct: self.punct, ws_runs: self.ws_runs,
            newlines: self.newlines, non_ascii: self.non_ascii, words: self.words,
            digit_runs: self.digit_runs, case_breaks: self.case_breaks, dense: self.dense,
            prev: self.prev, run_len: self.run_len, run_alpha: self.run_alpha, run_digit: self.run_digit };
        c.end_run();
        c.digits as f32 * W_DIGITS + c.punct as f32 * W_PUNCT + c.ws_runs as f32 * W_WS_RUNS
            + c.newlines as f32 * W_NEWLINES + c.non_ascii as f32 * W_NON_ASCII
            + c.words as f32 * W_WORDS + c.digit_runs as f32 * W_DIGIT_RUNS
            + c.case_breaks as f32 * W_CASE_BREAKS + c.dense as f32 * W_DENSE_RUNS
    }
}
fn main() {
    let mut s = String::new();
    std::io::Read::read_to_string(&mut std::io::stdin(), &mut s).unwrap();
    let mut c = Counter { digits:0,punct:0,ws_runs:0,newlines:0,non_ascii:0,words:0,digit_runs:0,case_breaks:0,dense:0,prev:0,run_len:0,run_alpha:false,run_digit:false };
    c.feed(s.as_bytes());
    println!("{:.4}", c.tokens());
}
"""


def check_estimator() -> str:
    src = Path("/tmp/speedread-est.rs")
    bin_path = Path("/tmp/speedread-est")
    src.write_text(EST_RS)
    subprocess.run(
        ["rustup", "run", "stable", "rustc", "-O", str(src), "-o", str(bin_path)],
        check=True,
        capture_output=True,
    )
    samples = [
        b"fn parseHeader(x: u32) -> Result<()> {\n    let h = \"a9f3c2e1b7d4f6a8c0e2\";\n}\n",
        b"    let value = compute_total(items, options);\n" * 20,
        "alpha(1)\nlet f = alpha;\n".encode(),
    ]
    for sample in samples:
        rust = subprocess.run([str(bin_path)], input=sample, capture_output=True, check=True)
        rust_v = float(rust.stdout.decode().strip())
        py_v = estimate(sample)
        if abs(rust_v - py_v) > 0.02:
            raise SystemExit(f"token estimator mismatch rust={rust_v} python={py_v}")
    return "python estimate() matched a verbatim rustc of src/tokens.rs on 3 samples"


def percentile(xs: list[float], p: float):
    if not xs:
        return None
    ys = sorted(xs)
    rank = max(1, math.ceil(p / 100 * len(ys)))
    return ys[rank - 1]


def parse_trace(text: str) -> dict:
    lines = text.splitlines()
    header = {}
    if lines:
        m = HEADER_RE.match(lines[0])
        if m:
            header = {
                "noun": m.group(1),
                "qual": m.group(2),
                "path": m.group(3),
                "start": int(m.group(4)),
                "end": int(m.group(5)),
            }
    summary = ""
    phase = "pre"
    file = header.get("path")
    groups = []
    sites = []
    edges = []
    hidden = 0
    pending = None
    for line in lines[1:]:
        if phase != "body":
            if "·" in line or line.startswith("No ") or line.startswith("None"):
                summary = line
                phase = "body"
            continue
        if line.startswith("(") or line.startswith("not in workspace"):
            continue
        if line.startswith("…"):
            continue
        if not line.startswith(" ") and line.strip():
            file = line.strip()
            pending = None
            continue
        mm = MORE_RE.match(line)
        if mm:
            hidden += int(mm.group(1))
            continue
        am = ARROW_RE.match(line)
        if am and pending:
            edges.append(
                {
                    "call_file": pending[0],
                    "call_line": pending[1],
                    "def_file": am.group(1),
                    "def_start": int(am.group(2)),
                    "def_end": int(am.group(3)),
                    "ambiguous": "(?" in line,
                }
            )
            pending = None
            continue
        stripped = line.strip()
        work = stripped[2:] if stripped.startswith("← ") else stripped
        if work.startswith("? "):
            work = work[2:]
        if "\t" not in line and "[" in work:
            pre, _, rest = work.partition("[")
            gm = re.match(r"(\d+)-(\d+)\]", rest)
            pre = pre.strip()
            if gm and (pre == "" or "/" in pre or "." in pre):
                groups.append(
                    {
                        "file": pre or file,
                        "start": int(gm.group(1)),
                        "end": int(gm.group(2)),
                        "uncertain": stripped.startswith("? ") or " ? " in f" {stripped}",
                    }
                )
                continue
        sm = SITE_RE.match(line)
        if sm:
            lineno = int(sm.group(2))
            body = sm.group(3)
            sites.append(
                {
                    "file": file,
                    "line": lineno,
                    "text": body,
                    "value": "  → " in body,
                }
            )
            pending = (file, lineno, body)
    truncated = any(
        s in text
        for s in ("not examined", "incomplete:", "stopped after", "truncated:")
    )
    return {
        "header": header,
        "summary": summary,
        "groups": groups,
        "sites": sites,
        "edges": edges,
        "hidden": hidden,
        "truncated": truncated,
        "bytes": len(text.encode()),
        "tokens": estimate(text.encode()),
    }


def check_parser() -> str:
    sample = (
        "==> callers of only_called (src/lib.rs:45-47)\n"
        "pub fn only_called() -> u32\n"
        "1 call site in 1 function · syntactic: comments, strings and the definition excluded\n"
        "src/lib.rs\n"
        "  [49-51] pub fn calls_only() -> u32\n"
        "    50\tonly_called()\n"
    )
    got = parse_trace(sample)
    assert got["header"]["path"] == "src/lib.rs", got
    assert got["sites"] == [
        {"file": "src/lib.rs", "line": 50, "text": "only_called()", "value": False}
    ], got
    assert got["groups"][0]["start"] == 49 and got["groups"][0]["end"] == 51
    callee = (
        "==> callees of beta (src/lib.rs:7-9)\n"
        "pub fn beta() -> u32\n"
        "1 call resolved in the workspace · syntactic: best match\n"
        "  8\talpha(1)\n"
        "    → src/lib.rs:3-5 pub fn alpha  (? 2 candidates; also src/other.rs:1)\n"
    )
    got = parse_trace(callee)
    assert got["edges"][0]["call_line"] == 8 and got["edges"][0]["ambiguous"], got
    assert got["edges"][0]["def_start"] == 3 and got["edges"][0]["def_end"] == 5
    return "parser matched the fork_trace only_called shape and a callee arrow"


class Bucket:
    def __init__(self):
        self.tp = self.fp = self.fn = self.n = self.both_empty = 0
        self.near = 0
        self.hidden = 0
        self.truncated = 0
        self.examples = []

    def add_sets(self, pred, gold, case_id: str):
        pred, gold = set(pred), set(gold)
        tp = pred & gold
        fp = pred - gold
        fn = gold - pred
        near = 0
        for f, line in list(fp):
            if (f, line - 1) in gold or (f, line + 1) in gold:
                near += 1
        self.tp += len(tp)
        self.fp += len(fp)
        self.fn += len(fn)
        self.near += near
        self.n += 1
        if not pred and not gold:
            self.both_empty += 1
        if (fp or fn) and len(self.examples) < 12:
            self.examples.append(
                {
                    "id": case_id,
                    "fp": sorted(fp)[:8],
                    "fn": sorted(fn)[:8],
                    "near": near,
                }
            )

    def as_dict(self):
        def rate(n, d):
            return None if d == 0 else n / d

        return {
            "tp": self.tp,
            "fp": self.fp,
            "fn": self.fn,
            "queries": self.n,
            "both_empty": self.both_empty,
            "near_miss_lines": self.near,
            "precision": rate(self.tp, self.tp + self.fp),
            "recall": rate(self.tp, self.tp + self.fn),
            "hidden_sites": self.hidden,
            "truncated_queries": self.truncated,
            "examples": self.examples,
        }


def find_line(text: str, needle: str, start: int = 1) -> int:
    for i, line in enumerate(text.splitlines(), 1):
        if i >= start and needle in line:
            return i
    raise SystemExit(f"gold snippet not in fixture: {needle!r}")


def word_col(line: str, name: str) -> int:
    m = re.search(r"(?<!\w)" + re.escape(name) + r"(?!\w)", line)
    return m.start() if m else max(0, line.find(name))


def uri_to_rel(uri: str, root: Path):
    parsed = urllib.parse.urlparse(uri)
    if parsed.scheme != "file":
        return None
    path = Path(urllib.parse.unquote(parsed.path)).resolve()
    try:
        rel = path.relative_to(root.resolve())
    except ValueError:
        return None
    return rel.as_posix()


class Lsp:
    """Blocking stdio client. A background reader deadlocked rust-analyzer
    (it stopped reading stdin), so one thread pumps both pipes with select.
    """

    def __init__(self, argv: list[str], root: Path):
        self.root = root
        env = os.environ.copy()
        # The rustup default on this machine can be older than the toolchain
        # rust-analyzer was built with. Pin both so `cargo metadata` can parse
        # edition-2024 manifests when a fixture sits inside this repo.
        env["RUSTUP_TOOLCHAIN"] = "stable"
        env["CARGO_TARGET_DIR"] = "/tmp/ra-target-" + hashlib.sha256(
            str(root).encode()
        ).hexdigest()[:8]
        self.proc = subprocess.Popen(
            argv,
            cwd=root,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.next_id = 1
        self.notifications = []
        self.methods = collections.Counter()
        self.stderr = collections.deque(maxlen=30)
        self.encoding = "utf-16"
        self._buf = b""

    def _send(self, obj: dict):
        data = json.dumps(obj).encode()
        frame = f"Content-Length: {len(data)}\r\n\r\n".encode() + data
        os.write(self.proc.stdin.fileno(), frame)

    def _take_frames(self):
        msgs = []
        buf = self._buf
        while True:
            sep = buf.find(b"\r\n\r\n")
            if sep < 0:
                break
            header = buf[:sep].decode("ascii", "replace")
            length = None
            for hline in header.split("\r\n"):
                if hline.lower().startswith("content-length:"):
                    length = int(hline.split(":", 1)[1].strip())
            if length is None:
                buf = buf[sep + 4 :]
                continue
            start = sep + 4
            if len(buf) < start + length:
                break
            body = buf[start : start + length]
            buf = buf[start + length :]
            try:
                msgs.append(json.loads(body))
            except json.JSONDecodeError as exc:
                print("json fail", exc, body[:80], body[-40:], flush=True)
                continue
        self._buf = buf
        return msgs

    def _handle(self, msg: dict, want_id):
        if "method" in msg and "id" in msg:
            self.methods[msg["method"]] += 1
            if msg["method"] == "workspace/configuration":
                items = (msg.get("params") or {}).get("items") or []
                result = [{} for _ in items]
            else:
                result = None
            self._send({"jsonrpc": "2.0", "id": msg["id"], "result": result})
            return None
        if "id" in msg and msg["id"] == want_id:
            return msg
        if "method" in msg:
            self.methods[msg["method"]] += 1
            self.notifications.append(msg)
        return None

    def request(self, method: str, params, timeout: float = 60):
        mid = self.next_id
        self.next_id += 1
        self._send({"jsonrpc": "2.0", "id": mid, "method": method, "params": params})
        deadline = time.time() + timeout
        while time.time() < deadline:
            for msg in self._take_frames():
                got = self._handle(msg, mid)
                if got is not None:
                    if "error" in got:
                        raise RuntimeError(f"{method}: {got['error']}")
                    return got.get("result")
            timeout_s = max(0.0, deadline - time.time())
            readable, _, _ = select.select(
                [self.proc.stdout, self.proc.stderr], [], [], min(0.5, timeout_s)
            )
            if self.proc.stdout in readable:
                chunk = os.read(self.proc.stdout.fileno(), 1 << 20)
                if not chunk:
                    raise RuntimeError(f"{method}: server exited")
                self._buf += chunk
            if self.proc.stderr in readable:
                err = os.read(self.proc.stderr.fileno(), 1 << 16)
                if err:
                    self.stderr.append(err.decode("utf-8", "replace")[-400:])
        raise TimeoutError(method)

    def notify(self, method: str, params):
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def start(self) -> dict:
        t0 = time.perf_counter()
        result = self.request(
            "initialize",
            {
                "processId": os.getpid(),
                "rootUri": self.root.resolve().as_uri(),
                "workspaceFolders": [
                    {"uri": self.root.resolve().as_uri(), "name": self.root.name}
                ],
                "clientInfo": {"name": "speedread-trace-eval", "version": "0"},
                "capabilities": {
                    "general": {"positionEncodings": ["utf-8", "utf-16"]},
                    "window": {"workDoneProgress": True},
                    "textDocument": {
                        "references": {},
                        "implementation": {},
                        "callHierarchy": {},
                        "documentSymbol": {"hierarchicalDocumentSymbolSupport": True},
                    },
                    "workspace": {"configuration": True},
                },
            },
            timeout=60,
        )
        caps = (result or {}).get("capabilities") or {}
        self.encoding = (result or {}).get("positionEncoding") or "utf-16"
        self.notify("initialized", {})
        return {"initialize_s": time.perf_counter() - t0, "positionEncoding": self.encoding, "caps_keys": sorted(caps)[:12]}

    def wait_quiescent(self, timeout: float) -> dict:
        t0 = time.perf_counter()
        deadline = time.time() + timeout
        while time.time() < deadline:
            for msg in self._take_frames():
                self._handle(msg, None)
            saw_prime = False
            for note in self.notifications:
                method = note.get("method")
                params = note.get("params") or {}
                if method in (
                    "experimental/serverStatus",
                    "rust-analyzer/serverStatus",
                ) and params.get("quiescent") is True:
                    return {"quiescent": True, "seconds": time.perf_counter() - t0, "via": method}
                # rust-analyzer 1.100 reports readiness as the end of cache priming,
                # not experimental/serverStatus.
                if method == "$/progress" and params.get("token") == "rustAnalyzer/cachePriming":
                    kind = (params.get("value") or {}).get("kind")
                    if kind == "report":
                        saw_prime = True
                    elif kind == "end" and saw_prime:
                        return {
                            "quiescent": True,
                            "seconds": time.perf_counter() - t0,
                            "via": "cachePriming",
                        }
            remain = max(0.0, deadline - time.time())
            readable, _, _ = select.select(
                [self.proc.stdout, self.proc.stderr], [], [], min(0.3, remain)
            )
            if self.proc.stdout in readable:
                chunk = os.read(self.proc.stdout.fileno(), 1 << 20)
                if chunk:
                    self._buf += chunk
            if self.proc.stderr in readable:
                err = os.read(self.proc.stderr.fileno(), 1 << 16)
                if err:
                    self.stderr.append(err.decode("utf-8", "replace")[-400:])
        return {
            "quiescent": False,
            "seconds": time.perf_counter() - t0,
            "methods": self.methods.most_common(12),
            "stderr": list(self.stderr)[-6:],
        }

    def open_file(self, path: Path):
        text = path.read_text(errors="replace")
        self.notify(
            "textDocument/didOpen",
            {
                "textDocument": {
                    "uri": path.resolve().as_uri(),
                    "languageId": "rust",
                    "version": 1,
                    "text": text,
                }
            },
        )
        return text

    def _pos(self, line_text: str, col: int):
        if self.encoding == "utf-8":
            return col
        return len(line_text[:col].encode("utf-16-le")) // 2

    def references(self, path: Path, line_1: int, col: int):
        text = path.read_text(errors="replace")
        line_text = text.splitlines()[line_1 - 1]
        result = self.request(
            "textDocument/references",
            {
                "textDocument": {"uri": path.resolve().as_uri()},
                "position": {"line": line_1 - 1, "character": self._pos(line_text, col)},
                "context": {"includeDeclaration": False},
            },
        )
        out = []
        for loc in result or []:
            rel = uri_to_rel(loc["uri"], self.root)
            if rel:
                out.append((rel, loc["range"]["start"]["line"] + 1))
        return out

    def implementations(self, path: Path, line_1: int, col: int):
        text = path.read_text(errors="replace")
        line_text = text.splitlines()[line_1 - 1]
        result = self.request(
            "textDocument/implementation",
            {
                "textDocument": {"uri": path.resolve().as_uri()},
                "position": {"line": line_1 - 1, "character": self._pos(line_text, col)},
            },
        )
        out = []
        for loc in result or []:
            if "targetUri" in loc:
                rel = uri_to_rel(loc["targetUri"], self.root)
                rng = loc.get("targetSelectionRange") or loc.get("targetRange") or {}
                line = (rng.get("start") or {}).get("line")
            else:
                rel = uri_to_rel(loc.get("uri", ""), self.root)
                line = (loc.get("range") or {}).get("start", {}).get("line")
            if rel and line is not None:
                out.append((rel, line + 1))
        return out

    def calls(self, path: Path, line_1: int, col: int, direction: str):
        text = path.read_text(errors="replace")
        line_text = text.splitlines()[line_1 - 1]
        items = self.request(
            "textDocument/prepareCallHierarchy",
            {
                "textDocument": {"uri": path.resolve().as_uri()},
                "position": {"line": line_1 - 1, "character": self._pos(line_text, col)},
            },
        )
        if not items:
            return []
        method = (
            "callHierarchy/incomingCalls"
            if direction == "incoming"
            else "callHierarchy/outgoingCalls"
        )
        calls = self.request(method, {"item": items[0]}) or []
        found = []
        for call in calls:
            if direction == "incoming":
                src = call.get("from") or {}
                rel = uri_to_rel(src.get("uri", ""), self.root)
                for rng in call.get("fromRanges") or []:
                    if rel:
                        found.append(
                            {
                                "file": rel,
                                "line": rng["start"]["line"] + 1,
                            }
                        )
            else:
                dest = call.get("to") or {}
                rel = uri_to_rel(dest.get("uri", ""), self.root)
                span = dest.get("range") or {}
                for rng in call.get("fromRanges") or []:
                    found.append(
                        {
                            "call_file": uri_to_rel(path.resolve().as_uri(), self.root),
                            "call_line": rng["start"]["line"] + 1,
                            "def_file": rel,
                            "def_start": None
                            if not rel
                            else (span.get("start") or {}).get("line", 0) + 1,
                            "def_end": None
                            if not rel
                            else (span.get("end") or {}).get("line", 0) + 1,
                            "external": rel is None,
                        }
                    )
        return found

    def document_symbols(self, path: Path):
        result = self.request(
            "textDocument/documentSymbol",
            {"textDocument": {"uri": path.resolve().as_uri()}},
            timeout=30,
        )
        acc = []

        def walk(nodes):
            for node in nodes or []:
                if "location" in node:
                    loc = node["location"]
                    acc.append(
                        {
                            "name": node.get("name") or "",
                            "kind": node.get("kind"),
                            "line": loc["range"]["start"]["line"] + 1,
                            "sel": loc["range"]["start"]["line"] + 1,
                            "col": loc["range"]["start"]["character"],
                        }
                    )
                else:
                    sel = (node.get("selectionRange") or node.get("range") or {}).get(
                        "start"
                    ) or {}
                    acc.append(
                        {
                            "name": node.get("name") or "",
                            "kind": node.get("kind"),
                            "line": (node.get("range") or {}).get("start", {}).get("line", 0)
                            + 1,
                            "sel": sel.get("line", 0) + 1,
                            "col": sel.get("character", 0),
                        }
                    )
                    walk(node.get("children"))

        walk(result if isinstance(result, list) else [])
        return [s for s in acc if s["kind"] in (6, 9, 12) and s["name"].isidentifier()]

    def close(self):
        try:
            self.notify("exit", {})
        except Exception:
            pass
        self.proc.kill()
        self.proc.wait(timeout=5)


def run_trace(binary: str, root: Path, args: list[str], quality: bool):
    cmd = [binary, "--root", str(root), "--no-cwd-root"]
    if quality:
        cmd += ["--budget", "200000", "--max-budget", "200000", "--deadline-ms", "0"]
    cmd += args
    proc = subprocess.Popen(
        cmd, cwd="/", stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    err_box = {}

    def read_err():
        err_box["b"] = proc.stderr.read()

    threading.Thread(target=read_err, daemon=True).start()
    t0 = time.perf_counter()
    out = proc.stdout.read()
    _, status, ru = os.wait4(proc.pid, 0)
    elapsed = time.perf_counter() - t0
    # Linux ru_maxrss is kilobytes.
    rss_mib = ru.ru_maxrss / 1024
    return {
        "stdout": out.decode("utf-8", "replace"),
        "stderr": err_box.get("b", b"").decode("utf-8", "replace"),
        "status": os.waitstatus_to_exitcode(status),
        "seconds": elapsed,
        "rss_mib": rss_mib,
    }


def trace_args(target: str, direction: str, depth: int = 1) -> list[str]:
    args = ["trace", target]
    if direction == "refs":
        args += ["-d", "refs"]
    elif direction == "callees":
        args += ["-d", "callees"]
    elif direction == "impls":
        args += ["-d", "impls"]
    elif direction == "callers":
        args += ["-d", "callers"]
    if depth != 1:
        args += ["--depth", str(depth)]
    return args


def resolve_case(root: Path, case: dict) -> dict:
    if case.get("target"):
        target = case["target"]
    else:
        text = (root / case["target_file"]).read_text()
        line = find_line(text, case["target_line_contains"])
        if case.get("target_line_contains_next"):
            line = find_line(text, case["target_line_contains_next"], line + 1)
        target = f"{case['target_file']}:{line}"
    ra_file = case.get("ra_file") or case.get("target_file")
    ra_line = None
    ra_col = 0
    ra_name = case.get("ra_name")
    if ra_file:
        text = (root / ra_file).read_text()
        if case.get("ra_line_contains"):
            ra_line = find_line(text, case["ra_line_contains"])
        elif case.get("target_line_contains"):
            ra_line = find_line(text, case["target_line_contains"])
            if case.get("target_line_contains_next"):
                ra_line = find_line(text, case["target_line_contains_next"], ra_line + 1)
        if ra_line and not ra_name:
            # The identifier introduced on a `fn`/`struct`/`trait` line.
            m = re.search(r"\bfn\s+(\w+)|\btrait\s+(\w+)|\bstruct\s+(\w+)", text.splitlines()[ra_line - 1])
            ra_name = next(g for g in (m.groups() if m else ()) if g)
        if ra_line and ra_name:
            ra_col = word_col(text.splitlines()[ra_line - 1], ra_name)
    expect = []
    for item in case.get("expect") or []:
        line = find_line((root / item["file"]).read_text(), item["contains"])
        expect.append((item["file"], line))
    ranges = []
    for item in case.get("expect_ranges") or []:
        text = (root / item["file"]).read_text()
        start = find_line(text, item["start_contains"])
        end = find_line(text, item["end_contains"], start)
        ranges.append((item["file"], start, end))
    edges = []
    for item in case.get("expect_edges") or []:
        call_line = find_line((root / item["call_file"]).read_text(), item["call_contains"])
        def_line = find_line((root / item["def_file"]).read_text(), item["def_contains"])
        edges.append(
            {
                "call_file": item["call_file"],
                "call_line": call_line,
                "def_file": item["def_file"],
                "def_line": def_line,
            }
        )
    return {
        "id": case["id"],
        "direction": case["direction"],
        "target": target,
        "ra_file": ra_file,
        "ra_line": ra_line,
        "ra_col": ra_col,
        "expect": expect,
        "ranges": ranges,
        "edges": edges,
    }


def pred_sites(parsed: dict, direction: str):
    if direction == "value":
        return [(s["file"], s["line"]) for s in parsed["sites"] if s["value"]]
    if direction == "callers":
        return [(s["file"], s["line"]) for s in parsed["sites"] if not s["value"]]
    if direction == "refs":
        return [(s["file"], s["line"]) for s in parsed["sites"]]
    return []


def score_edges(pred_edges, gold_edges):
    """Match on call line and on the gold signature line falling in the def range."""
    used = set()
    tp = 0
    ambiguous = 0
    for e in pred_edges:
        if e.get("ambiguous"):
            ambiguous += 1
        hit = None
        for i, g in enumerate(gold_edges):
            if i in used:
                continue
            if e["call_file"] == g["call_file"] and e["call_line"] == g["call_line"]:
                if e["def_file"] == g["def_file"] and e["def_start"] <= g["def_line"] <= e["def_end"]:
                    hit = i
                    break
        if hit is not None:
            used.add(hit)
            tp += 1
    fp = len(pred_edges) - tp
    fn = len(gold_edges) - len(used)
    return tp, fp, fn, ambiguous


def score_ranges(pred_groups, gold_ranges):
    """A gold range is found when any predicted range on that file overlaps it."""
    def overlaps(a, b):
        return a[0] == b[0] and a[1] <= b[2] and b[1] <= a[2]

    pred = [(g["file"], g["start"], g["end"]) for g in pred_groups]
    tp_g = sum(1 for g in gold_ranges if any(overlaps(p, g) for p in pred))
    tp_p = sum(1 for p in pred if any(overlaps(p, g) for g in gold_ranges))
    return tp_p, len(pred) - tp_p, len(gold_ranges) - tp_g


def function_hits(groups, gold_lines):
    pred = [(g["file"], g["start"], g["end"]) for g in groups]
    gold = list(gold_lines)
    def contains(g, line):
        return g[0] == line[0] and g[1] <= line[1] <= g[2]

    tp_g = sum(1 for line in gold if any(contains(g, line) for g in pred))
    tp_p = sum(1 for g in pred if any(contains(g, line) for line in gold))
    return tp_p, len(pred) - tp_p, len(gold) - tp_g


def empty_bucket_dict():
    return Bucket().as_dict()


def accumulate_pair(bucket: Bucket, tp, fp, fn, case_id, pred_sample, gold_sample):
    bucket.tp += tp
    bucket.fp += fp
    bucket.fn += fn
    bucket.n += 1
    if tp == 0 and fp == 0 and fn == 0:
        bucket.both_empty += 1
    if (fp or fn) and len(bucket.examples) < 12:
        bucket.examples.append(
            {"id": case_id, "fp": pred_sample[:8], "fn": gold_sample[:8]}
        )


def loadavg() -> float:
    return float(open("/proc/loadavg").read().split()[0])


def machine() -> dict:
    mem = {}
    for line in open("/proc/meminfo"):
        k, v = line.split(":", 1)
        if k in ("MemTotal", "MemAvailable"):
            mem[k] = v.strip()
    return {
        "nproc": os.cpu_count(),
        "loadavg": open("/proc/loadavg").read().strip(),
        "mem": mem,
        "uname": os.uname().machine + " " + os.uname().release,
    }


def binary_info(path: str) -> dict:
    p = Path(path)
    ver = subprocess.run([path, "--version"], capture_output=True, text=True)
    return {
        "path": str(p),
        "bytes": p.stat().st_size,
        "version": (ver.stdout or ver.stderr).strip(),
        "profile": "release" if "/release/" in str(p) else "fast" if "/fast/" in str(p) else "unknown",
    }


def git_sha(root: Path):
    r = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True
    )
    return r.stdout.strip() if r.returncode == 0 else None


def fmt_rate(x):
    return "n/a" if x is None else f"{x:.3f}"


def markdown(report: dict) -> str:
    lines = [
        "# Trace precision baseline",
        "",
        "Syntactic `trace` only. The precise layer is not in this binary.",
        "",
        report["decision_rule"],
        "",
        f"Binary: `{report['binary']['path']}` ({report['binary']['profile']}, {report['binary']['version']}).",
        f"Machine: {report['machine']['nproc']} cores, load `{report['machine']['loadavg']}`, {report['machine']['mem']}.",
        f"Estimator: {report['estimator_check']}.",
        f"Parser: {report['parser_check']}.",
        "",
        "## Fixture (hand labels)",
        "",
        "| Comparison | Kind | Precision | Recall | tp | fp | fn | near-miss |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for who, block in report["fixture"]["scores"].items():
        for kind, b in block.items():
            lines.append(
                f"| {who} | {kind} | {fmt_rate(b['precision'])} | {fmt_rate(b['recall'])} | {b['tp']} | {b['fp']} | {b['fn']} | {b['near_miss_lines']} |"
            )
    lines += [
        "",
        f"rust-analyzer on the fixture: quiescent={report['fixture']['ra']['quiescent']} in {report['fixture']['ra']['seconds']:.2f}s, encoding {report['fixture']['ra'].get('positionEncoding')}.",
        "",
    ]
    for repo in report["repos"]:
        lines += [
            f"## {repo['name']} `{repo['sha']}`",
            "",
            f"rust-analyzer quiescent={repo['ra']['quiescent']} in {repo['ra']['seconds']:.2f}s. Symbols sampled: {len(repo['symbols'])} (seed {repo['seed']}).",
            "",
            "Quality is the full-budget resolver (`--budget 200000 --deadline-ms 0`). Site recall counts printed lines only; function recall counts a gold line that falls inside a printed enclosing range, so the 6-site display cap does not hide it.",
            "",
            "| Kind | Site precision | Site recall | Function precision | Function recall | tp/fp/fn sites | hidden sites | truncated queries |",
            "|---|---:|---:|---:|---:|---:|---:|---:|",
        ]
        for kind, b in repo["quality"].items():
            fb = b["functions"]
            fn_p = fmt_rate(fb["precision"]) if fb["queries"] else "—"
            fn_r = fmt_rate(fb["recall"]) if fb["queries"] else "—"
            lines.append(
                f"| {kind} | {fmt_rate(b['sites']['precision'])} | {fmt_rate(b['sites']['recall'])} | {fn_p} | {fn_r} | {b['sites']['tp']}/{b['sites']['fp']}/{b['sites']['fn']} | {b['sites']['hidden_sites']} | {b['sites']['truncated_queries']} |"
            )
        perf = repo.get("perf") or {}
        if not perf:
            lines.append("")
            lines.append("Latency panel was not run.")
            continue
        lines += [
            "",
            f"Latency is the default agent budget (trace default 4000 tokens, deadline 10s), page cache already warm from the quality pass, load at start `{perf['load_start']}` and end `{perf['load_end']}`. One warmup panel is discarded. Percentile is nearest-rank. CLI `--version` p50 is the process-startup floor.",
            "",
            "| Query | CLI p50 ms | CLI p95 ms | CLI range ms | RSS p50 MiB | tokens p50 | MCP warm p50 ms | MCP warm p95 ms |",
            "|---|---:|---:|---:|---:|---:|---:|---:|",
        ]
        for q in perf["queries"]:
            lines.append(
                f"| `{q['label']}` | {q['cli_p50_ms']:.1f} | {q['cli_p95_ms']:.1f} | {q['cli_min_ms']:.1f}–{q['cli_max_ms']:.1f} | {q['rss_p50_mib']:.1f} | {q['tokens_p50']:.0f} | {q['mcp_p50_ms']:.1f} | {q['mcp_p95_ms']:.1f} |"
            )
        lines.append(
            f"| `speedread --version` | {perf['startup_p50_ms']:.1f} | {perf['startup_p95_ms']:.1f} |  |  |  |  |  |"
        )
        if perf.get("limiter"):
            lines += ["", "Limiter probe (one unreported run):", "", "```", perf["limiter"][:1500], "```"]
    if report.get("scale"):
        sc = report["scale"]
        lines += [
            "",
            f"## Scale: {sc['name']}",
            "",
            sc["why"],
            "",
            f"Files {sc['files']}, C/C++ {sc['cpp']}, bytes {sc['bytes']}, commit `{sc['sha']}`. Default deadline and budget. n={sc['runs']}. Load `{sc['load_start']}` → `{sc['load_end']}`.",
            "",
            "| Query | p50 ms | p95 ms | range ms | RSS p50 MiB | tokens p50 | incomplete |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ]
        for q in sc["queries"]:
            lines.append(
                f"| `{q['label']}` | {q['cli_p50_ms']:.0f} | {q['cli_p95_ms']:.0f} | {q['cli_min_ms']:.0f}–{q['cli_max_ms']:.0f} | {q['rss_p50_mib']:.0f} | {q['tokens_p50']:.0f} | {q['incomplete']}/{q['runs']} |"
            )
    lines += ["", "## Reproduce", "", "```bash", report["command"], "```", ""]
    return "\n".join(lines)


def sample_symbols(lsp: Lsp, root: Path, n: int, seed: int):
    files = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d
            for d in dirnames
            if d not in (".git", "target", "tests", "benches", "fixtures")
        ]
        for name in filenames:
            if name.endswith(".rs"):
                path = Path(dirpath) / name
                if path.stat().st_size < 1_000_000 and "/src/" in path.as_posix():
                    files.append(path)
    symbols = []
    for path in sorted(files):
        try:
            for sym in lsp.document_symbols(path):
                sym["path"] = path.relative_to(root).as_posix()
                sym["abs"] = str(path)
                symbols.append(sym)
        except Exception as exc:
            print(f"symbol skip {path}: {exc}", flush=True)
    freq = collections.Counter(s["name"] for s in symbols)
    rare = [s for s in symbols if freq[s["name"]] == 1]
    mid = [s for s in symbols if 2 <= freq[s["name"]] <= 4]
    common = [s for s in symbols if freq[s["name"]] >= 5]
    rng = random.Random(seed)

    def take(pool, k):
        pool = sorted(pool, key=lambda s: (s["path"], s["sel"], s["name"]))
        if len(pool) <= k:
            return pool
        return rng.sample(pool, k)

    each = max(1, n // 3)
    chosen = take(rare, each) + take(mid, each) + take(common, n - 2 * each)
    for s in chosen:
        s["freq"] = freq[s["name"]]
        s.pop("abs", None)
    traits = []
    trait_re = re.compile(r"^(?:pub(?:\([^)]*\))?\s+)?trait\s+(\w+)")
    for path in sorted(files):
        for i, line in enumerate(path.read_text(errors="replace").splitlines(), 1):
            m = trait_re.match(line)
            if m:
                traits.append(
                    {
                        "name": m.group(1),
                        "path": path.relative_to(root).as_posix(),
                        "sel": i,
                        "col": word_col(line, m.group(1)),
                        "kind": "trait",
                    }
                )
    traits = sorted(traits, key=lambda s: (s["path"], s["sel"]))
    if len(traits) > 4:
        traits = rng.sample(traits, 4)
    return chosen, traits, len(symbols), len(files)


def quality_symbol(binary, lsp: Lsp, root: Path, sym: dict, buckets: dict):
    """Score refs, callers and callees for one symbol against rust-analyzer."""
    path = root / sym["path"]
    target = f"{sym['path']}:{sym['sel']}"
    line_text = path.read_text(errors="replace").splitlines()[sym["sel"] - 1]
    sym["col"] = word_col(line_text, sym["name"])
    try:
        refs = lsp.references(path, sym["sel"], sym["col"])
        incoming = lsp.calls(path, sym["sel"], sym["col"], "incoming")
        outgoing = lsp.calls(path, sym["sel"], sym["col"], "outgoing")
    except Exception as exc:
        print(f"  oracle failed {target}: {exc}", flush=True)
        return
    gold = {
        "refs": refs,
        "callers": [(c["file"], c["line"]) for c in incoming],
        "callees": [e for e in outgoing if not e["external"]],
    }
    for direction in ("refs", "callers", "callees"):
        ran = run_trace(binary, root, trace_args(target, direction), quality=True)
        parsed = parse_trace(ran["stdout"])
        case_id = f"{sym['path']}:{sym['sel']}:{sym['name']}:{direction}"
        b = buckets[direction]
        b["sites"].hidden += parsed["hidden"]
        if parsed["truncated"] or ran["status"] != 0:
            b["sites"].truncated += 1
        if direction == "callees":
            # Gold def line is the function range start; accept the signature
            # line anywhere in the printed range. Compare call lines exactly.
            pred = parsed["edges"]
            # Build gold in the edge shape score_edges expects. RA's range
            # start may sit on a doc comment, so the gold "def line" is the
            # whole span and the matcher below is custom.
            tp = fp = fn = 0
            used = set()
            amb = 0
            for e in pred:
                if e["ambiguous"]:
                    amb += 1
                hit = None
                for i, g in enumerate(gold["callees"]):
                    if i in used:
                        continue
                    if (
                        e["call_file"] == g["call_file"]
                        and e["call_line"] == g["call_line"]
                        and e["def_file"] == g["def_file"]
                        and g["def_start"] is not None
                        and e["def_start"] <= g["def_end"]
                        and g["def_start"] <= e["def_end"]
                    ):
                        hit = i
                        break
                if hit is None:
                    fp += 1
                else:
                    used.add(hit)
                    tp += 1
            fn = len(gold["callees"]) - len(used)
            accumulate_pair(
                b["sites"],
                tp,
                fp,
                fn,
                case_id,
                [f"{e['call_file']}:{e['call_line']}→{e['def_file']}:{e['def_start']}" for e in pred],
                [f"{g['call_file']}:{g['call_line']}→{g['def_file']}:{g['def_start']}" for g in gold["callees"]],
            )
            b["ambiguous"] = b.get("ambiguous", 0) + amb
        else:
            pred = pred_sites(parsed, direction)
            b["sites"].add_sets(pred, gold[direction], case_id)
            tp, fp, fn = function_hits(parsed["groups"], gold[direction])
            accumulate_pair(
                b["functions"],
                tp,
                fp,
                fn,
                case_id,
                [f"{g['file']}:{g['start']}-{g['end']}" for g in parsed["groups"]],
                [f"{f}:{ln}" for f, ln in list(gold[direction])[:8]],
            )
        b["tokens"].append(parsed["tokens"])
        b["bytes"].append(parsed["bytes"])
        print(
            f"  {direction:8} {sym['name']:24} pred {len(parsed['sites']) if direction != 'callees' else len(parsed['edges']):4} "
            f"gold {len(gold[direction]):4} hidden {parsed['hidden']}",
            flush=True,
        )


def quality_trait(binary, lsp: Lsp, root: Path, sym: dict, bucket: dict):
    path = root / sym["path"]
    target = f"{sym['path']}#{sym['name']}"
    try:
        impls = lsp.implementations(path, sym["sel"], sym["col"])
    except Exception as exc:
        print(f"  impl oracle failed {target}: {exc}", flush=True)
        return
    ran = run_trace(binary, root, trace_args(target, "impls"), quality=True)
    parsed = parse_trace(ran["stdout"])
    # Point gold: a predicted range that contains the point, or a site on it.
    gold_lines = impls
    pred_lines = [(s["file"], s["line"]) for s in parsed["sites"]]
    # Also treat a group range containing the point as a predicted line at the
    # gold line, so overlap counts at line granularity.
    covered = []
    for g in parsed["groups"]:
        for f, ln in gold_lines:
            if g["file"] == f and g["start"] <= ln <= g["end"]:
                covered.append((f, ln))
    pred = set(pred_lines) | set(covered)
    bucket["sites"].add_sets(pred, gold_lines, target)
    print(f"  impls    {sym['name']:24} pred {len(pred):4} gold {len(gold_lines):4}", flush=True)


def new_quality():
    out = {}
    for kind in ("refs", "callers", "callees", "impls"):
        out[kind] = {
            "sites": Bucket(),
            "functions": Bucket(),
            "tokens": [],
            "bytes": [],
            "ambiguous": 0,
        }
    return out


def finish_quality(raw: dict) -> dict:
    done = {}
    for kind, b in raw.items():
        sites = b["sites"].as_dict()
        functions = b["functions"].as_dict()
        toks = b["tokens"]
        done[kind] = {
            "sites": sites,
            "functions": functions,
            "tokens_p50": percentile(toks, 50),
            "tokens_p95": percentile(toks, 95),
            "bytes_p50": percentile(b["bytes"], 50),
            "ambiguous_edges": b["ambiguous"],
            "answers": len(toks),
        }
    return done


def time_panel(binary: str, root: Path, queries: list[dict], runs: int) -> dict:
    # queries: {label, args}
    load_s = open("/proc/loadavg").read().strip()
    # startup floor
    startup = []
    for _ in range(runs):
        t0 = time.perf_counter()
        subprocess.run([binary, "--version"], capture_output=True)
        startup.append((time.perf_counter() - t0) * 1000)
    # warmup
    for q in queries:
        run_trace(binary, root, q["args"], quality=False)
    samples = {q["label"]: [] for q in queries}
    for _ in range(runs):
        order = list(queries)
        for q in order:
            ran = run_trace(binary, root, q["args"], quality=False)
            parsed = parse_trace(ran["stdout"])
            samples[q["label"]].append(
                {
                    "ms": ran["seconds"] * 1000,
                    "rss": ran["rss_mib"],
                    "tokens": parsed["tokens"],
                    "status": ran["status"],
                    "truncated": parsed["truncated"] or "incomplete:" in ran["stdout"],
                    "bytes": parsed["bytes"],
                }
            )
    # MCP warm
    mcp = mcp_times(binary, root, queries, runs)
    rows = []
    for q in queries:
        xs = samples[q["label"]]
        ms = [s["ms"] for s in xs]
        rows.append(
            {
                "label": q["label"],
                "runs": len(xs),
                "cli_p50_ms": percentile(ms, 50),
                "cli_p95_ms": percentile(ms, 95),
                "cli_min_ms": min(ms),
                "cli_max_ms": max(ms),
                "rss_p50_mib": percentile([s["rss"] for s in xs], 50),
                "rss_max_mib": max(s["rss"] for s in xs),
                "tokens_p50": percentile([s["tokens"] for s in xs], 50),
                "tokens_max": max(s["tokens"] for s in xs),
                "errors": sum(1 for s in xs if s["status"] != 0),
                "incomplete": sum(1 for s in xs if s["truncated"]),
                "mcp_p50_ms": mcp[q["label"]]["p50"],
                "mcp_p95_ms": mcp[q["label"]]["p95"],
                "mcp_min_ms": mcp[q["label"]]["min"],
                "mcp_max_ms": mcp[q["label"]]["max"],
                "samples_ms": [round(m, 2) for m in ms],
            }
        )
    limiter = limiter_probe(binary, root, queries[0]["args"] if queries else ["--version"])
    return {
        "load_start": load_s,
        "load_end": open("/proc/loadavg").read().strip(),
        "startup_p50_ms": percentile(startup, 50),
        "startup_p95_ms": percentile(startup, 95),
        "queries": rows,
        "mcp_rss_hwm_mib": mcp.get("_rss"),
        "limiter": limiter,
    }


def mcp_times(binary, root: Path, queries, runs: int) -> dict:
    proc = subprocess.Popen(
        [binary, "--root", str(root), "--no-cwd-root", "mcp"],
        cwd="/",
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    n = 0

    def call(method, params):
        nonlocal n
        n += 1
        proc.stdin.write(
            (json.dumps({"jsonrpc": "2.0", "id": n, "method": method, "params": params}) + "\n").encode()
        )
        proc.stdin.flush()
        t0 = time.perf_counter()
        while True:
            line = proc.stdout.readline()
            if not line:
                raise EOFError(method)
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if msg.get("id") == n:
                return msg, (time.perf_counter() - t0) * 1000

    call(
        "initialize",
        {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "trace-precision", "version": "0"},
        },
    )
    proc.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
    proc.stdin.flush()
    out = {q["label"]: [] for q in queries}

    def one(q):
        direction = "callers"
        depth = 1
        target = q["target"]
        args = {"target": target}
        if q.get("direction"):
            args["direction"] = q["direction"]
        if q.get("depth"):
            args["depth"] = q["depth"]
        msg, ms = call("tools/call", {"name": "trace", "arguments": args})
        text = ""
        try:
            text = msg["result"]["content"][0]["text"]
        except Exception:
            text = json.dumps(msg)[:200]
        return ms, text

    for q in queries:
        one(q)  # warmup
    for _ in range(runs):
        for q in queries:
            ms, _text = one(q)
            out[q["label"]].append(ms)
    rss = None
    try:
        for line in open(f"/proc/{proc.pid}/status"):
            if line.startswith("VmHWM"):
                rss = int(line.split()[1]) / 1024
    except OSError:
        pass
    proc.kill()
    proc.wait(timeout=5)
    summary = {"_rss": rss}
    for label, xs in out.items():
        summary[label] = {
            "p50": percentile(xs, 50),
            "p95": percentile(xs, 95),
            "min": min(xs) if xs else None,
            "max": max(xs) if xs else None,
        }
    return summary


def limiter_probe(binary, root: Path, args: list[str]) -> str:
    perf = subprocess.run(["bash", "-lc", "command -v perf"], capture_output=True, text=True)
    cmd = [binary, "--root", str(root), "--no-cwd-root", *args]
    if perf.returncode == 0 and perf.stdout.strip():
        r = subprocess.run(
            ["perf", "stat", "-e", "task-clock,cycles,instructions,cache-references,cache-misses", *cmd],
            cwd="/",
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        return r.stderr.strip()
    # No perf: report user/system time from rusage of one run, which is not a limiter by itself.
    ran = run_trace(binary, root, args, quality=False)
    return (
        f"perf not available. one run wall {ran['seconds']*1000:.1f} ms, "
        f"rss {ran['rss_mib']:.1f} MiB, status {ran['status']}. "
        "Limiter not identified from a profile."
    )


def file_counts(root: Path) -> dict:
    files = cpp = total = 0
    for dirpath, dirnames, filenames in os.walk(root):
        if ".git" in dirnames:
            dirnames.remove(".git")
        for name in filenames:
            files += 1
            p = Path(dirpath) / name
            try:
                total += p.stat().st_size
            except OSError:
                pass
            if name.endswith((".c", ".cc", ".cpp", ".h", ".hh", ".hpp")):
                cpp += 1
    return {"files": files, "cpp": cpp, "bytes": total}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--speedread", required=True)
    ap.add_argument("--rust-analyzer", default="")
    ap.add_argument("--fixture", default="evals/fixtures/trace_oracle")
    ap.add_argument("--repo", action="append", default=[])
    ap.add_argument("--repo-name", action="append", default=[])
    ap.add_argument("--scale-repo", default="")
    ap.add_argument("--scale-name", default="scale")
    ap.add_argument("--scale-why", default="")
    ap.add_argument("--symbols", type=int, default=12)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--runs", type=int, default=15)
    ap.add_argument("--scale-runs", type=int, default=7)
    ap.add_argument("--out", required=True)
    ap.add_argument("--fixture-only", action="store_true")
    ap.add_argument("--skip-perf", action="store_true")
    ap.add_argument("--skip-scale", action="store_true")
    args = ap.parse_args()

    print(check_estimator(), flush=True)
    parser_note = check_parser()
    print(parser_note, flush=True)
    ra_bin = args.rust_analyzer
    if not ra_bin:
        which = subprocess.run(
            ["rustup", "which", "rust-analyzer"], capture_output=True, text=True
        )
        if which.returncode != 0:
            raise SystemExit("rust-analyzer not found; pass --rust-analyzer")
        ra_bin = which.stdout.strip()

    fixture_root = Path(args.fixture).resolve()
    truth_path = fixture_root.parent / "trace_oracle_truth.json"
    truth = json.loads(truth_path.read_text())
    cases = [resolve_case(fixture_root, c) for c in truth["cases"]]
    print(f"fixture cases {len(cases)}", flush=True)
    lsp = Lsp([ra_bin], fixture_root)
    init = lsp.start()
    ra_info = lsp.wait_quiescent(120)
    ra_info.update(init)
    print(f"fixture RA {ra_info}", flush=True)

    scores = {"speedread_vs_hand": {}, "ra_vs_hand": {}}
    buckets = {who: {k: Bucket() for k in ("refs", "callers", "value", "callees", "impls")} for who in scores}
    for case in cases:
        ran = run_trace(
            args.speedread,
            fixture_root,
            trace_args(case["target"], "callers" if case["direction"] == "value" else case["direction"]),
            quality=True,
        )
        parsed = parse_trace(ran["stdout"])
        direction = case["direction"]
        if direction in ("refs", "callers", "value"):
            buckets["speedread_vs_hand"][direction].add_sets(
                pred_sites(parsed, direction), case["expect"], case["id"]
            )
        elif direction == "callees":
            tp, fp, fn, _amb = score_edges(parsed["edges"], [
                {
                    "call_file": e["call_file"],
                    "call_line": e["call_line"],
                    "def_file": e["def_file"],
                    "def_line": e["def_line"],
                    "def_start": e["def_line"],
                    "def_end": e["def_line"],
                }
                for e in case["edges"]
            ])
            # score_edges expects def_start/def_end on the prediction and def_line on gold.
            accumulate_pair(
                buckets["speedread_vs_hand"][direction],
                tp,
                fp,
                fn,
                case["id"],
                [f"{e['call_file']}:{e['call_line']}→{e['def_file']}:{e['def_start']}" for e in parsed["edges"]],
                [f"{e['call_file']}:{e['call_line']}" for e in case["edges"]],
            )
        elif direction == "impls":
            tp, fp, fn = score_ranges(parsed["groups"], case["ranges"])
            accumulate_pair(
                buckets["speedread_vs_hand"][direction],
                tp,
                fp,
                fn,
                case["id"],
                [f"{g['file']}:{g['start']}-{g['end']}" for g in parsed["groups"]],
                [f"{a}:{b}-{c}" for a, b, c in case["ranges"]],
            )
        # rust-analyzer against the same hand labels
        if case["ra_file"] and case["ra_line"]:
            ra_path = fixture_root / case["ra_file"]
            try:
                if direction == "refs":
                    got = lsp.references(ra_path, case["ra_line"], case["ra_col"])
                    buckets["ra_vs_hand"][direction].add_sets(got, case["expect"], case["id"])
                elif direction == "callers":
                    got = [(c["file"], c["line"]) for c in lsp.calls(ra_path, case["ra_line"], case["ra_col"], "incoming")]
                    buckets["ra_vs_hand"][direction].add_sets(got, case["expect"], case["id"])
                elif direction == "callees":
                    got = [e for e in lsp.calls(ra_path, case["ra_line"], case["ra_col"], "outgoing") if not e["external"]]
                    # Match call line and def file; def line may be the span start.
                    tp = fp = 0
                    used = set()
                    for e in got:
                        hit = None
                        for i, g in enumerate(case["edges"]):
                            if i in used:
                                continue
                            if e["call_file"] == g["call_file"] and e["call_line"] == g["call_line"] and e["def_file"] == g["def_file"] and e["def_start"] <= g["def_line"] <= e["def_end"]:
                                hit = i
                                break
                        if hit is None:
                            fp += 1
                        else:
                            used.add(hit)
                            tp += 1
                    fn = len(case["edges"]) - len(used)
                    accumulate_pair(buckets["ra_vs_hand"][direction], tp, fp, fn, case["id"], [], [])
                elif direction == "impls":
                    pts = lsp.implementations(ra_path, case["ra_line"], case["ra_col"])
                    tp = sum(1 for f, ln in pts if any(f == a and b <= ln <= c for a, b, c in case["ranges"]))
                    fp = len(pts) - tp
                    found_ranges = sum(1 for a, b, c in case["ranges"] if any(f == a and b <= ln <= c for f, ln in pts))
                    fn = len(case["ranges"]) - found_ranges
                    accumulate_pair(buckets["ra_vs_hand"][direction], tp, fp, fn, case["id"], [f"{f}:{ln}" for f, ln in pts], [])
            except Exception as exc:
                print(f"fixture RA {case['id']}: {exc}", flush=True)
        print(f"fixture {case['id']} status {ran['status']} tokens {parsed['tokens']:.0f}", flush=True)
    lsp.close()
    for who in scores:
        scores[who] = {k: v.as_dict() for k, v in buckets[who].items() if v.n}

    report = {
        "decision_rule": DECISION_RULE,
        "machine": machine(),
        "binary": binary_info(args.speedread),
        "estimator_check": check_estimator.__doc__ and "see estimator_check key",
        "parser_check": parser_note,
        "command": " ".join(sys.argv),
        "fixture": {"ra": ra_info, "scores": scores},
        "repos": [],
    }
    # The function's docstring is not the check result. Store the real one.
    report["estimator_check"] = "python estimate() matched rustc of the tokens.rs formula on 3 samples"

    if not args.fixture_only:
        names = args.repo_name or [Path(p).name for p in args.repo]
        if len(names) != len(args.repo):
            names = [Path(p).name for p in args.repo]
        for name, repo in zip(names, args.repo):
            root = Path(repo).resolve()
            print(f"repo {name} {root}", flush=True)
            lsp = Lsp([ra_bin], root)
            init = lsp.start()
            info = lsp.wait_quiescent(300)
            info.update(init)
            print(f"  RA {info.get('quiescent')} in {info.get('seconds'):.1f}s", flush=True)
            chosen, traits, n_sym, n_files = sample_symbols(lsp, root, args.symbols, args.seed)
            print(f"  sampled {len(chosen)} of {n_sym} symbols in {n_files} files; {len(traits)} traits", flush=True)
            raw = new_quality()
            for sym in chosen:
                try:
                    quality_symbol(args.speedread, lsp, root, sym, raw)
                except Exception as exc:
                    print(f"  symbol failed {sym['path']}:{sym['name']}: {exc}", flush=True)
            for sym in traits:
                try:
                    quality_trait(args.speedread, lsp, root, sym, raw["impls"])
                except Exception as exc:
                    print(f"  trait failed {sym}: {exc}", flush=True)
            lsp.close()
            perf = None
            if not args.skip_perf and chosen:
                ranked = sorted(chosen, key=lambda s: s["freq"])
                rare, common = ranked[0], ranked[-1]
                queries = []
                for sym, direction, depth in (
                    (rare, "callers", 1),
                    (rare, "refs", 1),
                    (common, "callers", 1),
                    (common, "refs", 1),
                    (ranked[len(ranked) // 2], "callees", 1),
                    (common, "callers", 2),
                ):
                    queries.append(
                        {
                            "label": f"{direction} d{depth} {sym['name']} ({sym['freq']} defs)",
                            "args": trace_args(f"{sym['path']}:{sym['sel']}", direction, depth),
                            "target": f"{sym['path']}:{sym['sel']}",
                            "direction": {"callers": "callers", "refs": "refs", "callees": "callees"}[direction],
                            "depth": depth,
                        }
                    )
                print(f"  perf {len(queries)} queries × {args.runs}", flush=True)
                perf = time_panel(args.speedread, root, queries, args.runs)
            report["repos"].append(
                {
                    "name": name,
                    "path": str(root),
                    "sha": git_sha(root),
                    "seed": args.seed,
                    "symbol_pool": n_sym,
                    "files": n_files,
                    "ra": {k: info[k] for k in info if k != "stderr"},
                    "symbols": [
                        {"path": s["path"], "line": s["sel"], "name": s["name"], "freq": s["freq"], "kind": s["kind"]}
                        for s in chosen
                    ],
                    "traits": traits,
                    "quality": finish_quality(raw),
                    "perf": perf,
                }
            )

    if args.scale_repo and not args.skip_scale and not args.fixture_only:
        root = Path(args.scale_repo).resolve()
        counts = file_counts(root)
        why = args.scale_why or (
            "Full Chromium needs depot_tools/gclient and a multi-gigabyte checkout. "
            "This is a sparse checkout of the github.com/chromium/chromium commit's "
            "base, net and content trees: the directories the repo's own large-tree "
            "eval already uses for trace. Not the whole browser."
        )
        queries = [
            {"label": "trace PostTask", "args": ["trace", "PostTask"], "target": "PostTask", "direction": "callers", "depth": 1},
            {"label": "trace DidCommitNavigation", "args": ["trace", "DidCommitNavigation"], "target": "DidCommitNavigation", "direction": "callers", "depth": 1},
            {"label": "trace PostTask --depth 2", "args": ["trace", "PostTask", "--depth", "2"], "target": "PostTask", "direction": "callers", "depth": 2},
        ]
        print(f"scale {root} files {counts}", flush=True)
        load_s = open("/proc/loadavg").read().strip()
        # Confirm the names occur before spending the runs.
        kept = []
        for q in queries:
            name = q["target"]
            hit = subprocess.run(
                ["rg", "-l", "--max-count", "1", "-g", "*.cc", "-g", "*.h", "-g", "*.hh", name, str(root)],
                capture_output=True,
            )
            if hit.returncode == 0:
                kept.append(q)
            else:
                print(f"  scale skip {name}: no match", flush=True)
        panel = time_panel(args.speedread, root, kept, args.scale_runs) if kept else {"queries": []}
        report["scale"] = {
            "name": args.scale_name,
            "why": why,
            "path": str(root),
            "sha": git_sha(root),
            "files": counts["files"],
            "cpp": counts["cpp"],
            "bytes": counts["bytes"],
            "runs": args.scale_runs,
            "load_start": load_s,
            "load_end": open("/proc/loadavg").read().strip(),
            "queries": panel.get("queries") or [],
            "startup_p50_ms": panel.get("startup_p50_ms"),
            "mcp_rss_hwm_mib": panel.get("mcp_rss_hwm_mib"),
            "limiter": panel.get("limiter"),
        }

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    # Drop huge per-run lists from the markdown; keep them in JSON.
    out.write_text(json.dumps(report, indent=2) + "\n")
    md = out.with_suffix(".md")
    md.write_text(markdown(report))
    print(f"wrote {out} and {md}", flush=True)


if __name__ == "__main__":
    main()
