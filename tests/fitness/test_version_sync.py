"""Fitness gate: the version has ONE source, and every mirror agrees (#176).

pyproject.toml is authoritative. The mirrors — ``__version__``, the README
badge, every ``@vX.Y.Z`` install pin, the README status line + additive
range, and the CLAUDE.md header version — must all carry the same number.
``scripts/bump_version.py`` rewrites them at release time; this gate makes
drift fail CI no matter how it happened (it happened three times before
this gate existed: ``__version__`` at v0.1.0, CLAUDE.md prose at v0.7.0,
README at v0.8.0).
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent

_spec = importlib.util.spec_from_file_location(
    "bump_version", ROOT / "scripts" / "bump_version.py"
)
assert _spec is not None and _spec.loader is not None
bump_version = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bump_version)


def test_all_mirrors_carry_the_pyproject_version() -> None:
    version = bump_version.read_version(ROOT)
    major_minor = ".".join(version.split(".")[:2])

    import datacrystal

    assert datacrystal.__version__ == version, (
        f"__version__ {datacrystal.__version__} != pyproject {version}"
    )

    readme = (ROOT / "README.md").read_text()
    badge = re.search(r"badge/version-(\d+\.\d+\.\d+)-blue", readme)
    assert badge is not None and badge.group(1) == version, (
        f"README badge {badge and badge.group(1)} != pyproject {version}"
    )
    pins = re.findall(r"@v(\d+\.\d+\.\d+)", readme)
    assert pins, "README lost its @vX.Y.Z install pins — update the gate's expectations"
    assert set(pins) == {version}, f"README install pins {set(pins)} != pyproject {version}"
    status = re.search(r"Status: \*\*(\d+\.\d+\.\d+)\*\*", readme)
    assert status is not None and status.group(1) == version, (
        f"README status line {status and status.group(1)} != pyproject {version}"
    )
    additive = re.search(r"v0\.2–(\d+\.\d+) are purely additive", readme)
    assert additive is not None and additive.group(1) == major_minor, (
        f"README additive range v0.2–{additive and additive.group(1)} != v0.2–{major_minor}"
    )

    claude = (ROOT / "CLAUDE.md").read_text()
    header = re.search(r"Version\s+`(\d+\.\d+\.\d+)`", claude)
    assert header is not None and header.group(1) == version, (
        f"CLAUDE.md header version {header and header.group(1)} != pyproject {version}"
    )


def test_bump_script_rewrites_every_mirror(tmp_path: Path) -> None:
    """The script's rewrite really hits every mirror on a fabricated tree,
    and a bumped tree passes the same checks this gate runs on the repo.
    """
    (tmp_path / "src/datacrystal").mkdir(parents=True)
    (tmp_path / "pyproject.toml").write_text('name = "x"\nversion = "1.2.3"\n')
    (tmp_path / "src/datacrystal/__init__.py").write_text('__version__ = "1.2.3"\n')
    (tmp_path / "README.md").write_text(
        "[![version](https://img.shields.io/badge/version-1.2.3-blue)](CHANGELOG.md)\n"
        '"datacrystal @ git+https://github.com/x/datacrystal@v1.2.3"\n'
        '"datacrystal @ git+https://github.com/x/datacrystal@main"\n'
        '"datacrystal[web] @ git+https://github.com/x/datacrystal@v1.2.3"\n'
        "Status: **1.2.3** — frozen; v0.2–1.2 are purely additive.\n"
    )
    (tmp_path / "CLAUDE.md").write_text("blah. Version\n`1.2.3` — v0.1.0 was the baseline\n")

    assert bump_version.next_version("1.2.3", "minor") == "1.3.0"
    assert bump_version.next_version("1.2.3", "patch") == "1.2.4"
    assert bump_version.next_version("1.2.3", "major") == "2.0.0"

    bump_version.rewrite_all(tmp_path, "1.3.0")

    assert bump_version.read_version(tmp_path) == "1.3.0"
    assert '__version__ = "1.3.0"' in (tmp_path / "src/datacrystal/__init__.py").read_text()
    readme = (tmp_path / "README.md").read_text()
    assert "version-1.3.0-blue" in readme
    assert readme.count("@v1.3.0") == 2 and "@v1.2.3" not in readme
    assert "@main" in readme  # main pins untouched
    assert "Status: **1.3.0**" in readme
    assert "v0.2–1.3 are purely additive" in readme
    assert "`1.3.0`" in (tmp_path / "CLAUDE.md").read_text()
