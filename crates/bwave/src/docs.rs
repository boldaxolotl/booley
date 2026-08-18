//! Embedded narrative docs + agent skill, served by `bwave docs` / `bwave skill`.
//!
//! Doc corpus lives in `docs/public/` and is baked into the binary at build
//! time via `include_dir!`. The skill markdown lives in `docs/skills/bwave.md`
//! and is embedded via `include_str!`.

use std::io::{self, BufWriter, Write};

use include_dir::{include_dir, Dir};

/// Doc corpus: relative paths under `docs/public/` (e.g. `intro.md`,
/// `commands/find.md`, `reference/sync-vs-async.md`).
pub static DOCS: Dir<'_> = include_dir!("$CARGO_MANIFEST_DIR/docs/public");

/// Skill markdown emitted by `bwave skill`. Suitable for piping into
/// `.claude/skills/bwave.md`.
pub const SKILL: &str = include_str!("../docs/skills/bwave.md");

/// List every doc topic as a `<rel/path>` (without the `.md` extension).
/// Walks the embedded directory tree and prints sorted, one per line.
pub fn print_topics() {
    let mut topics: Vec<String> = Vec::new();
    collect_topics(&DOCS, "", &mut topics);
    topics.sort();
    let stdout = io::stdout();
    let mut out = BufWriter::new(stdout.lock());
    for t in &topics {
        let _ = writeln!(out, "{}", t);
    }
    let _ = out.flush();
}

fn collect_topics(dir: &Dir<'_>, prefix: &str, out: &mut Vec<String>) {
    for entry in dir.entries() {
        match entry {
            include_dir::DirEntry::File(f) => {
                let path = f.path().to_string_lossy();
                // Strip `.md`; keep posix-style slashes regardless of host.
                let topic = path.strip_suffix(".md").unwrap_or(&path).replace('\\', "/");
                let full = if prefix.is_empty() {
                    topic
                } else {
                    format!("{}/{}", prefix, topic)
                };
                out.push(full);
            }
            include_dir::DirEntry::Dir(d) => {
                // Recurse — but `include_dir` already gives us full paths
                // on file entries, so we just walk to find more files.
                collect_topics(d, prefix, out);
            }
        }
    }
}

/// Substring search across the corpus. Prints `<topic>: <first matching line>`
/// per hit. Case-insensitive.
pub fn search(query: &str) {
    let needle = query.to_lowercase();
    let stdout = io::stdout();
    let mut out = BufWriter::new(stdout.lock());
    let mut hits = 0usize;

    let mut topics: Vec<String> = Vec::new();
    collect_topics(&DOCS, "", &mut topics);
    topics.sort();

    for topic in &topics {
        let file_path = format!("{}.md", topic);
        let Some(file) = DOCS.get_file(&file_path) else {
            continue;
        };
        let Some(text) = file.contents_utf8() else {
            continue;
        };
        for line in text.lines() {
            if line.to_lowercase().contains(&needle) {
                let _ = writeln!(out, "{}: {}", topic, line.trim());
                hits += 1;
                break;
            }
        }
    }

    if hits == 0 {
        let _ = writeln!(out, "(no matches for {:?})", query);
    }
    let _ = out.flush();
}

/// Print one doc topic by its relative path (with or without `.md`).
/// Returns false if the topic doesn't exist — caller exits 1.
pub fn show(topic: &str) -> bool {
    let path = if topic.ends_with(".md") {
        topic.to_string()
    } else {
        format!("{}.md", topic)
    };
    let Some(file) = DOCS.get_file(&path) else {
        return false;
    };
    let stdout = io::stdout();
    let mut out = BufWriter::new(stdout.lock());
    if let Some(text) = file.contents_utf8() {
        let _ = out.write_all(text.as_bytes());
        if !text.ends_with('\n') {
            let _ = writeln!(out);
        }
    }
    let _ = out.flush();
    true
}
