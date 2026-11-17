//! Ancestor context for search hits (docs/specs/03-ancestor-context.md).

use std::path::PathBuf;
use std::process::Command;

const RUST: &str = "\
pub struct Batch;

pub fn process_batch(batch: &Batch) -> Result<(), String> {
    let mut sent = 0;
    for item in batch.items() {
        if let Some(session) = item.session() {
            match session.state() {
                State::Ready => {
                    send_request(session)?;
                    sent += 1;
                }
                State::Closed => {
                    drop_session(session);
                }
            }
        }
    }
    log_total(sent);
    Ok(())
}
";

const TS: &str = "\
export function route(reqs: Req[]): number {
  let n = 0;
  for (const r of reqs) {
    if (r.ok) {
      switch (r.kind) {
        case \"a\":
          n += handle(r);
          break;
        default:
          n -= 1;
      }
    }
  }
  return n;
}
";

const PY: &str = "\
def total(xs):
    acc = 0
    seen = set()
    for x in xs:
        y = x * 2
        if y in seen:
            continue
        else:
            acc += weigh(y)
    return acc
";

const C: &str = "\
int run(int n)
{
    for (int i = 0; i < n; i++)
    {
        if (i % 2)
        {
            emit(i);
        }
    }
    return 0;
}
";

const TSX: &str = "\
export function Page(props: Props) {
  return (
    <Layout title=\"x\">
      <Sidebar>
        <NavItem to={props.home} />
      </Sidebar>
    </Layout>
  );
}
";

struct Fx {
    dir: PathBuf,
}

impl Fx {
    fn new(name: &str) -> Fx {
        let dir =
            std::env::temp_dir().join(format!("speedread-scopes-{name}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(dir.join("src")).unwrap();
        let dir = dir.canonicalize().unwrap();
        let fx = Fx { dir };
        fx.write("src/batch.rs", RUST);
        fx.write("src/route.ts", TS);
        fx.write("src/total.py", PY);
        fx.write("src/run.c", C);
        fx.write("src/page.tsx", TSX);
        fx.write(
            "notes.txt",
            "first line\nsecond line mentions send_request\nthird line\n",
        );
        // 12 nested conditions around one call.
        let mut deep = String::from("pub fn deep(a: u32) -> u32 {\n");
        for i in 0..12 {
            deep.push_str(&format!("{}if a > {i} {{\n", "    ".repeat(i + 1)));
        }
        deep.push_str(&format!("{}return bottom(a);\n", "    ".repeat(13)));
        for i in (0..12).rev() {
            deep.push_str(&format!("{}}}\n", "    ".repeat(i + 1)));
        }
        deep.push_str("    0\n}\n");
        fx.write("src/deep.rs", &deep);
        fx
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
        assert!(out.status.success(), "{:?}", out);
        String::from_utf8(out.stdout).unwrap()
    }

    /// Numbered lines of a search answer (`N<TAB>text`), as line numbers.
    fn rows(&self, args: &[&str]) -> Vec<u32> {
        self.run(args)
            .lines()
            .filter_map(|l| l.split_once('\t')?.0.parse().ok())
            .collect()
    }
}

impl Drop for Fx {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.dir);
    }
}

#[test]
fn rust_hit_shows_the_loop_condition_and_arm_that_govern_it() {
    let fx = Fx::new("rust");
    let out = fx.run(&[
        "search",
        "send_request",
        "src/batch.rs",
        "--output",
        "scopes",
    ]);
    let body: Vec<&str> = out.lines().skip(2).collect();
    assert_eq!(
        body,
        vec![
            "[3-20] pub fn process_batch(batch: &Batch) -> Result<(), String>",
            "5\t    for item in batch.items() {",
            "6\t        if let Some(session) = item.session() {",
            "7\t            match session.state() {",
            "8\t                State::Ready => {",
            "9\t                    send_request(session)?;",
        ],
        "{out}"
    );
    // Everything except the ancestor lines is what `matches` prints.
    let plain = fx.run(&["search", "send_request", "src/batch.rs"]);
    let without: Vec<&str> = out
        .lines()
        .filter(|l| {
            !["5\t", "6\t", "7\t", "8\t"]
                .iter()
                .any(|p| l.starts_with(p))
        })
        .collect();
    assert_eq!(without, plain.lines().collect::<Vec<_>>());
}

