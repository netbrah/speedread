//! Parallel, .gitignore-aware directory walking.
//!
//! On macOS this uses `getattrlistbulk(2)`, which returns name, type, size,
//! mtime, inode and flags for a whole batch of directory entries per syscall
//! instead of readdir + one lstat per entry (2.6–6.4× faster on APFS in
//! published measurements). Gitignore semantics come from the `ignore` crate's
//! matchers, but `.gitignore`/`.ignore` files are only opened when the
//! directory listing shows they exist (the `ignore` walker probes for them in
//! every directory). Dataless iCloud placeholders and firmlinks are skipped.
//! Other platforms fall back to `ignore::WalkParallel`.

use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, AtomicU8, AtomicU64, Ordering};
use std::time::{Duration, Instant};

#[cfg(target_os = "macos")]
use ignore::Match;
#[cfg(target_os = "macos")]
use ignore::gitignore::{Gitignore, GitignoreBuilder};
use ignore::overrides::{Override, OverrideBuilder};

/// Directory names that are never walked into unless they are the root.
pub const ALWAYS_SKIP: &[&str] = &[
    ".git",
    ".hg",
    ".svn",
    ".jj",
    ".DS_Store",
    "node_modules",
    "__pycache__",
    ".venv",
    ".build",
    "DerivedData",
    ".gradle",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".next",
    ".turbo",
    ".parcel-cache",
];

#[derive(Clone, Debug, Default)]
pub struct Entry {
    pub path: PathBuf,
    pub depth: usize,
    pub is_dir: bool,
    pub size: u64,
    pub mtime_ns: i128,
    /// iCloud placeholder whose content is not on disk.
    pub dataless: bool,
    /// A symbolic link (only reported with `WalkOpts::include_symlinks`;
    /// never followed).
    pub symlink: bool,
}

#[derive(Clone, Debug, Default)]
pub struct WalkOpts {
    /// ripgrep-style globs: `*.rs` includes, `!tests/**` excludes.
    pub globs: Vec<String>,
    pub max_depth: Option<usize>,
    pub include_dirs: bool,
    /// Report symlinks as entries (for listings). Walks never follow them.
    pub include_symlinks: bool,
    /// The request's discovery limits (`None`: unbounded).
    pub limits: Option<Arc<Limits>>,
}

/// Which limit stopped discovery.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Stop {
    /// A walk delivered `max_entries` files.
    Entries,
    /// The request ran past its deadline.
    Deadline,
    /// The client cancelled the request.
    Cancelled,
    /// More files mention a name than a definition lookup examines.
    Candidates,
}

/// Upper bound on files that mention a name and are parsed to find its
/// definitions.
pub const MAX_CANDIDATES: usize = 5000;

/// Discovery limits of one request, shared by every walk it performs.
///
/// The entry limit applies to each walk (a trace walks the same tree several
/// times); the deadline and cancellation apply to the request as a whole.
/// When a limit fires the walk stops and [`Limits::stopped`] says why, so the
/// answer can state that it is incomplete instead of claiming absence.
#[derive(Debug)]
pub struct Limits {
    max_entries: u64,
    budget: Option<Duration>,
    started: Instant,
    cancel: AtomicBool,
    stop: AtomicU8,
    examined: AtomicU64,
    /// Files a text search read only up to a line too long to buffer.
    long_lines: AtomicU64,
    /// The first of them in path order, to name one.
    long_first: parking_lot::Mutex<Option<PathBuf>>,
    /// Times a limit was reached, whether or not it became the reported one.
    events: AtomicU64,
    /// Files found to mention a name, by root and pattern: a trace asks the
    /// same question of the same tree several times, and one walk answers it.
    mentions: parking_lot::Mutex<Vec<(MentionKey, Mentions)>>,
    /// Whether those lists should cover every searchable file (a trace also
    /// reads call sites from them) or only files that can hold a definition.
    all_mentions: AtomicBool,
}

type MentionKey = (PathBuf, String);

/// Upper bound on files remembered as mentioning a name.
pub const MAX_MENTIONS: usize = 20_000;

/// The files of a tree with a line matching a pattern, in path order.
#[derive(Clone, Debug)]
pub struct Mentions {
    pub files: std::sync::Arc<Vec<PathBuf>>,
    /// False when the walk stopped early (a limit, or [`MAX_MENTIONS`]).
    pub complete: bool,
    /// Only files in a known language and of a parseable size were read.
    pub code_only: bool,
}

