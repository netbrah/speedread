//! `output=auto` collapses to `matches` or `symbols`. The boundary is the
//! shipped N/K pair (see `choose_auto` unit tests for the inequality itself).

use std::io::{BufRead, BufReader, Write};
use std::path::PathBuf;
use std::process::{Command, Stdio};

use serde_json::json;
use speedread::search::{AUTO_BUDGET_PERCENT, AUTO_MAX_SYMBOLS, SYMBOL_INLINE_MAX_SPAN};

const NEEDLE: &str = "auto_needle_zz";

struct Fx {
    dir: PathBuf,
}

impl Fx {
    fn new(name: &str) -> Fx {
        let dir = std::env::temp_dir().join(format!(
            "speedread-auto-{name}-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(dir.join("src")).unwrap();
        Fx {
            dir: dir.canonicalize().unwrap(),
        }
    }

    fn write(&self, rel: &str, text: &str) {
        std::fs::write(self.dir.join(rel), text).unwrap();
    }

    fn run(&self, args: &[&str]) -> String {
        let out = Command::new(env!("CARGO_BIN_EXE_speedread"))
            .arg("--root")
            .arg(&self.dir)
            .args(args)
            .current_dir("/")
            .output()
            .unwrap();
        assert!(
            out.status.success(),
            "status {:?}\nstdout {}\nstderr {}",
            out.status,
            String::from_utf8_lossy(&out.stdout),
            String::from_utf8_lossy(&out.stderr)
        );
        String::from_utf8(out.stdout).unwrap()
    }

    fn search(&self, output: &str) -> String {
        self.run(&["search", NEEDLE, "--output", output])
    }

    fn search_at(&self, output: &str, budget: &str) -> String {
        self.run(&[
            "--max-budget",
            budget,
            "--budget",
            budget,
            "search",
            NEEDLE,
            "--output",
            output,
        ])
    }
}

impl Drop for Fx {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.dir);
    }
}

fn funcs(n: usize) -> String {
    let mut s = String::new();
    for i in 0..n {
        s.push_str(&format!(
            "pub fn f{i}() {{\n    let marker_{i} = {i};\n    let hit = \"{NEEDLE}\";\n    let _ = (marker_{i}, hit);\n}}\n"
        ));
    }
    s
}

/// `n` source lines, all inside one function. Span shown in the header is `n - 1`.
fn span_fn(n: usize) -> String {
    let mut lines = vec![
        "pub fn span_case() {".to_string(),
        "    let marker_span = 1;".to_string(),
    ];
    while lines.len() + 2 < n {
        lines.push("    let p = 1;".to_string());
    }
    lines.push(format!("    let hit = \"{NEEDLE}\";"));
    lines.push("}".to_string());
    assert_eq!(lines.len(), n, "fixture line count");
    lines.join("\n") + "\n"
}

fn header_span(out: &str) -> u32 {
    let caps = regex_span(out);
    caps.1 - caps.0
}

fn regex_span(out: &str) -> (u32, u32) {
    for line in out.lines() {
        let Some(rest) = line.strip_prefix('[') else {
            continue;
        };
        let Some((range, _)) = rest.split_once("] ") else {
            continue;
        };
        let Some((a, b)) = range.split_once('-') else {
            continue;
        };
        if let (Ok(a), Ok(b)) = (a.parse::<u32>(), b.parse::<u32>()) {
            return (a, b);
        }
    }
    panic!("no symbol header in:\n{out}");
}

#[test]
fn default_output_stays_matches() {
    let fx = Fx::new("default");
    fx.write("src/one.rs", &funcs(1));
    let bare = fx.run(&["search", NEEDLE]);
    let matches = fx.search("matches");
    let auto = fx.search("auto");
    assert_eq!(bare, matches, "omitting --output must stay matches");
    assert_ne!(
        auto, matches,
        "this fixture is on the inline side of the policy, so auto is not the default"
    );
    assert_eq!(auto, fx.search("symbols"));
}

