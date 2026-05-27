#!/usr/bin/env python3
"""library: install/validate/add SCHE from a TOML manifest.

See ../SKILL.md for the manifest schema and subcommands.
"""
from __future__ import annotations

import argparse
import filecmp
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

try:
    import tomllib
except ModuleNotFoundError:
    sys.exit("library requires Python 3.11+ (for tomllib)")

DEFAULT_MANIFEST = Path("~/.claude/library.toml").expanduser()
DEFAULT_INSTALL_PATH = Path("~/.claude").expanduser()

# Canonical origin for `upgrade-self`. Hardcoded by design.
LIBRARY_ORIGIN_REPO = "https://github.com/DrCaveDev/library"
LIBRARY_ORIGIN_REF = "main"

# Directories treated as SCHE roots, both in source repos and under install_path.
# value: "folder" → each subdirectory under the root is one item (named by folder)
#        "file"   → each regular file (any extension) under the root is one item
SCHE_LAYOUT: dict[str, str] = {
    "skills":   "folder",
    "commands": "file",
    "hooks":    "file",
    "agents":   "file",
    "prompts":  "file",
}

STAGING_DIR = Path("~/.claude/.library-staging").expanduser()


# ---------- model ----------

@dataclass
class Entry:
    src: str   # path within source root
    dst: str   # path within install_path

@dataclass
class Source:
    repo: Optional[str]      # remote URL form, normalized
    ref: Optional[str]
    path: Optional[Path]     # local source root (mutually exclusive with repo)
    include: list[str] = field(default_factory=list)
    entries: list[Entry] = field(default_factory=list)

    @property
    def label(self) -> str:
        return f"{self.repo}@{self.ref}" if self.repo else f"local:{self.path}"

@dataclass
class Manifest:
    install_path: Path
    sources: list[Source]


# ---------- parsing ----------

def normalize_repo(url: str) -> str:
    if url.startswith("git@") or "://" in url:
        return url
    return "https://" + url.lstrip("/")

def load_manifest(file: Path) -> Manifest:
    if not file.exists():
        sys.exit(f"manifest not found: {file}")
    with file.open("rb") as f:
        data = tomllib.load(f)

    install_path = Path(os.path.expanduser(data.get("install_path", str(DEFAULT_INSTALL_PATH))))
    sources: list[Source] = []
    for i, raw in enumerate(data.get("sources", [])):
        has_repo = "repo" in raw
        has_path = "path" in raw
        if has_repo == has_path:
            sys.exit(f"sources[{i}]: must have exactly one of `repo` or `path`")
        if has_repo and "ref" not in raw:
            sys.exit(f"sources[{i}]: `ref` is required when `repo` is set")

        entries = [
            Entry(src=e["from"], dst=e["to"])
            for e in raw.get("entries", [])
        ]
        sources.append(Source(
            repo=normalize_repo(raw["repo"]) if has_repo else None,
            ref=raw.get("ref"),
            path=Path(os.path.expanduser(raw["path"])) if has_path else None,
            include=list(raw.get("include", [])),
            entries=entries,
        ))
    return Manifest(install_path=install_path, sources=sources)


# ---------- planning ----------

def plan_source(src: Source) -> list[Entry]:
    """Return the full list of (from, to) pairs for a source.

    `entries` overrides any `include` that matches its `from`. Other includes mirror.
    """
    overridden = {e.src for e in src.entries}
    plan = list(src.entries)
    for inc in src.include:
        if inc in overridden:
            continue
        plan.append(Entry(src=inc, dst=inc))
    return plan


# ---------- fetching ----------

def fetch_repo(repo: str, ref: str, paths: list[str], dest: Path) -> None:
    """Sparse + shallow clone of `repo` at `ref`, with only `paths` checked out."""
    run(["git", "clone", "--filter=blob:none", "--no-checkout", "--depth", "1", "--branch", ref, repo, str(dest)],
        fallback=lambda: clone_then_checkout(repo, ref, dest))
    run(["git", "-C", str(dest), "sparse-checkout", "init", "--cone"], check=False)
    run(["git", "-C", str(dest), "sparse-checkout", "set", *paths])
    run(["git", "-C", str(dest), "checkout", ref])

