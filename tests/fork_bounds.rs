//! Bounded discovery and residency (docs/specs/02-bounded-discovery.md).

use std::io::{BufRead, BufReader, Read, Write};
use std::path::PathBuf;
use std::process::{Child, ChildStdin, ChildStdout, Command, Stdio};

use serde_json::{Value, json};

struct Fx {
    dir: PathBuf,
}

impl Fx {
    /// A repository with two tracked sources and `untracked` loose files.
    fn new(name: &str, untracked: usize) -> Fx {
        let dir =
            std::env::temp_dir().join(format!("speedread-bounds-{name}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(dir.join("repo/.git")).unwrap();
        std::fs::create_dir_all(dir.join("repo/src")).unwrap();
        std::fs::create_dir_all(dir.join("elsewhere")).unwrap();
        std::fs::create_dir_all(dir.join("home")).unwrap();
        let dir = dir.canonicalize().unwrap();
        std::fs::write(
            dir.join("repo/src/core.rs"),
            "pub fn tracked_fn() -> u32 {\n    41\n}\n\npub fn tracked_caller() -> u32 {\n    tracked_fn() + 1\n}\n",
        )
        .unwrap();
        std::fs::write(
            dir.join("repo/src/more.rs"),
            "pub fn more() -> u32 {\n    crate::core::tracked_fn()\n}\n",
        )
        .unwrap();
        let fx = Fx { dir };
        fx.grow(0, untracked);
        fx
    }

    /// Add loose files `from..to` (100 per directory), as a build or a
    /// generator would leave behind.
    fn grow(&self, from: usize, to: usize) {
        for i in from..to {
            let d = self.dir.join(format!("repo/scratch/d{:04}", i / 100));
            if i % 100 == 0 || i == from {
                std::fs::create_dir_all(&d).unwrap();
            }
            std::fs::write(
                d.join(format!("f{i:06}.txt")),
                format!("scratch line {i}\n"),
            )
            .unwrap();
        }
    }

    fn p(&self, rel: &str) -> String {
        self.dir.join(rel).to_str().unwrap().to_string()
    }

    fn cmd(&self) -> Command {
        let mut c = Command::new(env!("CARGO_BIN_EXE_speedread"));
        c.current_dir(self.dir.join("elsewhere"))
            .env_remove("CLAUDE_PROJECT_DIR")
            .env_remove("SPEEDREAD_MAX_WALK")
            .env_remove("SPEEDREAD_DEADLINE_MS")
            .env_remove("SPEEDREAD_CACHE_MB")
            .env("HOME", self.dir.join("home"))
            .env("SPEEDREAD_STATS", "1")
            .args(["--root", &self.p("repo")]);
        c
    }

    /// (stdout, stderr)
    fn run(&self, args: &[&str]) -> (String, String) {
        let out = self.cmd().args(args).output().expect("run speedread");
        assert!(
            out.status.success(),
            "speedread {args:?} failed: {}",
            String::from_utf8_lossy(&out.stderr)
        );
        (
            String::from_utf8_lossy(&out.stdout).into_owned(),
            String::from_utf8_lossy(&out.stderr).into_owned(),
        )
    }
}

impl Drop for Fx {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.dir);
    }
}

fn stat(stderr: &str, key: &str) -> u64 {
    let line = stderr
        .lines()
        .find(|l| l.starts_with("speedread-stats"))
        .unwrap_or_else(|| panic!("no stats line in {stderr:?}"));
    line.split_whitespace()
        .find_map(|kv| kv.strip_prefix(&format!("{key}=")))
        .and_then(|v| v.parse().ok())
        .unwrap_or_else(|| panic!("no {key}= in {line:?}"))
}

#[test]
fn explicit_reads_never_walk_however_large_the_untracked_tree() {
    let fx = Fx::new("growth", 0);
    let abs = fx.p("repo/src/core.rs");
    let targets = [
        "src/core.rs",
        "src/core.rs:2",
        "src/core.rs:5-7",
        "src/core.rs#tracked_caller",
        abs.as_str(),
    ];
    let mut reference: Vec<String> = Vec::new();
    let mut grown = 0;
    for size in [0usize, 3_000, 20_000] {
        fx.grow(grown, size);
        grown = size;
        for (i, t) in targets.iter().enumerate() {
            let (out, err) = fx.run(&["read", t]);
            assert_eq!(stat(&err, "walks"), 0, "{t} walked at {size} files");
            assert_eq!(
                stat(&err, "entries"),
                0,
                "{t} listed entries at {size} files"
            );
            assert!(out.contains("tracked_"), "{out}");
            if size == 0 {
                reference.push(out);
            } else {
                assert_eq!(out, reference[i], "{t} changed at {size} files");
            }
        }
    }
}

