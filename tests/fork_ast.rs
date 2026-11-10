//! Structural search (docs/specs/04-structural-search.md).

use std::path::PathBuf;
use std::process::Command;

const RUST: &str = "\
// value.unwrap() in a comment is not a call
pub fn load(path: &str) -> String {
    let text = std::fs::read_to_string(path).unwrap();
    let note = \"never .unwrap() in a string\";
    text + note
}

pub fn chain(input: Option<&str>) -> usize {
    input
        .map(str::len)
        .unwrap()
}

pub fn safe(x: Option<u32>) -> u32 {
    x.unwrap_or(0)
}

pub fn same(x: u32, y: u32) -> bool {
    log(x);
    x == x || x == y
}
";

const TS: &str = "\
export function run(a: string, b: string) {
  console.log(a);
  console.log(a, b);
  console.log();
  // console.log(a)
  const s = \"console.log(a)\";
  return s;
}
";

const TSX: &str = "\
export function View(props: { name: string }) {
  console.log(props.name);
  return <b>{props.name}</b>;
}
";

const PY: &str = "\
def work(item):
    log(item)
    # log(nothing)
    return item
";

const GO: &str = "\
package main

func handle(w Writer) {
\tlog(w)
}
";

struct Fx {
    dir: PathBuf,
}

impl Fx {
    fn new(name: &str) -> Fx {
        let dir = std::env::temp_dir().join(format!("speedread-ast-{name}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(dir.join("src")).unwrap();
        let dir = dir.canonicalize().unwrap();
        for (rel, text) in [
            ("src/lib.rs", RUST),
            ("src/app.ts", TS),
            ("src/view.tsx", TSX),
            ("src/job.py", PY),
            ("src/main.go", GO),
        ] {
            std::fs::write(dir.join(rel), text).unwrap();
        }
        Fx { dir }
    }

    fn cmd(&self, args: &[&str]) -> Command {
        let mut c = Command::new(env!("CARGO_BIN_EXE_speedread"));
        c.arg("--root")
            .arg(&self.dir)
            .arg("search")
            .arg("--ast")
            .args(args)
            .current_dir("/");
        c
    }

    fn run(&self, args: &[&str]) -> String {
        let out = self.cmd(args).output().unwrap();
        assert!(out.status.success(), "{:?}", out);
        String::from_utf8(out.stdout).unwrap()
    }
}

impl Drop for Fx {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.dir);
    }
}

/// Hit lines (`N\ttext…`) of an answer, in order.
fn hits(out: &str) -> Vec<&str> {
    out.lines()
        .filter(|l| {
            l.split_once('\t')
                .is_some_and(|(n, _)| !n.is_empty() && n.bytes().all(|b| b.is_ascii_digit()))
        })
        .collect()
}

#[test]
fn rust_method_call_not_the_word() {
    let fx = Fx::new("rust");
    let out = fx.run(&["$V.unwrap()"]);
    assert!(
        out.starts_with("2 matches in 1 file for structural `$V.unwrap()` (rust)\n"),
        "{out}"
    );
    assert_eq!(
        hits(&out),
        vec![
            "3\t    let text = std::fs::read_to_string(path).unwrap();\t$V=std::fs::read_to_string(path)",
            // A chain spanning lines is shown where the literal is.
            "11\t        .unwrap()\t$V=input .map(str::len)",
        ],
        "{out}"
    );
    // Grouped under the enclosing symbols like any search.
    assert!(
        out.contains("[1-6] pub fn load(path: &str) -> String\n"),
        "{out}"
    );
    assert!(
        out.contains("[8-12] pub fn chain(input: Option<&str>) -> usize\n"),
        "{out}"
    );
    // The text search this replaces also hits the comment, the string and
    // `unwrap_or`.
    let text = Command::new(env!("CARGO_BIN_EXE_speedread"))
        .arg("--root")
        .arg(&fx.dir)
        .args(["search", "unwrap", "src/lib.rs"])
        .current_dir("/")
        .output()
        .unwrap();
    assert_eq!(hits(&String::from_utf8(text.stdout).unwrap()).len(), 5);
}