#[test]
fn auto_inlines_at_n_symbols_and_matches_past_it() {
    let fx = Fx::new("count");
    fx.write("src/at.rs", &funcs(AUTO_MAX_SYMBOLS));
    let auto = fx.search("auto");
    assert_eq!(
        auto,
        fx.search("symbols"),
        "N symbols whose bodies fit are inlined"
    );
    assert!(
        auto.contains("marker_0"),
        "inlined body should include a non-hit line:\n{auto}"
    );

    fx.write("src/at.rs", &funcs(AUTO_MAX_SYMBOLS + 1));
    let auto = fx.search("auto");
    let matches = fx.search("matches");
    assert_eq!(auto, matches, "N+1 symbols stay grouped match lines");
    assert!(
        !auto.contains("marker_0"),
        "matches rendering has the hit, not the sibling line:\n{auto}"
    );
    assert_ne!(auto, fx.search("symbols"));
}

#[test]
fn auto_respects_the_existing_symbol_span_cap() {
    // Large enough that K% of it holds either body, so the span cap is the
    // only reason the longer function stays match lines.
    let budget = "100000";
    let fx = Fx::new("span");
    let inlineable = SYMBOL_INLINE_MAX_SPAN as usize + 1; // end - start == 200
    fx.write("src/span.rs", &span_fn(inlineable));
    let matches = fx.search_at("matches", budget);
    assert_eq!(
        header_span(&matches),
        SYMBOL_INLINE_MAX_SPAN,
        "fixture span drifted:\n{matches}"
    );
    let auto = fx.search_at("auto", budget);
    assert_eq!(auto, fx.search_at("symbols", budget));
    assert!(
        auto.contains("marker_span"),
        "span of {} is inlined when the budget holds it:\n{auto}",
        SYMBOL_INLINE_MAX_SPAN
    );

    fx.write("src/span.rs", &span_fn(inlineable + 1));
    let matches = fx.search_at("matches", budget);
    assert_eq!(header_span(&matches), SYMBOL_INLINE_MAX_SPAN + 1);
    let auto = fx.search_at("auto", budget);
    assert_eq!(auto, matches);
    assert!(
        !auto.contains("marker_span"),
        "span past the cap is not inlined:\n{auto}"
    );
    assert_eq!(
        fx.search_at("symbols", budget),
        matches,
        "symbols mode shares the span cap"
    );
}

#[test]
fn auto_matches_when_bodies_exceed_the_budget() {
    // Long enough that the body estimate exceeds any K% of a 400-token budget
    // (K <= 100), and short enough to fit K% of the default 8000 when K >= 25.
    let mut body = String::from("pub fn wide() {\n    let marker_wide = 1;\n");
    for i in 0..80 {
        body.push_str(&format!(
            "    let pad_{i:04} = \"abcdefghijklmnopqrstuvwxyz012345\";\n"
        ));
    }
    body.push_str(&format!("    let hit = \"{NEEDLE}\";\n}}\n"));
    let fx = Fx::new("budget");
    fx.write("src/wide.rs", &body);
    let at_default = fx.search("auto");
    assert_eq!(
        at_default,
        fx.search("symbols"),
        "one symbol at the default budget is inlined:\n{at_default}"
    );
    assert!(at_default.contains("marker_wide"));
    let tight = fx.run(&["--budget", "400", "search", NEEDLE, "--output", "auto"]);
    let tight_matches = fx.run(&["--budget", "400", "search", NEEDLE, "--output", "matches"]);
    assert_eq!(
        tight, tight_matches,
        "bodies over the budget stay match lines:\n{tight}"
    );
    assert!(!tight.contains("marker_wide"), "{tight}");
}

/// One MCP session: search remembers the bytes it showed, so `path@etag` diffs.
struct Mcp {
    child: std::process::Child,
    stdin: std::process::ChildStdin,
    stdout: BufReader<std::process::ChildStdout>,
    next: u64,
}