#[test]
fn a_walk_that_hits_the_entry_limit_says_so() {
    let fx = Fx::new("entries", 3_000);
    // Unlimited enough: the plain negative answer, complete.
    let (out, err) = fx.run(&["search", "zzz_absent"]);
    assert!(out.contains("No matches for"), "{out}");
    assert!(!out.contains("incomplete"), "{out}");
    assert_eq!(stat(&err, "stopped"), 0);
    // Limited: never a plain "No matches", always the reason.
    let (out, err) = fx.run(&["--max-walk", "100", "search", "zzz_absent"]);
    assert!(out.contains("incomplete:"), "{out}");
    assert!(!out.contains("No matches for"), "{out}");
    assert!(out.contains("100"), "{out}");
    assert!(stat(&err, "entries") <= 100, "{err}");
    assert_eq!(stat(&err, "stopped"), 1);
    // A hit found before the limit still carries the warning.
    let (out, _) = fx.run(&["--max-walk", "100", "search", "scratch line"]);
    assert!(out.contains("==> ") && out.contains("incomplete:"), "{out}");
    // The environment variable is equivalent.
    let out = fx
        .cmd()
        .env("SPEEDREAD_MAX_WALK", "100")
        .args(["search", "zzz_absent"])
        .output()
        .unwrap();
    assert!(String::from_utf8_lossy(&out.stdout).contains("incomplete:"));
    // An explicit path under the limit is complete even with the limit set.
    let (out, _) = fx.run(&["--max-walk", "100", "search", "zzz_absent", "src"]);
    assert!(
        out.contains("No matches for") && !out.contains("incomplete"),
        "{out}"
    );
}

#[test]
fn every_tool_reports_incomplete_discovery() {
    let fx = Fx::new("tools", 3_000);
    let limited = |args: &[&str]| {
        let mut v = vec!["--max-walk", "50"];
        v.extend(args);
        fx.run(&v).0
    };
    // #Symbol: "not found" is only honest when the walk finished.
    let (out, _) = fx.run(&["read", "#zzz_absent_symbol"]);
    assert!(
        out.contains("no definition found in") && !out.contains("incomplete"),
        "{out}"
    );
    let out = limited(&["read", "#zzz_absent_symbol"]);
    assert!(out.contains("incomplete"), "{out}");
    assert!(!out.contains("no definition found in"), "{out}");
    // trace: a bare name, and callers of an explicit target.
    let out = limited(&["trace", "#zzz_absent_symbol"]);
    assert!(out.contains("incomplete"), "{out}");
    let out = limited(&["trace", "src/core.rs#tracked_fn"]);
    assert!(out.contains("incomplete:"), "{out}");
    let (out, _) = fx.run(&["trace", "src/core.rs#tracked_fn"]);
    assert!(
        out.contains("tracked_caller") && !out.contains("incomplete"),
        "{out}"
    );
    // map
    let out = limited(&["map"]);
    assert!(out.contains("incomplete:"), "{out}");
    let (out, _) = fx.run(&["map"]);
    assert!(!out.contains("incomplete"), "{out}");
    // Glob reads and JSON Lines (the note goes to stderr, records stay clean).
    let out = limited(&["read", "scratch/**/*.txt"]);
    assert!(out.contains("incomplete:"), "{out}");
    let res = fx
        .cmd()
        .args(["--max-walk", "50", "map", "--json"])
        .output()
        .unwrap();
    let (out, err) = (
        String::from_utf8_lossy(&res.stdout),
        String::from_utf8_lossy(&res.stderr),
    );
    assert!(err.contains("incomplete:"), "{err}");
    assert!(out.lines().all(|l| l.starts_with('{')), "{out}");
}