impl Limits {
    /// `max_entries` per walk and `deadline` per request; 0 / `None` disable.
    pub fn new(max_entries: u64, deadline: Option<Duration>) -> Limits {
        Limits {
            max_entries,
            budget: deadline.filter(|d| !d.is_zero()),
            started: Instant::now(),
            cancel: AtomicBool::new(false),
            stop: AtomicU8::new(0),
            examined: AtomicU64::new(0),
            long_lines: AtomicU64::new(0),
            long_first: parking_lot::Mutex::new(None),
            events: AtomicU64::new(0),
            mentions: parking_lot::Mutex::new(Vec::new()),
            all_mentions: AtomicBool::new(false),
        }
    }

    /// This request will look for occurrences as well as definitions.
    pub(crate) fn want_all_mentions(&self) {
        self.all_mentions.store(true, Ordering::Relaxed);
    }

    pub(crate) fn wants_all_mentions(&self) -> bool {
        self.all_mentions.load(Ordering::Relaxed)
    }

    /// How many times a limit has been reached so far; unchanged across a
    /// walk means the walk saw everything.
    pub(crate) fn events(&self) -> u64 {
        self.events.load(Ordering::Relaxed)
    }

    pub(crate) fn mentions(&self, root: &Path, pattern: &str) -> Option<Mentions> {
        self.mentions
            .lock()
            .iter()
            .find(|((r, p), _)| r == root && p == pattern)
            .map(|(_, m)| m.clone())
    }

    pub(crate) fn remember_mentions(&self, root: &Path, pattern: &str, m: Mentions) {
        const KEPT: usize = 16;
        let mut all = self.mentions.lock();
        if all.len() < KEPT {
            all.push(((root.to_path_buf(), pattern.to_string()), m));
        }
    }

    pub fn unlimited() -> Limits {
        Limits::new(0, None)
    }

    /// Stop this request's walks at their next entry.
    pub fn cancel(&self) {
        self.cancel.store(true, Ordering::Relaxed);
    }

    /// Record that discovery was cut short. The first reason is kept.
    pub fn mark(&self, why: Stop) {
        let code = match why {
            Stop::Entries => 1,
            Stop::Deadline => 2,
            Stop::Cancelled => 3,
            Stop::Candidates => 4,
        };
        // The first cause is the one reported, except that running out of
        // time (or being cancelled) outranks a count limit: after it nothing
        // more was examined, whatever limit was hit before.
        let hard = matches!(why, Stop::Deadline | Stop::Cancelled);
        self.events.fetch_add(1, Ordering::Relaxed);
        let mut cur = self.stop.load(Ordering::Relaxed);
        let first = loop {
            if !(cur == 0 || (hard && matches!(cur, 1 | 4))) {
                return;
            }
            match self
                .stop
                .compare_exchange_weak(cur, code, Ordering::Relaxed, Ordering::Relaxed)
            {
                Ok(_) => break cur == 0,
                Err(now) => cur = now,
            }
        };
        if first {
            match why {
                Stop::Cancelled => STATS.cancelled.fetch_add(1, Ordering::Relaxed),
                _ => STATS.stopped.fetch_add(1, Ordering::Relaxed),
            };
        }
    }

    pub fn stopped(&self) -> Option<Stop> {
        match self.stop.load(Ordering::Relaxed) {
            1 => Some(Stop::Entries),
            2 => Some(Stop::Deadline),
            3 => Some(Stop::Cancelled),
            4 => Some(Stop::Candidates),
            _ => None,
        }
    }

    /// Files delivered to this request so far, over all its walks.
    pub fn examined(&self) -> u64 {
        self.examined.load(Ordering::Relaxed)
    }

    /// Deadline or cancellation reached (checked for every entry seen, also
    /// ones a glob filters out).
    pub(crate) fn expired(&self) -> bool {
        if self.cancel.load(Ordering::Relaxed) {
            self.mark(Stop::Cancelled);
            return true;
        }
        if let Some(b) = self.budget
            && self.started.elapsed() >= b
        {
            self.mark(Stop::Deadline);
            return true;
        }
        false
    }

    /// May a walk that has delivered `seen` entries deliver `n` more?
    fn admit(&self, seen: u64, n: u64) -> bool {
        if self.expired() {
            return false;
        }
        if self.max_entries > 0 && seen + n > self.max_entries {
            self.mark(Stop::Entries);
            return false;
        }
        self.examined.fetch_add(n, Ordering::Relaxed);
        true
    }

