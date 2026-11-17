//! Workspace roots, path resolution and display.

use std::path::{Component, Path, PathBuf};
use std::sync::Arc;

use parking_lot::RwLock;

#[derive(Debug)]
pub enum PathError {
    NotFound(PathBuf),
    Outside(PathBuf),
    /// A relative path with nothing to resolve it against (the message says
    /// what to do instead).
    NoBase(String),
}

/// Roots, access policy and the base directory of one request.
///
/// Cloning is cheap and shares the root list, so a per-request view made by
/// [`Workspace::with_base`] still sees roots the client reports later.
#[derive(Clone, Debug)]
pub struct Workspace {
    /// roots[0] is the primary root (relative paths resolve against it
    /// unless a base directory is set).
    roots: Arc<RwLock<Vec<PathBuf>>>,
    /// Roots came from `--root` / `$CLAUDE_PROJECT_DIR`, not the cwd default.
    explicit: bool,
    /// The cwd default was `/` (typical for GUI-launched servers): it grants
    /// nothing until the client reports roots or `--root` is given.
    implicit_slash: bool,
    /// The cwd default was turned off (`--no-cwd-root`): the working
    /// directory is never a root.
    no_cwd: bool,
    /// Read-only dependency caches (cargo registry, SwiftPM checkouts, SDKs…).
    deps: Arc<Vec<PathBuf>>,
    unrestricted: bool,
    home: Option<PathBuf>,
    /// Per-request base directory (`base_dir`): where relative paths resolve
    /// and what a path-less search or map covers.
    base: Option<PathBuf>,
}

impl Workspace {
    pub fn new(
        roots: Vec<PathBuf>,
        unrestricted: bool,
        dep_roots: bool,
    ) -> anyhow::Result<Workspace> {
        Self::with_options(roots, unrestricted, dep_roots, true)
    }

    /// `cwd_root: false` never uses the working directory as a root: without
    /// `roots` (or roots reported by the client) there is no base at all.
    pub fn with_options(
        roots: Vec<PathBuf>,
        unrestricted: bool,
        dep_roots: bool,
        cwd_root: bool,
    ) -> anyhow::Result<Workspace> {
        let explicit = !roots.is_empty();
        let mut canon = Vec::new();
        for r in roots {
            let c = r
                .canonicalize()
                .map_err(|e| anyhow::anyhow!("root {}: {e}", r.display()))?;
            if !canon.contains(&c) {
                canon.push(c);
            }
        }
        let no_cwd = canon.is_empty() && !cwd_root;
        if canon.is_empty() {
            canon.push(if cwd_root {
                std::env::current_dir()?.canonicalize()?
            } else {
                // Same state as a server launched in `/`: a placeholder that
                // grants nothing and is never walked.
                PathBuf::from("/")
            });
        }
        let implicit_slash = !explicit && canon[0] == Path::new("/");
        let home = std::env::var_os("HOME").map(PathBuf::from);
        let mut deps = Vec::new();
        if dep_roots {
            let mut cands: Vec<PathBuf> = Vec::new();
            if let Some(h) = &home {
                for rel in [
                    ".cargo/registry/src",
                    ".cargo/git/checkouts",
                    ".rustup/toolchains",
                    "go/pkg/mod",
                    "Library/Developer/Xcode/DerivedData",
                ] {
                    cands.push(h.join(rel));
                }
            }
            for abs in [
                "/Library/Developer/CommandLineTools/SDKs",
                "/Applications/Xcode.app/Contents/Developer/Platforms",
                "/opt/homebrew/opt/go/libexec/src",
                "/usr/local/go/src",
            ] {
                cands.push(PathBuf::from(abs));
            }
            for c in cands {
                if let Ok(c) = c.canonicalize() {
                    deps.push(c);
                }
            }
        }
        Ok(Workspace {
            roots: Arc::new(RwLock::new(canon)),
            explicit,
            implicit_slash,
            no_cwd,
            deps: Arc::new(deps),
            unrestricted,
            home,
            base: None,
        })
    }