#[test]
fn typescript_one_argument_or_any() {
    let fx = Fx::new("ts");
    let one = fx.run(&["console.log($A)"]);
    assert_eq!(
        hits(&one),
        vec![
            "2\t  console.log(a);\t$A=a",
            "2\t  console.log(props.name);\t$A=props.name",
        ],
        "{one}"
    );
    assert!(one.contains("(typescript, tsx)"), "{one}");
    let any = fx.run(&["console.log($$$ARGS)", "src/app.ts"]);
    assert_eq!(
        hits(&any),
        vec![
            "2\t  console.log(a);\t$$$ARGS=a",
            "3\t  console.log(a, b);\t$$$ARGS=a, b",
            "4\t  console.log();\t$$$ARGS=",
        ],
        "{any}"
    );
    // `--lang ts` covers .tsx; `--lang tsx` only .tsx.
    let ts = fx.run(&["console.log($A)", "--lang", "ts"]);
    assert_eq!(hits(&ts).len(), 2, "{ts}");
    let tsx = fx.run(&["console.log($A)", "--lang", "tsx"]);
    assert_eq!(
        hits(&tsx),
        vec!["2\t  console.log(props.name);\t$A=props.name"],
        "{tsx}"
    );
}

#[test]
fn every_language_that_parses_the_pattern_runs() {
    let fx = Fx::new("mixed");
    let out = fx.run(&["log($A)"]);
    assert!(
        out.starts_with("3 matches in 3 files for structural `log($A)` (rust, python, go)\n"),
        "{out}"
    );
    assert_eq!(
        hits(&out),
        vec![
            "2\t    log(item)\t$A=item",
            "19\t    log(x);\t$A=x",
            "4\t\tlog(w)\t$A=w"
        ],
        "{out}"
    );
    let py = fx.run(&["log($A)", "--lang", "python"]);
    assert_eq!(hits(&py), vec!["2\t    log(item)\t$A=item"], "{py}");
}

#[test]
fn prefilter_does_not_change_the_answer() {
    let fx = Fx::new("prefilter");
    for pattern in ["$V.unwrap()", "console.log($$$A)", "log($A)"] {
        for extra in [&[][..], &["--output", "files"][..], &["-C", "1"][..]] {
            let mut args = vec![pattern];
            args.extend_from_slice(extra);
            let with = fx.run(&args);
            let without = fx
                .cmd(&args)
                .env("SPEEDREAD_AST_NO_PREFILTER", "1")
                .output()
                .unwrap();
            assert_eq!(
                with,
                String::from_utf8(without.stdout).unwrap(),
                "{pattern} {extra:?}"
            );
        }
    }
}

#[test]
fn a_repeated_metavariable_must_match_the_same_code() {
    let fx = Fx::new("same");
    // No literal: parsing everything needs an explicit scope.
    let refused = fx.run(&["$A == $A"]);
    assert!(
        refused.starts_with("Pattern `$A == $A` has no literal word"),
        "{refused}"
    );
    assert!(refused.contains("Give a path or glob"), "{refused}");
    let out = fx.run(&["$A == $A", "src/lib.rs"]);
    assert_eq!(hits(&out), vec!["20\t    x == x || x == y\t$A=x"], "{out}");
    let globbed = fx.run(&["$A == $A", "-g", "*.rs"]);
    assert_eq!(hits(&globbed), hits(&out), "{globbed}");
}

#[test]
fn errors_and_absence_say_what_was_examined() {
    let fx = Fx::new("errors");
    let bad = fx.run(&["fn (", "--lang", "rust"]);
    assert!(bad.starts_with("Invalid rust pattern `fn (`: "), "{bad}");
    let unknown = fx.run(&["log($A)", "--lang", "klingon"]);
    assert!(
        unknown.starts_with("Unknown language `klingon`"),
        "{unknown}"
    );
    let none = fx.run(&["missing_call($A)"]);
    assert!(
        none.starts_with(
            "No structural matches for `missing_call($A)` in . (0 files containing `missing_call`, 0 parsed)."
        ),
        "{none}"
    );
    // The word is there, the syntax is not.
    let none = fx.run(&["unwrap($A)", "--lang", "rust"]);
    assert_eq!(
        none,
        "No structural matches for `unwrap($A)` in . (1 file containing `unwrap`, 1 parsed). Ran for: rust.\n"
    );
    let other = fx.run(&["unwrap($A)", "--lang", "go"]);
    assert!(other.contains("None of them is go."), "{other}");
}

