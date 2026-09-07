# SPDX-FileCopyrightText: 2026 Venkata Sai Manikanta Yatam
# SPDX-License-Identifier: Apache-2.0
"""The mutation ratchet, docs/spec/assurance.md *The mutation gate*.

Reads every mutant's status from the completed mutmut run under ``mutants/`` (killed included),
keys each mutant by ``(function, sha256 of the diff body)`` so the key survives mutmut's
positional renumbering, and compares the unkilled set with ``.mutation-baseline.json``:

  gate      fail if an unkilled key is not baselined, or a baselined key matches no current
            mutant (vanished); warn if a baselined key is killed this run (stale: the runner
            flaps on some mutants, so a stale entry is regenerated away, never a red night) or
            changed bucket.
  baseline  write the baseline from this run and record the source digest (sha256 over the
            mutated sources and the test tree). A previously baselined entry the run killed is
            kept (marked `flaky`) when the digest is unchanged (a flap), and dropped when it
            moved (a new test killed it); an entry already marked `flaky` keeps that mark
            whatever this run did, until `retire-flaky` removes it. `--source DIGEST`
            overrides the computed digest.
  retire-flaky KEY
            drop one `flaky` entry from the baseline, for when its test has been made
            deterministic. The only way a flaky entry leaves the file.
  count     print the baseline's total (the number the README states).

``baseline --from-results FILE`` takes the statuses from a ``mutmut results --all true``
listing (the nightly's artefact) and only the keys from the local ``mutants/``: mutant names
and diffs are a function of the source, statuses are a function of the machine, and the
baseline must be the runner's truth, since the runner is what gates.

Run after ``mutmut run``; never runs mutmut itself.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import tomllib
from collections import defaultdict
from pathlib import Path
from typing import Any

BASELINE = Path(".mutation-baseline.json")
BUCKETS = (
    "no tests",
    "survived",
    "suspicious",
    "timeout",
    "segfault",
    "skipped",
    "not checked",
    "check was interrupted by user",
)
KILLED = {"killed", "caught by type check"}


def _function_of(name: str) -> str:
    # <module>.xǁFixedClockǁ__init____mutmut_2 -> <module>.xǁFixedClockǁ__init__
    return name.rsplit("__mutmut_", 1)[0]


def _parse_results(text: str) -> dict[str, str]:
    """`mutmut results --all true` output: `    <name>: <status>` per line."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or ": " not in line:
            continue
        name, status = line.rsplit(": ", 1)
        out[name] = status
    return out


def collect(statuses: dict[str, str] | None = None) -> dict[str, dict[str, Any]]:
    """{key: {function, bucket, names}} for every current mutant, killed as bucket 'killed'.
    ``statuses`` overrides the local run's statuses by mutant name (the runner's results)."""
    from mutmut.__main__ import (  # type: ignore[attr-defined]
        SourceFileMutationData,
        get_diff_for_mutant,
        status_by_exit_code,
        walk_mutatable_files,
    )
    from mutmut.configuration import Config

    Config.ensure_loaded()
    by_key: dict[str, dict[str, Any]] = {}
    for path in walk_mutatable_files():
        data = SourceFileMutationData(path=path)
        data.load()
        for name, code in data.exit_code_by_key.items():
            status = status_by_exit_code[code]
            if statuses is not None:
                if name not in statuses:
                    raise SystemExit(f"results file has no entry for {name}: not the same source")
                status = statuses[name]
            diff = get_diff_for_mutant(name, path=path)
            body = "\n".join(line for line in diff.splitlines() if not line.startswith("# "))
            function = _function_of(name)
            key = f"{function}:{hashlib.sha256(body.encode()).hexdigest()[:16]}"
            bucket = "killed" if status in KILLED else status
            entry = by_key.setdefault(key, {"function": function, "bucket": "killed", "names": []})
            entry["names"].append(name)
            if bucket != "killed" and (
                entry["bucket"] == "killed"
                or BUCKETS.index(bucket) < BUCKETS.index(entry["bucket"])
            ):
                entry["bucket"] = bucket  # unkilled iff any is unkilled; worst-first bucket
    return by_key