#[test]
fn the_deadline_bounds_a_request() {
    let fx = Fx::new("deadline", 20_000);
    let (out, err) = fx.run(&["--deadline-ms", "1", "search", "zzz_absent"]);
    assert!(out.contains("incomplete:") && out.contains("ms"), "{out}");
    assert!(!out.contains("No matches for"), "{out}");
    assert!(stat(&err, "entries") < 20_000, "{err}");
    // 0 turns the limit off.
    let (out, _) = fx.run(&[
        "--deadline-ms",
        "0",
        "--max-walk",
        "0",
        "search",
        "zzz_absent",
    ]);
    assert!(
        out.contains("No matches for") && !out.contains("incomplete"),
        "{out}"
    );
}

#[test]
fn the_definition_candidate_cap_is_no_longer_silent() {
    let fx = Fx::new("cands", 0);
    for i in 0..5_200 {
        let d = fx.dir.join(format!("repo/gen/d{:03}", i / 100));
        if i % 100 == 0 {
            std::fs::create_dir_all(&d).unwrap();
        }
        // Mentions the name without defining it.
        std::fs::write(d.join(format!("m{i:05}.rs")), "// popular_name\n").unwrap();
    }
    let (out, _) = fx.run(&["read", "#popular_name"]);
    assert!(out.contains("incomplete"), "{out}");
    assert!(out.contains("5,000"), "{out}");
}

struct Client {
    child: Child,
    stdin: Option<ChildStdin>,
    stdout: BufReader<ChildStdout>,
    next: u64,
}

impl Client {
    fn spawn(fx: &Fx, args: &[&str]) -> Client {
        let mut child = fx
            .cmd()
            .args(args)
            .arg("mcp")
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .expect("spawn speedread");
        let stdin = child.stdin.take();
        let stdout = BufReader::new(child.stdout.take().unwrap());
        let mut c = Client {
            child,
            stdin,
            stdout,
            next: 0,
        };
        c.rpc(
            "initialize",
            json!({"protocolVersion": "2025-06-18", "capabilities": {},
                   "clientInfo": {"name": "test", "version": "0"}}),
        );
        c.send(json!({"jsonrpc": "2.0", "method": "notifications/initialized"}));
        c
    }

    fn send(&mut self, v: Value) {
        let stdin = self.stdin.as_mut().unwrap();
        writeln!(stdin, "{v}").unwrap();
        stdin.flush().unwrap();
    }

    fn request(&mut self, method: &str, params: Value) -> u64 {
        self.next += 1;
        let id = self.next;
        self.send(json!({"jsonrpc": "2.0", "id": id, "method": method, "params": params}));
        id
    }

    fn wait(&mut self, id: u64) -> Value {
        loop {
            let mut line = String::new();
            assert!(
                self.stdout.read_line(&mut line).unwrap() > 0,
                "server closed stdout"
            );
            let v: Value = serde_json::from_str(&line).unwrap();
            if v["id"] == json!(id) && v.get("method").is_none() {
                return v;
            }
        }
    }

    fn rpc(&mut self, method: &str, params: Value) -> Value {
        let id = self.request(method, params);
        self.wait(id)
    }

    fn call(&mut self, tool: &str, args: Value) -> String {
        let r = self.rpc("tools/call", json!({"name": tool, "arguments": args}));
        assert!(r.get("error").is_none(), "tool error: {r}");
        r["result"]["content"]
            .as_array()
            .unwrap()
            .iter()
            .filter_map(|c| c["text"].as_str())
            .collect()
    }

    /// Close stdin, wait for exit and return stderr.
    fn finish(mut self) -> String {
        drop(self.stdin.take());
        let mut err = String::new();
        let _ = self.child.wait();
        if let Some(mut e) = self.child.stderr.take() {
            let _ = e.read_to_string(&mut err);
        }
        err
    }
}

impl Drop for Client {
    fn drop(&mut self) {
        let _ = self.child.kill();
    }
}

