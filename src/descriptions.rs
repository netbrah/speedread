//! Optional stand-in for the model-facing instructions and tool descriptions.
//!
//! Off unless `SPEEDREAD_DESCRIPTION_FILE` points at a JSON file. That file is
//! the only owner of variant text. The constants in `server.rs` stay the
//! shipped defaults and are what `tools/list` returns when the variable is unset.

use std::collections::BTreeMap;

use serde::Deserialize;

pub const DESCRIPTION_FILE_ENV: &str = "SPEEDREAD_DESCRIPTION_FILE";
pub const RSS_LOG_ENV: &str = "SPEEDREAD_RSS_LOG";

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct DescriptionFile {
    /// Replaces the server instructions. Omit to keep the shipped text.
    #[serde(default)]
    instructions: Option<String>,
    /// Tool name → description. Omitted tools keep their shipped description.
    #[serde(default)]
    tools: BTreeMap<String, String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DescriptionOverride {
    pub instructions: Option<String>,
    pub tools: BTreeMap<String, String>,
}

/// Load a variant file. `Ok(None)` when the env var is unset (shipped defaults).
/// A set but empty, missing, or empty-of-text file is an error: a variant arm
/// must not silently fall back to the defaults.
pub fn load_from_env() -> Result<Option<DescriptionOverride>, String> {
    let path = match std::env::var(DESCRIPTION_FILE_ENV) {
        Err(_) => return Ok(None),
        Ok(p) if p.is_empty() => {
            return Err(format!("{DESCRIPTION_FILE_ENV} is set but empty"));
        }
        Ok(p) => p,
    };
    let text = std::fs::read_to_string(&path)
        .map_err(|e| format!("reading {DESCRIPTION_FILE_ENV}={path}: {e}"))?;
    let file: DescriptionFile = serde_json::from_str(&text)
        .map_err(|e| format!("parsing {DESCRIPTION_FILE_ENV}={path}: {e}"))?;
    if file.instructions.is_none() && file.tools.is_empty() {
        return Err(format!(
            "{path} sets neither \"instructions\" nor \"tools\"; refusing to start as a silent no-op"
        ));
    }
    if let Some(text) = &file.instructions
        && text.is_empty()
    {
        return Err(format!("{path} has an empty \"instructions\" string"));
    }
    for (name, desc) in &file.tools {
        if name.is_empty() || desc.is_empty() {
            return Err(format!(
                "{path} has an empty tool name or description (tool {name:?})"
            ));
        }
    }
    Ok(Some(DescriptionOverride {
        instructions: file.instructions,
        tools: file.tools,
    }))
}

/// Peak resident set of this process, in kibibytes. Linux reads `VmHWM`.
/// Elsewhere uses `getrusage` (`ru_maxrss` is bytes on macOS, kibibytes on Linux).
pub fn peak_rss_kib() -> Option<u64> {
    if let Ok(text) = std::fs::read_to_string("/proc/self/status") {
        for line in text.lines() {
            if let Some(rest) = line.strip_prefix("VmHWM:")
                && let Some(n) = rest.split_whitespace().next()
                && let Ok(kib) = n.parse()
            {
                return Some(kib);
            }
        }
    }
    let mut usage = std::mem::MaybeUninit::<libc::rusage>::zeroed();
    // SAFETY: getrusage writes a rusage into the out-pointer we just allocated.
    let rc = unsafe { libc::getrusage(libc::RUSAGE_SELF, usage.as_mut_ptr()) };
    if rc != 0 {
        return None;
    }
    let rss = unsafe { usage.assume_init().ru_maxrss } as u64;
    Some(rss_kib(rss))
}

fn rss_kib(ru_maxrss: u64) -> u64 {
    // macOS reports bytes. Linux reports kibibytes, but Linux already returned above.
    if cfg!(target_os = "macos") {
        ru_maxrss / 1024
    } else {
        ru_maxrss
    }
}

pub fn write_rss_log(path: &str) {
    let line = match peak_rss_kib() {
        Some(kib) => format!("peak_rss_kib={kib}\n"),
        None => "peak_rss_kib=\n".to_string(),
    };
    if let Err(e) = std::fs::write(path, line) {
        eprintln!("speedread: could not write {RSS_LOG_ENV}={path}: {e}");
    }
}