    /// Where relative paths resolve and what a path-less search or map
    /// covers: the request's `base_dir`, else the primary root. `None` when
    /// there is neither — callers must fail rather than fall back to `/`.
    pub fn base(&self) -> Option<PathBuf> {
        if let Some(b) = &self.base {
            return Some(b.clone());
        }
        if self.rootless() {
            None
        } else {
            Some(self.roots.read()[0].clone())
        }
    }

    /// The base, or the reason there is none.
    pub fn require_base(&self) -> Result<PathBuf, String> {
        self.base().ok_or_else(|| self.no_base())
    }

    /// What to tell the caller when a request needs a base and has none.
    pub fn no_base(&self) -> String {
        if self.unrestricted {
            "no base directory: use an absolute path or pass base_dir (CLI: --base-dir)".into()
        } else if self.no_cwd {
            "no workspace root (--no-cwd-root without --root): pass --root <project dir>, or --unrestricted with absolute paths or base_dir".into()
        } else {
            "no workspace root (speedread was started in / without --root); add `--root <project dir>` to its MCP config".into()
        }
    }

    /// A view of this workspace whose base is `raw`. The directory must be
    /// absolute (or `~/…`), exist and pass the access policy: a base is never
    /// guessed from the process working directory.
    pub fn with_base(&self, raw: &str) -> Result<Workspace, String> {
        let clean = Self::clean(raw);
        let Some(lexical) = self.absolute(&clean) else {
            return Err(format!("base_dir must be an absolute path (got `{clean}`)"));
        };
        let lexical = normalize(&lexical);
        let canon = lexical
            .canonicalize()
            .map_err(|_| format!("base_dir does not exist: {}", lexical.display()))?;
        if !canon.is_dir() {
            return Err(format!("base_dir is not a directory: {}", canon.display()));
        }
        if !self.allowed(&canon) {
            return Err(format!(
                "base_dir is outside the workspace: {} (start speedread with --root <dir> or --unrestricted)",
                canon.display()
            ));
        }
        Ok(Workspace {
            base: Some(canon),
            ..self.clone()
        })
    }

    /// `raw` as a path when it does not depend on a base (`/…`, `~`, `~/…`).
    fn absolute(&self, clean: &str) -> Option<PathBuf> {
        if clean == "~" {
            return Some(self.home.clone().unwrap_or_else(|| PathBuf::from("/")));
        }
        if let Some(rest) = clean.strip_prefix("~/") {
            return Some(
                self.home
                    .clone()
                    .unwrap_or_else(|| PathBuf::from("/"))
                    .join(rest),
            );
        }
        let p = Path::new(clean);
        p.is_absolute().then(|| p.to_path_buf())
    }

    /// True when `raw` can only be resolved against a base and there is none.
    pub fn needs_base(&self, raw: &str) -> bool {
        self.absolute(&Self::clean(raw)).is_none() && self.base().is_none()
    }

    /// The tree to search for definitions and usages of `target`: the
    /// configured root containing it (longest match); for a file in a
    /// dependency cache, the base (who in the workspace uses it); else the
    /// nearest enclosing repository (`.git` directory or file, so worktrees
    /// and submodules count); else the base when it contains the target; else
    /// the target's own directory. Discovery sets scope only — it never
    /// grants access, and never selects `/` or the home directory.
    pub fn lookup_root(&self, target: &Path) -> PathBuf {
        {
            let roots = self.roots.read();
            if let Some(r) = roots
                .iter()
                .filter(|r| !(self.implicit_slash && *r == Path::new("/")) && target.starts_with(r))
                .max_by_key(|r| r.as_os_str().len())
            {
                return r.clone();
            }
        }
        let base = self.base();
        if self.deps.iter().any(|d| target.starts_with(d))
            && let Some(b) = &base
        {
            return b.clone();
        }
        let start = if target.is_dir() {
            target
        } else {
            target.parent().unwrap_or(target)
        };
        let mut cur = Some(start);
        while let Some(d) = cur {
            if d.parent().is_none() || Some(d) == self.home.as_deref() {
                break;
            }
            if d.join(".git").symlink_metadata().is_ok() && self.allowed(d) {
                return d.to_path_buf();
            }
            cur = d.parent();
        }
        match base {
            Some(b) if target.starts_with(&b) => b,
            _ => start.to_path_buf(),
        }
    }

