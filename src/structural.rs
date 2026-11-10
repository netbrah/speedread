//! Structural search: a code pattern with metavariables (`$NAME` one node,
//! `$$$NAME` zero or more) matched against syntax trees with `ast-grep-core`.
//!
//! Read-only and on demand, like everything else here: a literal taken from
//! the pattern narrows the candidate files with the ordinary text scan, and
//! only those are parsed. Results are ordinary [`FileHits`], so grouping,
//! `output` modes and budgets behave as they do for a text search.
//! See `docs/specs/04-structural-search.md`.

use std::borrow::Cow;
use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};

use ast_grep_core::matcher::{PatternBuilder, PatternNode};
use ast_grep_core::tree_sitter::{LanguageExt, StrDoc, TSLanguage};
use ast_grep_core::{AstGrep, Language, Pattern, PatternError};
use grep_regex::{RegexMatcher, RegexMatcherBuilder};
use parking_lot::Mutex;
use rayon::prelude::*;

use crate::engine::Engine;
use crate::lang::LangId;
use crate::search::{FileHits, Hit, MAX_GROUP_BYTES, MAX_HITS_PER_FILE, Scan, SearchRequest};
use crate::source::Source;
use crate::walk::{self, WalkOpts};

/// Longest capture value shown after a hit.
const MAX_CAPTURE: usize = 60;

/// A speedread grammar as an ast-grep language.
#[derive(Clone, Copy)]
struct Sg(LangId);

impl Sg {
    /// `$` is not an identifier character in most grammars: metavariables
    /// are rewritten with this one before the pattern is parsed (the same
    /// choices as `ast-grep-language`).
    fn expando(self) -> char {
        use LangId::*;
        match self.0 {
            Java | JavaScript | TypeScript | Tsx | Scala | Bash | Lua | Luau => '$',
            _ => 'µ',
        }
    }
}

impl Language for Sg {
    fn pre_process_pattern<'q>(&self, query: &'q str) -> Cow<'q, str> {
        let expando = self.expando();
        if expando == '$' {
            return Cow::Borrowed(query);
        }
        // `$A`, `$$A`, `$$$A` and the anonymous `$$$` become expando runs;
        // any other `$` (shell variables, template strings) stays.
        let mut out = String::with_capacity(query.len() + 8);
        let mut dollars = 0;
        let flush = |out: &mut String, dollars: &mut usize, meta: bool| {
            let sigil = if meta { expando } else { '$' };
            for _ in 0..*dollars {
                out.push(sigil);
            }
            *dollars = 0;
        };
        for c in query.chars() {
            if c == '$' {
                dollars += 1;
                continue;
            }
            let meta = matches!(c, 'A'..='Z' | '_') || dollars == 3;
            flush(&mut out, &mut dollars, meta);
            out.push(c);
        }
        let meta = dollars == 3;
        flush(&mut out, &mut dollars, meta);
        Cow::Owned(out)
    }

    fn expando_char(&self) -> char {
        self.expando()
    }

    fn kind_to_id(&self, kind: &str) -> u16 {
        self.get_ts_language().id_for_node_kind(kind, true)
    }

    fn field_to_id(&self, field: &str) -> Option<u16> {
        self.get_ts_language()
            .field_id_for_name(field)
            .map(|f| f.get())
    }

    fn build_pattern(&self, builder: &PatternBuilder) -> Result<Pattern, PatternError> {
        builder.build(|src| StrDoc::try_new(src, *self))
    }
}

impl LanguageExt for Sg {
    fn get_ts_language(&self) -> TSLanguage {
        self.0
            .grammar()
            .expect("only languages with a grammar are searched structurally")
    }
}

/// Languages a pattern can run for, in a stable order.
const CODE_LANGS: &[LangId] = &[
    LangId::Rust,
    LangId::Python,
    LangId::JavaScript,
    LangId::TypeScript,
    LangId::Tsx,
    LangId::Go,
    LangId::Java,
    LangId::C,
    LangId::Cpp,
    LangId::CSharp,
    LangId::Ruby,
    LangId::Php,
    LangId::Bash,
    LangId::Swift,
    LangId::Kotlin,
    LangId::Scala,
    LangId::Lua,
    LangId::Luau,
    LangId::ObjC,
];

/// A language named by a user: its name, or a file extension (`ts`, `py`).
pub fn lang_named(name: &str) -> Option<LangId> {
    let name = name.trim().to_ascii_lowercase();
    CODE_LANGS
        .iter()
        .copied()
        .find(|l| l.name() == name)
        .or_else(|| match name.as_str() {
            "c++" => Some(LangId::Cpp),
            "c#" | "cs" => Some(LangId::CSharp),
            "objective-c" | "objectivec" => Some(LangId::ObjC),
            "shell" | "sh" => Some(LangId::Bash),
            _ => crate::lang::detect(Path::new(&format!("x.{name}")), b""),
        })
        .filter(|l| l.grammar().is_some())
}

