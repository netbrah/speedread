//! The `context` tool: one natural-language question, one budgeted bundle.
//!
//! Routing and budget assignment are pure. Execution only calls `map`,
//! `search`, `trace`, and `read`. Those four stay the only ways to touch the tree.

use std::borrow::Cow;

use schemars::{JsonSchema, Schema, SchemaGenerator, json_schema};
use serde::Deserialize;

use crate::engine::Engine;
use crate::map::MapRequest;
use crate::read::{Mode, ReadRequest};
use crate::search::{Case, Output, SearchRequest};
use crate::trace::{Direction, TraceRequest};

/// Model-facing description. Not one of the four primitive descriptions.
pub const DESCRIPTION: &str = "Use instead of grep or cat when you have a natural-language question and do not want to choose among map, search, trace, and read. Returns one context bundle for that question (a map, search hits, symbol source, or relationships) trimmed to the token budget (default 8000 tokens).";

const MAX_QUESTION: usize = 4_000;
const CHAIN_READS: usize = 3;

const EXTS: &[&str] = &[
    "py", "go", "rs", "h", "cc", "hh", "cpp", "hpp", "ts", "tsx", "js", "swift", "toml", "json",
    "md", "mod",
];

const STOP: &[&str] = &[
    "the",
    "this",
    "that",
    "what",
    "which",
    "where",
    "when",
    "who",
    "how",
    "does",
    "did",
    "with",
    "from",
    "into",
    "onto",
    "about",
    "give",
    "list",
    "name",
    "file",
    "files",
    "line",
    "lines",
    "method",
    "methods",
    "function",
    "functions",
    "class",
    "struct",
    "interface",
    "defined",
    "define",
    "defines",
    "definition",
    "start",
    "starting",
    "number",
    "numbers",
    "repository",
    "repo",
    "code",
    "non",
    "test",
    "other",
    "than",
    "plain",
    "call",
    "calls",
    "called",
    "calling",
    "directly",
    "before",
    "after",
    "running",
    "under",
    "holds",
    "package",
    "directory",
    "structural",
    "overview",
    "integer",
    "default",
    "token",
    "budget",
    "constant",
    "tool",
    "return",
    "returns",
    "require",
    "required",
    "requires",
    "signature",
    "parameter",
    "parameters",
    "body",
    "not",
    "and",
    "for",
    "are",
    "its",
    "put",
    "keys",
    "dict",
    "built",
    "shell",
    "context",
    "processors",
    "created",
    "answer",
    "one",
    "each",
    "site",
    "sites",
    "type",
    "types",
    "version",
    "declare",
    "module",
    "python",
    "forward",
    "declaration",
    "include",
    "holding",
];

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Intent {
    Orient,
    Relate,
    Read,
    Search,
}

