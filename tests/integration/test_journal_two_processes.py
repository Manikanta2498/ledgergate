"""Two operating-system processes on one journal file.

The unit tests open two ``Journal`` instances in one process; SQLite treats them the same
way, but the claim in the spec is about processes, so one test uses real ones.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from tests.unit.journal.support import CHART, post

from ledgergate.journal import Journal
from ledgergate.ledger import EPOCH, SequentialIds, SteppingClock

WORKER = """
import json, sys
from ledgergate.journal import Journal
from ledgergate.ledger import EPOCH, SequentialIds, SteppingClock
path, start, key = sys.argv[1], int(sys.argv[2]), sys.argv[3]
j = Journal.open(path, clock=SteppingClock(EPOCH), ids=SequentialIds(start=start))
sale = {"postings": [
    {"account": "cash", "side": "debit", "money": {"amount": 5, "currency": "USD"}},
    {"account": "revenue", "side": "credit", "money": {"amount": 5, "currency": "USD"}},
]}
r = j.handle({"tool": "post", "call_id": "c-" + key, "key": key, "arguments": {"draft": sale}})
print(json.dumps({"response": r.response, "head": j.ledger.head, "cursor": j.cursor}))
j.close()
"""


def _worker(path: str, start: int, key: str) -> dict[str, object]:
    out = subprocess.run(
        [sys.executable, "-c", WORKER, path, str(start), key],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    return dict(json.loads(out.stdout.strip().splitlines()[-1]))


def test_a_second_process_sees_the_first_processs_writes_and_replays_its_key(
    tmp_path: Path,
) -> None:
    path = str(tmp_path / "shared.journal")
    first = Journal.create(path, CHART, clock=SteppingClock(EPOCH), ids=SequentialIds())
    applied = first.handle(post("k1", amount=5))
    assert applied.response == "applied"

    other = _worker(path, start=100, key="k1")  # same key from another process: a replay
    assert other["response"] == "replayed"
    assert other["head"] == first.ledger.head and other["cursor"] == first.cursor

    fresh = _worker(path, start=200, key="k2")  # a new key from another process: applied
    assert fresh["response"] == "applied"
    # ... and the first process, now stale, rebuilds before it evaluates anything
    again = first.handle(post("k2", call_id="from-first", amount=5))
    assert again.response == "replayed" and first.cursor == fresh["cursor"]
    first.close()