/// Does a file in language `file` belong to a search for `want`? `.tsx` is
/// TypeScript to whoever asks for TypeScript.
fn lang_selected(want: LangId, file: LangId) -> bool {
    want == file || (want == LangId::TypeScript && file == LangId::Tsx)
}

/// The pattern for one language, or why it cannot be used there: every way
/// it reads as code, as written first.
fn compile(pattern: &str, lang: LangId) -> Result<Vec<Pattern>, String> {
    let mut alts = Vec::new();
    let why = match Pattern::try_new(pattern, Sg(lang)) {
        Ok(p) if !p.has_error() => {
            alts.push(p);
            None
        }
        Ok(_) => Some("it does not parse as one syntax node".to_string()),
        Err(e) => Some(e.to_string()),
    };
    if let Some(p) = in_statement(pattern, lang, alts.first()) {
        alts.push(p);
    }
    match why {
        Some(why) if alts.is_empty() => Err(why),
        _ => Ok(alts),
    }
}

/// The node kind a pattern matches at its root, if it is not a metavariable.
fn root_kind(p: &Pattern) -> Option<u16> {
    match &p.node {
        PatternNode::MetaVar { .. } => None,
        PatternNode::Terminal { kind_id, .. } | PatternNode::Internal { kind_id, .. } => {
            Some(*kind_id)
        }
    }
}

/// The pattern read as an expression inside a statement, when that is a
/// different node from the pattern read on its own (`as_is`). On its own, C
/// and Objective-C read `foo($A)` as a macro type and PHP reads it as text
/// outside `<?php`: such a pattern would match no call at all.
fn in_statement(pattern: &str, lang: LangId, as_is: Option<&Pattern>) -> Option<Pattern> {
    let pat = pattern.trim();
    if pat.ends_with(';') || pat.ends_with('}') {
        return None;
    }
    let pre = if lang == LangId::Php { "<?php " } else { "" };
    let sg = Sg(lang);
    let context = format!("{pre}{pat};");
    let processed = sg.pre_process_pattern(&context);
    let tree = crate::outline::parse_tree(lang, processed.as_bytes())?;
    let (start, end) = (pre.len(), processed.len() - 1);
    let node = tree.root_node().descendant_for_byte_range(start, end)?;
    if node.start_byte() != start || node.end_byte() != end || !node.is_named() || node.has_error()
    {
        return None;
    }
    if as_is.is_some_and(|p| root_kind(p).is_none_or(|k| k == node.kind_id())) {
        return None;
    }
    // The selector takes the first node of the kind: that has to be this one.
    let mut cursor = tree.walk();
    let first = loop {
        let n = cursor.node();
        if n.kind_id() == node.kind_id() {
            break n;
        }
        if cursor.goto_first_child() {
            continue;
        }
        while !cursor.goto_next_sibling() {
            if !cursor.goto_parent() {
                return None;
            }
        }
    };
    if first.id() != node.id() {
        return None;
    }
    Pattern::contextual(&context, node.kind(), sg)
        .ok()
        .filter(|p| !p.has_error())
}

/// The literal words of a pattern (letters, digits, `_`; at least two bytes)
/// that are not metavariables. Every match contains each of them verbatim.
fn words(pattern: &str) -> Vec<&str> {
    let b = pattern.as_bytes();
    let word = |c: u8| c.is_ascii_alphanumeric() || c == b'_';
    let mut out: Vec<&str> = Vec::new();
    let mut i = 0;
    while i < b.len() {
        if !word(b[i]) {
            i += 1;
            continue;
        }
        let start = i;
        while i < b.len() && word(b[i]) {
            i += 1;
        }
        let meta = start > 0 && b[start - 1] == b'$';
        let w = &pattern[start..i];
        // Numbers are written in too many ways (`1_000`, `0x10`) to rely on.
        let numeric = w.as_bytes()[0].is_ascii_digit();
        if !meta && !numeric && w.len() >= 2 && !out.contains(&w) {
            out.push(w);
        }
    }
    out
}

/// The longest literal word of a pattern: files without it need no parse.
pub fn literal(pattern: &str) -> Option<&str> {
    let mut best: Option<&str> = None;
    for w in words(pattern) {
        if best.is_none_or(|x| w.len() > x.len()) {
            best = Some(w);
        }
    }
    best
}