    /// Record that a text search stopped in `path` at a line too long to
    /// buffer (`search::MAX_LINE_BYTES`).
    pub(crate) fn long_line(&self, path: &Path) {
        self.long_lines.fetch_add(1, Ordering::Relaxed);
        let mut first = self.long_first.lock();
        if first.as_deref().is_none_or(|f| path < f) {
            *first = Some(path.to_path_buf());
        }
    }

    /// One line for the end of a response, when files were not read to
    /// their end. `show` renders a path for the reader.
    pub fn long_note(&self, show: impl Fn(&Path) -> String) -> Option<String> {
        let n = self.long_lines.load(Ordering::Relaxed) as usize;
        let first = self.long_first.lock().clone()?;
        let limit = crate::search::MAX_LINE_BYTES >> 20;
        Some(if n == 1 {
            format!(
                "not fully searched: 1 file has a line longer than {limit} MiB and was read only up to it: {}",
                show(&first)
            )
        } else {
            format!(
                "not fully searched: {} files have a line longer than {limit} MiB and were read only up to it, e.g. {}",
                crate::util::thousands(n),
                show(&first)
            )
        })
    }

    /// One line for the end of a response, when discovery was cut short.
    pub fn note(&self) -> Option<String> {
        let n = crate::util::thousands(self.examined() as usize);
        Some(match self.stopped()? {
            Stop::Entries => format!(
                "incomplete: a walk stopped at its limit of {} files; results may be missing. Narrow with a path or glob.",
                crate::util::thousands(self.max_entries as usize)
            ),
            Stop::Deadline => format!(
                "incomplete: stopped after {} ms ({n} files examined); results may be missing. Narrow with a path or glob.",
                self.budget.map(|b| b.as_millis()).unwrap_or(0)
            ),
            Stop::Cancelled => format!("incomplete: cancelled ({n} files examined)."),
            Stop::Candidates => format!(
                "incomplete: the name is in more files than a lookup examines (at most {} searched, {} parsed); definitions may be missing. Qualify it (path#Name) or narrow with a path.",
                crate::util::thousands(MAX_MENTIONS),
                crate::util::thousands(MAX_CANDIDATES)
            ),
        })
    }
}

pub fn build_overrides(root: &Path, globs: &[String]) -> anyhow::Result<Option<Override>> {
    if globs.is_empty() {
        return Ok(None);
    }
    let mut b = OverrideBuilder::new(root);
    for g in globs {
        let g = g.trim();
        if !g.is_empty() {
            b.add(g)
                .map_err(|e| anyhow::anyhow!("invalid glob {g:?}: {e}"))?;
        }
    }
    Ok(Some(b.build()?))
}

/// Process-wide discovery counters. They make "this request did not walk"
/// a testable property (`SPEEDREAD_STATS=1` prints them on exit).
pub struct Stats {
    /// Directory walks started.
    pub walks: AtomicU64,
    /// Entries delivered to callers.
    pub entries: AtomicU64,
    /// Requests whose discovery hit a limit.
    pub stopped: AtomicU64,
    /// Requests cancelled by the client.
    pub cancelled: AtomicU64,
}

pub static STATS: Stats = Stats {
    walks: AtomicU64::new(0),
    entries: AtomicU64::new(0),
    stopped: AtomicU64::new(0),
    cancelled: AtomicU64::new(0),
};

impl Stats {
    /// `walks=… entries=… stopped=… cancelled=…`
    pub fn line(&self) -> String {
        format!(
            "walks={} entries={} stopped={} cancelled={}",
            self.walks.load(Ordering::Relaxed),
            self.entries.load(Ordering::Relaxed),
            self.stopped.load(Ordering::Relaxed),
            self.cancelled.load(Ordering::Relaxed)
        )
    }
}

/// Walk `root` in parallel; `f` is called for every file (and directory when
/// `include_dirs`), from multiple threads. Returning `false` stops the walk.
pub fn walk<F>(root: &Path, opts: &WalkOpts, f: F) -> anyhow::Result<()>
where
    F: Fn(Entry) -> bool + Sync,
{
    walk_batches(root, opts, |batch| batch.into_iter().all(&f))
}