def clone_then_checkout(repo: str, ref: str, dest: Path) -> None:
    """Fallback: --branch only works for branches/tags. For raw sha, clone then fetch+checkout."""
    if dest.exists():
        shutil.rmtree(dest)
    run(["git", "clone", "--filter=blob:none", "--no-checkout", repo, str(dest)])
    run(["git", "-C", str(dest), "fetch", "origin", ref])
    # ref may be a sha; checkout will resolve it after fetch.

def run(cmd: list[str], check: bool = True, fallback=None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, check=check, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        if fallback is not None:
            return fallback()
        sys.stderr.write(f"\ncommand failed: {' '.join(cmd)}\n{e.stderr}\n")
        raise


# ---------- SCHE enumeration ----------

def enumerate_sche(root: Path) -> dict[str, dict[str, Path]]:
    """Return {kind: {item_name: absolute_path}} for SCHE found under `root`.

    `root` is a repo root or an install path. Missing subdirs are simply absent.
    """
    out: dict[str, dict[str, Path]] = {k: {} for k in SCHE_LAYOUT}
    for kind, layout in SCHE_LAYOUT.items():
        base = root / kind
        if not base.is_dir():
            continue
        if layout == "folder":
            for child in sorted(base.iterdir()):
                if child.is_dir() and not child.name.startswith("."):
                    out[kind][child.name] = child
        else:
            for child in sorted(base.iterdir()):
                if child.is_file() and not child.name.startswith("."):
                    out[kind][child.name] = child
    return out

def read_description(item_path: Path) -> str:
    """Best-effort one-line description for a SCHE item.

    Looks for a `description:` line in YAML frontmatter of SKILL.md (for skill
    folders) or the file itself (for single-file items). Returns "" if absent.
    """
    target: Optional[Path] = None
    if item_path.is_dir():
        skill_md = item_path / "SKILL.md"
        if skill_md.exists():
            target = skill_md
    elif item_path.is_file() and item_path.suffix == ".md":
        target = item_path
    if target is None:
        return ""
    try:
        text = target.read_text(errors="replace")
    except OSError:
        return ""
    m = re.search(r"^description:\s*(.+)$", text, flags=re.MULTILINE)
    return m.group(1).strip() if m else ""

def tree_digest(path: Path) -> str:
    """Stable hash over a file or directory tree (content + relative paths)."""
    h = hashlib.sha256()
    if path.is_file():
        h.update(b"F\x00")
        h.update(path.read_bytes())
        return h.hexdigest()
    if not path.exists():
        return ""
    for root, dirs, files in os.walk(path):
        dirs.sort()
        for fname in sorted(files):
            f = Path(root) / fname
            rel = f.relative_to(path).as_posix().encode()
            h.update(b"P\x00" + rel + b"\x00")
            try:
                h.update(f.read_bytes())
            except OSError:
                pass
    return h.hexdigest()


# ---------- ref resolution + full fetch ----------

_SEMVER_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:[-+].*)?$")

def _semver_key(tag: str) -> Optional[tuple[int, int, int]]:
    m = _SEMVER_RE.match(tag)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None

def resolve_latest_ref(repo: str) -> str:
    """Newest semver tag if any; otherwise default-branch HEAD."""
    r = subprocess.run(["git", "ls-remote", "--tags", "--refs", repo],
                       capture_output=True, text=True)
    tags: list[str] = []
    if r.returncode == 0:
        for line in r.stdout.splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1].startswith("refs/tags/"):
                tags.append(parts[1][len("refs/tags/"):])
    semver = [(t, _semver_key(t)) for t in tags]
    semver = [(t, k) for t, k in semver if k is not None]
    if semver:
        semver.sort(key=lambda x: x[1])
        return semver[-1][0]
    # fall back to default branch
    r2 = subprocess.run(["git", "ls-remote", "--symref", repo, "HEAD"],
                        capture_output=True, text=True)
    for line in r2.stdout.splitlines():
        if line.startswith("ref: "):
            # "ref: refs/heads/main\tHEAD"
            ref = line.split()[1]
            if ref.startswith("refs/heads/"):
                return ref[len("refs/heads/"):]
    return "main"