/// Named metavariables in pattern order: `(name, multi)`. `$_X` and the
/// anonymous `$$$` capture nothing.
fn metavars(pattern: &str) -> Vec<(String, bool)> {
    let b = pattern.as_bytes();
    let mut out: Vec<(String, bool)> = Vec::new();
    let mut i = 0;
    while i < b.len() {
        if b[i] != b'$' {
            i += 1;
            continue;
        }
        let start = i;
        while i < b.len() && b[i] == b'$' {
            i += 1;
        }
        let dollars = i - start;
        let name_start = i;
        while i < b.len() && (b[i].is_ascii_uppercase() || b[i].is_ascii_digit() || b[i] == b'_') {
            i += 1;
        }
        let name = &pattern[name_start..i];
        if name.is_empty() || name.starts_with('_') || name.as_bytes()[0].is_ascii_digit() {
            continue;
        }
        if !out.iter().any(|(n, _)| n == name) {
            out.push((name.to_string(), dollars == 3));
        }
    }
    out
}

/// One line, at most [`MAX_CAPTURE`] bytes.
fn clip(text: &str) -> String {
    let mut out = String::new();
    for word in text.split_whitespace() {
        if !out.is_empty() {
            out.push(' ');
        }
        out.push_str(word);
        if out.len() > MAX_CAPTURE {
            break;
        }
    }
    if out.len() > MAX_CAPTURE {
        let mut cut = MAX_CAPTURE;
        while !out.is_char_boundary(cut) {
            cut -= 1;
        }
        out.truncate(cut);
        out.push('…');
    }
    out
}

/// What a structural scan did, beyond its hits.
pub(crate) struct AstScan {
    pub scan: Scan,
    /// Languages the pattern ran for (files of these were parsed).
    pub langs: Vec<LangId>,
    /// Languages it matched in.
    pub matched: Vec<LangId>,
    /// Files that contain the literal (or all files in scope without one).
    pub candidates: usize,
    /// Candidates parsed and matched against the pattern.
    pub parsed: usize,
    /// Candidates too large to parse (not searched).
    pub too_large: usize,
    /// Lines that mention the literal inside macro bodies that have no
    /// syntax tree to match (Rust `macro_rules!`, C `#define`), so a match
    /// there would go unseen.
    pub opaque: usize,
    /// The text prefilter, if the pattern has a literal.
    pub literal: Option<String>,
    /// The language asked for, if any.
    pub wanted: Option<LangId>,
}

impl AstScan {
    fn names(langs: &[LangId]) -> String {
        langs
            .iter()
            .map(|l| l.name())
            .collect::<Vec<_>>()
            .join(", ")
    }

    /// `structural `pattern` (rust, typescript)` for the summary line: the
    /// languages it matched in.
    pub(crate) fn title(&self, pattern: &str) -> String {
        format!(
            "structural `{}` ({})",
            pattern.trim(),
            Self::names(&self.matched)
        )
    }

    /// The answer when nothing matched: what was and was not examined.
    pub(crate) fn nothing(&self, e: &Engine, pattern: &str, scope: &str) -> String {
        let pattern = pattern.trim();
        let examined = match &self.literal {
            Some(lit) => format!(
                "{} containing `{lit}`, {} parsed",
                crate::util::plural(self.candidates, "file"),
                self.parsed
            ),
            None => format!(
                "{} in scope, {} parsed",
                crate::util::plural(self.candidates, "file"),
                self.parsed
            ),
        };
        let mut t = if e.limits.stopped().is_some() {
            format!(
                "Nothing matched structural `{pattern}` in {scope} before the search stopped ({examined})."
            )
        } else {
            format!("No structural matches for `{pattern}` in {scope} ({examined}).")
        };
        if let (Some(w), true) = (self.wanted, self.langs.is_empty()) {
            t.push_str(&format!(" None of them is {}.", w.name()));
        } else if self.langs.is_empty() {
            t.push_str(
                " The pattern did not parse as one syntax node in any language found there; a pattern is code with metavariables (`$NAME` one node, `$$$NAME` zero or more).",
            );
        } else {
            t.push_str(&format!(" Ran for: {}.", Self::names(&self.langs)));
        }
        t.push_str(&self.caveats());
        t.push('\n');
        t
    }

    /// What a complete-looking answer leaves out, as sentences (or nothing).
    pub(crate) fn caveats(&self) -> String {
        let mut t = String::new();
        if self.too_large > 0 {
            t.push_str(&format!(
                " {} larger than 1 MiB not parsed.",
                crate::util::plural(self.too_large, "file")
            ));
        }
        if let (Some(lit), true) = (&self.literal, self.opaque > 0) {
            t.push_str(&format!(
                " `{lit}` also appears on {} inside macro bodies that do not parse as code; those were not matched (a text search shows them).",
                crate::util::plural(self.opaque, "line")
            ));
        }
        t
    }
}

/// Per-language patterns, compiled on first use.
struct Patterns<'a> {
    pattern: &'a str,
    compiled: Mutex<HashMap<LangId, Option<Arc<Vec<Pattern>>>>>,
}

impl Patterns<'_> {
    fn get(&self, lang: LangId) -> Option<Arc<Vec<Pattern>>> {
        let mut map = self.compiled.lock();
        map.entry(lang)
            .or_insert_with(|| compile(self.pattern, lang).ok().map(Arc::new))
            .clone()
    }
}

