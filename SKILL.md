---
name: library
description: Install and manage a library of skills, commands, hooks, and other Claude Code extensions (SCHE) declared in a TOML manifest. Use when the user asks to install a library file, add a source/entry to a library manifest, or validate a manifest. Sources can be git repos (HTTPS or SSH) at a tag/branch/sha, or local paths.
---

# library

Manages a TOML manifest that declares a set of SCHE (skills, commands, hooks, etc.) sourced from one or more git repos or local paths, and installs them into `~/.claude/` (or a path the manifest specifies).

## Manifest

Default location: `~/.claude/library.toml`. Any TOML file path can be passed instead.

```toml
# install_path is optional; defaults to ~/.claude
install_path = "~/.claude"

[[sources]]
repo    = "github.com/acme/claude-pack"   # or git@github.com:acme/claude-pack
ref     = "v1.2.0"                        # tag, branch, or sha — required for repo sources
include = ["skills/deploy", "commands/ship"]   # mirror to <install_path>/<same path>

# optional per-entry override; takes precedence over `include` for matching `from` paths
[[sources.entries]]
from = "packages/deploy-skill"
to   = "skills/deploy"

[[sources]]
path    = "~/dev/my-skills"               # local path; no ref
include = ["skills/scratch"]
```

Rules:
- A source has either `repo`+`ref` OR `path`, not both.
- `include` paths mirror under `install_path` (e.g. `skills/foo` → `<install_path>/skills/foo`).
- `[[sources.entries]]` provides explicit `from`→`to` overrides relative to source root and `install_path`.
- `include` and `entries` may both be present; entries override mirrors with the same `from`.

## Subcommands

The skill dispatches by first argument. Always run `scripts/library.py` from this skill folder.

### install

```
python3 scripts/library.py install [FILE]
```

- `FILE` defaults to `~/.claude/library.toml`.
- For each source: shallow + sparse clone into a temp dir, copy included paths to their destinations, delete the temp dir.
- Per-file conflict prompt: `[o]verwrite / [s]kip / [a]ll-overwrite / [A]ll-skip`.

### validate

```
python3 scripts/library.py validate [FILE]
```

Parses the manifest, checks schema, resolves git refs (`git ls-remote`) for repo sources, and prints a dry-run of every file that would be copied and its destination. Exits non-zero on any problem.

### add

```
python3 scripts/library.py add [--file FILE] (--repo URL --ref REF | --path DIR) [--include PATH ...] [--entry FROM:TO ...]
```

Appends a new `[[sources]]` block (and any `[[sources.entries]]`) to the manifest. Existing content and comments above the appended block are preserved verbatim.

## When to use this skill

- "install my library" / "install ~/foo.toml"
- "add `skills/deploy` from acme/claude-pack@v1.2.0 to my library"
- "validate my library file"
- "what would my library install?"

## Notes

- Repo URL forms accepted: `github.com/o/r`, `https://github.com/o/r`, `git@github.com:o/r`. Anything starting with `git@` or containing `://` is used as-is; bare `host/o/r` is prefixed with `https://`.
- Sparse checkout produces a working tree, so a temp dir is unavoidable; we never write `.git/` into `install_path`.
- No uninstall, no globs, no lockfile — out of scope by design.