impl Intent {
    fn name(self) -> &'static str {
        match self {
            Intent::Orient => "orient",
            Intent::Relate => "relate",
            Intent::Read => "read",
            Intent::Search => "search",
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Route {
    pub intent: Intent,
    pub direction: Direction,
    pub depth: usize,
    /// Read targets, or the single trace target.
    pub targets: Vec<String>,
    pub pattern: String,
    pub literal: bool,
    pub word: bool,
    pub output: Output,
    pub scope: Vec<String>,
    /// Search with `matches`, then read up to three enclosing symbols.
    pub chain: bool,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Step {
    Map {
        path: Option<String>,
        budget: usize,
    },
    Search {
        pattern: String,
        paths: Vec<String>,
        literal: bool,
        word: bool,
        output: Output,
        budget: usize,
    },
    Read {
        targets: Vec<String>,
        budget: usize,
    },
    Trace {
        target: String,
        direction: Direction,
        depth: usize,
        budget: usize,
    },
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Plan {
    pub intent: Intent,
    pub steps: Vec<Step>,
    pub chain: bool,
}

#[derive(Debug, Deserialize)]
pub struct ContextArgs {
    pub question: String,
    #[serde(default)]
    pub path: Option<String>,
    #[serde(default, deserialize_with = "lenient_usize")]
    pub budget: Option<usize>,
    /// Absolute directory that relative paths and the default scope resolve
    /// against for this call. Same field the other tools take.
    #[serde(default, alias = "cwd")]
    pub base_dir: Option<String>,
}

fn lenient_usize<'de, D: serde::Deserializer<'de>>(d: D) -> Result<Option<usize>, D::Error> {
    #[derive(Deserialize)]
    #[serde(untagged)]
    enum V {
        N(f64),
        S(String),
    }
    Ok(match Option::<V>::deserialize(d)? {
        Some(V::N(n)) if n >= 0.0 => Some(n as usize),
        Some(V::S(s)) => s.trim().parse().ok(),
        _ => None,
    })
}

impl JsonSchema for ContextArgs {
    fn schema_name() -> Cow<'static, str> {
        "ContextArgs".into()
    }
    fn json_schema(_: &mut SchemaGenerator) -> Schema {
        json_schema!({
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "Natural-language question about the repository."},
                "path": {"type": "string", "description": "File or directory to scope the question."},
                "budget": {"type": "integer", "description": "Max response tokens for the whole bundle (default 8000)."},
                "base_dir": {"type": "string", "description": "Absolute directory that relative paths and the default scope resolve against (default: workspace root)."}
            },
            "required": ["question"]
        })
    }
}

pub struct ContextRequest {
    pub question: String,
    pub path: Option<String>,
    pub budget: Option<usize>,
}

pub fn context(engine: &Engine, req: &ContextRequest) -> String {
    let question = req.question.trim();
    if question.is_empty() {
        return "context: empty question\n".into();
    }
    if question.len() > MAX_QUESTION {
        return "context: question too long\n".into();
    }
    let routed = route(question, req.path.as_deref());
    let Some(routed) = routed else {
        return "context: no symbol or path in the question\n".into();
    };
    let budget = engine.budget(req.budget);
    let plan = plan(&routed, budget);
    let mut out = String::new();
    out.push_str(&format!(
        "context intent={} budget={budget}\n",
        plan.intent.name()
    ));
    out.push_str("plan:");
    for (i, step) in plan.steps.iter().enumerate() {
        if i > 0 {
            out.push(';');
        }
        out.push(' ');
        out.push_str(&step.summary());
    }
    out.push('\n');

    let mut search_text = String::new();
    let mut skipped = false;
    for step in &plan.steps {
        let step = if plan.chain && matches!(step, Step::Read { targets, .. } if targets.is_empty())
        {
            let targets = read_targets_from_search(&search_text);
            if targets.is_empty() {
                skipped = true;
                continue;
            }
            let Step::Read { budget, .. } = step else {
                continue;
            };
            Step::Read {
                targets,
                budget: *budget,
            }
        } else {
            step.clone()
        };
        out.push_str(&step.banner());
        let text = run_step(engine, &step);
        if matches!(step, Step::Search { .. }) {
            search_text = text.clone();
        }
        out.push_str(&text);
        if !out.ends_with('\n') {
            out.push('\n');
        }
    }
    if skipped {
        out.push_str("plan note: read skipped: no symbol headers\n");
    }
    engine.enforce_budget(&mut out, budget);
    out
}

fn run_step(engine: &Engine, step: &Step) -> String {
    let engine = match engine.scoped(None) {
        Ok(engine) => engine,
        Err(msg) => return format!("context: {msg}\n"),
    };
    match step {
        Step::Map { path, budget } => {
            let req = MapRequest {
                path: path.clone(),
                depth: None,
                symbols: true,
                globs: Vec::new(),
                budget: Some(*budget),
            };
            crate::map::map(&engine, &req)
        }
        Step::Search {
            pattern,
            paths,
            literal,
            word,
            output,
            budget,
        } => {
            let req = SearchRequest {
                pattern: pattern.clone(),
                paths: paths.clone(),
                globs: Vec::new(),
                literal: *literal,
                word: *word,
                case: Case::Smart,
                context: 0,
                output: *output,
                budget: Some(*budget),
                max_matches: 5000,
                ast: false,
                lang: None,
            };
            crate::search::search(&engine, &req)
        }
        Step::Read { targets, budget } => {
            let req = ReadRequest {
                targets: targets.clone(),
                mode: Mode::Auto,
                budget: Some(*budget),
                numbers: true,
            };
            crate::read::read(&engine, &req)
        }
        Step::Trace {
            target,
            direction,
            depth,
            budget,
        } => {
            let req = TraceRequest {
                target: target.clone(),
                direction: *direction,
                depth: *depth,
                budget: Some(*budget),
            };
            crate::trace::trace(&engine, &req)
        }
    }
}

impl Step {
    fn budget(&self) -> usize {
        match self {
            Step::Map { budget, .. }
            | Step::Search { budget, .. }
            | Step::Read { budget, .. }
            | Step::Trace { budget, .. } => *budget,
        }
    }

