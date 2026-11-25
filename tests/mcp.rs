//! Drives the real binary over MCP stdio: handshake, tool listing, and the
//! read → edit → `path@etag` diff / append flow within one session.

use std::io::{BufRead, BufReader, Write};
use std::process::{Child, ChildStdin, ChildStdout, Command, Stdio};

use serde_json::{Value, json};

struct Client {
    child: Child,
    stdin: ChildStdin,
    stdout: BufReader<ChildStdout>,
    next: u64,
}

impl Client {
    fn start(root: &std::path::Path) -> Client {
        Self::spawn(&["--root", root.to_str().unwrap(), "mcp"], None)
    }

    fn spawn(args: &[&str], cwd: Option<&std::path::Path>) -> Client {
        Self::spawn_env(args, cwd, &[])
    }

    fn spawn_env(args: &[&str], cwd: Option<&std::path::Path>, env: &[(&str, &str)]) -> Client {
        let mut cmd = Command::new(env!("CARGO_BIN_EXE_speedread"));
        if let Some(d) = cwd {
            cmd.current_dir(d);
        }
        // Description and RSS hooks are opt-in. Tests that don't pass them
        // must see the shipped server, even if the parent environment set them.
        cmd.env_remove("SPEEDREAD_DESCRIPTION_FILE");
        cmd.env_remove("SPEEDREAD_RSS_LOG");
        for (k, v) in env {
            cmd.env(k, v);
        }
        let mut child = cmd
            .args(args)
            .env_remove("CLAUDE_PROJECT_DIR")
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::inherit())
            .spawn()
            .expect("spawn speedread");
        let stdin = child.stdin.take().unwrap();
        let stdout = BufReader::new(child.stdout.take().unwrap());
        Client {
            child,
            stdin,
            stdout,
            next: 0,
        }
    }

    fn send(&mut self, v: Value) {
        writeln!(self.stdin, "{v}").unwrap();
        self.stdin.flush().unwrap();
    }

    fn rpc(&mut self, method: &str, params: Value) -> Value {
        self.rpc_with_roots(method, params, None)
    }

    /// Like `rpc`, answering any server `roots/list` request with `roots`.
    fn rpc_with_roots(&mut self, method: &str, params: Value, roots: Option<&Value>) -> Value {
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
            if v["method"] == "roots/list" {
                let result = json!({"roots": roots.cloned().unwrap_or(json!([]))});
                self.send(json!({"jsonrpc": "2.0", "id": v["id"], "result": result}));
                continue;
            }
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

fn etag_of(out: &str, file: &str) -> String {
    let i = out
        .find(file)
        .unwrap_or_else(|| panic!("{file} not in {out}"));
    let at = out[i..].find('@').unwrap() + i + 1;
    out[at..at + 16].to_string()
}

#[test]
fn mcp_session_end_to_end() {
    let dir = std::env::temp_dir().join(format!("speedread-mcp-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(dir.join("src")).unwrap();
    let mut lib = String::from(
        "/// Adds.\npub fn add(a: i32, b: i32) -> i32 {\n    let c = a + b;\n    let d = c;\n    let e = d;\n    e\n}\n\npub struct Point {\n    x: i32,\n}\n",
    );
    for i in 0..30 {
        lib.push_str(&format!("\npub fn f{i}() -> u32 {{\n    {i}\n}}\n"));
    }
    std::fs::write(dir.join("src/lib.rs"), &lib).unwrap();
    std::fs::write(dir.join("app.log"), "boot\n").unwrap();

    let mut c = Client::start(&dir);
    let init = c.rpc(
        "initialize",
        json!({"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}}),
    );
    assert_eq!(init["result"]["serverInfo"]["name"], "speedread");
    assert!(
        init["result"]["instructions"]
            .as_str()
            .unwrap()
            .contains("read")
    );
    c.send(json!({"jsonrpc": "2.0", "method": "notifications/initialized"}));

    let tools = c.rpc("tools/list", json!({}));
    let names: Vec<&str> = tools["result"]["tools"]
        .as_array()
        .unwrap()
        .iter()
        .map(|t| t["name"].as_str().unwrap())
        .collect();
    assert_eq!(names, vec!["map", "read", "search", "trace"]);
    for t in tools["result"]["tools"].as_array().unwrap() {
        assert_eq!(t["annotations"]["readOnlyHint"], json!(true));
    }

    // Symbol read, including its doc comment.
    let out = c.call("read", json!({"targets": ["src/lib.rs#add", "app.log"]}));
    assert!(out.contains("==> src/lib.rs:1-7"), "{out}");
    assert!(out.contains("1\t/// Adds."), "{out}");
    let lib_tag = etag_of(&out, "src/lib.rs");
    let log_tag = etag_of(&out, "app.log");

    // Edit + append, then re-read by etag: diff and appended lines only.
    let src = std::fs::read_to_string(dir.join("src/lib.rs")).unwrap();
    std::fs::write(
        dir.join("src/lib.rs"),
        src.replace("let d = c;", "let d = c * 2;"),
    )
    .unwrap();
    std::fs::write(dir.join("app.log"), "boot\nready\n").unwrap();
    let out = c.call(
        "read",
        json!({"targets": [format!("src/lib.rs@{lib_tag}"), format!("app.log@{log_tag}")]}),
    );
    assert!(
        out.contains(&format!("(was @{lib_tag}): 1 hunk, +1 -1")),
        "{out}"
    );
    // Symbol-aware: the changed function is named, with its signature status,
    // and the hunk carries git-style function context.
    assert!(
        out.contains("symbols:\n  add [1-7]: body changed, signature unchanged\n"),
        "{out}"
    );
    assert!(out.contains("@@ add\n"), "{out}");
    assert!(
        out.contains("-    let d = c;\n+    let d = c * 2;"),
        "{out}"
    );
    assert!(out.contains("+1 line appended"), "{out}");
    assert!(out.contains("2\tready"), "{out}");

    // Signature change + added function, summarised without hunks (mode=outline).
    let tag = etag_of(&out, "src/lib.rs");
    let src = std::fs::read_to_string(dir.join("src/lib.rs")).unwrap();
    std::fs::write(
        dir.join("src/lib.rs"),
        src.replace("pub fn f3() -> u32 {", "pub fn f3(k: u32) -> u32 {")
            + "\npub fn g() -> u8 {\n    1\n}\n",
    )
    .unwrap();
    let out = c.call(
        "read",
        json!({"targets": [format!("src/lib.rs@{tag}")], "mode": "outline"}),
    );
    assert!(
        out.contains(
            "f3 [25-27]: signature changed: `pub fn f3() -> u32` → `pub fn f3(k: u32) -> u32`"
        ),
        "{out}"
    );
    assert!(
        out.contains("g [133-135]: added `pub fn g() -> u8`"),
        "{out}"
    );
    assert!(!out.contains("@@"), "{out}");
    let src = std::fs::read_to_string(dir.join("src/lib.rs")).unwrap();
    std::fs::write(
        dir.join("src/lib.rs"),
        src.replace("pub fn f3(k: u32) -> u32 {", "pub fn f3() -> u32 {")
            .replace("\npub fn g() -> u8 {\n    1\n}\n", ""),
    )
    .unwrap();
    let out = c.call("read", json!({"targets": [format!("src/lib.rs@{tag}")]}));
    assert!(out.contains("unchanged (131 lines)"), "{out}");

    // Unchanged file → one line.
    let new_tag = etag_of(&out, "src/lib.rs");
    let out = c.call("read", json!({"targets": format!("src/lib.rs@{new_tag}")}));
    assert_eq!(
        out.trim_end(),
        format!("==> src/lib.rs @{new_tag} unchanged (131 lines)")
    );

    // Search groups hits under the enclosing symbol.
    let out = c.call("search", json!({"pattern": "let d"}));
    assert!(
        out.contains("[1-7] pub fn add(a: i32, b: i32) -> i32"),
        "{out}"
    );

    // Trace: callers grouped by enclosing function, callees resolved.
    std::fs::write(
        dir.join("src/use.rs"),
        "use crate::add;\n\npub fn twice(x: i32) -> i32 {\n    // add(1, 2) in a comment is not a call\n    let s = \"add(\";\n    add(x, x)\n}\n",
    )
    .unwrap();
    let out = c.call("trace", json!({"target": "#add"}));
    assert!(out.contains("==> callers of add (src/lib.rs:1-7)"), "{out}");
    assert!(out.contains("1 call site in 1 function"), "{out}");
    assert!(
        out.contains("[3-7] pub fn twice(x: i32) -> i32\n    6\tadd(x, x)"),
        "{out}"
    );
    let out = c.call(
        "trace",
        json!({"target": "src/use.rs#twice", "direction": "callees"}),
    );
    assert!(
        out.contains("→ src/lib.rs:1-7 pub fn add(a: i32, b: i32) -> i32"),
        "{out}"
    );
    let out = c.call("trace", json!({"target": "add", "direction": "references"}));
    assert!(out.contains("1 import"), "{out}");

    // Map lists files with line counts and symbols.
    let out = c.call("map", json!({"symbols": true}));
    assert!(out.contains("lib.rs 131L: add, Point, f0, f1"), "{out}");

    // Lenient arguments: a bare string target and a string budget.
    let out = c.call("read", json!({"path": "src/lib.rs:9-11", "budget": "500"}));
    assert!(out.contains("9\tpub struct Point {"), "{out}");

    // Typos get suggestions rather than a bare failure.
    let out = c.call("read", json!({"targets": ["src/lob.rs"]}));
    assert!(out.contains("Did you mean: src/lib.rs"), "{out}");

    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn client_roots_become_the_workspace() {
    let dir = std::env::temp_dir().join(format!("speedread-roots-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(dir.join("My App/src")).unwrap();
    std::fs::write(
        dir.join("My App/src/main.rs"),
        "fn main() {\n    println!(\"hi\");\n}\n",
    )
    .unwrap();
    // Launched elsewhere (like a GUI client), with no --root.
    let mut c = Client::spawn(&["mcp"], Some(&std::env::temp_dir()));
    c.rpc(
        "initialize",
        json!({"protocolVersion": "2025-06-18", "capabilities": {"roots": {"listChanged": true}},
               "clientInfo": {"name": "test", "version": "0"}}),
    );
    c.send(json!({"jsonrpc": "2.0", "method": "notifications/initialized"}));
    let root = dir.canonicalize().unwrap().join("My App");
    let uri = format!("file://{}", root.to_str().unwrap().replace(' ', "%20"));
    let roots = json!([{"uri": uri, "name": "My App"}]);
    let r = c.rpc_with_roots(
        "tools/call",
        json!({"name": "read", "arguments": {"targets": ["src/main.rs#main"]}}),
        Some(&roots),
    );
    let text = r["result"]["content"][0]["text"]
        .as_str()
        .unwrap()
        .to_string();
    assert!(text.starts_with("==> src/main.rs:1-3 @"), "{text}");
    let _ = std::fs::remove_dir_all(&dir);
}

/// How the Claude Desktop bundle and the plugins start it: no folder is
/// asked for, nothing is fenced, and the launch directory means nothing.
#[test]
fn a_server_with_no_folder_reads_by_absolute_path_and_says_so() {
    let dir = std::env::temp_dir().join(format!("speedread-nofolder-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(dir.join("launched/here")).unwrap();
    std::fs::create_dir_all(dir.join("project/src")).unwrap();
    std::fs::write(dir.join("launched/here/decoy.rs"), "fn needle() {}\n").unwrap();
    std::fs::write(
        dir.join("project/src/lib.rs"),
        "pub fn needle() -> u32 {\n    7\n}\n",
    )
    .unwrap();
    let project = dir.canonicalize().unwrap().join("project");
    let init_params = json!({"protocolVersion": "2025-06-18", "capabilities": {},
                             "clientInfo": {"name": "test", "version": "0"}});

    let mut c = Client::spawn(
        &["mcp", "--unrestricted", "--no-cwd-root"],
        Some(&dir.join("launched/here")),
    );
    let init = c.rpc("initialize", init_params.clone());
    let instructions = init["result"]["instructions"].as_str().unwrap();
    assert!(
        instructions.contains("No project folder is configured"),
        "{instructions}"
    );
    c.send(json!({"jsonrpc": "2.0", "method": "notifications/initialized"}));

    // Nothing to resolve against: it says what to pass instead of searching
    // the directory it happened to be launched in.
    let out = c.call("search", json!({"query": "needle"}));
    assert!(out.contains("pass base_dir"), "{out}");
    assert!(!out.contains("decoy.rs"), "{out}");

    let out = c.call(
        "search",
        json!({"query": "needle", "base_dir": project.to_str().unwrap()}),
    );
    assert!(out.contains("src/lib.rs"), "{out}");
    assert!(!out.contains("decoy.rs"), "{out}");

    let target = format!("{}#needle", project.join("src/lib.rs").display());
    let out = c.call("read", json!({"targets": [target]}));
    assert!(out.contains("pub fn needle() -> u32"), "{out}");
    drop(c);

    // A server that has a folder is told nothing extra.
    let mut c = Client::start(&project);
    let init = c.rpc("initialize", init_params);
    let instructions = init["result"]["instructions"].as_str().unwrap();
    assert!(
        !instructions.contains("No project folder is configured"),
        "{instructions}"
    );
    let _ = std::fs::remove_dir_all(&dir);
}

/// `speedread mcp` reads whatever path it is given. Its folders say where
/// relative paths and path-less calls resolve; they are not a fence unless
/// the server was started with `--restricted`.
#[test]
fn an_mcp_server_reads_outside_its_folder_unless_restricted() {
    let dir = std::env::temp_dir().join(format!("speedread-nofence-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(dir.join("project/src")).unwrap();
    std::fs::create_dir_all(dir.join("other/src")).unwrap();
    std::fs::write(
        dir.join("project/src/lib.rs"),
        "pub fn inside() -> u32 {\n    1\n}\n",
    )
    .unwrap();
    std::fs::write(
        dir.join("other/src/lib.rs"),
        "pub fn outside() -> u32 {\n    2\n}\n\npub fn calls_outside() -> u32 {\n    outside()\n}\n",
    )
    .unwrap();
    let dir = dir.canonicalize().unwrap();
    let project = dir.join("project");
    let other = dir.join("other");
    let other_s = other.to_str().unwrap();
    let other_file = other.join("src/lib.rs");
    let init_params = json!({"protocolVersion": "2025-06-18", "capabilities": {},
                             "clientInfo": {"name": "test", "version": "0"}});

    // Spawn a server and complete the handshake; returns its instructions.
    let open = |args: &[&str], cwd: Option<&std::path::Path>| {
        let mut c = Client::spawn(args, cwd);
        let init = c.rpc("initialize", init_params.clone());
        let instructions = init["result"]["instructions"].as_str().unwrap().to_string();
        c.send(json!({"jsonrpc": "2.0", "method": "notifications/initialized"}));
        (c, instructions)
    };

    // Every tool, on a path outside the configured folder.
    let reads_outside = |c: &mut Client, what: &str| {
        let out = c.call("map", json!({"path": other_s}));
        assert!(out.contains("lib.rs"), "{what}: map: {out}");
        let out = c.call(
            "read",
            json!({"targets": [format!("{}#outside", other_file.display())]}),
        );
        assert!(
            out.contains("pub fn outside() -> u32"),
            "{what}: read: {out}"
        );
        let out = c.call("search", json!({"pattern": "outside", "path": other_s}));
        assert!(out.contains("calls_outside"), "{what}: search: {out}");
        let out = c.call("search", json!({"pattern": "outside", "base_dir": other_s}));
        assert!(out.contains("==> src/lib.rs @"), "{what}: base_dir: {out}");
        let out = c.call(
            "trace",
            json!({"target": format!("{}#outside", other_file.display())}),
        );
        assert!(out.contains("calls_outside"), "{what}: trace: {out}");
        assert!(!out.contains("utside the workspace"), "{what}: {out}");
    };

    // A server with a folder: the folder scopes path-less calls, nothing more.
    let (mut c, _) = open(&["--root", project.to_str().unwrap(), "mcp"], None);
    reads_outside(&mut c, "--root project");
    let out = c.call("search", json!({"pattern": "fn "}));
    assert!(out.contains("inside") && !out.contains("outside"), "{out}");
    drop(c);

    // Launched in the project with no flags at all, as a hand-written MCP
    // entry does it.
    let (mut c, _) = open(&["mcp"], Some(&project));
    reads_outside(&mut c, "mcp, launched in project");
    drop(c);

    // No subcommand and a pipe for stdin is also the MCP server.
    let (mut c, _) = open(&[], Some(&project));
    reads_outside(&mut c, "no subcommand");
    drop(c);

    // Launched from `/`, as desktop apps do: it has no folder, says so, and
    // the absolute paths it asks for work.
    let (mut c, instructions) = open(&["mcp"], Some(std::path::Path::new("/")));
    assert!(
        instructions.contains("No project folder is configured"),
        "{instructions}"
    );
    let out = c.call("map", json!({}));
    assert!(out.contains("pass base_dir"), "{out}");
    reads_outside(&mut c, "mcp, launched in /");
    drop(c);

    // `--restricted` brings the fence back, and each refusal says how to lift it.
    let (mut c, _) = open(
        &["--restricted", "--root", project.to_str().unwrap(), "mcp"],
        None,
    );
    let out = c.call("search", json!({"pattern": "fn "}));
    assert!(out.contains("inside"), "{out}");
    let refusals = [
        c.call("map", json!({"path": other_s})),
        c.call(
            "read",
            json!({"targets": [format!("{}#outside", other_file.display())]}),
        ),
        c.call("search", json!({"pattern": "outside", "path": other_s})),
        c.call("search", json!({"pattern": "outside", "base_dir": other_s})),
        c.call(
            "trace",
            json!({"target": format!("{}#outside", other_file.display())}),
        ),
    ];
    for out in &refusals {
        assert!(out.contains("utside the workspace"), "{out}");
        assert!(out.contains("--restricted"), "no remedy in: {out}");
        assert!(
            !out.contains("calls_outside") && !out.contains("u32"),
            "{out}"
        );
    }
    drop(c);

    // A restricted server with no folder is not told to use absolute paths:
    // it would refuse them.
    let (c, instructions) = open(&["mcp", "--restricted"], Some(std::path::Path::new("/")));
    assert!(
        !instructions.contains("No project folder is configured"),
        "{instructions}"
    );
    drop(c);

    // The two flags contradict each other; the server does not guess.
    let out = Command::new(env!("CARGO_BIN_EXE_speedread"))
        .args(["mcp", "--restricted", "--unrestricted"])
        .stdin(Stdio::null())
        .output()
        .unwrap();
    assert!(!out.status.success());
    assert!(
        String::from_utf8_lossy(&out.stderr).contains("cannot be used with"),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );

    // Commands keep their fence: only the server changed.
    let out = Command::new(env!("CARGO_BIN_EXE_speedread"))
        .current_dir(&project)
        .env_remove("CLAUDE_PROJECT_DIR")
        .args(["map", other_s])
        .output()
        .unwrap();
    let text = format!(
        "{}{}",
        String::from_utf8_lossy(&out.stdout),
        String::from_utf8_lossy(&out.stderr)
    );
    assert!(
        text.contains("Outside the workspace") && text.contains("--unrestricted"),
        "{text}"
    );
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn luau_symbols_and_trace() {
    let dir = std::env::temp_dir().join(format!("speedread-luau-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(dir.join("src")).unwrap();
    std::fs::write(
        dir.join("src/Account.luau"),
        "--!strict\nlocal Account = {}\nAccount.__index = Account\n\nfunction Account.new(owner: string)\n\tlocal self = setmetatable({}, Account)\n\tself.balance = 0\n\treturn self\nend\n\n@native\nfunction Account:deposit(amount: number)\n\tself.balance += amount\nend\n\nreturn Account\n",
    )
    .unwrap();
    std::fs::write(
        dir.join("src/Bank.luau"),
        "local Account = require(script.Parent.Account)\n\nlocal Bank = {}\n\nfunction Bank.open(name: string)\n\tlocal acct = Account.new(name)\n\tacct:deposit(5)\n\treturn acct\nend\n\ngame.Players.PlayerAdded:Connect(function(player)\n\tlocal acct = Bank.open(player.Name)\n\tacct:deposit(1)\nend)\n\nfunction Bank.spawn(name: string)\n\treturn Bank.open(name)\nend\n\ntask.spawn(function()\n\tBank.spawn(\"b\")\nend)\n\nreturn Bank\n",
    )
    .unwrap();
    // Roblox globals called next to a type annotation naming them, and in a
    // `.lua` file that only Luau parses (`+=`).
    std::fs::write(
        dir.join("src/Place.luau"),
        "local function place()\n\tlocal pos: Vector3 = Vector3.new(0, 1, 0)\n\treturn pos\nend\n\nreturn place\n",
    )
    .unwrap();
    std::fs::write(
        dir.join("src/Tick.lua"),
        "local count = 0\n\nlocal function tick()\n\tcount += 1\n\ttask.spawn(function() end)\nend\n\nreturn tick\n",
    )
    .unwrap();
    let mut c = Client::start(&dir);
    c.rpc(
        "initialize",
        json!({"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}}),
    );
    c.send(json!({"jsonrpc": "2.0", "method": "notifications/initialized"}));

    // A Luau method by `#Class:method`, `path#Class.method` or `#method`, with its attribute.
    for t in [
        "#Account:deposit",
        "src/Account.luau#Account.deposit",
        "#deposit",
    ] {
        let out = c.call("read", json!({"targets": [t]}));
        assert!(
            out.starts_with("==> src/Account.luau:11-14 @") && out.contains("11\t@native\n"),
            "{t}: {out}"
        );
    }

    // `obj:method()` calls resolve to the method, across files and at top level.
    let out = c.call("trace", json!({"target": "#Account:deposit"}));
    assert!(out.contains("2 call sites"), "{out}");
    assert!(
        out.contains("[5-9] function Bank.open(name: string)\n    7\tacct:deposit(5)"),
        "{out}"
    );
    let out = c.call("trace", json!({"target": "#Account.new"}));
    assert!(out.contains("1 call site"), "{out}");
    assert!(!out.contains("Vector3"), "{out}");
    // `task.spawn(...)` is Luau's task library, not a call to `Bank.spawn`.
    let out = c.call("trace", json!({"target": "#Bank.spawn"}));
    assert!(
        out.contains("1 call site") && out.contains("21\tBank.spawn(\"b\")"),
        "{out}"
    );
    assert!(!out.contains("task.spawn"), "{out}");
    let out = c.call(
        "trace",
        json!({"target": "src/Bank.luau#open", "direction": "callees"}),
    );
    assert!(
        out.contains("→ src/Account.luau:5-9 function Account.new(owner: string)"),
        "{out}"
    );
    assert!(
        out.contains("→ src/Account.luau:11-14 function Account:deposit(amount: number)"),
        "{out}"
    );

    // Search hits are grouped under their function.
    let out = c.call("search", json!({"pattern": "balance"}));
    assert!(
        out.contains("[11-14] function Account:deposit(amount: number)\n13\t"),
        "{out}"
    );

    // Lua modules are mostly all `M`: a file's own `M.new()` is not a call to
    // another file's, and a local `vector` module is not a library.
    std::fs::create_dir_all(dir.join("lua")).unwrap();
    for f in ["a", "b"] {
        std::fs::write(
            dir.join(format!("lua/{f}.lua")),
            "local M = {}\n\nfunction M.new()\n  return {}\nend\n\nfunction M.copy()\n  return M.new()\nend\n\nreturn M\n",
        )
        .unwrap();
    }
    let out = c.call("trace", json!({"target": "lua/a.lua#new"}));
    assert!(
        out.contains("1 call site") && out.contains("lua/a.lua\n  [7-9] function M.copy()"),
        "{out}"
    );
    std::fs::write(
        dir.join("lua/vector.lua"),
        "local function create(x, y)\n  return { x = x, y = y }\nend\n\nreturn { create = create }\n",
    )
    .unwrap();
    std::fs::write(
        dir.join("lua/main.lua"),
        "local vector = require(\"vector\")\n\nfunction spawn()\n  return vector.create(1, 2)\nend\n",
    )
    .unwrap();
    let out = c.call("trace", json!({"target": "lua/vector.lua#create"}));
    assert!(
        out.contains("[3-5] function spawn()\n    4\treturn vector.create(1, 2)"),
        "{out}"
    );

    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn keys_with_colons_are_found_in_large_workspaces() {
    let dir = std::env::temp_dir().join(format!("speedread-keys-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(dir.join("src")).unwrap();
    std::fs::write(
        dir.join("package.json"),
        "{\n  \"scripts\": {\n    \"test:unit\": \"vitest run\"\n  }\n}\n",
    )
    .unwrap();
    // Over 200 files mention the name, so definitions are prefiltered.
    for i in 0..210 {
        std::fs::write(
            dir.join(format!("src/f{i}.js")),
            format!("// unit test helper\nexport const value{i} = 1;\n"),
        )
        .unwrap();
    }
    let mut c = Client::start(&dir);
    c.rpc(
        "initialize",
        json!({"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}}),
    );
    c.send(json!({"jsonrpc": "2.0", "method": "notifications/initialized"}));
    let out = c.call("read", json!({"targets": ["#test:unit"]}));
    assert!(out.starts_with("==> package.json:3-3 @"), "{out}");
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn line_targets_outside_symbols_read_the_enclosing_block() {
    let dir = std::env::temp_dir().join(format!("speedread-blocks-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    // Top-level code in a language with a grammar, and a language without one.
    std::fs::write(
        dir.join("main.client.luau"),
        "local Players = game:GetService(\"Players\")\n\nPlayers.PlayerAdded:Connect(function(player)\n\tlocal name = player.Name\n\tprint(name)\nend)\n",
    )
    .unwrap();
    std::fs::write(
        dir.join("greeter.ex"),
        "defmodule Greeter do\n  # Says hello.\n  def hello(name) do\n    \"hello \" <> name\n  end\nend\n",
    )
    .unwrap();

    let mut c = Client::start(&dir);
    c.rpc(
        "initialize",
        json!({"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}}),
    );
    c.send(json!({"jsonrpc": "2.0", "method": "notifications/initialized"}));

    let out = c.call(
        "read",
        json!({"targets": ["main.client.luau:5", "greeter.ex:4", "main.client.luau:1"]}),
    );
    // The block around the line, titled by its first line.
    assert!(
        out.contains("==> main.client.luau:3-6 @")
            && out.contains("(6 lines) Players.PlayerAdded:Connect(function(player)\n3\t"),
        "{out}"
    );
    // A definition with its comment, not the whole module.
    assert!(
        out.contains("==> greeter.ex:2-5 @")
            && out.contains("def hello(name) do\n2\t  # Says hello."),
        "{out}"
    );
    // A top-level line outside any block keeps the window around it.
    assert!(out.contains("==> main.client.luau:1-6 @"), "{out}");

    let _ = std::fs::remove_dir_all(&dir);
}

/// A server that parsed something large must not stay that large: what a
/// request freed goes back to the system when the request ends.
#[cfg(target_os = "linux")]
#[test]
fn memory_a_request_freed_is_returned_to_the_system() {
    let dir = std::env::temp_dir().join(format!("speedread-release-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(dir.join("src")).unwrap();
    // About 6 MiB of source: its syntax tree takes several times that.
    let mut big = String::new();
    for i in 0..60_000 {
        big.push_str(&format!(
            "pub fn generated_{i}(a: u64, b: u64) -> u64 {{\n    let c = a.wrapping_mul({i}) ^ b;\n    c.rotate_left(7) + {i}\n}}\n"
        ));
    }
    std::fs::write(dir.join("src/big.rs"), big).unwrap();

    let mut c = Client::spawn(
        &["--root", dir.to_str().unwrap(), "--cache-mb", "1", "mcp"],
        None,
    );
    c.rpc(
        "initialize",
        json!({"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}}),
    );
    c.send(json!({"jsonrpc": "2.0", "method": "notifications/initialized"}));
    let out = c.call("read", json!({"targets": ["src/big.rs#generated_31337"]}));
    assert!(out.contains("pub fn generated_31337("), "{out}");

    let mib = |status: &str, key: &str| -> u64 {
        let line = status.lines().find(|l| l.starts_with(key)).expect(key);
        line.split_whitespace()
            .nth(1)
            .unwrap()
            .parse::<u64>()
            .unwrap()
            >> 10
    };
    let read = || std::fs::read_to_string(format!("/proc/{}/status", c.child.id())).unwrap();
    let (mut now, mut peak) = (0, 0);
    // The pool's threads give their share back just after the answer.
    for _ in 0..30 {
        let status = read();
        (now, peak) = (mib(&status, "VmRSS:"), mib(&status, "VmHWM:"));
        if now + 24 <= peak {
            break;
        }
        std::thread::sleep(std::time::Duration::from_millis(100));
    }
    let _ = std::fs::remove_dir_all(&dir);
    assert!(
        peak >= 40,
        "the request was meant to be large: peak {peak} MiB"
    );
    assert!(
        now + 24 <= peak,
        "resident {now} MiB after a request that peaked at {peak} MiB"
    );
}

fn tool_descriptions(listed: &Value) -> std::collections::BTreeMap<String, String> {
    listed["result"]["tools"]
        .as_array()
        .unwrap()
        .iter()
        .map(|t| {
            (
                t["name"].as_str().unwrap().to_string(),
                t["description"].as_str().unwrap().to_string(),
            )
        })
        .collect()
}

fn schemas(listed: &Value) -> std::collections::BTreeMap<String, String> {
    listed["result"]["tools"]
        .as_array()
        .unwrap()
        .iter()
        .map(|t| {
            (
                t["name"].as_str().unwrap().to_string(),
                t["inputSchema"].to_string(),
            )
        })
        .collect()
}

/// Shipped `tools/list` descriptions and initialize instructions, byte for byte.
/// Regenerating this file is a product change to the model-facing text, which
/// does not belong in a description-variant PR.
#[test]
fn default_tool_text_is_byte_identical() {
    let dir = std::env::temp_dir().join(format!("speedread-text-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    let mut c = Client::spawn(&["--root", dir.to_str().unwrap(), "mcp"], None);
    let init = c.rpc(
        "initialize",
        json!({"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}}),
    );
    c.send(json!({"jsonrpc": "2.0", "method": "notifications/initialized"}));
    let listed = c.rpc("tools/list", json!({}));
    let mut body = serde_json::Map::new();
    body.insert(
        "instructions".into(),
        init["result"]["instructions"].clone(),
    );
    body.insert(
        "tools".into(),
        serde_json::to_value(tool_descriptions(&listed)).unwrap(),
    );
    let got = serde_json::to_string_pretty(&Value::Object(body)).unwrap() + "\n";
    let path = concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/tests/fixtures/default_tool_text.json"
    );
    let expected = std::fs::read_to_string(path).unwrap_or_default();
    assert_eq!(
        got, expected,
        "shipped tool text changed; the fixture is the byte-identical contract"
    );
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn description_file_replaces_text_and_not_schemas_or_answers() {
    let dir = std::env::temp_dir().join(format!("speedread-over-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(dir.join("src")).unwrap();
    std::fs::write(dir.join("src/lib.rs"), "pub fn add(a: i32) -> i32 { a }\n").unwrap();
    let variant = dir.join("variant.json");
    std::fs::write(
        &variant,
        r#"{"instructions":"VARIANT INSTRUCTIONS","tools":{"read":"VARIANT READ"}}"#,
    )
    .unwrap();

    let mut plain = Client::spawn(&["--root", dir.to_str().unwrap(), "mcp"], None);
    let plain_init = plain.rpc(
        "initialize",
        json!({"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}}),
    );
    plain.send(json!({"jsonrpc": "2.0", "method": "notifications/initialized"}));
    let plain_list = plain.rpc("tools/list", json!({}));
    let plain_answer = plain.call("read", json!({"targets": ["src/lib.rs#add"]}));

    let mut over = Client::spawn_env(
        &["--root", dir.to_str().unwrap(), "mcp"],
        None,
        &[("SPEEDREAD_DESCRIPTION_FILE", variant.to_str().unwrap())],
    );
    let over_init = over.rpc(
        "initialize",
        json!({"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}}),
    );
    over.send(json!({"jsonrpc": "2.0", "method": "notifications/initialized"}));
    let over_list = over.rpc("tools/list", json!({}));
    let over_answer = over.call("read", json!({"targets": ["src/lib.rs#add"]}));

    let plain_desc = tool_descriptions(&plain_list);
    let over_desc = tool_descriptions(&over_list);
    assert_eq!(over_desc["read"], "VARIANT READ");
    assert_eq!(over_desc["search"], plain_desc["search"]);
    assert_eq!(over_desc["map"], plain_desc["map"]);
    assert_eq!(over_desc["trace"], plain_desc["trace"]);
    assert_eq!(schemas(&plain_list), schemas(&over_list));
    assert_eq!(
        over_init["result"]["instructions"].as_str().unwrap(),
        "VARIANT INSTRUCTIONS"
    );
    assert_ne!(
        plain_init["result"]["instructions"].as_str().unwrap(),
        "VARIANT INSTRUCTIONS"
    );
    assert_eq!(
        plain_answer, over_answer,
        "override must not change tool answers"
    );

    // The CLI path does not consult the description file.
    let read = |env: bool| {
        let mut cmd = Command::new(env!("CARGO_BIN_EXE_speedread"));
        cmd.args(["--root", dir.to_str().unwrap(), "read", "src/lib.rs#add"]);
        cmd.env_remove("SPEEDREAD_DESCRIPTION_FILE");
        if env {
            cmd.env("SPEEDREAD_DESCRIPTION_FILE", &variant);
        }
        let out = cmd.output().unwrap();
        assert!(
            out.status.success(),
            "{}",
            String::from_utf8_lossy(&out.stderr)
        );
        out.stdout
    };
    assert_eq!(read(false), read(true));
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn description_file_unknown_tool_refuses_to_start() {
    let dir = std::env::temp_dir().join(format!("speedread-bad-desc-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    let variant = dir.join("variant.json");
    std::fs::write(&variant, r#"{"tools":{"not_a_tool":"nope"}}"#).unwrap();
    let out = Command::new(env!("CARGO_BIN_EXE_speedread"))
        .args(["--root", dir.to_str().unwrap(), "mcp"])
        .env("SPEEDREAD_DESCRIPTION_FILE", &variant)
        .stdin(Stdio::null())
        .output()
        .unwrap();
    assert!(!out.status.success());
    let err = String::from_utf8_lossy(&out.stderr);
    assert!(err.contains("unknown tool"), "{err}");
    let _ = std::fs::remove_dir_all(&dir);
}
