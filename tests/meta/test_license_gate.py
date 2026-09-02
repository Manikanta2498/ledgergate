"""The license gate must actually catch violations.

Same reasoning as the determinism gate: a checker that never fires reads as protection
while providing none. This runs the real logic over synthetic input. The wrong-license
case matters most -- a file that drifts from BUSL to Apache is the failure that silently
gives away the part of the repository that is meant to be paid for.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from scripts import check_licenses
from scripts.check_licenses import HEADER_LINES, check_file, main

BUSL = "# SPDX-License-Identifier: BUSL-1.1\n"


def test_flags_missing_identifier(tmp_path: Path) -> None:
    bad = tmp_path / "bad.py"
    bad.write_text('"""No header."""\n', encoding="utf-8")
    violation = check_file(bad, "BUSL-1.1")
    assert violation is not None
    assert "BUSL-1.1" in violation


def test_flags_wrong_identifier(tmp_path: Path) -> None:
    bad = tmp_path / "bad.py"
    bad.write_text("# SPDX-License-Identifier: Apache-2.0\n", encoding="utf-8")
    violation = check_file(bad, "BUSL-1.1")
    assert violation is not None
    assert "declares 'Apache-2.0'" in violation


def test_accepts_correct_identifier(tmp_path: Path) -> None:
    good = tmp_path / "good.py"
    good.write_text(BUSL + '"""Fine."""\n', encoding="utf-8")
    assert check_file(good, "BUSL-1.1") is None


def test_identifier_must_be_a_comment_not_prose(tmp_path: Path) -> None:
    """A docstring quoting an identifier is documentation, not a license declaration."""
    prose = tmp_path / "prose.py"
    prose.write_text('"""Docs.\n\nSPDX-License-Identifier: BUSL-1.1\n"""\n', encoding="utf-8")
    assert check_file(prose, "BUSL-1.1") is not None


def test_identifier_below_the_header_window_is_not_counted(tmp_path: Path) -> None:
    """A header buried under a long preamble is not a header a reader will see."""
    buried = tmp_path / "buried.py"
    buried.write_text("#\n" * (HEADER_LINES + 1) + BUSL, encoding="utf-8")
    assert check_file(buried, "BUSL-1.1") is not None


def test_uncommentable_file_needs_a_sidecar(tmp_path: Path) -> None:
    """JSON has no comment syntax, so an inline header is impossible and a sidecar is required."""
    schema = tmp_path / "v1.json"
    schema.write_text("{}\n", encoding="utf-8")
    violation = check_file(schema, "Apache-2.0")
    assert violation is not None
    assert "v1.json.license" in violation


def test_sidecar_satisfies_the_gate(tmp_path: Path) -> None:
    schema = tmp_path / "v1.json"
    schema.write_text("{}\n", encoding="utf-8")
    (tmp_path / "v1.json.license").write_text(
        "# SPDX-License-Identifier: Apache-2.0\n", encoding="utf-8"
    )
    assert check_file(schema, "Apache-2.0") is None


def test_sidecar_with_wrong_license_is_flagged(tmp_path: Path) -> None:
    schema = tmp_path / "v1.json"
    schema.write_text("{}\n", encoding="utf-8")
    (tmp_path / "v1.json.license").write_text(
        "# SPDX-License-Identifier: BUSL-1.1\n", encoding="utf-8"
    )
    violation = check_file(schema, "Apache-2.0")
    assert violation is not None
    assert "sidecar declares 'BUSL-1.1'" in violation


def test_empty_marker_file_is_covered_by_sidecar(tmp_path: Path) -> None:
    """py.typed is a PEP 561 marker with no comment syntax, so it is annotated externally.

    Its contents are reserved by PEP 561 -- empty for an inline-typed package, ``partial``
    for a partial stub distribution -- so a header cannot go in the file either way.
    """
    marker = tmp_path / "py.typed"
    marker.write_text("", encoding="utf-8")
    assert check_file(marker, "BUSL-1.1") is not None
    (tmp_path / "py.typed.license").write_text(BUSL, encoding="utf-8")
    assert check_file(marker, "BUSL-1.1") is None


def test_missing_region_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A renamed or deleted region must fail, not silently check zero files."""
    monkeypatch.setattr(check_licenses, "ROOT", tmp_path)
    monkeypatch.setattr(
        check_licenses, "REGIONS", [check_licenses.Region("does/not/exist", "BUSL-1.1")]
    )
    assert main() == 1
    assert "configured region is missing" in capsys.readouterr().out