/// Is the text prefilter disabled (tests compare both ways)?
fn prefilter_off() -> bool {
    std::env::var_os("SPEEDREAD_AST_NO_PREFILTER").is_some_and(|v| v == "1")
}

/// A matcher for the literal as an exact, case-sensitive word.
pub fn literal_matcher(lit: &str) -> Result<RegexMatcher, String> {
    RegexMatcherBuilder::new()
        .line_terminator(Some(b'\n'))
        .fixed_strings(true)
        .build(lit)
        .map_err(|e| e.to_string())
}

/// Files in scope that can contain a match.
fn candidates(
    e: &Engine,
    roots: &[PathBuf],
    globs: &[String],
    lit: Option<&str>,
) -> Result<Vec<PathBuf>, String> {
    if let Some(lit) = lit {
        // Paths only: the lines are matched again on the syntax tree.
        let matcher = literal_matcher(lit)?;
        return Ok(crate::search::files_with(e, roots, globs, &matcher));
    }
    let mut out = Vec::new();
    for root in roots {
        if root.is_file() {
            out.push(root.clone());
            continue;
        }
        let opts = WalkOpts {
            globs: globs.to_vec(),
            ..e.walk_opts()
        };
        let found = Mutex::new(Vec::new());
        let _ = walk::walk(root, &opts, |entry| {
            if !entry.dataless && entry.size <= MAX_GROUP_BYTES as u64 {
                found.lock().push(entry.path.clone());
            }
            true
        });
        out.extend(found.into_inner());
    }
    out.sort_unstable();
    out.dedup();
    Ok(out)
}

type Doc = StrDoc<Sg>;

/// Where a match is shown and what it captured: the byte offset (in the
/// matched text) of the pattern's own literal, i.e. its first occurrence
/// outside every captured node, and the `\t$NAME=value` suffix. For
/// `$V.unwrap()` on `a.unwrap().b().unwrap()` the outer match is the last
/// `unwrap`, not the one inside `$V`.
fn shown(
    text: &str,
    m: &ast_grep_core::NodeMatch<Doc>,
    names: &[(String, bool)],
    word: Option<(&str, bool)>,
) -> (usize, String) {
    let env = m.get_env();
    let mut held: Vec<std::ops::Range<usize>> = Vec::new();
    let mut suffix = String::new();
    for (name, multi) in names {
        let range = if *multi {
            let nodes = env.get_multiple_matches(name);
            match (nodes.first(), nodes.last()) {
                (Some(a), Some(b)) => Some(a.range().start..b.range().end),
                _ => Some(0..0),
            }
        } else {
            env.get_match(name).map(|n| n.range())
        };
        let Some(range) = range else { continue };
        let value = text.get(range.clone()).map(clip).unwrap_or_default();
        suffix.push_str(&format!(
            "\t${}{name}={value}",
            if *multi { "$$" } else { "" }
        ));
        if !range.is_empty() {
            held.push(range);
        }
    }
    let span = m.range();
    let at = word
        .and_then(|(lit, trailing)| {
            let hay = text.as_bytes().get(span.clone())?;
            let mut all: Vec<usize> = memchr::memmem::find_iter(hay, lit.as_bytes())
                .map(|i| span.start + i)
                .collect();
            // With nothing captured to rule occurrences out, the pattern's
            // own literal is the last one when it follows the metavariables.
            if trailing {
                all.reverse();
            }
            all.iter()
                .find(|off| !held.iter().any(|r| r.contains(off)))
                .or(all.first())
                .copied()
        })
        .unwrap_or(span.start);
    (at, suffix)
}

/// Does the literal come after every metavariable of the pattern?
fn trailing(pattern: &str, lit: &str) -> bool {
    match (pattern.rfind(lit), pattern.rfind('$')) {
        (Some(l), Some(m)) => l > m,
        _ => false,
    }
}

/// Rust macro arguments are token trees, so `assert!(x.unwrap())` has no call
/// node to match. Arguments that read as code are parsed on their own and
/// matched; `found` gets `(file offset, captures)`, `opaque` the file offsets
/// of mentions in bodies that do not parse (and in `macro_rules!` rules).
struct Macros<'a> {
    pat: &'a [Pattern],
    names: &'a [(String, bool)],
    word: &'a str,
    trailing: bool,
    found: Vec<(usize, String)>,
    opaque: Vec<usize>,
}