/// Like [`walk`], but `f` receives each directory's accepted entries as one
/// batch (one call — and one lock, for collectors — per directory).
pub fn walk_batches<F>(root: &Path, opts: &WalkOpts, f: F) -> anyhow::Result<()>
where
    F: Fn(Vec<Entry>) -> bool + Sync,
{
    let overrides = build_overrides(root, &opts.globs)?;
    STATS.walks.fetch_add(1, Ordering::Relaxed);
    let limits = opts.limits.as_deref();
    if limits.is_some_and(|l| l.expired()) {
        return Ok(());
    }
    // Entries this walk has delivered (the entry limit is per walk).
    let seen = AtomicU64::new(0);
    let f = |batch: Vec<Entry>| {
        let n = batch.len() as u64;
        if let Some(l) = limits
            && !l.admit(seen.fetch_add(n, Ordering::Relaxed), n)
        {
            return false;
        }
        STATS.entries.fetch_add(n, Ordering::Relaxed);
        f(batch)
    };
    #[cfg(target_os = "macos")]
    if !use_portable() {
        fast::walk(root, opts, overrides, &f);
        return Ok(());
    }
    portable::walk(root, opts, overrides, &|e| f(vec![e]));
    Ok(())
}

/// `SPEEDREAD_WALKER=portable` selects the `ignore`-crate walker (benchmarks,
/// or an escape hatch on unusual filesystems).
#[cfg(target_os = "macos")]
fn use_portable() -> bool {
    static PORTABLE: std::sync::OnceLock<bool> = std::sync::OnceLock::new();
    *PORTABLE.get_or_init(|| std::env::var("SPEEDREAD_WALKER").is_ok_and(|v| v == "portable"))
}

#[cfg(target_os = "macos")]
struct IgnoreNode {
    parent: Option<Arc<IgnoreNode>>,
    /// Lowest precedence first.
    matchers: Vec<Gitignore>,
}

#[cfg(target_os = "macos")]
fn is_ignored(stack: &Option<Arc<IgnoreNode>>, path: &Path, is_dir: bool) -> bool {
    let mut cur = stack.as_ref();
    while let Some(node) = cur {
        for m in node.matchers.iter().rev() {
            match m.matched(path, is_dir) {
                Match::None => {}
                Match::Ignore(_) => return true,
                Match::Whitelist(_) => return false,
            }
        }
        cur = node.parent.as_ref();
    }
    false
}

#[cfg(target_os = "macos")]
fn load_matcher(dir: &Path, files: &[&str]) -> Option<Gitignore> {
    let mut b = GitignoreBuilder::new(dir);
    let mut any = false;
    for f in files {
        let p = dir.join(f);
        if p.is_file() && b.add(&p).is_none() {
            any = true;
        }
    }
    if !any {
        return None;
    }
    b.build().ok().filter(|g| !g.is_empty())
}

/// Ignore matchers that apply above the walk root: the global excludes file,
/// then every `.gitignore` from the enclosing repository root down to (but
/// excluding) `root` itself.
/// The global excludes file (`core.excludesFile`), the base of every stack.
#[cfg(target_os = "macos")]
fn global_node() -> Option<Arc<IgnoreNode>> {
    let (global, _) = Gitignore::global();
    (!global.is_empty()).then(|| {
        Arc::new(IgnoreNode {
            parent: None,
            matchers: vec![global],
        })
    })
}

/// A repository root is an ignore boundary: rules from an enclosing
/// repository (a dotfiles repo in `~`, a monorepo around a nested checkout)
/// never apply inside it, exactly as in git.
#[cfg(target_os = "macos")]
fn initial_stack(root: &Path, global: Option<Arc<IgnoreNode>>) -> Option<Arc<IgnoreNode>> {
    let mut node = global;
    if root.join(".git").exists() {
        return node;
    }
    let mut ancestors: Vec<&Path> = Vec::new();
    let mut cur = root.parent();
    while let Some(p) = cur {
        ancestors.push(p);
        if p.join(".git").exists() {
            break;
        }
        cur = p.parent();
    }
    // Only use ancestors if we actually found an enclosing repository.
    if ancestors.last().is_some_and(|p| p.join(".git").exists()) {
        for dir in ancestors.into_iter().rev() {
            let mut matchers = Vec::new();
            if dir.join(".git").exists()
                && let Some(m) = load_matcher(dir, &[".git/info/exclude"])
            {
                matchers.push(m);
            }
            if let Some(m) = load_matcher(dir, &[".gitignore"]) {
                matchers.push(m);
            }
            if let Some(m) = load_matcher(dir, &[".ignore"]) {
                matchers.push(m);
            }
            if !matchers.is_empty() {
                node = Some(Arc::new(IgnoreNode {
                    parent: node,
                    matchers,
                }));
            }
        }
    }
    node
}

