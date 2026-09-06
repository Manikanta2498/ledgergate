"""Packaging claims of docs/spec/assurance.md, *Releases*: one version source; the sdist is the
runtime only."""

from __future__ import annotations

import importlib.metadata
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

import ledgergate

ROOT = Path(__file__).resolve().parents[3]


def test_installed_metadata_version_is_dunder_version() -> None:
    assert importlib.metadata.version("ledgergate") == ledgergate.__version__


def test_the_version_is_a_pep440_prerelease_or_final() -> None:
    from packaging.version import Version

    v = Version(ledgergate.__version__)
    assert str(v) == ledgergate.__version__


@pytest.mark.slow
def test_sdist_carries_only_the_runtime(tmp_path: Path) -> None:
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is not on PATH")
    subprocess.run(
        [uv, "build", "--sdist", "--out-dir", str(tmp_path)],
        check=True,
        cwd=ROOT,
        capture_output=True,
    )
    (sdist,) = tmp_path.glob("*.tar.gz")
    with tarfile.open(sdist) as tar:
        tops = {m.name.split("/", 1)[1].split("/", 1)[0] for m in tar.getmembers() if "/" in m.name}
    assert tops <= {
        "src",
        "LICENSE",
        "LICENSES",
        "README.md",
        "pyproject.toml",
        "PKG-INFO",
        ".gitignore",
    }
    for forbidden in ("corpus", "schema", "tests", ".github", "docs"):
        assert forbidden not in tops