impl Macros<'_> {
    /// `text` parsed as `tree`; `origin` maps its offsets to file offsets.
    fn visit(
        &mut self,
        text: &str,
        tree: &tree_sitter::Tree,
        origin: &dyn Fn(usize) -> usize,
        depth: usize,
    ) {
        let lit = self.word.as_bytes();
        let mut done = 0usize;
        for off in memchr::memmem::find_iter(text.as_bytes(), lit) {
            if off < done {
                continue;
            }
            let mut node = tree
                .root_node()
                .descendant_for_byte_range(off, off + lit.len());
            let mut body: Option<(tree_sitter::Node, bool)> = None;
            while let Some(n) = node {
                match n.kind() {
                    "macro_invocation" => {
                        body = Some((n, true));
                        break;
                    }
                    "macro_definition" => {
                        body = Some((n, false));
                        break;
                    }
                    _ => node = n.parent(),
                }
            }
            let Some((mac, call)) = body else { continue };
            let args = if call {
                let mut c = mac.walk();
                mac.children(&mut c).find(|n| n.kind() == "token_tree")
            } else {
                None
            };
            let inner = match args {
                Some(tt) if tt.end_byte() - tt.start_byte() >= 2 => {
                    tt.start_byte() + 1..tt.end_byte() - 1
                }
                Some(_) => continue,
                None if call => continue,
                None => mac.start_byte()..mac.end_byte(),
            };
            if off < inner.start {
                // The macro's own name.
                continue;
            }
            done = inner.end;
            let Some(code) = text.get(inner.clone()) else {
                continue;
            };
            let parsed = if call && depth < 4 {
                // As call arguments, as statements, then as items.
                [("fn __m(){__m(", ");}"), ("fn __m(){", "}"), ("", "")]
                    .iter()
                    .find_map(|(pre, post)| {
                        let frag = format!("{pre}{code}{post}");
                        let t = crate::outline::parse_tree(LangId::Rust, frag.as_bytes())?;
                        (!t.root_node().has_error()).then_some((frag, t, pre.len()))
                    })
            } else {
                None
            };
            let Some((frag, t, pre)) = parsed else {
                for i in memchr::memmem::find_iter(code.as_bytes(), lit) {
                    self.opaque.push(origin(inner.start + i));
                }
                continue;
            };
            let start = inner.start;
            let here = move |o: usize| origin(o.saturating_sub(pre) + start);
            let within = pre..pre + code.len();
            let doc = AstGrep::doc(StrDoc {
                src: frag.clone(),
                lang: Sg(LangId::Rust),
                tree: t.clone(),
            });
            let top = doc.root();
            for m in self.pat.iter().flat_map(|p| top.find_all(p)) {
                let r = m.range();
                if r.start >= within.start && r.end <= within.end {
                    let (at, caps) = shown(&frag, &m, self.names, Some((self.word, self.trailing)));
                    self.found.push((here(at), caps));
                }
            }
            self.visit(&frag, &t, &here, depth + 1);
        }
    }
}