#[cfg(target_os = "macos")]
mod fast {
    use std::cell::RefCell;
    use std::ffi::{CString, OsStr};
    use std::os::unix::ffi::OsStrExt;

    use super::*;

    const ATTR_CMN_ERROR: u32 = 0x2000_0000;
    const VREG: u32 = 1;
    const VDIR: u32 = 2;
    const VLNK: u32 = 5;
    const SF_FIRMLINK: u32 = 0x0080_0000;
    const SF_DATALESS: u32 = 0x4000_0000;
    const BUF_SIZE: usize = 256 * 1024;
    /// Bytes of always-present fields before the optional file group.
    const FIXED: usize = 68;

    struct Raw {
        name: Vec<u8>,
        objtype: u32,
        flags: u32,
        mtime_ns: i128,
        size: u64,
    }

    thread_local! {
        static BUF: RefCell<Vec<u8>> = RefCell::new(vec![0u8; BUF_SIZE]);
    }

    fn rd_u32(b: &[u8], o: usize) -> u32 {
        u32::from_ne_bytes(b[o..o + 4].try_into().unwrap())
    }
    fn rd_i32(b: &[u8], o: usize) -> i32 {
        i32::from_ne_bytes(b[o..o + 4].try_into().unwrap())
    }
    fn rd_i64(b: &[u8], o: usize) -> i64 {
        i64::from_ne_bytes(b[o..o + 8].try_into().unwrap())
    }

    /// List a directory with getattrlistbulk. Record layout with
    /// FSOPT_PACK_INVAL_ATTRS (common attributes always present, 4-byte aligned):
    /// len u32 | returned attribute_set 20B | error u32 | name attrreference 8B |
    /// objtype u32 | modtime timespec 16B | flags u32 | fileid u64 | [datalength i64]
    /// The file group (datalength) is only packed for non-directories, so it is
    /// read only when the returned attribute set says it is present.
    fn list_dir(dir: &Path) -> std::io::Result<Vec<Raw>> {
        match list_dir_bulk(dir) {
            Err(e)
                if matches!(
                    e.raw_os_error(),
                    Some(libc::ENOTSUP) | Some(libc::EINVAL) | Some(libc::ENOSYS)
                ) =>
            {
                list_dir_readdir(dir)
            }
            other => other,
        }
    }

    /// Fallback for filesystems without getattrlistbulk support.
    fn list_dir_readdir(dir: &Path) -> std::io::Result<Vec<Raw>> {
        use std::os::unix::fs::MetadataExt;
        let mut out = Vec::new();
        for ent in std::fs::read_dir(dir)? {
            let Ok(ent) = ent else { continue };
            let Ok(md) = ent.metadata() else { continue };
            let ft = md.file_type();
            out.push(Raw {
                name: ent.file_name().as_bytes().to_vec(),
                objtype: if ft.is_dir() {
                    VDIR
                } else if ft.is_file() {
                    VREG
                } else {
                    0
                },
                flags: 0,
                mtime_ns: md.mtime() as i128 * 1_000_000_000 + md.mtime_nsec() as i128,
                size: md.len(),
            });
        }
        Ok(out)
    }