def load_baseline() -> dict[str, Any]:
    if not BASELINE.exists():
        return {"unkilled": {}, "equivalent": {}}
    doc: dict[str, Any] = json.loads(BASELINE.read_text())
    return doc


def gate(current: dict[str, dict[str, Any]]) -> int:
    base = load_baseline()
    failures: list[str] = []
    warnings: list[str] = []
    overlap = sorted(set(base["unkilled"]) & set(base["equivalent"]))
    for key in overlap:
        failures.append(f"key {key} is both unkilled and equivalent in the baseline")
    baselined = {**base["unkilled"], **base["equivalent"]}
    readme = Path("README.md")
    if readme.exists():
        m = re.search(r"unkilled mutants\*\* of ([0-9,]+)", readme.read_text())
        if m is not None and int(m.group(1).replace(",", "")) != len(current):
            failures.append(
                f"README says of {m.group(1)} mutants, this run has {len(current)}; update it"
            )
    for key, info in current.items():
        if info["bucket"] == "killed":
            continue
        if key not in baselined:
            failures.append(f"new unkilled mutant {key} [{info['bucket']}] ({info['names'][0]})")
        elif key in base["unkilled"] and base["unkilled"][key]["bucket"] != info["bucket"]:
            warnings.append(
                f"bucket changed {key}: {base['unkilled'][key]['bucket']} -> {info['bucket']}"
            )
    for key in baselined:
        if key not in current:
            failures.append(f"stale entry {key}: matches no current mutant (vanished)")
        elif current[key]["bucket"] == "killed":
            # the runner itself kills a given mutant on one night and not the next (observed on
            # the second nightly: 615 then 614), a property of the tests, not of the mutant; a
            # stale entry is therefore a warning, and the baseline shrinks by regeneration
            # from the runner's results, never by a red night
            warnings.append(f"stale entry {key}: killed this run; regenerate from the runner")
    for w in warnings:
        print(f"warning: {w}")
    for f in failures:
        print(f"FAIL: {f}")
    total = sum(1 for i in current.values() if i["bucket"] != "killed")
    print(
        f"mutation gate: {len(current)} mutants, {total} unkilled, baseline {len(base['unkilled'])}"
    )
    return 1 if failures else 0


def _mutated_globs() -> list[str]:
    pyproject = Path("pyproject.toml")
    if not pyproject.exists():
        return []
    cfg = tomllib.loads(pyproject.read_text()).get("tool", {}).get("mutmut", {})
    globs = cfg.get("only_mutate") or cfg.get("source_paths") or []
    return [str(g) for g in globs]


def _digest_inputs() -> list[Path]:
    """Every file whose content can change a mutant's status: what mutmut mutates, and the
    tests that do the killing."""
    roots: list[Path] = []
    for pattern in _mutated_globs():
        roots.extend(sorted(Path().glob(pattern)))
    roots.append(Path("tests"))
    files: set[Path] = set()
    for root in roots:
        candidates = [root] if root.is_file() else sorted(root.rglob("*"))
        for path in candidates:
            parts = set(path.parts)
            if not path.is_file() or path.suffix == ".pyc":
                continue
            if "__pycache__" in parts or ".hypothesis" in parts:
                continue
            files.add(path)
    return sorted(files)


def _source_digest() -> str:
    """A commit is the wrong identity for "the same source": a docs-only commit moves it while
    nothing mutmut sees changed. Digest what mutmut sees instead."""
    h = hashlib.sha256()
    for path in _digest_inputs():
        h.update(path.as_posix().encode())
        h.update(b"\0")
        h.update(hashlib.sha256(path.read_bytes()).hexdigest().encode())
        h.update(b"\0")
    return f"sha256:{h.hexdigest()}"