impl Mcp {
    fn start(root: &std::path::Path) -> Mcp {
        let mut child = Command::new(env!("CARGO_BIN_EXE_speedread"))
            .args(["--root", root.to_str().unwrap(), "mcp"])
            .env_remove("CLAUDE_PROJECT_DIR")
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn()
            .unwrap();
        let stdin = child.stdin.take().unwrap();
        let stdout = BufReader::new(child.stdout.take().unwrap());
        let mut s = Mcp {
            child,
            stdin,
            stdout,
            next: 0,
        };
        s.rpc(
            "initialize",
            json!({"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "auto-test", "version": "0"}}),
        );
        writeln!(
            s.stdin,
            "{}",
            json!({"jsonrpc": "2.0", "method": "notifications/initialized"})
        )
        .unwrap();
        s.stdin.flush().unwrap();
        s
    }

    fn rpc(&mut self, method: &str, params: serde_json::Value) -> serde_json::Value {
        self.next += 1;
        let id = self.next;
        writeln!(
            self.stdin,
            "{}",
            json!({"jsonrpc": "2.0", "id": id, "method": method, "params": params})
        )
        .unwrap();
        self.stdin.flush().unwrap();
        loop {
            let mut line = String::new();
            assert!(
                self.stdout.read_line(&mut line).unwrap() > 0,
                "server closed stdout"
            );
            let v: serde_json::Value = serde_json::from_str(&line).unwrap();
            if v["method"] == "roots/list" {
                writeln!(
                    self.stdin,
                    "{}",
                    json!({"jsonrpc": "2.0", "id": v["id"], "result": {"roots": []}})
                )
                .unwrap();
                self.stdin.flush().unwrap();
                continue;
            }
            if v["id"] == json!(id) && v.get("method").is_none() {
                return v;
            }
        }
    }

    fn call(&mut self, tool: &str, args: serde_json::Value) -> String {
        let v = self.rpc("tools/call", json!({"name": tool, "arguments": args}));
        assert!(v.get("error").is_none(), "{v}");
        v["result"]["content"]
            .as_array()
            .unwrap()
            .iter()
            .filter_map(|c| c["text"].as_str())
            .collect()
    }
}

impl Drop for Mcp {
    fn drop(&mut self) {
        let _ = self.child.kill();
    }
}

#[test]
fn auto_keeps_the_etag_header_on_both_branches() {
    let fx = Fx::new("etag");
    fx.write("src/one.rs", &funcs(1));
    let mut session = Mcp::start(&fx.dir);
    let auto = session.call("search", json!({"pattern": NEEDLE, "output": "auto"}));
    let etag = auto
        .lines()
        .find_map(|l| l.split_once(" @").map(|(_, rest)| rest[..16].to_string()))
        .expect("etag");
    assert!(etag.chars().all(|c| c.is_ascii_hexdigit()), "{etag}");
    let again = session.call("read", json!({"targets": [format!("src/one.rs@{etag}")]}));
    assert!(
        again.contains("unchanged"),
        "path@etag in the same session diffs against the bytes search showed:\n{again}"
    );

    fx.write("src/one.rs", &funcs(AUTO_MAX_SYMBOLS + 1));
    let matches_branch = fx.search("auto");
    assert!(
        matches_branch.contains(" @"),
        "the matches branch still prints the etag:\n{matches_branch}"
    );
}

#[test]
fn auto_no_match_matches_the_matches_wording() {
    let fx = Fx::new("none");
    fx.write("src/one.rs", "pub fn f() {}\n");
    assert_eq!(fx.search("auto"), fx.search("matches"));
}

#[test]
fn auto_json_follows_the_same_branch() {
    let fx = Fx::new("json");
    fx.write("src/one.rs", &funcs(1));
    let auto = fx.run(&["search", NEEDLE, "--output", "auto", "--json"]);
    let symbols = fx.run(&["search", NEEDLE, "--output", "symbols", "--json"]);
    assert_eq!(auto, symbols);
    assert!(auto.contains("\"symbol\""));

    fx.write("src/one.rs", &funcs(AUTO_MAX_SYMBOLS + 1));
    let auto = fx.run(&["search", NEEDLE, "--output", "auto", "--json"]);
    let matches = fx.run(&["search", NEEDLE, "--output", "matches", "--json"]);
    assert_eq!(auto, matches);
    assert!(auto.contains("\"line\""));
}

#[test]
fn env_override_can_force_the_matches_branch() {
    let fx = Fx::new("env");
    fx.write("src/one.rs", &funcs(1));
    let out = Command::new(env!("CARGO_BIN_EXE_speedread"))
        .arg("--root")
        .arg(&fx.dir)
        .args(["search", NEEDLE, "--output", "auto"])
        .env("SPEEDREAD_AUTO_N", "0")
        .current_dir("/")
        .output()
        .unwrap();
    assert!(out.status.success());
    let auto = String::from_utf8(out.stdout).unwrap();
    assert_eq!(auto, fx.search("matches"));
    assert!(
        (25..=100).contains(&AUTO_BUDGET_PERCENT),
        "K percent shipped as {AUTO_BUDGET_PERCENT}"
    );
}
