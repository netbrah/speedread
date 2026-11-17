//! Path and scope contract (docs/specs/01-path-and-scope-contract.md).
//!
//! Every test drives the built binary with an explicit working directory, so
//! CWD independence is exercised for real rather than assumed.

use std::io::{BufRead, BufReader, Write};
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStdin, ChildStdout, Command, Stdio};

use serde_json::{Value, json};

/// Two sibling repositories that both define `target`, plus two "worktrees"
/// holding the same relative file with different content.
struct Fx {
    dir: PathBuf,
}

impl Fx {
    fn new(name: &str) -> Fx {
        let dir =
            std::env::temp_dir().join(format!("speedread-fork-{name}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let dir = dir.canonicalize().unwrap();
        let w = |rel: &str, text: &str| {
            let p = dir.join(rel);
            std::fs::create_dir_all(p.parent().unwrap()).unwrap();
            std::fs::write(p, text).unwrap();
        };
        // Repo `a`: a regular checkout (`.git` directory).
        std::fs::create_dir_all(dir.join("a/.git")).unwrap();
        w(
            "a/src/lib.rs",
            "pub fn target() -> u32 {\n    1\n}\n\npub fn a_caller() -> u32 {\n    target()\n}\n",
        );
        // Repo `b`: a linked worktree or submodule (`.git` file).
        w("b/.git", "gitdir: /nonexistent/worktrees/b\n");
        w(
            "b/src/lib.rs",
            "pub fn target() -> u32 {\n    2\n}\n\npub fn b_caller() -> u32 {\n    target() + 1\n}\n",
        );
        w(
            "b/src/other.rs",
            "pub fn b_other() -> u32 {\n    crate::target()\n}\n",
        );
        w("w1/f.rs", "pub fn which() -> u32 {\n    1\n}\n");
        w("w2/f.rs", "pub fn which() -> u32 {\n    2222\n}\n");
        // A fake home keeps dependency caches and `~` display deterministic.
        w(
            "home/.cargo/registry/src/idx/dep-1.0/src/lib.rs",
            "pub fn dep_fn() -> u32 {\n    7\n}\n",
        );
        w(
            "a/src/uses_dep.rs",
            "pub fn calls_dep() -> u32 {\n    dep_fn()\n}\n",
        );
        std::fs::create_dir_all(dir.join("elsewhere")).unwrap();
        Fx { dir }
    }

    fn p(&self, rel: &str) -> String {
        self.dir.join(rel).to_str().unwrap().to_string()
    }

    fn cmd(&self, cwd: &str) -> Command {
        let mut c = Command::new(env!("CARGO_BIN_EXE_speedread"));
        c.current_dir(self.dir.join(cwd))
            .env_remove("CLAUDE_PROJECT_DIR")
            .env_remove("SPEEDREAD_NO_CWD_ROOT")
            .env("HOME", self.dir.join("home"))
            .env("SPEEDREAD_STATS", "1");
        c
    }

    /// (stdout, stderr, success)
    fn run(&self, cwd: &str, args: &[&str]) -> (String, String, bool) {
        let out = self.cmd(cwd).args(args).output().expect("run speedread");
        (
            String::from_utf8_lossy(&out.stdout).into_owned(),
            String::from_utf8_lossy(&out.stderr).into_owned(),
            out.status.success(),
        )
    }

    fn ok(&self, cwd: &str, args: &[&str]) -> String {
        let (out, err, ok) = self.run(cwd, args);
        assert!(ok, "speedread {args:?} failed: {err}");
        out
    }
}

impl Drop for Fx {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.dir);
    }
}

/// Directory walks reported by `SPEEDREAD_STATS=1`.
fn walks(stderr: &str) -> u64 {
    let line = stderr
        .lines()
        .find(|l| l.starts_with("speedread-stats"))
        .unwrap_or_else(|| panic!("no stats line in {stderr:?}"));
    line.split_whitespace()
        .find_map(|kv| kv.strip_prefix("walks="))
        .and_then(|v| v.parse().ok())
        .unwrap_or_else(|| panic!("no walks= in {line:?}"))
}

const HARNESS: &[&str] = &["--unrestricted", "--no-cwd-root"];