def test_missing_region_license_text_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Exempting a license text is only sound while that text exists.

    corpus/ and schema/ carry the entire Apache-2.0 grant in a single LICENSE file. If it
    is deleted the directory still exists and holds no annotated targets, so without this
    check the gate reports OK on a region whose license notice has vanished.
    """
    region = tmp_path / "data"
    region.mkdir()
    monkeypatch.setattr(check_licenses, "ROOT", tmp_path)
    monkeypatch.setattr(
        check_licenses,
        "REGIONS",
        [check_licenses.Region("data", "Apache-2.0", license_text="LICENSE")],
    )
    assert main() == 1
    assert "required license text LICENSE is missing" in capsys.readouterr().out

    (region / "LICENSE").write_text("Apache License, Version 2.0\n", encoding="utf-8")
    assert main() == 0


def test_orphan_sidecar_is_flagged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`.license` is a reserved suffix the target scan skips, so an orphan is a hole."""
    region = tmp_path / "runtime"
    region.mkdir()
    (region / "orphan.license").write_text("arbitrary content\n", encoding="utf-8")
    monkeypatch.setattr(check_licenses, "ROOT", tmp_path)
    monkeypatch.setattr(check_licenses, "REGIONS", [check_licenses.Region("runtime", "BUSL-1.1")])
    assert main() == 1
    assert "sidecar annotates no such file" in capsys.readouterr().out


def test_reserved_directory_name_does_not_hide_a_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A LICENSES/ subdirectory is not an exemption; it would still ship in the wheel."""
    nested = tmp_path / "runtime" / "LICENSES"
    nested.mkdir(parents=True)
    (nested / "sneaky.py").write_text("VALUE = 1\n", encoding="utf-8")
    monkeypatch.setattr(check_licenses, "ROOT", tmp_path)
    monkeypatch.setattr(check_licenses, "REGIONS", [check_licenses.Region("runtime", "BUSL-1.1")])
    assert main() == 1
    assert "sneaky.py" in capsys.readouterr().out


def test_runtime_region_grants_no_license_text_exemption() -> None:
    """Only pure-data regions carry their own LICENSE; the shipped package must not.

    Otherwise a file named LICENSE could sit in the wheel carrying anything at all.
    """
    by_path = {r.path: r for r in check_licenses.REGIONS}
    assert by_path["src/ledgergate"].license_text is None
    assert by_path["corpus"].license_text == "LICENSE"


def test_every_shipped_runtime_file_is_covered() -> None:
    """The real runtime region passes, and covers every file in it rather than only .py.

    This is the claim the README makes. If package data is ever added without a
    declaration, this fails alongside the gate itself.
    """
    runtime = check_licenses.ROOT / "src" / "ledgergate"
    targets = check_licenses._targets(runtime)
    assert {p.name for p in targets} >= {"__init__.py", "py.typed"}
    assert [check_file(p, "BUSL-1.1") for p in targets] == [None] * len(targets)


def test_no_runtime_file_escapes_the_gate() -> None:
    """Everything under src/ledgergate ships in the wheel, so nothing may be unaccounted for.

    A file is accounted for if the gate checks it, or if it *is* a sidecar declaration.
    Anything else -- a stray data file, a new package-data format -- would ride into the
    BUSL wheel unlabelled, so it fails here.
    """
    runtime = check_licenses.ROOT / "src" / "ledgergate"
    everything = {
        p
        for p in runtime.rglob("*")
        if p.is_file() and not check_licenses.EXEMPT_DIRS.intersection(p.parts)
    }
    accounted = set(check_licenses._targets(runtime)) | {
        p for p in everything if p.suffix == check_licenses.SIDECAR_SUFFIX
    }
    assert everything - accounted == set()


def test_wheel_build_config_adds_no_content_outside_the_scanned_region() -> None:
    """Pin the Hatch config that keeps the wheel a subset of the scanned source region.

    This is a static check of pyproject.toml, not an inspection of a built artefact: it
    guards the inclusion mechanisms Hatch offers today and would not notice a new one.
    That is enough here only because the public claim is scoped to the source regions
    rather than to the wheel.
    """
    config = tomllib.loads((check_licenses.ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    wheel = config["tool"]["hatch"]["build"]["targets"]["wheel"]
    assert wheel["packages"] == ["src/ledgergate"]
    assert not {"force-include", "artifacts", "include", "only-include"} & set(wheel)
    assert "artifacts" not in config["tool"]["hatch"].get("build", {})