/// Run `req.pattern` as a structural pattern over `roots`.
pub(crate) fn scan(
    e: &Engine,
    roots: &[PathBuf],
    req: &SearchRequest,
    context: usize,
) -> Result<AstScan, String> {
    let pattern = req.pattern.trim();
    if pattern.is_empty() {
        return Err("Structural search needs a pattern, e.g. `$X.unwrap()`.\n".to_string());
    }
    let want = match req.lang.as_deref().map(str::trim).filter(|l| !l.is_empty()) {
        Some(name) => Some(lang_named(name).ok_or_else(|| {
            format!(
                "Unknown language `{name}` for a structural search. Known: {}.\n",
                CODE_LANGS
                    .iter()
                    .map(|l| l.name())
                    .collect::<Vec<_>>()
                    .join(", ")
            )
        })?),
        None => None,
    };
    if let Some(lang) = want
        && let Err(why) = compile(pattern, lang)
    {
        return Err(format!(
            "Invalid {} pattern `{pattern}`: {why}. A pattern is code with metavariables: `$NAME` matches one node, `$$$NAME` zero or more.\n",
            lang.name()
        ));
    }
    let word = literal(pattern);
    let tail = word.is_some_and(|w| trailing(pattern, w));
    let lit = word.filter(|_| !prefilter_off());
    let all_words = if prefilter_off() {
        Vec::new()
    } else {
        words(pattern)
    };
    if word.is_none() && req.paths.is_empty() && req.globs.is_empty() {
        return Err(format!(
            "Pattern `{pattern}` has no literal word to narrow files with, so every file would be parsed. Give a path or glob.\n"
        ));
    }
    let files = candidates(e, roots, &req.globs, lit)?;
    let patterns = Patterns {
        pattern,
        compiled: Mutex::new(HashMap::new()),
    };
    let names = metavars(pattern);
    // What a long hit line is windowed on (as the renderer does).
    let shows = crate::search::line_matcher(req)?;
    let max_matches = req.max_matches.max(1);
    let total = AtomicUsize::new(0);
    let parsed = AtomicUsize::new(0);
    let too_large = AtomicUsize::new(0);
    let opaque = AtomicUsize::new(0);
    let stopped = AtomicBool::new(false);
    let ran: Mutex<Vec<LangId>> = Mutex::new(Vec::new());
    let matched: Mutex<Vec<LangId>> = Mutex::new(Vec::new());
    let note = |list: &Mutex<Vec<LangId>>, lang: LangId| {
        let mut list = list.lock();
        if !list.contains(&lang) {
            list.push(lang);
        }
    };

    let search_file = |path: &PathBuf| -> Option<FileHits> {
        if total.load(Ordering::Relaxed) >= max_matches {
            stopped.store(true, Ordering::Relaxed);
            return None;
        }
        if e.limits.expired() {
            return None;
        }
        // Not through the cache: a scan reads far more files than it
        // shows, and would fill the cache with them and evict what the
        // session is working on.
        let size = std::fs::metadata(path).ok()?.len();
        if size > MAX_GROUP_BYTES as u64 {
            let lang = crate::lang::detect(path, b"").filter(|l| l.grammar().is_some())?;
            if want.is_none_or(|w| lang_selected(w, lang)) {
                too_large.fetch_add(1, Ordering::Relaxed);
            }
            return None;
        }
        let src = Source::load(path).ok()?;
        let lang = src.lang.filter(|l| l.grammar().is_some())?;
        if want.is_some_and(|w| !lang_selected(w, lang)) {
            return None;
        }
        if src.binary {
            return None;
        }
        let text = std::str::from_utf8(&src.data).ok()?;
        // A match contains every literal word of the pattern.
        if all_words
            .iter()
            .any(|w| memchr::memmem::find(&src.data, w.as_bytes()).is_none())
        {
            return None;
        }
        // The tree only: outlines are made for the files that are shown.
        let tree = std::panic::catch_unwind(|| crate::outline::parse_tree(lang, &src.data))
            .ok()
            .flatten()?;
        // A `.lua` file may have been parsed as Luau.
        let lang = if lang == LangId::Lua
            && LangId::Luau
                .grammar()
                .is_some_and(|g| *tree.language() == g)
        {
            LangId::Luau
        } else {
            lang
        };
        let pat = patterns.get(lang)?;
        parsed.fetch_add(1, Ordering::Relaxed);
        note(&ran, lang);
        let macros = (lang == LangId::Rust).then(|| tree.clone());
        let defines = matches!(lang, LangId::C | LangId::Cpp | LangId::ObjC).then(|| tree.clone());
        let root = AstGrep::doc(StrDoc {
            src: text.to_string(),
            lang: Sg(lang),
            tree,
        });
        // (file offset shown, captures) per match.
        let top = root.root();
        let mut found: Vec<(usize, String)> = pat
            .iter()
            .flat_map(|p| top.find_all(p))
            .map(|m| shown(text, &m, &names, word.map(|w| (w, tail))))
            .collect();
        if let (Some(tree), Some(w)) = (macros, word) {
            let mut inside = Macros {
                pat: &pat,
                names: &names,
                word: w,
                trailing: tail,
                found: Vec::new(),
                opaque: Vec::new(),
            };
            inside.visit(text, &tree, &|o| o, 0);
            found.append(&mut inside.found);
            let row = |o: &usize| src.lines.partition_point(|&s| s as usize <= *o);
            let mut lines: Vec<usize> = inside.opaque.iter().map(row).collect();
            lines.sort_unstable();
            lines.dedup();
            opaque.fetch_add(lines.len(), Ordering::Relaxed);
        }
        if let (Some(tree), Some(w)) = (defines, word) {
            // The body of a `#define` is raw text to the grammar.
            let mut lines: Vec<usize> = memchr::memmem::find_iter(&src.data, w.as_bytes())
                .filter(|&o| {
                    tree.root_node()
                        .descendant_for_byte_range(o, o + w.len())
                        .is_some_and(|n| n.kind() == "preproc_arg")
                })
                .map(|o| src.lines.partition_point(|&s| s as usize <= o))
                .collect();
            lines.dedup();
            opaque.fetch_add(lines.len(), Ordering::Relaxed);
        }
        // A pattern that is a single token matches inside token trees too.
        found.sort();
        found.dedup();
        let matches = found.len();
        if matches == 0 {
            return None;
        }
        // One hit per line, in line order; a line with several matches shows
        // each one's captures.
        let mut at: Vec<(u32, String)> = found
            .into_iter()
            .map(|(o, caps)| {
                let row = src
                    .lines
                    .partition_point(|&s| s as usize <= o)
                    .saturating_sub(1);
                (row as u32, caps)
            })
            .collect();
        at.sort_by_key(|(row, _)| *row);
        // The text of a row is its line and the captures of every match
        // on it.
        let mut shown_rows: Vec<(u32, String)> = Vec::new();
        for (row, caps) in at {
            match shown_rows.last_mut() {
                Some((r, all)) if *r == row => all.push_str(&caps),
                _ => {
                    if shown_rows.len() >= MAX_HITS_PER_FILE {
                        break;
                    }
                    shown_rows.push((row, caps));
                }
            }
        }
        let mut hits: Vec<Hit> = Vec::new();
        let mut rows: Vec<u32> = Vec::new();
        for (row, caps) in shown_rows {
            let mut line = src.line(row as usize).to_vec();
            line.extend_from_slice(caps.as_bytes());
            rows.push(row);
            hits.push(Hit::new(row + 1, &line, true, &shows));
        }
        if context > 0 {
            let last = src.line_count().saturating_sub(1) as u32;
            for row in rows {
                let lo = row.saturating_sub(context as u32);
                let hi = (row + context as u32).min(last);
                for r in lo..=hi {
                    if r != row && hits.len() < MAX_HITS_PER_FILE {
                        hits.push(Hit::new(r + 1, src.line(r as usize), false, &shows));
                    }
                }
            }
        }
        total.fetch_add(matches, Ordering::Relaxed);
        note(&matched, lang);
        Some(FileHits {
            path: path.clone(),
            hits,
            matches,
        })
    };
    // Sorted candidates in parallel chunks, so a match cap stops early and
    // the files reported are the first ones in path order.
    let chunk = (4 * crate::macos::worker_threads()).max(8);
    let mut out: Vec<FileHits> = Vec::new();
    for part in files.chunks(chunk) {
        if stopped.load(Ordering::Relaxed) || e.limits.stopped().is_some() {
            break;
        }
        if total.load(Ordering::Relaxed) >= max_matches {
            stopped.store(true, Ordering::Relaxed);
            break;
        }
        out.extend(
            part.par_iter()
                .filter_map(search_file)
                .collect::<Vec<FileHits>>(),
        );
    }
    let order = |mut v: Vec<LangId>| {
        v.sort_by_key(|l| CODE_LANGS.iter().position(|x| x == l));
        v
    };
    let (langs, matched) = (order(ran.into_inner()), order(matched.into_inner()));
    Ok(AstScan {
        scan: Scan {
            files: out,
            searched: files.len(),
            stopped: stopped.load(Ordering::Relaxed),
        },
        langs,
        matched,
        candidates: files.len(),
        parsed: parsed.load(Ordering::Relaxed),
        too_large: too_large.load(Ordering::Relaxed),
        opaque: opaque.load(Ordering::Relaxed),
        literal: lit.map(str::to_string),
        wanted: want,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn literal_is_the_longest_plain_word() {
        assert_eq!(literal("$V.unwrap()"), Some("unwrap"));
        assert_eq!(literal("console.log($A)"), Some("console"));
        assert_eq!(literal("router.$METHOD($PATH, $$$H)"), Some("router"));
        assert_eq!(literal("$A == $A"), None);
        assert_eq!(literal("$A + 1000"), None);
    }

    #[test]
    fn metavariables_in_pattern_order() {
        assert_eq!(
            metavars("router.$METHOD($PATH, $$$HANDLERS, $_SKIP, $$$)"),
            vec![
                ("METHOD".to_string(), false),
                ("PATH".to_string(), false),
                ("HANDLERS".to_string(), true),
            ]
        );
        assert_eq!(metavars("$A == $A"), vec![("A".to_string(), false)]);
    }

    #[test]
    fn dollar_is_rewritten_only_for_metavariables() {
        let rust = Sg(LangId::Rust);
        assert_eq!(rust.pre_process_pattern("$A.unwrap()"), "µA.unwrap()");
        assert_eq!(rust.pre_process_pattern("f($$$ARGS)"), "f(µµµARGS)");
        assert_eq!(rust.pre_process_pattern("f($$$)"), "f(µµµ)");
        assert_eq!(rust.pre_process_pattern("a $ b"), "a $ b");
        let ts = Sg(LangId::TypeScript);
        assert_eq!(ts.pre_process_pattern("f($A)"), "f($A)");
    }

    #[test]
    fn a_call_pattern_compiles_for_every_grammar() {
        // The expando character must be an identifier character in each
        // grammar, or the pattern would parse as an error everywhere.
        let mut failed = Vec::new();
        for &lang in CODE_LANGS {
            let pattern = match lang {
                LangId::Bash => "echo $A",
                LangId::ObjC => "[$A release]",
                _ => "foo($A)",
            };
            if let Err(why) = compile(pattern, lang) {
                failed.push(format!("{}: {why}", lang.name()));
            }
        }
        assert!(failed.is_empty(), "{failed:?}");
    }

    /// Distinct places `pattern` matches in `src`.
    fn count(src: &str, lang: LangId, pattern: &str) -> usize {
        let alts = compile(pattern, lang).unwrap_or_else(|e| panic!("{}: {e}", lang.name()));
        let root = AstGrep::new(src, Sg(lang));
        let top = root.root();
        let mut at: Vec<usize> = alts
            .iter()
            .flat_map(|p| top.find_all(p))
            .map(|m| m.range().start)
            .collect();
        at.sort_unstable();
        at.dedup();
        at.len()
    }

    #[test]
    fn a_call_pattern_matches_calls_in_every_grammar() {
        // A call as a statement, in an initializer, in a condition and in a
        // return. Read on its own, `foo($A)` is a macro type in C and
        // Objective-C and plain text in PHP: compiling is not matching.
        let c_like =
            "int f(void) { foo(1); int x = foo(2); if (foo(3)) { return foo(4); } return x; }";
        let js = "function f() { foo(1); const x = foo(2); if (foo(3)) { return foo(4); } }";
        let java = "class C { int f() { foo(1); int x = foo(2); if (foo(3)) { return foo(4); } return x; } }";
        let lua = "function f() foo(1); local x = foo(2); if foo(3) then return foo(4) end end";
        let mut wrong = Vec::new();
        for &lang in CODE_LANGS {
            use LangId::*;
            let (src, pattern) = match lang {
                Rust => (
                    "fn f() { foo(1); let x = foo(2); if foo(3) { return foo(4); } }",
                    "foo($A)",
                ),
                Python => (
                    "def f():\n    foo(1)\n    x = foo(2)\n    if foo(3):\n        return foo(4)\n",
                    "foo($A)",
                ),
                JavaScript | TypeScript | Tsx => (js, "foo($A)"),
                Go => (
                    "package p\nfunc f() int { foo(1); x := foo(2); if foo(3) { return foo(4) }; return x }",
                    "foo($A)",
                ),
                Java | CSharp => (java, "foo($A)"),
                C | Cpp | ObjC => (c_like, "foo($A)"),
                Ruby => (
                    "def f\n  foo(1)\n  x = foo(2)\n  if foo(3)\n    return foo(4)\n  end\nend\n",
                    "foo($A)",
                ),
                Php => (
                    "<?php function f() { foo(1); $x = foo(2); if (foo(3)) { return foo(4); } }",
                    "foo($A)",
                ),
                Bash => (
                    "f() { foo 1; x=$(foo 2); if foo 3; then foo 4; fi; }",
                    "foo $A",
                ),
                Swift => (
                    "func f() -> Int { foo(1); let x = foo(2); if foo(3) { return foo(4) }; return x }",
                    "foo($A)",
                ),
                Kotlin => (
                    "fun f(): Int { foo(1); val x = foo(2); if (foo(3)) { return foo(4) }; return x }",
                    "foo($A)",
                ),
                Scala => (
                    "object O { def f(): Int = { foo(1); val x = foo(2); if (foo(3)) { return foo(4) }; x } }",
                    "foo($A)",
                ),
                Lua | Luau => (lua, "foo($A)"),
                other => panic!("no sample for {}", other.name()),
            };
            let n = count(src, lang, pattern);
            if n != 4 {
                wrong.push(format!("{}: {n} of 4", lang.name()));
            }
        }
        assert!(wrong.is_empty(), "{wrong:?}");
    }

    #[test]
    fn c_patterns_match_expressions_and_statements() {
        let src = "struct n { struct n *next; int v; };\nint g(struct n *p, int k) {\n    struct n *q = bar(k, 2);\n    p->next = q;\n    if (p->next == NULL) { return -1; }\n    q->v = bar(sizeof(*q), k) + 1;\n    kfree(q), kfree(p);\n    return p->v == NULL ? 0 : k;\n}\n";
        let mut wrong = Vec::new();
        for lang in [LangId::C, LangId::Cpp, LangId::ObjC] {
            for (pattern, want) in [
                ("kfree($A)", 2),
                ("bar($A, $B)", 2),
                ("$P->next", 2),
                ("$A == NULL", 2),
                ("$P->$F = $V", 2),
                ("sizeof($E)", 1),
                ("$A ? $B : $C", 1),
                ("return $A;", 2),
                ("if ($C) { $$$B }", 1),
                ("struct n *$N = $V;", 1),
            ] {
                let n = count(src, lang, pattern);
                if n != want {
                    wrong.push(format!("{} `{pattern}`: {n} of {want}", lang.name()));
                }
            }
        }
        assert!(wrong.is_empty(), "{wrong:?}");
    }

    #[test]
    fn languages_by_name_or_extension() {
        assert_eq!(lang_named("rust"), Some(LangId::Rust));
        assert_eq!(lang_named("ts"), Some(LangId::TypeScript));
        assert_eq!(lang_named("TSX"), Some(LangId::Tsx));
        assert_eq!(lang_named("py"), Some(LangId::Python));
        assert_eq!(lang_named("c++"), Some(LangId::Cpp));
        assert_eq!(lang_named("json"), None);
        assert_eq!(lang_named("klingon"), None);
    }
}