def fetch_full(repo: str, ref: str, dest: Path) -> None:
    """Shallow full-tree clone at `ref` (no sparse filter). Used by discover/upgrade."""
    if dest.exists():
        shutil.rmtree(dest)
    try:
        subprocess.run(
            ["git", "clone", "--depth", "1", "--branch", ref, repo, str(dest)],
            check=True, capture_output=True, text=True,
        )
    except subprocess.CalledProcessError:
        # ref may be a sha — clone default, then fetch+checkout
        if dest.exists():
            shutil.rmtree(dest)
        subprocess.run(["git", "clone", "--depth", "1", repo, str(dest)],
                       check=True, capture_output=True, text=True)
        subprocess.run(["git", "-C", str(dest), "fetch", "--depth", "1", "origin", ref],
                       check=True, capture_output=True, text=True)
        subprocess.run(["git", "-C", str(dest), "checkout", ref],
                       check=True, capture_output=True, text=True)


# ---------- TOML ref bump (line-oriented, preserves comments) ----------

def update_ref_in_toml(file: Path, repo_url: str, new_ref: str) -> bool:
    """Bump the `ref = "..."` of the [[sources]] block whose repo matches.

    Matches against both the raw and normalized form of the repo URL, so a
    manifest written as `github.com/o/r` still matches our normalized
    `https://github.com/o/r`. Returns True if a change was written.
    """
    text = file.read_text()
    lines = text.splitlines(keepends=True)

    candidates = {repo_url, repo_url.removeprefix("https://"), repo_url.removeprefix("http://")}

    in_block = False
    matched_block = False
    changed = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("[["):
            in_block = stripped == "[[sources]]"
            matched_block = False
            continue
        if not in_block:
            continue
        m = re.match(r'\s*repo\s*=\s*"([^"]+)"', line)
        if m and m.group(1) in candidates:
            matched_block = True
            continue
        if matched_block:
            m2 = re.match(r'(\s*ref\s*=\s*")([^"]+)(".*)', line)
            if m2 and m2.group(2) != new_ref:
                lines[i] = f"{m2.group(1)}{new_ref}{m2.group(3)}"
                if not lines[i].endswith("\n"):
                    lines[i] += "\n"
                changed = True
                matched_block = False
    if changed:
        file.write_text("".join(lines))
    return changed


# ---------- conflict prompt ----------

class ConflictResolver:
    def __init__(self):
        self.always: Optional[str] = None  # "overwrite" | "skip"
        try:
            self.tty = open("/dev/tty", "r+")
        except OSError:
            self.tty = None

    def decide(self, dst: Path) -> bool:
        """Return True to overwrite, False to skip."""
        if self.always == "overwrite":
            return True
        if self.always == "skip":
            return False
        if self.tty is None:
            # Non-interactive: default to skip for safety.
            print(f"  conflict (no tty, skipping): {dst}", file=sys.stderr)
            return False
        self.tty.write(f"\nconflict: {dst} exists.\n  [o]verwrite / [s]kip / [a]ll-overwrite / [A]ll-skip: ")
        self.tty.flush()
        choice = self.tty.readline().strip()
        if choice == "a":
            self.always = "overwrite"; return True
        if choice == "A":
            self.always = "skip"; return False
        if choice == "o":
            return True
        return False  # default skip


# ---------- copy ----------

def copy_path(src: Path, dst: Path, resolver: ConflictResolver) -> tuple[int, int]:
    """Copy file or dir tree. Returns (copied, skipped) file counts."""
    if not src.exists():
        sys.stderr.write(f"  missing in source: {src}\n")
        return (0, 0)
    if src.is_file():
        return (1, 0) if copy_file(src, dst, resolver) else (0, 1)
    copied = skipped = 0
    for root, _, files in os.walk(src):
        rel = Path(root).relative_to(src)
        for fname in files:
            s = Path(root) / fname
            d = dst / rel / fname
            if copy_file(s, d, resolver):
                copied += 1
            else:
                skipped += 1
    return (copied, skipped)

def copy_file(src: Path, dst: Path, resolver: ConflictResolver) -> bool:
    if dst.exists():
        if not resolver.decide(dst):
            return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return True


# ---------- subcommands ----------