    /// The base, or `/` when there is none (display and legacy callers; use
    /// [`Workspace::require_base`] before walking).
    pub fn primary(&self) -> PathBuf {
        self.base().unwrap_or_else(|| PathBuf::from("/"))
    }

    pub fn roots(&self) -> Vec<PathBuf> {
        self.roots.read().clone()
    }

    /// No usable root yet: started in `/` without `--root` or client roots.
    pub fn rootless(&self) -> bool {
        self.implicit_slash && self.roots.read().iter().all(|r| r == Path::new("/"))
    }

    /// Apply workspace folders reported by the client (MCP `roots/list`):
    /// they replace the cwd default, or extend explicitly configured roots.
    pub fn set_client_roots(&self, client: Vec<PathBuf>) {
        let client: Vec<PathBuf> = client
            .into_iter()
            .filter_map(|p| p.canonicalize().ok())
            .collect();
        if client.is_empty() {
            return;
        }
        let mut roots = self.roots.write();
        if !self.explicit {
            roots.clear();
        }
        for c in client {
            if !roots.contains(&c) {
                roots.push(c);
            }
        }
    }

    /// Normalize what a model typed: quotes, backticks, `file://`, `~`.
    pub fn clean(raw: &str) -> String {
        let mut s = raw
            .trim()
            .trim_matches(|c| c == '`' || c == '"' || c == '\'')
            .trim();
        if let Some(rest) = s.strip_prefix("file://") {
            s = rest;
        }
        s.to_string()
    }

    /// Absolute, lexically normalized path (no filesystem access).
    pub fn join(&self, raw: &str) -> PathBuf {
        let raw = Self::clean(raw);
        let p = self
            .absolute(&raw)
            .unwrap_or_else(|| self.primary().join(&raw));
        normalize(&p)
    }

    pub fn resolve(&self, raw: &str) -> Result<PathBuf, PathError> {
        if self.needs_base(raw) {
            return Err(PathError::NoBase(self.no_base()));
        }
        let lexical = self.join(raw);
        let found = lexical.canonicalize().ok().or_else(|| {
            // A relative path might belong to a secondary root.
            let clean = Self::clean(raw);
            if Path::new(&clean).is_absolute() || clean.starts_with('~') {
                return None;
            }
            let roots = self.roots.read();
            // With an explicit base, every configured root is a fallback.
            let skip = usize::from(self.base.is_none());
            roots[skip..]
                .iter()
                .filter(|r| !(self.implicit_slash && *r == Path::new("/")))
                .find_map(|r| normalize(&r.join(&clean)).canonicalize().ok())
        });
        match found {
            Some(c) if self.allowed(&c) => Ok(c),
            Some(c) => Err(PathError::Outside(c)),
            None => Err(PathError::NotFound(lexical)),
        }
    }

    pub fn allowed(&self, canon: &Path) -> bool {
        self.unrestricted
            || self
                .roots
                .read()
                .iter()
                .any(|r| !(self.implicit_slash && r == Path::new("/")) && canon.starts_with(r))
            || self.deps.iter().any(|r| canon.starts_with(r))
    }