def write_baseline(current: dict[str, dict[str, Any]], source: str | None = None) -> int:
    old = load_baseline()
    source = source or _source_digest()
    # a baseline written before the digest existed carries a commit sha here, which is never
    # equal to a digest, so it reads as "the source moved": fail-safe, not a crash
    same_source = old.get("source") == source
    unkilled = {
        key: {"function": info["function"], "bucket": info["bucket"], "example": info["names"][0]}
        for key, info in sorted(current.items())
        if info["bucket"] != "killed" and key not in old["equivalent"]
    }
    preserved = []
    dropped_killed = []
    for key, entry in old["unkilled"].items():
        if key not in current:
            continue
        if key in unkilled:
            if entry.get("flaky"):
                # known flakiness is not forgotten by a run that happened to see it unkilled;
                # only `retire-flaky` clears the mark
                unkilled[key] = {**unkilled[key], "flaky": True}
            continue
        if entry.get("flaky") or same_source:
            # baselined before, killed in a run over the *same* source: a mutant killed on
            # one run and not another is unkilled *sometimes*, which is not proven; it
            # stays, marked flaky, and leaves only by `retire-flaky` (the baseline is the
            # memory the nightly has none of)
            unkilled[key] = {**entry, "flaky": True}
            preserved.append(key)
        else:
            # the source moved: a new or stronger test killed it, which is progress
            dropped_killed.append(key)
    equivalent = {
        k: v
        for k, v in old["equivalent"].items()
        if k in current and current[k]["bucket"] != "killed"
    }
    dropped = sorted(set(old["equivalent"]) - set(equivalent))
    doc = {
        "_": "docs/spec/assurance.md, the mutation gate; regenerate with `make mutation-baseline`",
        "source": source,
        "unkilled": dict(sorted(unkilled.items())),
        "equivalent": dict(sorted(equivalent.items())),
    }
    BASELINE.write_text(json.dumps(doc, indent=2, ensure_ascii=False, sort_keys=False) + "\n")
    by_bucket: dict[str, int] = defaultdict(int)
    for e in unkilled.values():
        by_bucket[e["bucket"]] += 1
    summary = f"{len(unkilled)} unkilled ({dict(by_bucket)}), {len(equivalent)} equivalent"
    print(f"baseline written: {summary}")
    for key in preserved:
        print(f"kept as flaky (baselined, killed this run; retire it by hand when fixed): {key}")
    if dropped_killed:
        print(f"dropped {len(dropped_killed)} baselined entries killed by the moved source")
    for key in dropped:
        print(f"dropped equivalent (its key vanished or it is now killed): {key}")
    return 0


def count() -> int:
    print(len(load_baseline()["unkilled"]))
    return 0


def retire_flaky(key: str) -> int:
    doc = load_baseline()
    entry = doc["unkilled"].get(key)
    if entry is None:
        print(f"no baselined unkilled entry {key}", file=sys.stderr)
        return 2
    if not entry.get("flaky"):
        print(f"entry {key} is not marked flaky; regenerate the baseline instead", file=sys.stderr)
        return 2
    del doc["unkilled"][key]
    BASELINE.write_text(json.dumps(doc, indent=2, ensure_ascii=False, sort_keys=False) + "\n")
    print(f"retired flaky entry {key}; {len(doc['unkilled'])} unkilled remain")
    return 0


def main(argv: list[str]) -> int:
    mode = argv[0] if argv else "gate"
    if mode == "count":
        return count()
    if mode == "retire-flaky":
        if len(argv) != 2:
            print("usage: mutation_gate.py retire-flaky KEY", file=sys.stderr)
            return 2
        return retire_flaky(argv[1])
    if not Path("mutants").is_dir():
        print("no mutants/ directory: run `mutmut run` first", file=sys.stderr)
        return 2
    statuses = None
    source = None
    rest = list(argv[1:])
    while rest:
        flag = rest.pop(0)
        if flag == "--from-results" and rest:
            statuses = _parse_results(Path(rest.pop(0)).read_text())
        elif flag == "--source" and rest:
            source = rest.pop(0)  # overrides the digest of the source the results are over
        else:
            print(f"unknown argument {flag!r}", file=sys.stderr)
            return 2
    current = collect(statuses)
    if mode == "baseline":
        return write_baseline(current, source)
    if mode == "gate":
        return gate(current)
    print(f"unknown mode {mode!r}: gate | baseline | count | retire-flaky", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
