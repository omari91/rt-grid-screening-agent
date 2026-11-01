"""Pre-release distribution guard for `grid-screen-mcp`.

Why this script gates every build and release:
    1. Runtime data, not just code: the CSIRO `.dss` feeders and `reference_profiles.json`
       must ship inside the wheel. If they are missing, every installed copy silently
       abstains on Tier-2 studies, which tests run from a source checkout would never catch.
    2. Single version truth: PyPI rejects re-used versions forever and the MCP Registry
       validates `server.json` against the published package. A mismatch between
       `__version__`, `server.json.version` and `server.json.packages[0].version`
       would publish an unresolvable registry entry.
    3. Registry ownership: the MCP Registry verifies PyPI ownership by finding
       `mcp-name: <server.json name>` inside the rendered README (wheel METADATA).

Usage:
    python scripts/check_release.py dist/            # validate built wheel + sdist
    python scripts/check_release.py dist/ --tag v0.1.0  # also enforce git tag == version
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tarfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST_NAME = "grid-screen-mcp"
WHEEL_PREFIX = "grid_screen_mcp-"
PKG_DIR = "grid_screening_agent"

REQUIRED_WHEEL_FILES: tuple[str, ...] = (
    f"{PKG_DIR}/__init__.py",
    f"{PKG_DIR}/mcp_server.py",
    f"{PKG_DIR}/physics/_dss_runner.py",
    f"{PKG_DIR}/fixtures/reference_profiles.json",
    f"{PKG_DIR}/fixtures/networks/R/Master.dss",
    f"{PKG_DIR}/fixtures/networks/R/Lines.dss",
    f"{PKG_DIR}/fixtures/networks/R/LineCodes.dss",
    f"{PKG_DIR}/fixtures/networks/R/Loads.dss",
    f"{PKG_DIR}/fixtures/networks/R/Transformers.dss",
    f"{PKG_DIR}/fixtures/networks/H/Master.dss",
    f"{PKG_DIR}/fixtures/networks/H/Lines.dss",
    f"{PKG_DIR}/fixtures/networks/H/LineCodes.dss",
    f"{PKG_DIR}/fixtures/networks/H/Loads.dss",
)
REQUIRED_SCRIPTS: tuple[str, ...] = ("grid-screen", "grid-screen-mcp", "grid-screen-api")
FORBIDDEN_WHEEL_FRAGMENTS: tuple[str, ...] = ("__pycache__", ".pyc", ".env", "deploy/", "tests/")


def _fail(errors: list[str]) -> None:
    for err in errors:
        print(f"  FAIL: {err}", file=sys.stderr)
    sys.exit(1)


def _read_source_version() -> str:
    text = (ROOT / "src" / PKG_DIR / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'^__version__\s*=\s*"([^"]+)"', text, re.MULTILINE)
    if not match:
        _fail(["Could not parse __version__ from src/grid_screening_agent/__init__.py"])
    return match.group(1)  # type: ignore[union-attr]


def check_metadata_consistency(version: str, tag: str | None) -> list[str]:
    errors: list[str] = []
    server = json.loads((ROOT / "server.json").read_text(encoding="utf-8"))
    if server.get("version") != version:
        errors.append(f"server.json version {server.get('version')!r} != __version__ {version!r}")
    pkgs = server.get("packages", [])
    pypi_pkgs = [p for p in pkgs if p.get("registryType") == "pypi"]
    if not pypi_pkgs:
        errors.append("server.json has no registryType='pypi' package entry")
    for pkg in pypi_pkgs:
        if pkg.get("identifier") != DIST_NAME:
            errors.append(f"server.json identifier {pkg.get('identifier')!r} != {DIST_NAME!r}")
        if pkg.get("version") != version:
            errors.append(f"server.json packages[].version {pkg.get('version')!r} != {version!r}")
    desc = server.get("description", "")
    if len(desc) > 100:
        errors.append(f"server.json description is {len(desc)} chars (registry limit: 100)")

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    marker = f"mcp-name: {server.get('name')}"
    if marker not in readme:
        errors.append(f"README.md missing registry ownership marker '{marker}'")

    if tag is not None and tag.lstrip("v") != version:
        errors.append(f"git tag {tag!r} does not match __version__ {version!r}")
    return errors


def check_wheel(wheel: Path, version: str, server_name: str) -> list[str]:
    errors: list[str] = []
    with zipfile.ZipFile(wheel) as zf:
        names = set(zf.namelist())
        for required in REQUIRED_WHEEL_FILES:
            if required not in names:
                errors.append(f"wheel missing runtime file: {required}")
        for name in names:
            if any(frag in name for frag in FORBIDDEN_WHEEL_FRAGMENTS):
                errors.append(f"wheel contains forbidden path: {name}")

        dist_info = f"{WHEEL_PREFIX.rstrip('-')}-{version}.dist-info"
        metadata = zf.read(f"{dist_info}/METADATA").decode("utf-8")
        if f"Name: {DIST_NAME}" not in metadata:
            errors.append(f"wheel METADATA Name is not {DIST_NAME}")
        if f"Version: {version}" not in metadata:
            errors.append(f"wheel METADATA Version is not {version}")
        if f"mcp-name: {server_name}" not in metadata:
            errors.append("wheel METADATA long description lacks mcp-name marker (registry verification will fail)")

        entry_points = zf.read(f"{dist_info}/entry_points.txt").decode("utf-8")
        for script in REQUIRED_SCRIPTS:
            if not re.search(rf"^{re.escape(script)}\s*=", entry_points, re.MULTILINE):
                errors.append(f"wheel entry_points.txt missing console script: {script}")
    return errors


def check_sdist(sdist: Path) -> list[str]:
    errors: list[str] = []
    with tarfile.open(sdist, "r:gz") as tf:
        names = tf.getnames()
    for required in ("pyproject.toml", "README.md", "LICENSE", "server.json", "CHANGELOG.md"):
        if not any(n.endswith(f"/{required}") for n in names):
            errors.append(f"sdist missing {required}")
    if not any(n.endswith("fixtures/networks/R/Master.dss") for n in names):
        errors.append("sdist missing CSIRO Network R fixtures")
    if any("/deploy/" in n or "/.env" in n for n in names):
        errors.append("sdist leaked deploy/ or .env content")
    return errors


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("dist_dir", type=Path)
    parser.add_argument("--tag", default=None, help="Git tag being released (e.g. v0.1.0)")
    args = parser.parse_args()

    version = _read_source_version()
    server_name = json.loads((ROOT / "server.json").read_text(encoding="utf-8"))["name"]
    print(f"Checking {DIST_NAME} {version} (registry name {server_name})")

    errors = check_metadata_consistency(version, args.tag)

    wheels = sorted(args.dist_dir.glob(f"{WHEEL_PREFIX}{version}-*.whl"))
    sdists = sorted(args.dist_dir.glob(f"{WHEEL_PREFIX}{version}.tar.gz"))
    if len(wheels) != 1:
        errors.append(f"expected exactly 1 wheel for {version} in {args.dist_dir}, found {len(wheels)}")
    else:
        errors += check_wheel(wheels[0], version, server_name)
        print(f"  wheel: {wheels[0].name}")
    if len(sdists) != 1:
        errors.append(f"expected exactly 1 sdist for {version} in {args.dist_dir}, found {len(sdists)}")
    else:
        errors += check_sdist(sdists[0])
        print(f"  sdist: {sdists[0].name}")

    if errors:
        _fail(errors)
    print("OK: wheel/sdist contents, versions, entry points and registry marker are consistent.")


if __name__ == "__main__":
    main()