#[test]
fn cache_size_changes_residency_not_answers() {
    let fx = Fx::new("cache", 200);
    // Sources well beyond a 1 MiB cache.
    for i in 0..8 {
        let mut s = String::new();
        for j in 0..6_000 {
            s.push_str(&format!("pub fn big_{i}_{j}() -> u32 {{\n    {j}\n}}\n\n"));
        }
        std::fs::write(fx.dir.join(format!("repo/src/big{i}.rs")), s).unwrap();
    }
    let session = |args: &[&str]| -> Vec<String> {
        let mut c = Client::spawn(&fx, args);
        let mut outs = Vec::new();
        for round in 0..2 {
            for i in 0..8 {
                outs.push(c.call(
                    "read",
                    json!({"targets": [format!("src/big{i}.rs#big_{i}_{}", 17 + round)]}),
                ));
            }
            outs.push(c.call("search", json!({"pattern": "fn big_3_59\\d\\d\\("})));
            outs.push(c.call("trace", json!({"target": "src/core.rs#tracked_fn"})));
            outs.push(c.call("read", json!({"targets": ["#big_7_5999"]})));
            outs.push(c.call("map", json!({"path": "src", "symbols": true})));
        }
        outs
    };
    let default = session(&[]);
    let small = session(&["--cache-mb", "1"]);
    assert_eq!(default.len(), small.len());
    for (a, b) in default.iter().zip(&small) {
        assert_eq!(a, b);
    }
    assert!(default.iter().all(|o| !o.contains("incomplete")));
}

#[test]
fn mcp_limits_are_per_request() {
    let fx = Fx::new("perreq", 200);
    let mut c = Client::spawn(&fx, &["--deadline-ms", "400"]);
    let a = c.call("search", json!({"pattern": "tracked_caller"}));
    assert!(
        a.contains("==> src/core.rs") && !a.contains("incomplete"),
        "{a}"
    );
    // The server has now lived longer than one deadline.
    std::thread::sleep(std::time::Duration::from_millis(700));
    let b = c.call("search", json!({"pattern": "tracked_caller"}));
    assert_eq!(a, b);
    // The entry limit applies to each walk, so a trace (several walks) of a
    // tree under the limit stays complete.
    let mut c = Client::spawn(&fx, &["--max-walk", "250"]);
    let t = c.call(
        "trace",
        json!({"target": "src/core.rs#tracked_fn", "depth": 2}),
    );
    assert!(
        t.contains("tracked_caller") && !t.contains("incomplete"),
        "{t}"
    );
}

#[test]
fn mcp_cancellation_stops_the_walk() {
    let fx = Fx::new("cancel", 30_000);
    let mut c = Client::spawn(&fx, &[]);
    let id = c.request(
        "tools/call",
        json!({"name": "search", "arguments": {"pattern": "zzz_absent"}}),
    );
    c.send(
        json!({"jsonrpc": "2.0", "method": "notifications/cancelled",
                  "params": {"requestId": id, "reason": "test"}}),
    );
    // The server stays healthy and answers the next request in full.
    let out = c.call(
        "search",
        json!({"pattern": "tracked_caller", "paths": ["src"]}),
    );
    assert!(
        out.contains("==> src/core.rs") && !out.contains("incomplete"),
        "{out}"
    );
    let err = c.finish();
    assert_eq!(stat(&err, "cancelled"), 1, "{err}");
    assert!(stat(&err, "entries") < 30_000, "{err}");
}

/// Files a text search can hurt itself on: one line of 3 MiB, one of 6 MiB
/// (more than a search buffers), and many matching lines of 256 KiB.
fn long_lines(fx: &Fx) {
    let mut wide = vec![b'a'; 3 << 20];
    wide[2 << 20..(2 << 20) + 10].copy_from_slice(b" needle_w ");
    wide.push(b'\n');
    std::fs::write(fx.dir.join("repo/src/wide.min.js"), wide).unwrap();

    let mut huge = b"var needle_early = 1;\n".to_vec();
    huge.extend(std::iter::repeat_n(b'b', 6 << 20));
    huge.extend_from_slice(b"\nvar needle_late = 2;\n");
    std::fs::write(fx.dir.join("repo/src/huge.min.js"), huge).unwrap();

    let mut many = Vec::new();
    for i in 0..300 {
        many.extend_from_slice(format!("{{\"k\":\"needle_many\",\"i\":{i},\"v\":\"").as_bytes());
        many.extend(std::iter::repeat_n(b'x', 256 << 10));
        many.extend_from_slice(b"\"}\n");
    }
    std::fs::write(fx.dir.join("repo/src/many.jsonl"), many).unwrap();
}