#[test]
fn shared_ancestors_print_once_and_lines_stay_in_order() {
    let fx = Fx::new("shared");
    let rows = fx.rows(&[
        "search",
        "send_request|drop_session",
        "src/batch.rs",
        "--output",
        "scopes",
    ]);
    assert_eq!(rows, vec![5, 6, 7, 8, 9, 12, 13]);
}

#[test]
fn typescript_python_c_and_tsx() {
    let fx = Fx::new("langs");
    // for → if → switch → case.
    assert_eq!(
        fx.rows(&[
            "search",
            "handle\\(r\\)",
            "src/route.ts",
            "--output",
            "scopes"
        ]),
        vec![3, 4, 5, 6, 7]
    );
    // Python blocks have no braces: earlier statements of a block (`acc = 0`,
    // `y = x * 2`) are siblings, not ancestors. `if` and its `else` are.
    assert_eq!(
        fx.rows(&["search", "weigh", "src/total.py", "--output", "scopes"]),
        vec![4, 6, 8, 9]
    );
    // Lines holding only a brace are not context.
    assert_eq!(
        fx.rows(&["search", "emit", "src/run.c", "--output", "scopes"]),
        vec![3, 5, 7]
    );
    // Enclosing JSX elements.
    assert_eq!(
        fx.rows(&["search", "NavItem", "src/page.tsx", "--output", "scopes"]),
        vec![2, 3, 4, 5]
    );
}

#[test]
fn deep_nesting_is_capped_at_eight_ancestor_lines() {
    let fx = Fx::new("deep");
    let out = fx.run(&["search", "bottom", "src/deep.rs", "--output", "scopes"]);
    let rows: Vec<u32> = out
        .lines()
        .filter_map(|l| l.split_once('\t')?.0.parse().ok())
        .collect();
    // Outermost, then the seven innermost, then the hit.
    assert_eq!(rows, vec![2, 7, 8, 9, 10, 11, 12, 13, 14], "{out}");
    assert_eq!(
        out.lines().filter(|l| l.starts_with('⋯')).count(),
        1,
        "{out}"
    );
    let lines: Vec<&str> = out.lines().collect();
    let at = lines.iter().position(|l| l.starts_with("2\t")).unwrap();
    assert!(lines[at + 1].starts_with("⋯ 4 "), "{out}");
}

#[test]
fn nothing_is_added_where_there_is_nothing_to_add() {
    let fx = Fx::new("plain");
    // A hit directly in the function body.
    assert_eq!(
        fx.run(&["search", "log_total", "src/batch.rs", "--output", "scopes"]),
        fx.run(&["search", "log_total", "src/batch.rs"])
    );
    // A file without a grammar.
    assert_eq!(
        fx.run(&["search", "send_request", "notes.txt", "--output", "scopes"]),
        fx.run(&["search", "send_request", "notes.txt"])
    );
}

#[test]
fn context_lines_merge_by_line_number() {
    let fx = Fx::new("context");
    let rows = fx.rows(&[
        "search",
        "send_request",
        "src/batch.rs",
        "--output",
        "scopes",
        "-C",
        "1",
    ]);
    assert_eq!(rows, vec![5, 6, 7, 8, 9, 10]);
}

#[test]
fn json_lines_carry_the_ancestor_line_numbers() {
    let fx = Fx::new("json");
    let out = fx.run(&[
        "search",
        "send_request",
        "src/batch.rs",
        "--output",
        "scopes",
        "--json",
    ]);
    let v: serde_json::Value = serde_json::from_str(out.lines().next().unwrap()).unwrap();
    assert_eq!(v["line"], 9);
    assert_eq!(v["scopes"], serde_json::json!([5, 6, 7, 8]));
    // Other outputs are unchanged.
    let plain = fx.run(&["search", "send_request", "src/batch.rs", "--json"]);
    assert!(!plain.contains("scopes"), "{plain}");
}

#[test]
fn mcp_schema_offers_scopes() {
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
        .unwrap();
    let outputs = &search["inputSchema"]["properties"]["output"]["enum"];
    assert!(
        outputs.as_array().unwrap().contains(&"scopes".into()),
        "{outputs}"
    );
    let res = rpc(
        serde_json::json!({"jsonrpc":"2.0","id":3,"method":"tools/call","params":{
            "name":"search","arguments":{"pattern":"send_request","paths":["src/batch.rs"],"output":"scopes"}}}),
        Some(3),
    );
    let text = res["result"]["content"][0]["text"].as_str().unwrap();
    assert!(
        text.contains("7\t            match session.state() {"),
        "{text}"
    );
    drop(stdin);
    let _ = child.wait();
}
