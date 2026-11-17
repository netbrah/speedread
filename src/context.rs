//! Ancestor context for search hits: the header lines of the syntax nodes
//! that enclose a line (the loop, condition, match arm … that governs it).
//!
//! Language-agnostic: no per-grammar node lists. A node contributes its
//! first line when it is named, spans several lines and is not a plain
//! sequence of siblings (see [`headless`]).

use tree_sitter::{Node, Tree};

use crate::outline::Outline;
use crate::source::Source;

/// Ancestor lines shown per hit: the outermost and the innermost levels.
pub const MAX_SCOPES: usize = 8;

/// Multi-line nodes of one file as `(first row, last row)`, sorted by first
/// row, one entry per first row (the longest node starting there).
pub struct ScopeIndex {
    spans: Vec<(u32, u32)>,
}

fn last_row(n: Node<'_>) -> usize {
    let end = n.end_position();
    // A node ending at column 0 ends on the previous line.
    if end.column == 0 && end.row > n.start_position().row {
        end.row - 1
    } else {
        end.row
    }
}

/// A sequence container without a header of its own: it starts where its
/// first child starts and its next child begins on a later line at the same
/// (or a shallower) indentation. Python blocks, Go statement lists and
/// decorated definitions look like this; their first line is a sibling of the
/// hit, not something that encloses it. A JSX element does not: its children
/// are indented under the opening tag.
fn headless(n: Node<'_>) -> bool {
    let Some(first) = n.named_child(0) else {
        return false;
    };
    if first.start_byte() != n.start_byte() {
        return false;
    }
    let Some(second) = n.named_child(1) else {
        return false;
    };
    let (a, b) = (n.start_position(), second.start_position());
    b.row > a.row && b.column <= a.column
}

impl ScopeIndex {
    pub fn build(tree: &Tree) -> ScopeIndex {
        let mut spans: Vec<(u32, u32)> = Vec::new();
        let mut cur = tree.walk();
        // Pre-order, so first rows never decrease. Single-line subtrees are
        // skipped whole.
        'walk: loop {
            let n = cur.node();
            let (a, b) = (n.start_position().row, last_row(n));
            if b > a {
                if n.is_named() && n.parent().is_some() && !headless(n) {
                    match spans.last_mut() {
                        Some(last) if last.0 == a as u32 => last.1 = last.1.max(b as u32),
                        _ => spans.push((a as u32, b as u32)),
                    }
                }
                if cur.goto_first_child() {
                    continue;
                }
            }
            while !cur.goto_next_sibling() {
                if !cur.goto_parent() {
                    break 'walk;
                }
            }
        }
        ScopeIndex { spans }
    }

    /// First rows (0-based, ascending) of the nodes that enclose `row` and
    /// start before it and after `floor`.
    pub fn ancestors(&self, row: u32, floor: Option<u32>) -> Vec<u32> {
        self.spans
            .iter()
            .take_while(|s| s.0 < row)
            .filter(|s| s.1 >= row && floor.is_none_or(|f| s.0 > f))
            .map(|s| s.0)
            .collect()
    }
}

/// Ancestor rows worth printing for a hit on `row` (0-based): below the
/// enclosing symbol's signature (its header already names it) and holding
/// more than punctuation.
pub fn scope_rows(
    index: &ScopeIndex,
    src: &Source,
    outline: Option<&Outline>,
    row: u32,
) -> Vec<u32> {
    let floor = outline
        .and_then(|o| o.innermost_at(row).map(|i| &o.symbols[i]))
        .map(|s| match s.collapse {
            // The line before the body's interior: skips a signature that
            // spans several lines and a brace on a line of its own.
            Some((interior, _)) => s.def.max(interior.saturating_sub(1)),
            None => s.def,
        });
    index
        .ancestors(row, floor)
        .into_iter()
        .filter(|&r| {
            (r as usize) < src.line_count()
                && src
                    .line(r as usize)
                    .iter()
                    .any(|c| c.is_ascii_alphanumeric() || *c >= 0x80)
        })
        .collect()
}

/// Keep at most [`MAX_SCOPES`] rows: the outermost, then the innermost.
/// Returns the kept rows and how many were dropped after the first.
pub fn cap(rows: Vec<u32>) -> (Vec<u32>, usize) {
    if rows.len() <= MAX_SCOPES {
        return (rows, 0);
    }
    let dropped = rows.len() - MAX_SCOPES;
    let mut kept = vec![rows[0]];
    kept.extend_from_slice(&rows[rows.len() - (MAX_SCOPES - 1)..]);
    (kept, dropped)
}