fn with<'a>(pre: &[&'a str], rest: &[&'a str]) -> Vec<&'a str> {
    pre.iter().chain(rest).copied().collect()
}

#[test]
fn absolute_targets_do_not_depend_on_the_cwd() {
    let fx = Fx::new("abs");
    let abs = fx.p("b/src/lib.rs");
    let target = format!("{abs}#target");
    let from_elsewhere = fx.ok("elsewhere", &with(HARNESS, &["read", &target]));
    assert!(from_elsewhere.contains("\t    2\n"), "{from_elsewhere}");
    let from_root = fx.ok("/", &with(HARNESS, &["read", &target]));
    assert_eq!(from_elsewhere, from_root);
    // The same request expressed relative to an explicit base.
    let b = fx.p("b");
    let based = fx.ok(
        "elsewhere",
        &with(HARNESS, &["--base-dir", &b, "read", "src/lib.rs#target"]),
    );
    assert_eq!(based.replace("src/lib.rs", &abs), from_elsewhere);
    // …and identical to running inside the repository the classic way.
    let classic = fx.ok("b", &["read", "src/lib.rs#target"]);
    assert_eq!(classic, based);
}

#[test]
fn trace_searches_the_targets_repository_not_the_cwd() {
    let fx = Fx::new("trace");
    let target = format!("{}#target", fx.p("b/src/lib.rs"));
    // CWD (and therefore the primary root) is repo `a`.
    let out = fx.ok("a", &["--unrestricted", "trace", &target]);
    assert!(out.contains("b_caller"), "{out}");
    assert!(out.contains("b_other"), "{out}");
    assert!(!out.contains("a_caller"), "{out}");
    // Refs and callees follow the same rule.
    let refs = fx.ok("a", &["--unrestricted", "trace", &target, "-d", "refs"]);
    assert!(
        refs.contains("b_other") && !refs.contains("a_caller"),
        "{refs}"
    );
    let callee = format!("{}#b_caller", fx.p("b/src/lib.rs"));
    let out = fx.ok("a", &["--unrestricted", "trace", &callee, "-d", "callees"]);
    // The callee resolves to b's definition (upstream resolved it to a's).
    let resolved = format!("→ {}:1-3 pub fn target", fx.p("b/src/lib.rs"));
    assert!(out.contains(&resolved), "{out}");
}

#[test]
fn secondary_roots_are_searched_for_their_own_targets() {
    let fx = Fx::new("multi");
    let (a, b) = (fx.p("a"), fx.p("b"));
    let target = format!("{}#target", fx.p("b/src/lib.rs"));
    let out = fx.ok("elsewhere", &["--root", &a, "--root", &b, "trace", &target]);
    assert!(
        out.contains("b_caller") && !out.contains("a_caller"),
        "{out}"
    );
}

#[test]
fn base_dir_scopes_pathless_lookups() {
    let fx = Fx::new("base");
    let b = fx.p("b");
    let pre = with(HARNESS, &["--base-dir", &b]);
    let out = fx.ok("a", &with(&pre, &["read", "#b_other"]));
    assert!(out.starts_with("==> src/other.rs:1-3 @"), "{out}");
    let out = fx.ok("a", &with(&pre, &["trace", "#target"]));
    assert!(
        out.contains("b_caller") && !out.contains("a_caller"),
        "{out}"
    );
    let out = fx.ok("a", &with(&pre, &["search", "fn target"]));
    assert!(out.contains("==> src/lib.rs @"), "{out}");
    assert!(
        out.contains("\t    2\n") || out.contains("pub fn target() -> u32"),
        "{out}"
    );
    assert_eq!(out.matches("==> ").count(), 1, "{out}");
    let out = fx.ok("a", &with(&pre, &["map"]));
    assert!(
        out.contains("other.rs") && !out.contains("uses_dep.rs"),
        "{out}"
    );
    // base_dir is the scope exactly: it is not widened to the repository.
    let src = fx.p("a/src");
    std::fs::write(fx.dir.join("a/top.rs"), "pub fn target_top() {}\n").unwrap();
    let out = fx.ok(
        "elsewhere",
        &with(HARNESS, &["--base-dir", &src, "search", "target"]),
    );
    assert!(
        out.contains("==> lib.rs @") && !out.contains("top.rs"),
        "{out}"
    );
    // …while a symbol lookup from there covers the repository it sits in.
    let out = fx.ok(
        "elsewhere",
        &with(HARNESS, &["--base-dir", &src, "read", "#target_top"]),
    );
    assert!(out.contains("target_top"), "{out}");
    assert!(out.contains("top.rs"), "{out}");
}

#[test]
fn no_base_means_no_guessing_and_no_walks() {
    let fx = Fx::new("nobase");
    // The CWD is a real repository; with --no-cwd-root it must be ignored.
    for args in [
        &["search", "target"][..],
        &["map"],
        &["read", "src/lib.rs"],
        &["read", "#target"],
        &["read", "src/*.rs"],
        &["trace", "#target"],
        &["trace", "src/lib.rs#target"],
        &["symbols"],
        &["search", "target", "--json"],
        &["map", "--json"],
    ] {
        let (out, err, _) = fx.run("a", &with(HARNESS, args));
        let all = format!("{out}{err}");
        assert!(all.contains("base_dir"), "{args:?}: {all}");
        assert!(all.contains("absolute path"), "{args:?}: {all}");
        assert!(!out.contains("a_caller"), "{args:?}: {out}");
        assert_eq!(walks(&err), 0, "{args:?} walked: {err}");
    }
    // The environment variable is equivalent to the flag.
    let out = fx
        .cmd("a")
        .env("SPEEDREAD_NO_CWD_ROOT", "1")
        .args(["--unrestricted", "search", "target"])
        .output()
        .unwrap();
    let text = String::from_utf8_lossy(&out.stdout);
    assert!(text.contains("base_dir"), "{text}");
    // Absolute paths keep working, and are displayed so they round-trip.
    let abs = fx.p("a/src");
    let out = fx.ok("a", &with(HARNESS, &["search", "a_caller", &abs]));
    let header = format!("==> {}/lib.rs @", abs);
    assert!(out.contains(&header), "{out}");
}

#[test]
fn a_server_started_in_slash_never_walks_the_filesystem() {
    let fx = Fx::new("slash");
    for args in [
        &["search", "zzz_not_there"][..],
        &["map"],
        &["read", "#zzz_not_there"],
    ] {
        let (out, err, _) = fx.run("/", args);
        assert_eq!(walks(&err), 0, "{args:?} walked: {err}");
        assert!(
            format!("{out}{err}").contains("--root"),
            "{args:?}: {out}{err}"
        );
    }
}

#[test]
fn base_dir_is_validated_not_guessed() {
    let fx = Fx::new("validate");
    let cases: Vec<(Vec<String>, &str)> = vec![
        (
            vec!["--unrestricted".into(), "--base-dir".into(), "b".into()],
            "absolute",
        ),
        (
            vec![
                "--unrestricted".into(),
                "--base-dir".into(),
                fx.p("missing"),
            ],
            "does not exist",
        ),
        (
            vec![
                "--unrestricted".into(),
                "--base-dir".into(),
                fx.p("b/src/lib.rs"),
            ],
            "not a directory",
        ),
        (
            vec!["--root".into(), fx.p("a"), "--base-dir".into(), fx.p("b")],
            "outside the workspace",
        ),
    ];
    for (pre, want) in cases {
        let mut args: Vec<&str> = pre.iter().map(String::as_str).collect();
        args.extend(["search", "target"]);
        let (out, err, ok) = fx.run("a", &args);
        assert!(!ok, "{args:?} should fail: {out}");
        assert!(err.contains(want), "{args:?}: expected {want:?} in {err}");
        assert!(err.contains("base_dir"), "{args:?}: {err}");
        assert!(!out.contains("a_caller"), "{out}");
    }
}

#[test]
fn displayed_paths_round_trip() {
    let fx = Fx::new("display");
    let src = fx.p("b/src");
    let pre = with(HARNESS, &["--base-dir", &src]);
    // Under the base: relative to it.
    let out = fx.ok("elsewhere", &with(&pre, &["search", "b_other"]));
    assert!(out.contains("==> other.rs @"), "{out}");
    let back = fx.ok("elsewhere", &with(&pre, &["read", "other.rs"]));
    assert!(back.starts_with("==> other.rs @"), "{back}");
    // Outside the base: absolute, never a path relative to something else.
    let a = fx.p("a/src");
    let out = fx.ok("elsewhere", &with(&pre, &["search", "a_caller", &a]));
    let shown = out
        .lines()
        .find_map(|l| l.strip_prefix("==> "))
        .and_then(|l| l.split(" @").next())
        .unwrap_or_else(|| panic!("no header in {out}"))
        .to_string();
    assert_eq!(shown, fx.p("a/src/lib.rs"));
    let back = fx.ok("elsewhere", &with(&pre, &["read", &shown]));
    assert!(back.contains("a_caller"), "{back}");
}

#[test]
fn dependency_targets_are_traced_into_the_workspace() {
    // Unchanged behaviour: who in *my project* calls this dependency function.
    let fx = Fx::new("deps");
    let dep = format!(
        "{}#dep_fn",
        fx.p("home/.cargo/registry/src/idx/dep-1.0/src/lib.rs")
    );
    let out = fx.ok("a", &["trace", &dep]);
    assert!(out.contains("calls_dep"), "{out}");
}

#[test]
fn lookup_root_falls_back_to_the_targets_directory() {
    // No configured root and no repository: only the target's directory is
    // searched (never a parent chosen by guesswork).
    let fx = Fx::new("fallback");
    std::fs::write(
        fx.dir.join("w1/g.rs"),
        "pub fn g() -> u32 {\n    crate::which()\n}\n",
    )
    .unwrap();
    let target = format!("{}#which", fx.p("w1/f.rs"));
    let (out, err, ok) = fx.run("elsewhere", &with(HARNESS, &["trace", &target]));
    assert!(ok, "{err}");
    assert!(out.contains("g.rs"), "{out}");
    assert!(!out.contains("w2"), "{out}");
}

struct Client {
    child: Child,
    stdin: ChildStdin,
    stdout: BufReader<ChildStdout>,
    next: u64,
}

impl Client {
    fn spawn(fx: &Fx, cwd: &str, args: &[&str]) -> Client {
        let mut child = fx
            .cmd(cwd)
            .args(args)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn()
            .expect("spawn speedread");
        let stdin = child.stdin.take().unwrap();
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
        writeln!(self.stdin, "{v}").unwrap();
        self.stdin.flush().unwrap();
    }

    fn rpc(&mut self, method: &str, params: Value) -> Value {
        self.next += 1;
        let id = self.next;
        self.send(json!({"jsonrpc": "2.0", "id": id, "method": method, "params": params}));
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
}

impl Drop for Client {
    fn drop(&mut self) {
        let _ = self.child.kill();
    }
}

fn etag(out: &str) -> &str {
    let at = out.find(" @").expect("etag") + 2;
    &out[at..at + 16]
}

#[test]
fn mcp_base_dir_on_every_tool() {
    let fx = Fx::new("mcp");
    let mut c = Client::spawn(
        &fx,
        "elsewhere",
        &["--unrestricted", "--no-cwd-root", "mcp"],
    );
    let tools = c.rpc("tools/list", json!({}));
    for t in tools["result"]["tools"].as_array().unwrap() {
        assert!(
            t["inputSchema"]["properties"].get("base_dir").is_some(),
            "{} lacks base_dir",
            t["name"]
        );
    }
    let b = fx.p("b");
    // Without a base there is nothing to resolve against.
    let out = c.call("search", json!({"pattern": "b_caller"}));
    assert!(out.contains("base_dir"), "{out}");
    let out = c.call("read", json!({"targets": ["src/lib.rs"]}));
    assert!(out.contains("base_dir"), "{out}");
    // With one, every tool behaves as if started in that directory.
    let out = c.call("search", json!({"pattern": "b_caller", "base_dir": b}));
    assert!(out.contains("==> src/lib.rs @"), "{out}");
    let out = c.call(
        "read",
        json!({"targets": ["src/other.rs#b_other"], "cwd": b}),
    );
    assert!(out.starts_with("==> src/other.rs:1-3 @"), "{out}");
    let out = c.call("map", json!({"base_dir": b}));
    assert!(out.contains("other.rs"), "{out}");
    let out = c.call("trace", json!({"target": "#target", "base_dir": b}));
    assert!(
        out.contains("b_caller") && !out.contains("a_caller"),
        "{out}"
    );
    // Validation errors are tool output, not protocol failures.
    let out = c.call(
        "search",
        json!({"pattern": "x", "base_dir": "relative/dir"}),
    );
    assert!(
        out.contains("base_dir") && out.contains("absolute"),
        "{out}"
    );
    // Bases are per call: one session serves two worktrees without mixing
    // identically named files.
    let (w1, w2) = (fx.p("w1"), fx.p("w2"));
    let r1 = c.call("read", json!({"targets": ["f.rs"], "base_dir": w1}));
    let r2 = c.call("read", json!({"targets": ["f.rs"], "base_dir": w2}));
    assert!(
        r1.contains("\t    1\n") && r2.contains("\t    2222\n"),
        "{r1}{r2}"
    );
    assert_ne!(etag(&r1), etag(&r2));
    let again = c.call("read", json!({"targets": ["f.rs"], "base_dir": w1}));
    assert_eq!(again, r1);
    // The server's own working directory never changed.
    let out = c.call("map", json!({}));
    assert!(out.contains("base_dir"), "{out}");
}

#[test]
fn mcp_default_behaviour_is_unchanged_without_base_dir() {
    let fx = Fx::new("mcpdefault");
    let a = fx.p("a");
    let mut c = Client::spawn(&fx, "elsewhere", &["--root", &a, "mcp"]);
    let out = c.call("search", json!({"pattern": "a_caller"}));
    assert!(out.contains("==> src/lib.rs @"), "{out}");
    // A base inside the root narrows the default scope.
    let src: &Path = &fx.dir.join("a/src");
    let out = c.call("search", json!({"pattern": "a_caller", "base_dir": src}));
    assert!(out.contains("==> lib.rs @"), "{out}");
    // A base outside the access policy is refused.
    let out = c.call(
        "search",
        json!({"pattern": "target", "base_dir": fx.p("b")}),
    );
    assert!(
        out.contains("outside the workspace") && !out.contains("b_caller"),
        "{out}"
    );
}