    fn summary(&self) -> String {
        match self {
            Step::Map { .. } => format!("map {}", self.budget()),
            Step::Search { .. } => format!("search {}", self.budget()),
            Step::Read { targets, .. } if targets.is_empty() => {
                format!("read {}", self.budget())
            }
            Step::Read { .. } => format!("read {}", self.budget()),
            Step::Trace {
                direction, depth, ..
            } => format!("trace {} {direction:?} depth={depth}", self.budget()),
        }
    }

    fn banner(&self) -> String {
        match self {
            Step::Map { path, .. } => format!("--- map {} ---\n", path.as_deref().unwrap_or(".")),
            Step::Search { pattern, .. } => format!("--- search {pattern} ---\n"),
            Step::Read { targets, .. } => format!("--- read {} ---\n", targets.join(" ")),
            Step::Trace {
                target, direction, ..
            } => format!("--- trace {target} {direction:?} ---\n"),
        }
    }
}

pub fn route(question: &str, path: Option<&str>) -> Option<Route> {
    let scope_override = path.map(str::trim).filter(|p| !p.is_empty());
    let files = match &scope_override {
        Some(scope) => vec![(*scope).to_string()],
        None => paths_in(question),
    };
    let names = symbols(question);
    let explicit = if scope_override.is_some() {
        Vec::new()
    } else {
        explicit_targets(question)
    };

    if is_orient(question) && files.is_empty() && explicit.is_empty() {
        return Some(base_route(Intent::Orient, files));
    }
    if is_relate(question) {
        let target = trace_target(question)
            .or_else(|| names.first().map(|name| format!("#{name}")))
            .or_else(|| explicit.into_iter().next());
        let target = target?;
        let mut route = base_route(Intent::Relate, files);
        route.direction = direction_of(question);
        route.depth = depth_of(question);
        route.targets = vec![target];
        return Some(route);
    }
    if !explicit.is_empty() {
        let mut route = base_route(Intent::Read, Vec::new());
        route.targets = explicit;
        return Some(route);
    }
    let file = files.len() == 1 && is_path_token(&files[0]);
    if file && names.is_empty() {
        let mut route = base_route(Intent::Read, Vec::new());
        route.targets = files;
        return Some(route);
    }
    if file && !names.is_empty() && wants_body(question) {
        let mut route = base_route(Intent::Read, Vec::new());
        route.targets = names
            .iter()
            .map(|name| format!("{}#{name}", files[0]))
            .collect();
        return Some(route);
    }
    if names.is_empty() {
        // A directory scope and no symbol is a map, not a search for nothing.
        if files.is_empty() {
            return None;
        }
        return Some(base_route(Intent::Orient, files));
    }
    let mut route = base_route(Intent::Search, files);
    fill_search(&mut route, question, &names);
    if route.pattern.is_empty() {
        return None;
    }
    // A body question that also asks where, and does not already name the
    // file, searches then reads. The budgets are fixed before either step.
    route.chain = wants_body(question) && wants_location(question) && !file;
    Some(route)
}

fn base_route(intent: Intent, scope: Vec<String>) -> Route {
    Route {
        intent,
        direction: Direction::Callers,
        depth: 1,
        targets: Vec::new(),
        pattern: String::new(),
        literal: false,
        word: false,
        output: Output::Matches,
        scope,
        chain: false,
    }
}

fn fill_search(route: &mut Route, question: &str, names: &[String]) {
    if names.is_empty() {
        // Searching a named file for nothing useful: read it instead is handled
        // earlier. A directory scope with no symbol searches nothing; leave
        // the pattern empty so plan can refuse.
        return;
    }
    if names.len() == 1
        && let Some(pattern) = definition_pattern(question, &names[0])
    {
        route.pattern = pattern;
        route.literal = false;
        route.word = false;
    } else if names.len() == 1 {
        route.pattern = names[0].clone();
        route.literal = true;
        route.word = true;
    } else {
        route.pattern = names
            .iter()
            .map(|name| format!(r"\b{}\b", escape_regex(name)))
            .collect::<Vec<_>>()
            .join("|");
        route.literal = false;
        route.word = false;
    }
    route.output = if wants_body(question) && !wants_location(question) {
        Output::Symbols
    } else {
        Output::Matches
    };
}

pub fn plan(route: &Route, budget: usize) -> Plan {
    let (search_budget, read_budget) = if route.chain {
        let search_budget = (budget / 5).clamp(200, budget.saturating_sub(200).max(200));
        let search_budget = search_budget.min(budget);
        let read_budget = budget.saturating_sub(search_budget);
        (search_budget, read_budget)
    } else {
        (budget, budget)
    };
    let steps = match route.intent {
        Intent::Orient => vec![Step::Map {
            path: route.scope.first().cloned(),
            budget,
        }],
        Intent::Relate => vec![Step::Trace {
            target: route.targets.first().cloned().unwrap_or_else(|| "#".into()),
            direction: route.direction,
            depth: route.depth,
            budget,
        }],
        Intent::Read => vec![Step::Read {
            targets: route.targets.clone(),
            budget,
        }],
        Intent::Search if route.chain && read_budget > 0 => vec![
            Step::Search {
                pattern: route.pattern.clone(),
                paths: route.scope.clone(),
                literal: route.literal,
                word: route.word,
                output: Output::Matches,
                budget: search_budget,
            },
            Step::Read {
                targets: Vec::new(),
                budget: read_budget,
            },
        ],
        Intent::Search => vec![Step::Search {
            pattern: route.pattern.clone(),
            paths: route.scope.clone(),
            literal: route.literal,
            word: route.word,
            output: route.output,
            budget,
        }],
    };
    Plan {
        intent: route.intent,
        steps,
        chain: route.chain && read_budget > 0 && matches!(route.intent, Intent::Search),
    }
}

fn read_targets_from_search(text: &str) -> Vec<String> {
    let mut path: Option<String> = None;
    let mut out = Vec::new();
    for line in text.lines() {
        if let Some(rest) = line.strip_prefix("==> ") {
            let path_tok = rest
                .split(" @")
                .next()
                .unwrap_or(rest)
                .split(" (")
                .next()
                .unwrap_or(rest)
                .trim();
            path = Some(path_tok.to_string());
            continue;
        }
        let Some(path) = path.as_deref() else {
            continue;
        };
        let Some(start) = line.strip_prefix('[') else {
            continue;
        };
        let Some(number) = start.split('-').next() else {
            continue;
        };
        if number.bytes().all(|b| b.is_ascii_digit()) && !number.is_empty() {
            let target = format!("{path}:{number}");
            if !out.contains(&target) {
                out.push(target);
            }
            if out.len() == CHAIN_READS {
                break;
            }
        }
    }
    out
}

fn is_orient(question: &str) -> bool {
    let q = question.to_ascii_lowercase();
    q.contains("overview") || q.contains("layout") || has_word(question, "orient")
}

fn is_relate(question: &str) -> bool {
    has_word(question, "callers")
        || has_word(question, "caller")
        || has_word(question, "callees")
        || has_word(question, "callee")
        || has_phrase(question, "who calls")
        || has_phrase(question, "call site")
        || has_word(question, "implementations")
        || has_word(question, "implements")
        || has_phrase(question, "who uses")
        || has_phrase(question, "references of")
        || (has_word(question, "call") && !has_question_word(question))
}

fn has_question_word(question: &str) -> bool {
    has_word(question, "what")
        || has_word(question, "how")
        || has_word(question, "when")
        || has_word(question, "why")
}

fn direction_of(question: &str) -> Direction {
    if has_word(question, "callees")
        || has_word(question, "callee")
        || (has_word(question, "what") && has_word(question, "calls"))
    {
        Direction::Callees
    } else if has_word(question, "implementations")
        || has_word(question, "implements")
        || has_word(question, "implement")
    {
        Direction::Impls
    } else if has_phrase(question, "references of") || has_phrase(question, "who uses") {
        Direction::Refs
    } else {
        Direction::Callers
    }
}

fn depth_of(question: &str) -> usize {
    let q = question.to_ascii_lowercase();
    if q.contains("two hop")
        || q.contains("two-hop")
        || q.contains("exactly one intermediate")
        || q.contains("depth 2")
    {
        2
    } else {
        1
    }
}

fn wants_body(question: &str) -> bool {
    let q = question.to_ascii_lowercase();
    q.contains("what does")
        || q.contains("what do")
        || q.contains("what keys")
        || q.contains("what methods")
        || q.contains("put in")
        || has_word(question, "how")
        || has_word(question, "signature")
}

fn wants_location(question: &str) -> bool {
    let q = question.to_ascii_lowercase();
    q.contains("on which line")
        || q.contains("which line")
        || q.contains("starting line")
        || q.contains("line number")
        || q.contains("which file")
        || q.contains("where is")
        || q.contains("where are")
        || has_word(question, "declared")
        || has_word(question, "defined")
}

fn definition_pattern(question: &str, name: &str) -> Option<String> {
    let name = escape_regex(name);
    if has_word(question, "class") {
        Some(format!(r"class\s+(?:\w+\s+)*{name}\b"))
    } else if has_word(question, "struct") {
        Some(format!(r"struct\s+(?:\w+\s+)*{name}\b"))
    } else if has_word(question, "interface") {
        Some(format!(r"(?:interface|type)\s+{name}\b"))
    } else if has_word(question, "function") {
        Some(format!(
            r"(?:function|func|fn|def)\s+(?:\([^)]*\)\s*)?{name}\b"
        ))
    } else if has_word(question, "method") {
        Some(format!(r"(?:func|fn|def)\s+(?:\([^)]*\)\s*)?{name}\b"))
    } else {
        None
    }
}

fn trace_target(question: &str) -> Option<String> {
    for token in raw_tokens(question) {
        if let Some((left, right)) = split_dotted(token) {
            if is_ext(right) || is_path_token(token) {
                continue;
            }
            return Some(format!("{left}.{right}"));
        }
    }
    None
}

fn explicit_targets(question: &str) -> Vec<String> {
    let mut out = Vec::new();
    for token in raw_tokens(question) {
        if let Some((path, name)) = token.split_once('#') {
            if is_ident(name) {
                if path.is_empty() {
                    push_unique(&mut out, format!("#{name}"));
                } else {
                    push_unique(&mut out, format!("{path}#{name}"));
                }
            }
            continue;
        }
        if let Some((path, rest)) = token.split_once(':')
            && is_path_token(path)
            && rest.starts_with(|c: char| c.is_ascii_digit())
        {
            push_unique(&mut out, token.to_string());
        }
    }
    out
}

fn symbols(question: &str) -> Vec<String> {
    let mut dotted = Vec::new();
    for token in raw_tokens(question) {
        let Some((_, right)) = split_dotted(token) else {
            continue;
        };
        if is_ext(right) || is_path_token(token) || !is_ident(right) {
            continue;
        }
        push_unique(&mut dotted, right.to_string());
    }
    if !dotted.is_empty() {
        return dotted;
    }
    let paths = paths_in(question);
    let extensions: Vec<&str> = paths
        .iter()
        .filter_map(|path| path.rsplit_once('.').map(|(_, ext)| ext))
        .collect();
    let mut specific = Vec::new();
    let mut plain = Vec::new();
    for ident in idents(question) {
        if extensions.iter().any(|ext| ext == &ident) || is_stop(&ident) {
            continue;
        }
        if ident.contains('_') || ident.chars().skip(1).any(|c| c.is_ascii_uppercase()) {
            push_unique(&mut specific, ident);
        } else {
            push_unique(&mut plain, ident);
        }
    }
    if !specific.is_empty() {
        return specific;
    }
    plain.sort_by_key(|ident| std::cmp::Reverse(ident.len()));
    plain.into_iter().take(1).collect()
}

fn paths_in(question: &str) -> Vec<String> {
    let mut out = Vec::new();
    for token in raw_tokens(question) {
        if is_path_token(token) {
            push_unique(&mut out, token.to_string());
        }
    }
    out
}

fn raw_tokens(question: &str) -> Vec<&str> {
    question
        .split_whitespace()
        .map(|token| {
            token.trim_matches(|c: char| {
                !c.is_ascii_alphanumeric() && !matches!(c, '_' | '/' | '.' | ':' | '#' | '-')
            })
        })
        .filter(|token| !token.is_empty())
        .collect()
}

fn idents(question: &str) -> Vec<String> {
    let mut out = Vec::new();
    let mut current = String::new();
    for c in question.chars() {
        if c.is_ascii_alphanumeric() || c == '_' {
            current.push(c);
        } else if !current.is_empty() {
            if is_ident(&current) {
                out.push(current.clone());
            }
            current.clear();
        }
    }
    if is_ident(&current) {
        out.push(current);
    }
    out
}

fn is_ident(token: &str) -> bool {
    let mut chars = token.chars();
    match chars.next() {
        Some(c) if c.is_ascii_alphabetic() || c == '_' => {}
        _ => return false,
    }
    token.len() >= 3 && chars.all(|c| c.is_ascii_alphanumeric() || c == '_')
}

fn split_dotted(token: &str) -> Option<(&str, &str)> {
    if let Some((left, right)) = token.split_once("::") {
        return Some((left, right));
    }
    let (left, right) = token.split_once('.')?;
    if left.is_empty() || right.is_empty() || right.contains('.') {
        return None;
    }
    Some((left, right))
}

fn is_path_token(token: &str) -> bool {
    if token.eq_ignore_ascii_case("go.mod") || token == "Cargo.toml" || token == "package.json" {
        return true;
    }
    if token.contains('/') {
        return token.rsplit('/').next().is_some_and(is_path_token);
    }
    matches!(
        token.rsplit_once('.'),
        Some((stem, ext)) if !stem.is_empty() && is_ext(ext)
    )
}

fn is_ext(ext: &str) -> bool {
    EXTS.iter().any(|known| known.eq_ignore_ascii_case(ext))
}

fn is_stop(word: &str) -> bool {
    STOP.iter().any(|stop| stop.eq_ignore_ascii_case(word))
}

fn has_word(question: &str, word: &str) -> bool {
    question
        .split(|c: char| !c.is_ascii_alphanumeric() && c != '_')
        .any(|token| token.eq_ignore_ascii_case(word))
}

fn has_phrase(question: &str, phrase: &str) -> bool {
    question.to_ascii_lowercase().contains(phrase)
}

fn escape_regex(name: &str) -> String {
    let mut out = String::new();
    for c in name.chars() {
        if !c.is_ascii_alphanumeric() && c != '_' {
            out.push('\\');
        }
        out.push(c);
    }
    out
}

fn push_unique(out: &mut Vec<String>, item: String) {
    if !out.iter().any(|existing| existing == &item) {
        out.push(item);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn routed(question: &str) -> Route {
        route(question, None).unwrap_or_else(|| panic!("no route for {question}"))
    }

    #[test]
    fn empty_and_extension_are_not_symbols() {
        assert!(route("   ", None).is_none());
        assert!(route("what about it", None).is_none());
        let go = routed("Which Go version does go.mod declare?");
        assert_eq!(go.intent, Intent::Read);
        assert_eq!(go.targets, vec!["go.mod".to_string()]);
    }

    #[test]
    fn body_question_reads_the_named_symbol() {
        let route = routed(
            "In src/flask/app.py, what keys does Flask.make_shell_context put in the dict it builds?",
        );
        assert_eq!(route.intent, Intent::Read);
        assert_eq!(
            route.targets,
            vec!["src/flask/app.py#make_shell_context".to_string()]
        );
    }

    #[test]
    fn line_question_searches_instead_of_reading_bodies() {
        let route = routed(
            "In src/flask/app.py, give the starting line numbers of the Flask methods dispatch_request, full_dispatch_request and finalize_request.",
        );
        assert_eq!(route.intent, Intent::Search);
        assert_eq!(route.output, Output::Matches);
        assert!(!route.chain);
        assert!(route.pattern.contains("dispatch_request"));
        assert!(route.pattern.contains("finalize_request"));
    }

    #[test]
    fn class_question_uses_a_definition_pattern() {
        let route = routed(
            "Which file defines the class FilePath, and on which line does that class definition start?",
        );
        assert_eq!(route.intent, Intent::Search);
        assert_eq!(route.pattern, r"class\s+(?:\w+\s+)*FilePath\b");
        assert!(!route.literal);
        assert_eq!(route.output, Output::Matches);
    }

    #[test]
    fn callers_trace_the_dotted_name() {
        let route = routed("Who calls Context.AbortWithStatus directly?");
        assert_eq!(route.intent, Intent::Relate);
        assert_eq!(route.targets, vec!["Context.AbortWithStatus".to_string()]);
        assert_eq!(route.direction, Direction::Callers);
        assert_eq!(route.depth, 1);
    }

    #[test]
    fn filename_is_not_a_trace_target() {
        let route = routed(
            "In this repository, which non-test .go files other than context.go call AbortWithStatus?",
        );
        assert_eq!(route.intent, Intent::Relate);
        assert_eq!(route.targets, vec!["#AbortWithStatus".to_string()]);
    }

    #[test]
    fn orient_maps_and_budget_sums() {
        let route = routed("Give a structural overview of this repository.");
        assert_eq!(route.intent, Intent::Orient);
        let plan = plan(&route, 8000);
        assert_eq!(
            plan.steps,
            vec![Step::Map {
                path: None,
                budget: 8000
            }]
        );
    }

    #[test]
    fn chain_splits_the_budget_before_either_step() {
        let route =
            routed("Where is the Render interface defined, and what methods does it require?");
        assert!(route.chain, "{route:?}");
        let plan = plan(&route, 8000);
        assert!(plan.chain);
        match &plan.steps[..] {
            [
                Step::Search {
                    budget: search,
                    output,
                    ..
                },
                Step::Read {
                    budget: read,
                    targets,
                },
            ] => {
                assert_eq!(*output, Output::Matches);
                assert_eq!(*search, 1600);
                assert_eq!(*read, 6400);
                assert!(targets.is_empty());
            }
            other => panic!("unexpected plan: {other:?}"),
        }
    }

    #[test]
    fn two_routes_of_the_same_question_match() {
        let a = routed("Who calls Context.AbortWithStatus directly?");
        let b = routed("Who calls Context.AbortWithStatus directly?");
        assert_eq!(a, b);
    }

    #[test]
    fn a_question_reads_the_function_and_repeats() {
        let dir = std::env::temp_dir().join(format!("speedread-context-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        std::fs::write(
            dir.join("lib.rs"),
            "pub fn parse_header() -> i32 {\n    42\n}\n",
        )
        .unwrap();
        let ws = crate::workspace::Workspace::new(vec![dir.clone()], true, false).unwrap();
        let engine = Engine::new(ws, crate::engine::Config::default());
        let req = ContextRequest {
            question: "In lib.rs, how does parse_header work?".into(),
            path: None,
            budget: Some(2000),
        };
        let out = context(&engine, &req);
        assert!(out.contains("intent=read"), "{out}");
        assert!(out.contains("42"), "{out}");
        assert_eq!(out, context(&engine, &req));
        let empty = context(
            &engine,
            &ContextRequest {
                question: "  ".into(),
                path: None,
                budget: None,
            },
        );
        assert_eq!(empty, "context: empty question\n");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn headers_become_line_reads() {
        let text = "\
==> render/render.go @abc (1 match)
[9-15] type Render
10\ttype Render interface {
";
        assert_eq!(
            read_targets_from_search(text),
            vec!["render/render.go:9".to_string()]
        );
    }
}
