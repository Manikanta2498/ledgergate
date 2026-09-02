"""The determinism gate must actually catch violations.

A checker that never fires is worse than no checker, because it reads as protection.
This runs the real AST logic over synthetic bad input.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from scripts.check_determinism import check_file


def test_flags_banned_import(tmp_path: Path) -> None:
    bad = tmp_path / "bad.py"
    bad.write_text("import random\n", encoding="utf-8")
    assert any("random" in v for v in check_file(bad))


def test_flags_wall_clock_call(tmp_path: Path) -> None:
    bad = tmp_path / "bad.py"
    bad.write_text(
        "import datetime\nfrom datetime import datetime as dt\n"
        "x = datetime.datetime.now()\ny = dt.utcnow()\n",
        encoding="utf-8",
    )
    violations = check_file(bad)
    assert len([v for v in violations if "injected Clock" in v]) == 2


@pytest.mark.parametrize(
    "source",
    [
        "from datetime import datetime as DT\nx = DT.now()\n",
        "from time import time as now\nx = now()\n",
        "import time as t\nx = t.monotonic()\n",
        "from time import perf_counter\nx = perf_counter()\n",
        "import uuid as u\nx = u.uuid4()\n",
        "from uuid import uuid4 as fresh\nx = fresh()\n",
    ],
)
def test_import_aliases_do_not_bypass_the_gate(tmp_path: Path, source: str) -> None:
    """Renaming a clock on import is not a way around the ban."""
    bad = tmp_path / "bad.py"
    bad.write_text(source, encoding="utf-8")
    assert check_file(bad), source


@pytest.mark.parametrize(
    "source",
    [
        # Assignment aliases propagate the banned path onto the new name.
        "from datetime import datetime\nnow = datetime.now\nv = now()\n",
        "from datetime import datetime\nct = datetime\nv = ct.now()\n",
        "import time\nt = time\nv = t.time()\n",
        "import datetime as d\na = d\nb = a.datetime\nv = b.utcnow()\n",
        # Order and scope do not matter: a later alias binds an earlier function body.
        "def late():\n    return tick()\nfrom time import monotonic\ntick = monotonic\n",
        # Function-local imports.
        "def f():\n    from time import time\n    return time()\n",
        "def f():\n    import uuid\n    return uuid.uuid4()\n",
        "import time\ndef f():\n    global time\n    return time.time()\n",
    ],
)
def test_aliases_in_any_scope_are_resolved(tmp_path: Path, source: str) -> None:
    bad = tmp_path / "probe.py"
    bad.write_text(source, encoding="utf-8")
    assert any("injected" in v for v in check_file(bad)), source


@pytest.mark.parametrize(
    "source",
    [
        # A class body does not rebind the module-level name; the final call is real.
        "from datetime import datetime\nclass C:\n    datetime = object()\nx = datetime.now()\n",
        # A dead branch does not rebind it either.
        "from datetime import datetime\nif False:\n    datetime = object()\nx = datetime.now()\n",
        # A parameter or local that reuses a clock alias: refused, with a rename hint.
        "from datetime import datetime as dt\ndef f(dt):\n    return dt.now()\n",
        "from datetime import datetime as dt\ndef f(c):\n    dt = c\n    return dt.now()\n",
        "import time\ndef f(time):\n    return time.time()\n",
        "from datetime import datetime\n[datetime.now() for datetime in cs]\n",
        "from datetime import datetime\nwith ctx() as datetime:\n    v = datetime.now()\n",
        "from datetime import datetime\ndef f(datetime):\n    pass\nv = datetime.now()\n",
    ],
)
def test_rebinding_a_tracked_name_is_refused(tmp_path: Path, source: str) -> None:
    """The gate is sound, not precise: it does not model scopes, so it forbids the reuse.

    Precise shadowing analysis is where the bypasses lived (class bodies, dead branches,
    execution order). Refusing to let a clock alias be reused as anything else costs a
    rename; accepting a real wall-clock call because a resolver got a scope slightly
    wrong costs the guarantee.
    """
    bad = tmp_path / "probe.py"
    bad.write_text(source, encoding="utf-8")
    violations = check_file(bad)
    assert any("rebinds a name" in v for v in violations), (source, violations)


@pytest.mark.parametrize(
    "source",
    [
        # Every way a callable can flow without a simple `name = value` assignment.
        "from datetime import datetime\n(now,) = (datetime.now,)\nvalue = now()\n",
        "from datetime import datetime\ndef f(now=datetime.now):\n    return now()\n",
        "from datetime import datetime\ncallbacks = [datetime.now]\n",
        "import time\nHOOKS = {'t': time.time}\n",
        "import time\nregister(factory=time.monotonic)\n",
        "from datetime import datetime\nx = datetime.now\n",
        "import datetime as d\nfns = (d.datetime.utcnow,)\n",
        "from time import perf_counter\nreturn_it = lambda: perf_counter\n",
    ],
)
def test_taking_a_reference_to_a_banned_callable_is_refused(tmp_path: Path, source: str) -> None:
    """Refused where the value is *taken*, so no flow path has to be modelled.

    Tuple unpacking, default arguments, literals, keyword arguments: each is a place a
    call-site-only check can be routed around. Reporting the reference closes them all.
    """
    bad = tmp_path / "probe.py"
    bad.write_text(source, encoding="utf-8")
    assert any("takes a reference" in v for v in check_file(bad)), source


def test_call_is_reported_once_not_also_as_a_reference(tmp_path: Path) -> None:
    bad = tmp_path / "probe.py"
    bad.write_text("import time\nv = time.time()\n", encoding="utf-8")
    violations = check_file(bad)
    assert len(violations) == 1 and "calls time.time()" in violations[0]


@pytest.mark.parametrize(
    "source",
    [
        "def run(clock):\n    return clock.now()\n",
        "def f(dt):\n    return dt.now()\n",
        "from datetime import datetime\ndef f(at: datetime) -> datetime:\n    return at\n",
        "from datetime import UTC, datetime\nEPOCH = datetime(2026, 1, 1, tzinfo=UTC)\n",
        "from fractions import Fraction\nrate = Fraction(9, 10)\n",
    ],
)
def test_clean_code_is_not_flagged(tmp_path: Path, source: str) -> None:
    good = tmp_path / "probe.py"
    good.write_text(source, encoding="utf-8")
    assert check_file(good) == [], source


def test_unrelated_now_is_not_flagged(tmp_path: Path) -> None:
    """A user-defined `now` or an injected `clock.now()` is exactly what we want."""
    good = tmp_path / "good.py"
    good.write_text("def run(clock):\n    return clock.now()\n", encoding="utf-8")
    assert check_file(good) == []


def test_flags_float_literal(tmp_path: Path) -> None:
    bad = tmp_path / "bad.py"
    bad.write_text("RATE = 0.1\n", encoding="utf-8")
    assert any("float literal 0.1" in v for v in check_file(bad))


def test_flags_float_call_and_annotation(tmp_path: Path) -> None:
    bad = tmp_path / "bad.py"
    bad.write_text("def f(x: float) -> int:\n    return int(float(x))\n", encoding="utf-8")
    violations = check_file(bad)
    assert len([v for v in violations if "uses 'float'" in v]) == 2


def test_string_mentioning_float_is_fine(tmp_path: Path) -> None:
    """Only the type is banned, not the word: error messages may still say 'float'."""
    good = tmp_path / "good.py"
    good.write_text('MSG = "money is never a float"\n', encoding="utf-8")
    assert check_file(good) == []


def test_clean_file_passes(tmp_path: Path) -> None:
    good = tmp_path / "good.py"
    good.write_text("def add(a: int, b: int) -> int:\n    return a + b\n", encoding="utf-8")
    assert check_file(good) == []
