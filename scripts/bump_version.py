"""Bump the project version in EVERY file that carries it (issue #176).

pyproject.toml is the single source of truth; everything else is a mirror
this script rewrites in lockstep:

* ``src/datacrystal/__init__.py`` — ``__version__`` (drift bit us at v0.1.0)
* ``README.md`` — the version badge, every ``@vX.Y.Z`` git-pin install
  command, and the ``Status: **X.Y.Z**`` line incl. its ``v0.2–X.Y`` range
  (drifted to 0.7.0 after the v0.8.0 release — the incident behind #176)
* ``CLAUDE.md`` — the bare version number in the header prose (the
  per-version summary prose stays human-written)

``release.yml`` calls this as its bump step; the fitness gate
``tests/fitness/test_version_sync.py`` asserts the mirrors agree on every
CI run, so drift fails loudly no matter how it happened.

Usage: ``python scripts/bump_version.py {patch|minor|major} [--root DIR]``.
Prints ``old -> new`` and, when ``$GITHUB_OUTPUT`` is set, appends
``version=<new>`` for the workflow.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

# Every mirror the fitness gate checks; keep the two lists in sync by
# importing MIRROR_FILES there, never by copying.
MIRROR_FILES = (
    "pyproject.toml",
    "src/datacrystal/__init__.py",
    "README.md",
    "CLAUDE.md",
)


def read_version(root: Path) -> str:
    """The authoritative version: pyproject.toml's ``version = "X.Y.Z"``."""
    text = (root / "pyproject.toml").read_text()
    m = re.search(r'^version = "([^"]+)"', text, re.M)
    if not m:
        sys.exit("could not find version in pyproject.toml")
    return m.group(1)


def next_version(current: str, bump: str) -> str:
    """Semver-bump ``current`` by ``bump`` (patch|minor|major)."""
    base = re.match(r"(\d+)\.(\d+)\.(\d+)", current)  # ignore any .devN / pre suffix
    if not base:
        sys.exit(f"version {current!r} is not X.Y.Z")
    major, minor, patch = (int(base.group(i)) for i in (1, 2, 3))
    if bump == "major":
        major, minor, patch = major + 1, 0, 0
    elif bump == "minor":
        minor, patch = minor + 1, 0
    elif bump == "patch":
        patch += 1
    else:
        sys.exit(f"unknown bump kind {bump!r} (patch|minor|major)")
    return f"{major}.{minor}.{patch}"


def _sub_exactly(pattern: str, repl: str, text: str, count: int, where: str) -> str:
    new, n = re.subn(pattern, repl, text, flags=re.M)
    if n != count:
        sys.exit(f"expected exactly {count} match(es) of {pattern!r} in {where}, found {n}")
    return new


def rewrite_all(root: Path, new: str) -> None:
    """Rewrite every mirror to ``new``. Exits loudly on any pattern miss."""
    major_minor = ".".join(new.split(".")[:2])

    p = root / "pyproject.toml"
    p.write_text(_sub_exactly(r'^(version = ")[^"]+(")', rf"\g<1>{new}\g<2>",
                              p.read_text(), 1, "pyproject.toml"))

    init = root / "src/datacrystal/__init__.py"
    init.write_text(_sub_exactly(r'^(__version__ = ")[^"]+(")', rf"\g<1>{new}\g<2>",
                                 init.read_text(), 1, "__init__.py"))

    readme = root / "README.md"
    text = readme.read_text()
    text = _sub_exactly(r"(badge/version-)\d+\.\d+\.\d+(-blue)", rf"\g<1>{new}\g<2>",
                        text, 1, "README badge")
    # the git-pin install commands; @main pins are untouched
    text = re.sub(r"(@v)\d+\.\d+\.\d+", rf"\g<1>{new}", text)
    text = _sub_exactly(r"(Status: \*\*)\d+\.\d+\.\d+(\*\*)", rf"\g<1>{new}\g<2>",
                        text, 1, "README status line")
    text = _sub_exactly(r"(v0\.2–)\d+\.\d+( are purely additive)", rf"\g<1>{major_minor}\g<2>",
                        text, 1, "README additive range")
    readme.write_text(text)

    claude = root / "CLAUDE.md"
    # "Version" and the backticked number may be split across a line break
    claude.write_text(_sub_exactly(r"(Version\s+`)\d+\.\d+\.\d+(`)", rf"\g<1>{new}\g<2>",
                                   claude.read_text(), 1, "CLAUDE.md version prose"))


def main() -> None:
    """CLI entry: bump, rewrite all mirrors, report."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bump", choices=("patch", "minor", "major"))
    parser.add_argument("--root", type=Path,
                        default=Path(__file__).resolve().parent.parent)
    args = parser.parse_args()

    current = read_version(args.root)
    new = next_version(current, args.bump)
    rewrite_all(args.root, new)

    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a") as out:
            out.write(f"version={new}\n")
    print(f"{current} -> {new}  ({args.bump})")


if __name__ == "__main__":
    main()