    fn list_dir_bulk(dir: &Path) -> std::io::Result<Vec<Raw>> {
        let c = CString::new(dir.as_os_str().as_bytes())?;
        // SAFETY: valid NUL-terminated path.
        let fd = unsafe {
            libc::open(
                c.as_ptr(),
                libc::O_RDONLY | libc::O_DIRECTORY | libc::O_CLOEXEC,
            )
        };
        if fd < 0 {
            return Err(std::io::Error::last_os_error());
        }
        // SAFETY: attrlist is a plain C struct; zero is a valid initial state.
        let mut al: libc::attrlist = unsafe { std::mem::zeroed() };
        al.bitmapcount = libc::ATTR_BIT_MAP_COUNT;
        al.commonattr = libc::ATTR_CMN_RETURNED_ATTRS
            | ATTR_CMN_ERROR
            | libc::ATTR_CMN_NAME
            | libc::ATTR_CMN_OBJTYPE
            | libc::ATTR_CMN_MODTIME
            | libc::ATTR_CMN_FLAGS
            | libc::ATTR_CMN_FILEID;
        al.fileattr = libc::ATTR_FILE_DATALENGTH;
        let mut out = Vec::new();
        let res = BUF.with(|cell| {
            let mut buf = cell.borrow_mut();
            loop {
                // SAFETY: fd is an open directory, buffers are valid for their lengths.
                let n = unsafe {
                    libc::getattrlistbulk(
                        fd,
                        &mut al as *mut libc::attrlist as *mut libc::c_void,
                        buf.as_mut_ptr() as *mut libc::c_void,
                        buf.len(),
                        libc::FSOPT_PACK_INVAL_ATTRS as u64,
                    )
                };
                if n < 0 {
                    let e = std::io::Error::last_os_error();
                    if e.kind() == std::io::ErrorKind::Interrupted {
                        continue;
                    }
                    return Err(e);
                }
                if n == 0 {
                    return Ok(());
                }
                let mut off = 0usize;
                for _ in 0..n {
                    if off + FIXED > buf.len() {
                        break;
                    }
                    let b = &buf[off..];
                    let len = rd_u32(b, 0) as usize;
                    if len < FIXED || off + len > buf.len() {
                        break;
                    }
                    let rec = &b[..len];
                    let err = rd_u32(rec, 24);
                    let name_off = rd_i32(rec, 28);
                    let name_len = rd_u32(rec, 32) as usize;
                    let name_start = (28i64 + name_off as i64) as usize;
                    off += len;
                    if err != 0 || name_len == 0 || name_start + name_len > len {
                        continue;
                    }
                    let name = &rec[name_start..name_start + name_len - 1];
                    if name == b"." || name == b".." {
                        continue;
                    }
                    let objtype = rd_u32(rec, 36);
                    let secs = rd_i64(rec, 40);
                    let nsecs = rd_i64(rec, 48);
                    let flags = rd_u32(rec, 56);
                    let returned_file = rd_u32(rec, 16);
                    let size =
                        if returned_file & libc::ATTR_FILE_DATALENGTH != 0 && len >= FIXED + 8 {
                            rd_i64(rec, FIXED).max(0) as u64
                        } else {
                            0
                        };
                    out.push(Raw {
                        name: name.to_vec(),
                        objtype,
                        flags,
                        mtime_ns: secs as i128 * 1_000_000_000 + nsecs as i128,
                        size,
                    });
                }
            }
        });
        // SAFETY: closing the fd we opened.
        unsafe { libc::close(fd) };
        res.map(|_| out)
    }

    struct Ctx<'a, F> {
        f: &'a F,
        overrides: Option<Override>,
        global: Option<Arc<IgnoreNode>>,
        stop: AtomicBool,
        max_depth: Option<usize>,
        include_dirs: bool,
        include_symlinks: bool,
    }

    pub(super) fn walk<F>(root: &Path, opts: &WalkOpts, overrides: Option<Override>, f: &F)
    where
        F: Fn(Vec<Entry>) -> bool + Sync,
    {
        let ctx = Ctx {
            f,
            overrides,
            global: global_node(),
            stop: AtomicBool::new(false),
            max_depth: opts.max_depth,
            include_dirs: opts.include_dirs,
            include_symlinks: opts.include_symlinks,
        };
        let stack = initial_stack(root, ctx.global.clone());
        let ctx = &ctx;
        rayon::in_place_scope(|s| {
            s.spawn(move |s| process(s, ctx, root.to_path_buf(), 1, stack));
        });
    }

    fn process<'s, F>(
        s: &rayon::Scope<'s>,
        ctx: &'s Ctx<'s, F>,
        dir: PathBuf,
        depth: usize,
        parent: Option<Arc<IgnoreNode>>,
    ) where
        F: Fn(Vec<Entry>) -> bool + Sync,
    {
        if ctx.stop.load(Ordering::Relaxed) {
            return;
        }
        let Ok(mut entries) = list_dir(&dir) else {
            return;
        };
        let mut batch: Vec<Entry> = Vec::with_capacity(entries.len());
        // Deterministic order within a directory (APFS returns hash order).
        entries.sort_unstable_by(|a, b| a.name.cmp(&b.name));
        let has = |n: &[u8]| entries.iter().any(|e| e.name == n);
        let (git, gi, ig) = (has(b".git"), has(b".gitignore"), has(b".ignore"));
        // A nested repository starts a fresh stack (global excludes only).
        let parent = if git { ctx.global.clone() } else { parent };
        let stack = if git || gi || ig {
            let mut matchers = Vec::new();
            if git && let Some(m) = load_matcher(&dir, &[".git/info/exclude"]) {
                matchers.push(m);
            }
            if gi && let Some(m) = load_matcher(&dir, &[".gitignore"]) {
                matchers.push(m);
            }
            if ig && let Some(m) = load_matcher(&dir, &[".ignore"]) {
                matchers.push(m);
            }
            if matchers.is_empty() {
                parent
            } else {
                Some(Arc::new(IgnoreNode { parent, matchers }))
            }
        } else {
            parent
        };
        for e in entries {
            if ctx.stop.load(Ordering::Relaxed) {
                return;
            }
            let is_dir = e.objtype == VDIR;
            let is_link = e.objtype == VLNK;
            if !is_dir && e.objtype != VREG && !(is_link && ctx.include_symlinks) {
                continue;
            }
            let name = OsStr::from_bytes(&e.name);
            if ALWAYS_SKIP.iter().any(|s| name == OsStr::new(s)) {
                continue;
            }
            let path = dir.join(name);
            let decided = match &ctx.overrides {
                Some(ov) => match ov.matched(&path, is_dir) {
                    Match::Ignore(_) => continue,
                    Match::Whitelist(_) => true,
                    Match::None => false,
                },
                None => false,
            };
            if !decided && is_ignored(&stack, &path, is_dir) {
                continue;
            }
            let entry = Entry {
                path,
                depth,
                is_dir,
                size: e.size,
                mtime_ns: e.mtime_ns,
                dataless: e.flags & SF_DATALESS != 0,
                symlink: is_link,
            };
            if is_dir {
                if e.flags & SF_FIRMLINK != 0 {
                    continue;
                }
                if ctx.max_depth.is_none_or(|m| depth < m) {
                    let st = stack.clone();
                    let p = entry.path.clone();
                    s.spawn(move |s| process(s, ctx, p, depth + 1, st));
                }
                if ctx.include_dirs {
                    batch.push(entry);
                }
            } else {
                batch.push(entry);
            }
        }
        if !batch.is_empty() && !(ctx.f)(batch) {
            ctx.stop.store(true, Ordering::Relaxed);
        }
    }
}