def cmd_install(args) -> int:
    manifest = load_manifest(args.file)
    resolver = ConflictResolver()
    total_copied = total_skipped = 0
    for src in manifest.sources:
        plan = plan_source(src)
        if not plan:
            continue
        print(f"\n→ {src.label}")
        if src.repo:
            with tempfile.TemporaryDirectory(prefix="library-") as td:
                fetch_repo(src.repo, src.ref, [e.src for e in plan], Path(td))
                root = Path(td)
                for entry in plan:
                    c, s = copy_path(root / entry.src, manifest.install_path / entry.dst, resolver)
                    total_copied += c; total_skipped += s
                    print(f"  {entry.src} → {manifest.install_path / entry.dst}  (copied {c}, skipped {s})")
        else:
            root = src.path
            for entry in plan:
                c, s = copy_path(root / entry.src, manifest.install_path / entry.dst, resolver)
                total_copied += c; total_skipped += s
                print(f"  {entry.src} → {manifest.install_path / entry.dst}  (copied {c}, skipped {s})")
    print(f"\ndone. files copied: {total_copied}, skipped: {total_skipped}")
    return 0

def cmd_validate(args) -> int:
    manifest = load_manifest(args.file)
    print(f"manifest: {args.file}")
    print(f"install_path: {manifest.install_path}")
    problems = 0
    for i, src in enumerate(manifest.sources):
        print(f"\n[sources.{i}] {src.label}")
        if src.repo:
            r = subprocess.run(["git", "ls-remote", src.repo, src.ref],
                               capture_output=True, text=True)
            if r.returncode != 0 or not r.stdout.strip():
                # may still be a sha — ls-remote can't resolve raw shas
                print(f"  warning: could not resolve ref via ls-remote (may be a sha): {src.ref}")
            else:
                print(f"  ref ok: {r.stdout.split()[0][:12]}")
        else:
            if not src.path.exists():
                print(f"  ERROR: local path does not exist: {src.path}"); problems += 1
        for entry in plan_source(src):
            print(f"  {entry.src} → {manifest.install_path / entry.dst}")
    return 1 if problems else 0

def cmd_add(args) -> int:
    if (args.repo is None) == (args.path is None):
        sys.exit("add: provide exactly one of --repo or --path")
    if args.repo and not args.ref:
        sys.exit("add: --ref is required with --repo")

    file = args.file
    file.parent.mkdir(parents=True, exist_ok=True)
    existing = file.read_text() if file.exists() else ""

    lines = ["", "[[sources]]"]
    if args.repo:
        lines.append(f'repo = "{args.repo}"')
        lines.append(f'ref  = "{args.ref}"')
    else:
        lines.append(f'path = "{args.path}"')
    if args.include:
        items = ", ".join(f'"{p}"' for p in args.include)
        lines.append(f"include = [{items}]")
    for spec in args.entry or []:
        if ":" not in spec:
            sys.exit(f"--entry must be FROM:TO, got {spec!r}")
        frm, to = spec.split(":", 1)
        lines += ["", "[[sources.entries]]", f'from = "{frm}"', f'to   = "{to}"']

    new = existing.rstrip() + "\n" + "\n".join(lines) + "\n"
    file.write_text(new)
    print(f"appended source to {file}")
    # validate the result so we fail fast on a typo
    try:
        load_manifest(file)
    except SystemExit as e:
        sys.stderr.write(f"warning: appended block does not parse cleanly: {e}\n")
        return 1
    return 0


# ---------- discover ----------

def _match_keywords(name: str, desc: str, keywords: list[str]) -> bool:
    if not keywords:
        return True
    hay = (name + " " + desc).lower()
    return any(k.lower() in hay for k in keywords)

