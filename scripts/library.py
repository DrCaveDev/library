#!/usr/bin/env python3
"""library: install/validate/add SCHE from a TOML manifest.

See ../SKILL.md for the manifest schema and subcommands.
"""
from __future__ import annotations

import argparse
import os
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

    args = p.parse_args(argv)
    if args.cmd == "add" and args.repo:
        args.repo = normalize_repo(args.repo)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
