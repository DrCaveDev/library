---
name: library
description: Install and manage a library of skills, commands, hooks, and other Claude Code extensions (SCHE) declared in a TOML manifest. Use when the user asks to install/add/validate library entries, discover what's available across their sources, or upgrade installed items (including the library skill itself). Sources can be git repos (HTTPS or SSH) at a tag/branch/sha, or local paths.
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

### discover

```
python3 scripts/library.py discover [KEYWORD ...]
```

Enumerates SCHE across every source in the manifest (full shallow clones for repo sources) and the user's `install_path`. Items are grouped by kind and tagged:

- `[installed]`  — present locally, source content matches what's installed
- `[updatable]`  — installed but the source has a newer/different version
- `[available]`  — provided by a source but not installed
- `[local]`      — present in `install_path` but no source provides it

`KEYWORD` arguments filter by name and (where present) the `description:` frontmatter line.

### upgrade

Two phases. First stage, then review, then apply.

```
python3 scripts/library.py upgrade [NAME ...]          # phase 1: stage
python3 scripts/library.py upgrade --apply             # phase 2: apply staged
```

Phase 1 resolves the newest semver tag (falling back to default-branch HEAD) for each repo source, fetches it, compares every installed SCHE item against the staged copy, and writes a JSON manifest of changes to `~/.claude/.library-staging/manifest.json`. The JSON also lists each `ref_bump` (source URL, old ref, new ref).

**Assistant workflow for upgrade:**
1. Run `upgrade` (phase 1). Parse the JSON it emits.
2. For each item in `items`, read the file or directory at both `installed_path` and `staged_path`. Write a plain-English summary of what changed per item, grouped by item name. Do not paste raw diffs unless the user asks.
3. List any `ref_bumps` so the user knows the manifest itself will move.
4. Ask the user to confirm. On yes, run `upgrade --apply` (which copies staged files into place and bumps `ref = "..."` in the TOML).

### upgrade-self

```
python3 scripts/library.py upgrade-self          # stage
python3 scripts/library.py upgrade-self --apply  # apply
```

Same two-phase flow, but hardcoded to upgrade the `library` skill itself from its canonical origin (`https://github.com/DrCaveDev/library`). Phase 2 re-execs from the staged copy before overwriting the installed skill, so the running script isn't rewritten mid-run. If a `[[sources]]` entry pointing at the library's own repo exists in the user's manifest, its `ref` is bumped too.

## Conventions

SCHE roots (used by `discover` and `upgrade` to enumerate items, both inside source repos and under `install_path`):

| Directory   | Layout |
|-------------|--------|
| `skills/`   | one subfolder per skill (contains `SKILL.md`) |
| `commands/` | one file per slash command |
| `hooks/`    | one file per hook |
| `agents/`   | one file per subagent |
| `prompts/`  | one file per prompt |

Items are identified by their name (folder name for skills, filename including extension for the rest).

## When to use this skill

- "install my library" / "install ~/foo.toml"
- "add `skills/deploy` from acme/claude-pack@v1.2.0 to my library"
- "validate my library file"
- "what would my library install?"
- "discover what's in my library" / "find any skills about <topic>"
- "upgrade my library skills" / "upgrade the library skill itself"

## Notes

- Repo URL forms accepted: `github.com/o/r`, `https://github.com/o/r`, `git@github.com:o/r`. Anything starting with `git@` or containing `://` is used as-is; bare `host/o/r` is prefixed with `https://`.
- Sparse checkout produces a working tree, so a temp dir is unavoidable; we never write `.git/` into `install_path`.
- No uninstall, no globs, no lockfile — out of scope by design.