const LONG_NOTE: &str = "not fully searched: 1 file has a line longer than 4 MiB and was read only up to it: src/huge.min.js\n";

#[test]
fn a_line_too_long_to_search_is_reported_not_skipped_silently() {
    let fx = Fx::new("longlines", 0);
    long_lines(&fx);

    // A long line within the limit: found, shown as a window on the match.
    let (out, _) = fx.run(&["search", "needle_w"]);
    assert!(
        out.starts_with("1 match in 1 file for /needle_w/\n"),
        "{out}"
    );
    let hit = out.lines().find(|l| l.starts_with("1\t")).expect("a hit");
    assert_eq!(
        hit,
        format!("1\t…{} needle_w {}…", "a".repeat(99), "a".repeat(191)),
        "{out}"
    );
    // The other file in scope has a line over the limit, and the answer
    // says so; a search that does not reach that file has nothing to add.
    assert!(out.ends_with(LONG_NOTE), "{out}");
    let (out, _) = fx.run(&["search", "needle_w", &fx.p("repo/src/wide.min.js")]);
    assert!(
        out.starts_with("1 match in 1 file for /needle_w/\n"),
        "{out}"
    );
    assert!(!out.contains("not fully searched"), "{out}");

    // What precedes a line over the limit is found; the answer says the
    // file was not read to its end.
    let (out, _) = fx.run(&["search", "needle_early"]);
    assert!(
        out.starts_with("1 match in 1 file for /needle_early/\n"),
        "{out}"
    );
    assert!(out.contains("1\tvar needle_early = 1;\n"), "{out}");
    assert!(out.ends_with(LONG_NOTE), "{out}");

    // What follows it is not reached, and absence is not claimed outright.
    let (out, _) = fx.run(&["search", "needle_late"]);
    assert!(out.starts_with("No matches for /needle_late/"), "{out}");
    assert!(out.ends_with(LONG_NOTE), "{out}");

    // Hits on long lines keep what output needs, in text and JSON.
    let (out, _) = fx.run(&["search", "needle_many", "--output", "files"]);
    assert!(
        out.starts_with("300 matches in 1 file for /needle_many/\n"),
        "{out}"
    );
    let (out, err) = fx.run(&["search", "needle_many", "--json"]);
    // Machine-readable output carries the note on stderr.
    assert!(err.contains(LONG_NOTE), "{err}");
    assert!(!out.contains("not fully searched"), "{out}");
    let first: Value = serde_json::from_str(out.lines().next().unwrap()).unwrap();
    assert_eq!(first["clipped"], json!(true), "{first}");
    let text = first["text"].as_str().unwrap();
    assert_eq!(text.len(), 1000);
    assert!(
        text.starts_with("{\"k\":\"needle_many\",\"i\":0,\"v\":\"xxx"),
        "{text}"
    );
}

/// Symbol lookups pick files by a text search first. A source file with a
/// line too long for that search is still parsed, so a definition after
/// the line is found.
#[test]
fn a_definition_after_a_very_long_line_is_still_found() {
    let fx = Fx::new("longdef", 0);
    let mut generated = b"pub fn before_long() -> u32 {\n    1\n}\n// ".to_vec();
    generated.extend(std::iter::repeat_n(b'c', 5 << 20));
    generated.extend_from_slice(b"\npub fn after_long() -> u32 {\n    before_long()\n}\n");
    std::fs::write(fx.dir.join("repo/src/generated.rs"), generated).unwrap();
    std::fs::write(
        fx.dir.join("repo/src/user.rs"),
        "pub fn user() -> u32 {\n    crate::generated::after_long()\n}\n",
    )
    .unwrap();

    let (out, _) = fx.run(&["read", "#after_long"]);
    assert!(out.contains("src/generated.rs:"), "{out}");
    assert!(out.contains("pub fn after_long() -> u32 {"), "{out}");

    let (out, _) = fx.run(&["trace", "after_long"]);
    assert!(
        out.starts_with("==> callers of after_long (src/generated.rs:"),
        "{out}"
    );
    assert!(out.contains("crate::generated::after_long()"), "{out}");
}