    /// Shortest unambiguous display form: base-relative, `~/…`, or absolute.
    /// Whatever is shown resolves back to the same file under the same base.
    pub fn display(&self, p: &Path) -> String {
        if let Some(base) = self.base()
            && let Ok(rel) = p.strip_prefix(base)
        {
            let s = rel.to_string_lossy();
            return if s.is_empty() {
                ".".to_string()
            } else {
                s.into_owned()
            };
        }
        if let Some(h) = &self.home
            && let Ok(rel) = p.strip_prefix(h)
        {
            return format!("~/{}", rel.to_string_lossy());
        }
        p.to_string_lossy().into_owned()
    }
}

pub fn normalize(p: &Path) -> PathBuf {
    let mut out = PathBuf::new();
    for c in p.components() {
        match c {
            Component::CurDir => {}
            Component::ParentDir => {
                out.pop();
            }
            other => out.push(other.as_os_str()),
        }
    }
    out
}

/// `file:///Users/me/My%20App` → `/Users/me/My App`.
pub fn file_uri_to_path(uri: &str) -> Option<PathBuf> {
    use std::os::unix::ffi::OsStringExt;
    let rest = uri.strip_prefix("file://")?;
    let rest = rest.strip_prefix("localhost").unwrap_or(rest);
    let b = rest.as_bytes();
    let mut out = Vec::with_capacity(b.len());
    let mut i = 0;
    while i < b.len() {
        if b[i] == b'%'
            && i + 2 < b.len()
            && let Some(v) = std::str::from_utf8(&b[i + 1..i + 3])
                .ok()
                .and_then(|h| u8::from_str_radix(h, 16).ok())
        {
            out.push(v);
            i += 3;
            continue;
        }
        out.push(b[i]);
        i += 1;
    }
    Some(PathBuf::from(std::ffi::OsString::from_vec(out)))
}