mod portable {
    use std::os::unix::fs::MetadataExt;

    use ignore::{WalkBuilder, WalkState};

    use super::*;

    pub(super) fn walk<F>(root: &Path, opts: &WalkOpts, overrides: Option<Override>, f: &F)
    where
        F: Fn(Entry) -> bool + Sync,
    {
        let mut b = WalkBuilder::new(root);
        b.hidden(false)
            .git_ignore(true)
            .git_global(true)
            .git_exclude(true)
            .ignore(true)
            // A root that is itself a repository ignores enclosing repos' rules.
            .parents(!root.join(".git").exists())
            .require_git(false)
            .follow_links(false)
            .max_depth(opts.max_depth)
            .filter_entry(|e| {
                !ALWAYS_SKIP
                    .iter()
                    .any(|s| e.file_name() == std::ffi::OsStr::new(s))
            });
        if let Some(ov) = overrides {
            b.overrides(ov);
        }
        let stop = AtomicBool::new(false);
        let include_dirs = opts.include_dirs;
        let include_symlinks = opts.include_symlinks;
        let limits = opts.limits.as_deref();
        b.build_parallel().run(|| {
            let stop = &stop;
            Box::new(move |res| {
                // Checked for every entry, so a glob that filters everything
                // out cannot outrun the deadline or a cancellation.
                if stop.load(Ordering::Relaxed) || limits.is_some_and(|l| l.expired()) {
                    return ignore::WalkState::Quit;
                }
                let Ok(e) = res else {
                    return WalkState::Continue;
                };
                if e.depth() == 0 {
                    return WalkState::Continue;
                }
                let Some(ft) = e.file_type() else {
                    return WalkState::Continue;
                };
                let is_dir = ft.is_dir();
                let is_link = ft.is_symlink();
                if !(is_dir || ft.is_file() || (is_link && include_symlinks)) {
                    return WalkState::Continue;
                }
                if is_dir && !include_dirs {
                    return WalkState::Continue;
                }
                let md = e.metadata().ok();
                let entry = Entry {
                    path: e.path().to_path_buf(),
                    depth: e.depth(),
                    is_dir,
                    size: md.as_ref().map(|m| m.len()).unwrap_or(0),
                    mtime_ns: md
                        .as_ref()
                        .map(|m| m.mtime() as i128 * 1_000_000_000 + m.mtime_nsec() as i128)
                        .unwrap_or(0),
                    dataless: false,
                    symlink: is_link,
                };
                if f(entry) {
                    WalkState::Continue
                } else {
                    stop.store(true, Ordering::Relaxed);
                    WalkState::Quit
                }
            })
        });
    }
}