def cmd_discover(args) -> int:
    manifest = load_manifest(args.file)
    keywords: list[str] = args.keyword or []

    installed = enumerate_sche(manifest.install_path)

    # source_idx -> (label, repo_url_or_None, ref, root_path, sche_dict)
    source_inventories: list[tuple[int, str, Optional[str], Optional[str], Path, dict]] = []
    tmpdirs: list[tempfile.TemporaryDirectory] = []
    try:
        for idx, src in enumerate(manifest.sources):
            if src.repo:
                td = tempfile.TemporaryDirectory(prefix="library-discover-")
                tmpdirs.append(td)
                root = Path(td.name)
                try:
                    fetch_full(src.repo, src.ref, root)
                except subprocess.CalledProcessError as e:
                    print(f"  ! failed to fetch {src.label}: {e.stderr.strip() if e.stderr else e}",
                          file=sys.stderr)
                    continue
            else:
                root = src.path
                if not root.exists():
                    print(f"  ! missing local path: {root}", file=sys.stderr)
                    continue
            source_inventories.append((idx, src.label, src.repo, src.ref, root, enumerate_sche(root)))

        # Build set of all (kind, name) across all sources
        by_kind: dict[str, dict[str, dict]] = {k: {} for k in SCHE_LAYOUT}
        for idx, label, repo, ref, root, inv in source_inventories:
            for kind, items in inv.items():
                for name, path in items.items():
                    rec = by_kind[kind].setdefault(name, {
                        "name": name,
                        "kind": kind,
                        "description": "",
                        "sources": [],
                        "installed": name in installed[kind],
                    })
                    desc = read_description(path)
                    if desc and not rec["description"]:
                        rec["description"] = desc
                    src_digest = tree_digest(path)
                    inst_digest = tree_digest(installed[kind][name]) if rec["installed"] else ""
                    rec["sources"].append({
                        "source_idx": idx,
                        "label": label,
                        "updatable": rec["installed"] and src_digest != inst_digest,
                    })

        # Items installed locally but not provided by any source
        local_only: dict[str, list[str]] = {k: [] for k in SCHE_LAYOUT}
        for kind, items in installed.items():
            for name, path in items.items():
                if name not in by_kind[kind]:
                    desc = read_description(path)
                    if _match_keywords(name, desc, keywords):
                        local_only[kind].append(f"{name}  {desc}".rstrip())

        any_output = False
        for kind in SCHE_LAYOUT:
            section_lines: list[str] = []
            for name, rec in sorted(by_kind[kind].items()):
                if not _match_keywords(name, rec["description"], keywords):
                    continue
                updatable = any(s["updatable"] for s in rec["sources"])
                if rec["installed"]:
                    tag = "[updatable]" if updatable else "[installed] "
                else:
                    tag = "[available] "
                src_labels = ", ".join(sorted({s["label"] for s in rec["sources"]}))
                desc = f" — {rec['description']}" if rec["description"] else ""
                section_lines.append(f"  {tag} {name}{desc}  ({src_labels})")
            for line in local_only[kind]:
                section_lines.append(f"  [local]      {line}")
            if section_lines:
                any_output = True
                print(f"\n{kind}/")
                for line in section_lines:
                    print(line)

        if not any_output:
            print("no items found" + (f" matching {keywords!r}" if keywords else ""))
        return 0
    finally:
        for td in tmpdirs:
            td.cleanup()


# ---------- upgrade ----------

def _find_source_for_item(manifest: Manifest, kind: str, name: str) -> Optional[Source]:
    """Return the first source that provides this SCHE item, or None."""
    for src in manifest.sources:
        if src.repo:
            # We can't enumerate without fetching; defer — caller fetches all anyway.
            continue
        items = enumerate_sche(src.path).get(kind, {})
        if name in items:
            return src
    return None

def _stage_source(src: Source, target_ref: str, dest: Path) -> Path:
    """Clone or copy a source into `dest` at `target_ref`. Returns the root path."""
    if src.repo:
        fetch_full(src.repo, target_ref, dest)
        return dest
    # local: just symlink-ish copy reference (use the path directly)
    return src.path

def cmd_upgrade(args) -> int:
    if args.apply:
        return _upgrade_apply()
    return _upgrade_stage(args)

