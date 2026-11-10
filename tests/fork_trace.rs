//! Relationship kinds and evidence in `trace` (docs/specs/05-relationship-kinds.md).

use std::path::PathBuf;
use std::process::Command;

const RUST: &str = "\
pub fn target(x: u32) -> u32 {
    x + 1
}

pub fn direct() -> u32 {
    target(1)
}

pub fn in_closure(items: &[u32]) -> u32 {
    let mut n = 0;
    items.iter().for_each(|i| {
        n += target(*i);
    });
    n
}

pub fn passes(xs: Vec<u32>) -> Vec<u32> {
    xs.into_iter().map(target).collect()
}

pub fn assigns() -> u32 {
    let f = target;
    f(2)
}

pub fn shadow(target: u32) -> u32 {
    consume(target)
}

pub fn rebinds() -> u32 {
    let target = 3;
    consume(target)
}

fn consume(x: u32) -> u32 {
    x
}

pub const LIMIT: u32 = 3;

pub fn uses_limit() -> u32 {
    consume(LIMIT)
}

pub fn only_called() -> u32 {
    1
}

pub fn calls_only() -> u32 {
    only_called()
}
";

const TS: &str = "\
export function authenticate(req: Req): boolean {
  return req.ok;
}

export function register(router: Router) {
  router.post(\"/login\", authenticate, finish);
  const check = authenticate;
  const table = { auth: authenticate };
  reqs.forEach((r) => {
    authenticate(r);
  });
  return authenticate;
}

export function plain(req: Req) {
  return authenticate(req);
}

export function shadowed(authenticate: boolean) {
  log(authenticate);
}

export const arrow = (req: Req) => {
  return authenticate(req);
};
";

const GO: &str = "\
package main

func handle(w Writer) {
	w.Write(\"x\")
}

func setup(mux *Mux) {
	mux.HandleFunc(\"/x\", handle)
	h := handle
	go func() {
		handle(nil)
	}()
	h(nil)
}
";

const PY: &str = "\
def work(item):
    return item * 2


def schedule(pool, items):
    pool.map(work, items)
    callback = work
    run(lambda x: work(x))
    return work


def other(work):
    print(work)
";

const TS_MODEL: &str = "\
export function count(xs: number[]): number {
  return xs.length;
}

export class Mock {
  get ready(): boolean {
    return true;
  }
  run(): void {}
  wire(bus: Bus) {
    bus.on(\"tick\", this.run);
    bus.on(\"ready\", this.ready);
  }
}

export function fields(node: Item, sizes: Set<count>) {
  log(node.count);
  const copy = { count: node.count };
  const n = 7 as count;
  return each(count);
}
";

const RUST_BUILDER: &str = "\
pub struct Builder {
    limit: u32,
}

impl Builder {
    pub fn limit(&self) -> u32 {
        self.limit
    }

    pub fn show(&self) {
        consume(self.limit);
        let f = Builder::limit;
        let g = Self::limit;
        let n = self.limit();
    }
}
";

struct Fx {
    dir: PathBuf,
}

impl Fx {
    fn new(name: &str) -> Fx {
        let dir =
            std::env::temp_dir().join(format!("speedread-trace-{name}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(dir.join("src")).unwrap();
        let dir = dir.canonicalize().unwrap();
        for (rel, text) in [
            ("src/lib.rs", RUST),
            ("src/app.ts", TS),
            ("src/main.go", GO),
            ("src/job.py", PY),
            ("src/model.ts", TS_MODEL),
            ("src/builder.rs", RUST_BUILDER),
        ] {
            std::fs::write(dir.join(rel), text).unwrap();
        }
        Fx { dir }
    }

    fn trace(&self, args: &[&str]) -> String {
        let out = Command::new(env!("CARGO_BIN_EXE_speedread"))
            .arg("--root")
            .arg(&self.dir)
            .arg("trace")
            .args(args)
            .current_dir("/")
            .output()
            .unwrap();
        assert!(out.status.success(), "{:?}", out);
        String::from_utf8(out.stdout).unwrap()
    }
}

impl Drop for Fx {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.dir);
    }
}

/// The site line for `line` (1-based), without its indentation.
fn site(out: &str, line: u32) -> Option<&str> {
    let prefix = format!("{line}\t");
    out.lines()
        .map(str::trim_start)
        .find(|l| l.starts_with(&prefix))
}

#[test]
fn rust_closures_and_value_uses() {
    let fx = Fx::new("rust");
    let out = fx.trace(&["src/lib.rs#target"]);
    assert_eq!(site(&out, 6), Some("6\ttarget(1)"), "{out}");
    assert_eq!(site(&out, 12), Some("12\tn += target(*i);  λ"), "{out}");
    assert_eq!(
        site(&out, 18),
        Some("18\txs.into_iter().map(target).collect()  → passed to ….map"),
        "{out}"
    );
    assert_eq!(
        site(&out, 22),
        Some("22\tlet f = target;  → assigned"),
        "{out}"
    );
    // A parameter or local of the same name is not the function.
    assert_eq!(site(&out, 27), None, "{out}");
    assert_eq!(site(&out, 32), None, "{out}");
    let summary = out.lines().nth(2).unwrap();
    assert!(
        summary
            .starts_with("2 call sites + 2 passed as value in 4 functions, 1 inside a closure · "),
        "{summary}"
    );
    assert!(
        summary.contains("λ = ") && summary.contains("→ = "),
        "{summary}"
    );
}

#[test]
fn typescript_registration_assignment_return() {
    let fx = Fx::new("ts");
    let out = fx.trace(&["src/app.ts#authenticate"]);
    assert_eq!(
        site(&out, 6),
        Some("6\trouter.post(\"/login\", authenticate, finish);  → passed to router.post"),
        "{out}"
    );
    assert_eq!(
        site(&out, 7),
        Some("7\tconst check = authenticate;  → assigned"),
        "{out}"
    );
    assert_eq!(
        site(&out, 8),
        Some("8\tconst table = { auth: authenticate };  → assigned"),
        "{out}"
    );
    assert_eq!(site(&out, 10), Some("10\tauthenticate(r);  λ"), "{out}");
    assert_eq!(
        site(&out, 12),
        Some("12\treturn authenticate;  → returned"),
        "{out}"
    );
    assert_eq!(
        site(&out, 16),
        Some("16\treturn authenticate(req);"),
        "{out}"
    );
    assert_eq!(site(&out, 20), None, "{out}");
    // An arrow function that is itself the named symbol is not a nested closure.
    assert_eq!(
        site(&out, 24),
        Some("24\treturn authenticate(req);"),
        "{out}"
    );
    // Calls come first within a function.
    let at = |n: u32| out.find(&format!("\n    {n}\t")).unwrap();
    assert!(at(10) < at(6) && at(6) < at(7) && at(8) < at(12), "{out}");
}

#[test]
fn go_and_python() {
    let fx = Fx::new("gopy");
    let go = fx.trace(&["src/main.go#handle"]);
    assert_eq!(
        site(&go, 8),
        Some("8\tmux.HandleFunc(\"/x\", handle)  → passed to mux.HandleFunc"),
        "{go}"
    );
    assert_eq!(site(&go, 9), Some("9\th := handle  → assigned"), "{go}");
    assert_eq!(site(&go, 11), Some("11\thandle(nil)  λ"), "{go}");

    let py = fx.trace(&["src/job.py#work"]);
    assert_eq!(
        site(&py, 6),
        Some("6\tpool.map(work, items)  → passed to pool.map"),
        "{py}"
    );
    assert_eq!(site(&py, 7), Some("7\tcallback = work  → assigned"), "{py}");
    assert_eq!(site(&py, 8), Some("8\trun(lambda x: work(x))  λ"), "{py}");
    assert_eq!(site(&py, 9), Some("9\treturn work  → returned"), "{py}");
    assert_eq!(site(&py, 13), None, "{py}");
}

#[test]
fn answers_without_closures_or_value_uses_are_unchanged() {
    let fx = Fx::new("same");
    let out = fx.trace(&["src/lib.rs#only_called"]);
    assert_eq!(
        out,
        "==> callers of only_called (src/lib.rs:45-47)\n\
         pub fn only_called() -> u32\n\
         1 call site in 1 function · syntactic: comments, strings and the definition excluded\n\
         src/lib.rs\n  [49-51] pub fn calls_only() -> u32\n    50\tonly_called()\n"
    );
}

#[test]
fn only_callables_have_value_uses() {
    let fx = Fx::new("const");
    // A constant passed to a function is a reference, not a registration.
    let callers = fx.trace(&["src/lib.rs#LIMIT"]);
    assert!(!callers.contains("passed"), "{callers}");
    let refs = fx.trace(&["src/lib.rs#LIMIT", "-d", "refs"]);
    assert_eq!(site(&refs, 42), Some("42\tconsume(LIMIT)"), "{refs}");
}

#[test]
fn refs_carry_the_same_marks() {
    let fx = Fx::new("refs");
    let out = fx.trace(&["src/lib.rs#target", "-d", "refs"]);
    assert_eq!(site(&out, 12), Some("12\tn += target(*i);  λ"), "{out}");
    assert_eq!(
        site(&out, 22),
        Some("22\tlet f = target;  → assigned"),
        "{out}"
    );
    // Refs still lists every mention, including ones that are not the function.
    assert_eq!(site(&out, 27), Some("27\tconsume(target)"), "{out}");
}

#[test]
fn fields_types_and_getters_are_not_value_uses() {
    let fx = Fx::new("precision");
    // `node.count` is a field of something unknown, `Set<count>` and
    // `7 as count` are types: only the bare name handed to `each` counts.
    let out = fx.trace(&["src/model.ts#count"]);
    assert_eq!(
        site(&out, 20),
        Some("20\treturn each(count);  → passed to each"),
        "{out}"
    );
    for line in [16, 17, 18, 19] {
        assert_eq!(site(&out, line), None, "line {line}: {out}");
    }
    assert!(out.contains("0 call sites + 1 passed as value"), "{out}");

    // `this.run` inside the owner is the method; `this.ready` reads a getter.
    let run = fx.trace(&["src/model.ts#Mock.run"]);
    assert_eq!(
        site(&run, 11),
        Some("11\tbus.on(\"tick\", this.run);  → passed to bus.on"),
        "{run}"
    );
    let ready = fx.trace(&["src/model.ts#Mock.ready"]);
    assert_eq!(site(&ready, 12), None, "{ready}");

    // Rust fields and methods have separate namespaces: `self.limit` is the
    // field, `Builder::limit` and `Self::limit` are the method.
    let limit = fx.trace(&["src/builder.rs#Builder.limit"]);
    assert_eq!(site(&limit, 11), None, "{limit}");
    assert_eq!(
        site(&limit, 12),
        Some("12\tlet f = Builder::limit;  → assigned"),
        "{limit}"
    );
    assert_eq!(
        site(&limit, 13),
        Some("13\tlet g = Self::limit;  → assigned"),
        "{limit}"
    );
    assert_eq!(
        site(&limit, 14),
        Some("14\tlet n = self.limit();"),
        "{limit}"
    );
}