/// Collect all files under `root` (sorted bytewise), up to `limit`.
pub fn list_files(
    root: &Path,
    opts: &WalkOpts,
    limit: usize,
) -> anyhow::Result<(Vec<Entry>, bool)> {
    let out = parking_lot::Mutex::new(Vec::new());
    let truncated = AtomicBool::new(false);
    walk_batches(root, opts, |batch| {
        let mut v = out.lock();
        let room = limit.saturating_sub(v.len());
        if batch.len() > room {
            v.extend(batch.into_iter().take(room));
            truncated.store(true, Ordering::Relaxed);
            return false;
        }
        v.extend(batch);
        true
    })?;
    let mut v = out.into_inner();
    sort_by_path(&mut v);
    Ok((v, truncated.load(Ordering::Relaxed)))
}

/// Bytewise path order: deterministic and far cheaper than `Path::cmp`,
/// which re-parses components on every comparison.
pub fn sort_by_path(v: &mut [Entry]) {
    use std::os::unix::ffi::OsStrExt;
    v.sort_unstable_by(|a, b| {
        a.path
            .as_os_str()
            .as_bytes()
            .cmp(b.path.as_os_str().as_bytes())
    });
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;

    #[test]
    fn running_out_of_time_outranks_a_count_limit() {
        let l = Limits::unlimited();
        l.mark(Stop::Candidates);
        assert_eq!(l.stopped(), Some(Stop::Candidates));
        l.mark(Stop::Entries);
        assert_eq!(
            l.stopped(),
            Some(Stop::Candidates),
            "first count limit wins"
        );
        l.mark(Stop::Deadline);
        assert_eq!(l.stopped(), Some(Stop::Deadline));
        l.mark(Stop::Candidates);
        l.mark(Stop::Cancelled);
        assert_eq!(l.stopped(), Some(Stop::Deadline), "a hard stop is final");
    }

    #[test]
    fn walks_respecting_gitignore_and_globs() {
        let dir = std::env::temp_dir().join(format!("speedread-walk-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(dir.join("src/nested")).unwrap();
        fs::create_dir_all(dir.join("target/debug")).unwrap();
        fs::create_dir_all(dir.join("node_modules/x")).unwrap();
        fs::create_dir_all(dir.join(".git")).unwrap();
        fs::write(dir.join(".gitignore"), "target/\n*.log\n!keep.log\n").unwrap();
        fs::write(dir.join("src/nested/.gitignore"), "secret.txt\n").unwrap();
        for f in [
            "src/a.rs",
            "src/nested/b.rs",
            "src/nested/secret.txt",
            "target/debug/out.rs",
            "node_modules/x/i.js",
            "debug.log",
            "keep.log",
            "README.md",
            ".github-like",
        ] {
            fs::write(dir.join(f), "x\n").unwrap();
        }
        let root = dir.canonicalize().unwrap();
        let (files, _) = list_files(&root, &WalkOpts::default(), 1000).unwrap();
        let rel: Vec<String> = files
            .iter()
            .map(|e| {
                e.path
                    .strip_prefix(&root)
                    .unwrap()
                    .to_string_lossy()
                    .into_owned()
            })
            .collect();
        assert_eq!(
            rel,
            vec![
                ".github-like",
                ".gitignore",
                "README.md",
                "keep.log",
                "src/a.rs",
                "src/nested/.gitignore",
                "src/nested/b.rs"
            ]
        );
        // A nested repository is not subject to the outer repo's rules.
        fs::create_dir_all(dir.join("inner/.git")).unwrap();
        fs::write(dir.join("inner/.gitignore"), "").unwrap();
        fs::write(dir.join("inner/app.log"), "x\n").unwrap();
        #[cfg(target_os = "macos")]
        if !use_portable() {
            let (files, _) = list_files(&root, &WalkOpts::default(), 1000).unwrap();
            assert!(files.iter().any(|e| e.path.ends_with("inner/app.log")));
        }
        let (files, _) = list_files(&root.join("inner"), &WalkOpts::default(), 1000).unwrap();
        assert!(files.iter().any(|e| e.path.ends_with("inner/app.log")));
        fs::remove_dir_all(dir.join("inner")).unwrap();
        let opts = WalkOpts {
            globs: vec!["*.rs".into()],
            ..Default::default()
        };
        let (files, _) = list_files(&root, &opts, 1000).unwrap();
        assert_eq!(files.len(), 2);
        fs::remove_dir_all(&dir).unwrap();
    }
}