def _upgrade_stage(args) -> int:
    manifest = load_manifest(args.file)
    names: list[str] = args.name or []

    installed = enumerate_sche(manifest.install_path)
    installed_set = {(k, n) for k, items in installed.items() for n in items}

    if STAGING_DIR.exists():
        shutil.rmtree(STAGING_DIR)
    STAGING_DIR.mkdir(parents=True)

    staging_manifest: dict = {
        "file": str(args.file),
        "ref_bumps": [],   # [{repo, old_ref, new_ref}]
        "items": [],       # [{kind, name, source_label, installed_path, staged_path, status}]
    }

    for idx, src in enumerate(manifest.sources):
        src_stage_root = STAGING_DIR / f"src{idx}"
        target_ref = src.ref
        if src.repo:
            try:
                target_ref = resolve_latest_ref(src.repo)
            except Exception as e:
                print(f"  ! failed to resolve latest ref for {src.label}: {e}", file=sys.stderr)
                target_ref = src.ref
            try:
                fetch_full(src.repo, target_ref, src_stage_root)
            except subprocess.CalledProcessError as e:
                print(f"  ! failed to fetch {src.label}@{target_ref}: "
                      f"{e.stderr.strip() if e.stderr else e}", file=sys.stderr)
                continue
            root = src_stage_root
            if target_ref != src.ref:
                staging_manifest["ref_bumps"].append({
                    "repo": src.repo, "old_ref": src.ref, "new_ref": target_ref,
                })
        else:
            if not src.path.exists():
                print(f"  ! missing local path: {src.path}", file=sys.stderr)
                continue
            root = src.path

        inv = enumerate_sche(root)
        for kind, items in inv.items():
            for name, src_item_path in items.items():
                if (kind, name) not in installed_set:
                    continue
                if names and name not in names:
                    continue
                inst_path = installed[kind][name]
                if tree_digest(src_item_path) == tree_digest(inst_path):
                    continue  # unchanged
                staged_item_path = src_stage_root / kind / name if not src.repo else src_item_path
                # for local sources we still want a staged copy under STAGING_DIR for uniformity
                if not src.repo:
                    staged_item_path = STAGING_DIR / f"src{idx}" / kind / name
                    staged_item_path.parent.mkdir(parents=True, exist_ok=True)
                    if src_item_path.is_dir():
                        if staged_item_path.exists():
                            shutil.rmtree(staged_item_path)
                        shutil.copytree(src_item_path, staged_item_path)
                    else:
                        shutil.copy2(src_item_path, staged_item_path)
                staging_manifest["items"].append({
                    "kind": kind,
                    "name": name,
                    "source_label": src.label,
                    "source_ref": target_ref,
                    "installed_path": str(inst_path),
                    "staged_path": str(staged_item_path),
                    "status": "modified",
                })

    manifest_file = STAGING_DIR / "manifest.json"
    manifest_file.write_text(json.dumps(staging_manifest, indent=2))
    print(json.dumps(staging_manifest, indent=2))
    print(f"\nstaged {len(staging_manifest['items'])} item(s) to {STAGING_DIR}", file=sys.stderr)
    print(f"review diffs, then run: python3 scripts/library.py upgrade --apply", file=sys.stderr)
    return 0

def _upgrade_apply() -> int:
    manifest_file = STAGING_DIR / "manifest.json"
    if not manifest_file.exists():
        sys.exit(f"no staged upgrade found at {manifest_file}. Run `upgrade` first.")
    staged = json.loads(manifest_file.read_text())

    for item in staged["items"]:
        src = Path(item["staged_path"])
        dst = Path(item["installed_path"])
        if not src.exists():
            print(f"  ! staged path missing: {src}", file=sys.stderr); continue
        if src.is_dir():
            if dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(src, dst)
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        print(f"  upgraded {item['kind']}/{item['name']}  ({item['source_label']})")

    manifest_path = Path(staged["file"])
    for bump in staged["ref_bumps"]:
        if update_ref_in_toml(manifest_path, bump["repo"], bump["new_ref"]):
            print(f"  bumped {bump['repo']}: {bump['old_ref']} → {bump['new_ref']}")

    shutil.rmtree(STAGING_DIR, ignore_errors=True)
    print("upgrade applied.")
    return 0


# ---------- upgrade-self ----------

def cmd_upgrade_self(args) -> int:
    if args.apply:
        return _upgrade_self_apply()
    return _upgrade_self_stage(args)

def _self_install_dir() -> Path:
    return DEFAULT_INSTALL_PATH / "skills" / "library"

def _upgrade_self_stage(args) -> int:
    target_ref = LIBRARY_ORIGIN_REF
    try:
        target_ref = resolve_latest_ref(LIBRARY_ORIGIN_REPO)
    except Exception as e:
        print(f"  ! failed to resolve latest ref: {e}", file=sys.stderr)

    if STAGING_DIR.exists():
        shutil.rmtree(STAGING_DIR)
    STAGING_DIR.mkdir(parents=True)
    clone_root = STAGING_DIR / "_self_repo"
    fetch_full(LIBRARY_ORIGIN_REPO, target_ref, clone_root)

    # The skill lives at the repo root (SKILL.md + scripts/ + examples/).
    staged = clone_root
    installed = _self_install_dir()

    same = installed.exists() and tree_digest(installed) == tree_digest(staged)

    staging_manifest = {
        "self": True,
        "repo": LIBRARY_ORIGIN_REPO,
        "target_ref": target_ref,
        "installed_path": str(installed),
        "staged_path": str(staged),
        "unchanged": same,
        "manifest_file": str(args.file) if args.file else str(DEFAULT_MANIFEST),
    }
    (STAGING_DIR / "manifest.json").write_text(json.dumps(staging_manifest, indent=2))
    print(json.dumps(staging_manifest, indent=2))
    if same:
        print("\nalready up to date.", file=sys.stderr)
    else:
        print(f"\nstaged self-upgrade to {target_ref}. review, then run: "
              f"python3 scripts/library.py upgrade-self --apply", file=sys.stderr)
    return 0