/// Rank candidate files for a path that does not exist.
pub fn suggest(wanted: &str, files: &[String], limit: usize) -> Vec<String> {
    let wanted = wanted.trim_start_matches("./").to_ascii_lowercase();
    let wname = wanted.rsplit('/').next().unwrap_or(&wanted).to_string();
    let wstem = wname.split('.').next().unwrap_or(&wname).to_string();
    let wdirs: Vec<&str> = wanted.split('/').collect();
    let mut scored: Vec<(i64, &String)> = Vec::new();
    for f in files {
        let fl = f.to_ascii_lowercase();
        let name = fl.rsplit('/').next().unwrap_or(&fl);
        let stem = name.split('.').next().unwrap_or(name);
        let mut score: i64 = if name == wname {
            1000
        } else if stem == wstem && !wstem.is_empty() {
            600
        } else if wname.len() >= 4 {
            let d = crate::util::levenshtein(name, &wname);
            if d <= (wname.len() / 4).max(1) {
                400 - 50 * d as i64
            } else if name.contains(&wstem) && wstem.len() >= 4 {
                200
            } else {
                continue;
            }
        } else {
            continue;
        };
        // Reward shared directory components, penalize depth.
        let fdirs: Vec<&str> = fl.split('/').collect();
        score += 20 * wdirs.iter().filter(|d| fdirs.contains(d)).count() as i64;
        score -= fdirs.len() as i64;
        scored.push((score, f));
    }
    scored.sort_by(|a, b| b.0.cmp(&a.0).then_with(|| a.1.cmp(b.1)));
    scored
        .into_iter()
        .take(limit)
        .map(|(_, f)| f.clone())
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn suggestions_prefer_same_name() {
        let files: Vec<String> = [
            "src/lib/utils.ts",
            "src/utils/index.ts",
            "src/util.ts",
            "test/utils.test.ts",
            "README.md",
        ]
        .iter()
        .map(|s| s.to_string())
        .collect();
        let s = suggest("src/utils.ts", &files, 3);
        assert_eq!(s[0], "src/lib/utils.ts");
        assert!(s.contains(&"src/util.ts".to_string()));
        assert_eq!(
            normalize(Path::new("/a/b/../c/./d")),
            PathBuf::from("/a/c/d")
        );
        assert_eq!(
            file_uri_to_path("file:///Users/me/My%20App"),
            Some(PathBuf::from("/Users/me/My App"))
        );
        assert_eq!(file_uri_to_path("/nope"), None);
    }

    fn tmp(name: &str) -> PathBuf {
        let d = std::env::temp_dir().join(format!("speedread-ws-{name}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&d);
        std::fs::create_dir_all(&d).unwrap();
        d.canonicalize().unwrap()
    }

    #[test]
    fn lookup_root_prefers_roots_then_repositories() {
        let d = tmp("lookup");
        for sub in [
            "root/pkg/src",
            "repo/.git",
            "repo/deep/er",
            "wt/sub",
            "plain/x",
        ] {
            std::fs::create_dir_all(d.join(sub)).unwrap();
        }
        std::fs::write(d.join("wt/.git"), "gitdir: /elsewhere\n").unwrap();
        std::fs::write(d.join("repo/deep/er/f.rs"), "").unwrap();
        let ws = Workspace::with_options(vec![d.join("root")], true, false, true).unwrap();
        // A configured root wins, and is not narrowed to a nested directory.
        assert_eq!(ws.lookup_root(&d.join("root/pkg/src/a.rs")), d.join("root"));
        // Outside the roots: the nearest `.git`, directory or file.
        assert_eq!(ws.lookup_root(&d.join("repo/deep/er/f.rs")), d.join("repo"));
        assert_eq!(ws.lookup_root(&d.join("repo/deep/er")), d.join("repo"));
        assert_eq!(ws.lookup_root(&d.join("wt/sub/a.rs")), d.join("wt"));
        // No repository: the target's own directory, never a guessed parent.
        assert_eq!(ws.lookup_root(&d.join("plain/x/a.rs")), d.join("plain/x"));
        // …unless the request's base contains it.
        let based = ws.with_base(d.join("plain").to_str().unwrap()).unwrap();
        assert_eq!(based.lookup_root(&d.join("plain/x/a.rs")), d.join("plain"));
        // Discovery grants nothing: a repository outside the policy is not used.
        let strict = Workspace::with_options(vec![d.join("root")], false, false, true).unwrap();
        assert_eq!(
            strict.lookup_root(&d.join("repo/deep/er/f.rs")),
            d.join("repo/deep/er")
        );
        let _ = std::fs::remove_dir_all(&d);
    }

    #[test]
    fn no_cwd_root_has_no_base_until_one_is_given() {
        let d = tmp("nocwd");
        std::fs::write(d.join("f.rs"), "").unwrap();
        let ws = Workspace::with_options(Vec::new(), true, false, false).unwrap();
        assert!(ws.base().is_none());
        assert!(matches!(ws.resolve("f.rs"), Err(PathError::NoBase(_))));
        // Absolute paths never consult a base, and display stays absolute.
        let abs = d.join("f.rs");
        assert_eq!(ws.resolve(abs.to_str().unwrap()).unwrap(), abs);
        assert_eq!(ws.display(&abs), abs.to_string_lossy());
        let based = ws.with_base(d.to_str().unwrap()).unwrap();
        assert_eq!(based.resolve("f.rs").unwrap(), abs);
        assert_eq!(based.display(&abs), "f.rs");
        assert!(ws.with_base("relative").unwrap_err().contains("absolute"));
        assert!(
            ws.with_base(abs.to_str().unwrap())
                .unwrap_err()
                .contains("not a directory")
        );
        // Without --unrestricted nothing is reachable, base or not.
        let strict = Workspace::with_options(Vec::new(), false, false, false).unwrap();
        assert!(
            strict
                .with_base(d.to_str().unwrap())
                .unwrap_err()
                .contains("outside the workspace")
        );
        // Client roots replace the missing default and are seen by views.
        let view = strict.clone();
        strict.set_client_roots(vec![d.clone()]);
        assert_eq!(view.base(), Some(d.clone()));
        let _ = std::fs::remove_dir_all(&d);
    }
}