#[test]
fn output_modes_context_and_json() {
    let fx = Fx::new("modes");
    let files = fx.run(&["$V.unwrap()", "--output", "files"]);
    assert!(files.contains("==> src/lib.rs"), "{files}");
    assert!(files.contains("(2)\n"), "{files}");
    let symbols = fx.run(&["log($A)", "--lang", "go", "--output", "symbols"]);
    assert!(symbols.contains("func handle(w Writer) {"), "{symbols}");
    let scopes = fx.run(&["log($A)", "--lang", "rust", "--output", "scopes"]);
    assert_eq!(hits(&scopes).len(), 1, "{scopes}");
    let context = fx.run(&["log($A)", "--lang", "python", "-C", "1"]);
    assert_eq!(
        hits(&context),
        vec![
            "1\tdef work(item):",
            "2\t    log(item)\t$A=item",
            "3\t    # log(nothing)"
        ],
        "{context}"
    );
    let json = fx.run(&["log($A)", "--lang", "python", "--json"]);
    let rows: Vec<serde_json::Value> = json
        .lines()
        .map(|l| serde_json::from_str(l).unwrap())
        .collect();
    assert_eq!(rows.len(), 1, "{json}");
    assert_eq!(rows[0]["line"], 2, "{json}");
    assert!(
        rows[0]["path"].as_str().unwrap().ends_with("src/job.py"),
        "{json}"
    );
}

#[test]
fn mcp_search_takes_ast_and_lang() {
    use std::io::{BufRead, BufReader, Write};
    use std::process::Stdio;
    let fx = Fx::new("mcp");
    let mut child = Command::new(env!("CARGO_BIN_EXE_speedread"))
        .arg("--root")
        .arg(&fx.dir)
        .arg("mcp")
        .current_dir("/")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .unwrap();
    let mut stdin = child.stdin.take().unwrap();
    let mut stdout = BufReader::new(child.stdout.take().unwrap());
    let mut rpc = |msg: serde_json::Value, id: Option<u64>| -> serde_json::Value {
        writeln!(stdin, "{msg}").unwrap();
        stdin.flush().unwrap();
        let Some(id) = id else {
            return serde_json::Value::Null;
        };
        loop {
            let mut line = String::new();
            assert!(stdout.read_line(&mut line).unwrap() > 0);
            let v: serde_json::Value = serde_json::from_str(&line).unwrap();
            if v["id"] == id {
                return v;
            }
        }
    };
    rpc(
        serde_json::json!({"jsonrpc":"2.0","id":1,"method":"initialize","params":{
            "protocolVersion":"2025-06-18","capabilities":{},
            "clientInfo":{"name":"t","version":"0"}}}),
        Some(1),
    );
    rpc(
        serde_json::json!({"jsonrpc":"2.0","method":"notifications/initialized"}),
        None,
    );
    let tools = rpc(
        serde_json::json!({"jsonrpc":"2.0","id":2,"method":"tools/list"}),
        Some(2),
    );
    let search = tools["result"]["tools"]
        .as_array()
        .unwrap()
        .iter()
        .find(|t| t["name"] == "search")
        .unwrap()
        .clone();
    let props = &search["inputSchema"]["properties"];
    assert_eq!(props["ast"]["type"], "boolean", "{props}");
    assert_eq!(props["lang"]["type"], "string", "{props}");
    let res = rpc(
        serde_json::json!({"jsonrpc":"2.0","id":3,"method":"tools/call","params":{
            "name":"search","arguments":{"pattern":"$V.unwrap()","ast":true,"lang":"rust"}}}),
        Some(3),
    );
    let text = res["result"]["content"][0]["text"].as_str().unwrap();
    assert!(
        text.starts_with("2 matches in 1 file for structural `$V.unwrap()` (rust)\n"),
        "{text}"
    );
    drop(stdin);
    let _ = child.wait();
}