def _upgrade_self_apply() -> int:
    mf = STAGING_DIR / "manifest.json"
    if not mf.exists():
        sys.exit(f"no staged self-upgrade at {mf}. Run `upgrade-self` first.")
    staged = json.loads(mf.read_text())
    if staged.get("unchanged"):
        shutil.rmtree(STAGING_DIR, ignore_errors=True)
        print("nothing to do.")
        return 0

    staged_path = Path(staged["staged_path"])
    installed = Path(staged["installed_path"])

    # Re-exec ourselves from the staged copy if we're about to overwrite the
    # running script. We pass a private sentinel arg so the re-exec'd process
    # knows to skip straight to the copy step without re-staging.
    running = Path(__file__).resolve()
    will_overwrite_self = running.is_relative_to(installed)
    if will_overwrite_self and os.environ.get("LIBRARY_SELF_REEXEC") != "1":
        env = {**os.environ, "LIBRARY_SELF_REEXEC": "1"}
        staged_script = staged_path / "scripts" / "library.py"
        os.execvpe(sys.executable, [sys.executable, str(staged_script), "upgrade-self", "--apply"], env)

    if installed.exists():
        shutil.rmtree(installed)
    shutil.copytree(staged_path, installed)
    # don't carry .git into the install dir
    git_dir = installed / ".git"
    if git_dir.exists():
        shutil.rmtree(git_dir)

    # Bump ref in the user's manifest if a self-entry exists.
    manifest_path = Path(staged["manifest_file"])
    if manifest_path.exists():
        if update_ref_in_toml(manifest_path, LIBRARY_ORIGIN_REPO, staged["target_ref"]):
            print(f"  bumped self-entry ref → {staged['target_ref']}")

    shutil.rmtree(STAGING_DIR, ignore_errors=True)
    print(f"library upgraded to {staged['target_ref']}.")
    return 0


# ---------- entrypoint ----------

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="library")
    sub = p.add_subparsers(dest="cmd", required=True)

    pi = sub.add_parser("install")
    pi.add_argument("file", nargs="?", type=Path, default=DEFAULT_MANIFEST)
    pi.set_defaults(func=cmd_install)

    pv = sub.add_parser("validate")
    pv.add_argument("file", nargs="?", type=Path, default=DEFAULT_MANIFEST)
    pv.set_defaults(func=cmd_validate)

    pa = sub.add_parser("add")
    pa.add_argument("--file", type=Path, default=DEFAULT_MANIFEST)
    pa.add_argument("--repo")
    pa.add_argument("--ref")
    pa.add_argument("--path")
    pa.add_argument("--include", action="append", default=[])
    pa.add_argument("--entry", action="append", default=[],
                    help="FROM:TO override (repeatable)")
    pa.set_defaults(func=cmd_add)

    pd = sub.add_parser("discover",
                        help="enumerate SCHE in manifest sources; flag installed/updatable/available")
    pd.add_argument("--file", type=Path, default=DEFAULT_MANIFEST)
    pd.add_argument("keyword", nargs="*",
                    help="filter items whose name or description contains any keyword")
    pd.set_defaults(func=cmd_discover)

    pu = sub.add_parser("upgrade",
                        help="stage upgrades from sources (default), or --apply staged upgrades")
    pu.add_argument("--file", type=Path, default=DEFAULT_MANIFEST)
    pu.add_argument("--apply", action="store_true",
                    help="apply a previously staged upgrade")
    pu.add_argument("name", nargs="*",
                    help="restrict to specific item names (default: all installed)")
    pu.set_defaults(func=cmd_upgrade)

    pus = sub.add_parser("upgrade-self",
                         help="upgrade the library skill itself from its canonical origin")
    pus.add_argument("--file", type=Path, default=DEFAULT_MANIFEST)
    pus.add_argument("--apply", action="store_true")
    pus.set_defaults(func=cmd_upgrade_self)

    args = p.parse_args(argv)
    if args.cmd == "add" and args.repo:
        args.repo = normalize_repo(args.repo)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
